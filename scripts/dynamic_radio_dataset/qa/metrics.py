from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Dict

import numpy as np

from dynamic_radio_dataset.geometry.collision import (
    building_rectangles_from_manifest,
    polygons_from_rectangle,
    polygons_intersect_sat,
)
from dynamic_radio_dataset.geometry.regions import point_polyline_distance
from dynamic_radio_dataset.json_utils import iter_jsonl, load_json


JsonDict = Dict[str, Any]


def route_lookup(routes_path: Path) -> dict[str, JsonDict]:
    data = load_json(routes_path)
    return {str(route["route_id"]): route for route in data.get("routes", [])}


def speed_magnitude(actor_row: JsonDict) -> float:
    velocity = actor_row.get("velocity", {})
    return float(
        math.sqrt(
            float(velocity.get("x", 0.0)) ** 2
            + float(velocity.get("y", 0.0)) ** 2
            + float(velocity.get("z", 0.0)) ** 2
        )
    )


def actor_tracks_from_states(actor_states_path: Path) -> dict[int, list[JsonDict]]:
    tracks: dict[int, list[JsonDict]] = {}
    for frame_row in iter_jsonl(actor_states_path):
        frame_index = int(frame_row["frame_index"])
        timestamp = float(frame_row["timestamp"])
        for actor in frame_row.get("actors", []):
            actor_copy = dict(actor)
            actor_copy["_frame_index"] = frame_index
            actor_copy["_timestamp"] = timestamp
            tracks.setdefault(int(actor["actor_id"]), []).append(actor_copy)
    return tracks


def actor_center_xy(actor_row: JsonDict) -> np.ndarray:
    transform = actor_row["transform"]
    return np.array([float(transform["x"]), float(transform["y"])], dtype=np.float64)


def actor_yaw_deg(actor_row: JsonDict) -> float:
    return float(actor_row["transform"]["yaw"])


def actor_bbox_extent_xy(actor_row: JsonDict) -> tuple[float, float]:
    bbox = actor_row.get("bbox_extent", {})
    return float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))


def lane_adherence_metrics(
    tracks: dict[int, list[JsonDict]],
    routes_by_id: dict[str, JsonDict],
    max_offset_m: float,
) -> tuple[list[JsonDict], list[JsonDict]]:
    violations: list[JsonDict] = []
    details: list[JsonDict] = []
    for actor_id, rows in tracks.items():
        route_id = rows[0].get("route_id")
        route = routes_by_id.get(str(route_id)) if route_id is not None else None
        if route is None:
            continue
        polyline = np.array([[float(item["x"]), float(item["y"])] for item in route.get("polyline", [])], dtype=np.float64)
        if polyline.shape[0] < 2:
            continue
        offsets = [point_polyline_distance(actor_center_xy(row), polyline) for row in rows]
        max_offset = max(offsets) if offsets else 0.0
        details.append(
            {
                "actor_id": int(actor_id),
                "route_id": route_id,
                "max_route_offset_m": float(max_offset),
                "frames_over_threshold": int(sum(value > max_offset_m for value in offsets)),
            }
        )
        consecutive = 0
        longest = 0
        first_bad_frame = None
        for row, offset in zip(rows, offsets):
            if offset > max_offset_m:
                consecutive += 1
                longest = max(longest, consecutive)
                if first_bad_frame is None:
                    first_bad_frame = int(row["_frame_index"])
            else:
                consecutive = 0
        if longest > 0:
            violations.append(
                {
                    "actor_id": int(actor_id),
                    "route_id": route_id,
                    "max_route_offset_m": float(max_offset),
                    "longest_consecutive_violation_frames": int(longest),
                    "first_bad_frame": first_bad_frame,
                }
            )
    return violations, details


def building_collision_metrics(tracks: dict[int, list[JsonDict]], manifest: JsonDict) -> list[JsonDict]:
    building_rects = building_rectangles_from_manifest(manifest)
    collisions: list[JsonDict] = []
    for actor_id, rows in tracks.items():
        for row in rows:
            vehicle_poly = polygons_from_rectangle(actor_center_xy(row), actor_bbox_extent_xy(row), actor_yaw_deg(row))
            for building in building_rects:
                building_poly = polygons_from_rectangle(building["center_xy"], building["extent_xy"], building["yaw_deg"])
                if polygons_intersect_sat(vehicle_poly, building_poly):
                    collisions.append(
                        {
                            "actor_id": int(actor_id),
                            "frame_index": int(row["_frame_index"]),
                            "building_id": building.get("id"),
                        }
                    )
                    break
            if collisions and collisions[-1]["actor_id"] == int(actor_id):
                break
    return collisions


def vehicle_vehicle_collision_metrics(tracks: dict[int, list[JsonDict]], shrink_m: float) -> list[JsonDict]:
    rows_by_frame: dict[int, list[tuple[int, JsonDict]]] = {}
    for actor_id, rows in tracks.items():
        for row in rows:
            rows_by_frame.setdefault(int(row["_frame_index"]), []).append((int(actor_id), row))

    collisions: list[JsonDict] = []
    seen_pairs = set()
    for frame_index in sorted(rows_by_frame):
        rows = rows_by_frame[frame_index]
        for idx, (actor_a, row_a) in enumerate(rows):
            for actor_b, row_b in rows[idx + 1 :]:
                pair_key = tuple(sorted((actor_a, actor_b)))
                extent_a = actor_bbox_extent_xy(row_a)
                extent_b = actor_bbox_extent_xy(row_b)
                poly_a = polygons_from_rectangle(
                    actor_center_xy(row_a),
                    [max(extent_a[0] - shrink_m, 0.05), max(extent_a[1] - shrink_m, 0.05)],
                    actor_yaw_deg(row_a),
                )
                poly_b = polygons_from_rectangle(
                    actor_center_xy(row_b),
                    [max(extent_b[0] - shrink_m, 0.05), max(extent_b[1] - shrink_m, 0.05)],
                    actor_yaw_deg(row_b),
                )
                if not polygons_intersect_sat(poly_a, poly_b):
                    continue
                center_distance = float(np.linalg.norm(actor_center_xy(row_a) - actor_center_xy(row_b)))
                collisions.append(
                    {
                        "frame_index": int(frame_index),
                        "actor_a": int(actor_a),
                        "actor_b": int(actor_b),
                        "route_a": row_a.get("route_id"),
                        "route_b": row_b.get("route_id"),
                        "center_distance_m": center_distance,
                        "first_collision_for_pair": pair_key not in seen_pairs,
                    }
                )
                seen_pairs.add(pair_key)
    return collisions


def jump_metrics(tracks: dict[int, list[JsonDict]], max_jump_per_frame_m: float) -> list[JsonDict]:
    violations: list[JsonDict] = []
    for actor_id, rows in tracks.items():
        for prev_row, row in zip(rows[:-1], rows[1:]):
            jump = float(np.linalg.norm(actor_center_xy(row) - actor_center_xy(prev_row)))
            if jump > max_jump_per_frame_m:
                violations.append(
                    {
                        "actor_id": int(actor_id),
                        "prev_frame_index": int(prev_row["_frame_index"]),
                        "frame_index": int(row["_frame_index"]),
                        "jump_m": jump,
                    }
                )
                break
    return violations


def idle_metrics(
    tracks: dict[int, list[JsonDict]],
    idle_speed_threshold_mps: float,
    max_all_slow_duration_s: float,
    fps: float,
) -> JsonDict:
    actor_rows = []
    per_actor = []
    for actor_id, rows in tracks.items():
        enters_label = any(bool(row.get("in_valid_crop")) for row in rows)
        if not enters_label:
            continue
        speeds = [speed_magnitude(row) for row in rows]
        idle_fraction = float(sum(speed < idle_speed_threshold_mps for speed in speeds) / max(len(speeds), 1))
        per_actor.append(
            {
                "actor_id": int(actor_id),
                "idle_fraction": idle_fraction,
                "entered_label_region": enters_label,
                "max_speed_mps": float(max(speeds) if speeds else 0.0),
            }
        )
        actor_rows.append(rows)

    if not actor_rows:
        return {"per_actor": per_actor, "all_slow_longest_frames": 0, "all_slow_longest_s": 0.0}

    frame_count = min(len(rows) for rows in actor_rows)
    all_slow_flags = []
    for frame_idx in range(frame_count):
        all_slow = all(speed_magnitude(rows[frame_idx]) < idle_speed_threshold_mps for rows in actor_rows)
        all_slow_flags.append(all_slow)
    longest = 0
    current = 0
    for flag in all_slow_flags:
        if flag:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "per_actor": per_actor,
        "all_slow_longest_frames": int(longest),
        "all_slow_longest_s": float(longest / max(fps, 1e-6)),
        "all_slow_limit_s": float(max_all_slow_duration_s),
    }


def route_mix_metrics(tracks: dict[int, list[JsonDict]], routes_by_id: dict[str, JsonDict], min_displacement_m: float) -> JsonDict:
    actors_crossing_label = 0
    straight_count = 0
    turning_count = 0
    displacement_count = 0
    details = []
    for actor_id, rows in tracks.items():
        route_id = str(rows[0].get("route_id")) if rows and rows[0].get("route_id") is not None else None
        route = routes_by_id.get(route_id) if route_id is not None else None
        turn_type = str(route.get("turn_type")) if route is not None else None
        enters_label = any(bool(row.get("in_valid_crop")) for row in rows)
        first_xy = actor_center_xy(rows[0])
        last_xy = actor_center_xy(rows[-1])
        displacement = float(np.linalg.norm(last_xy - first_xy))
        if enters_label:
            actors_crossing_label += 1
        if turn_type == "straight" and enters_label:
            straight_count += 1
        if turn_type not in (None, "straight") and enters_label:
            turning_count += 1
        if displacement >= min_displacement_m:
            displacement_count += 1
        details.append(
            {
                "actor_id": int(actor_id),
                "route_id": route_id,
                "turn_type": turn_type,
                "entered_label_region": enters_label,
                "displacement_m": displacement,
            }
        )
    return {
        "details": details,
        "actors_crossing_label_region": int(actors_crossing_label),
        "straight_actor_count": int(straight_count),
        "turning_actor_count": int(turning_count),
        "actors_over_min_displacement": int(displacement_count),
    }


def vehicle_type_mix_metrics(
    tracks: dict[int, list[JsonDict]],
    large_vehicle_token: str,
) -> JsonDict:
    token = str(large_vehicle_token).lower().strip()
    details = []
    large_count = 0
    for actor_id, rows in tracks.items():
        vehicle_type = str(rows[0].get("vehicle_type", "")) if rows else ""
        is_large = bool(token and token in vehicle_type.lower())
        if is_large:
            large_count += 1
        details.append(
            {
                "actor_id": int(actor_id),
                "vehicle_type": vehicle_type,
                "is_large_vehicle": is_large,
            }
        )
    return {
        "large_vehicle_count": int(large_count),
        "details": details,
    }


def dynamic_rss_summary(tx_dir: Path, static_rss_path: Path | None = None, floor_dbm: float = -200.0) -> JsonDict:
    if static_rss_path is not None and static_rss_path.exists() and (tx_dir / "rss_maps.npz").exists():
        with np.load(tx_dir / "rss_maps.npz") as data:
            rss_stack = np.asarray(data["rss_dbm"], dtype=np.float64)
            building_mask = np.asarray(data["building_mask"], dtype=bool) if "building_mask" in data.files else None
        static = np.asarray(np.load(static_rss_path), dtype=np.float64)
        if building_mask is None:
            building_mask = np.zeros(static.shape, dtype=bool)
        static_valid = np.isfinite(static) & (static > float(floor_dbm))
        dynamic_finite_all = np.all(np.isfinite(rss_stack), axis=0)
        dynamic_above_floor_all = np.all(rss_stack > float(floor_dbm), axis=0)
        common = (~building_mask) & static_valid & dynamic_finite_all & dynamic_above_floor_all
        if not np.any(common):
            return {
                "mask_policy": "valid_non_building_common_mask",
                "finite_cell_count": 0,
                "p50_temporal_range_db": None,
                "p90_temporal_range_db": None,
                "p95_temporal_range_db": None,
                "p99_temporal_range_db": None,
                "max_temporal_range_db": None,
            }
        temporal_range = np.max(rss_stack[:, common], axis=0) - np.min(rss_stack[:, common], axis=0)
        delta = rss_stack[:, common] - static[common][None, :]
        return {
            "mask_policy": "valid_non_building_common_mask",
            "floor_dbm": float(floor_dbm),
            "finite_cell_count": int(temporal_range.size),
            "common_mask_cell_count": int(np.sum(common)),
            "non_building_cell_count": int(np.sum(~building_mask)),
            "static_valid_cell_count": int(np.sum((~building_mask) & static_valid)),
            "dynamic_all_frames_valid_cell_count": int(np.sum((~building_mask) & dynamic_finite_all & dynamic_above_floor_all)),
            "p50_temporal_range_db": float(np.percentile(temporal_range, 50)),
            "p90_temporal_range_db": float(np.percentile(temporal_range, 90)),
            "p95_temporal_range_db": float(np.percentile(temporal_range, 95)),
            "p99_temporal_range_db": float(np.percentile(temporal_range, 99)),
            "max_temporal_range_db": float(np.max(temporal_range)),
            "cells_over_1db": int(np.sum(temporal_range > 1.0)),
            "cells_over_3db": int(np.sum(temporal_range > 3.0)),
            "cells_over_6db": int(np.sum(temporal_range > 6.0)),
            "delta_from_static_db": {
                "p50": float(np.percentile(delta, 50)),
                "p95": float(np.percentile(delta, 95)),
                "min": float(np.min(delta)),
                "max": float(np.max(delta)),
            },
            "abs_delta_from_static_db": {
                "p50": float(np.percentile(np.abs(delta), 50)),
                "p95": float(np.percentile(np.abs(delta), 95)),
                "max": float(np.max(np.abs(delta))),
            },
        }
    meta_path = tx_dir / "rss_heatmap_meta.json"
    return load_json(meta_path).get("dynamic_rss_change_validation", {})
