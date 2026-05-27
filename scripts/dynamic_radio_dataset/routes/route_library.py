from __future__ import annotations

import math
import shutil
from typing import Any

import numpy as np

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import resolve_repo_path
from dynamic_radio_dataset.routes.geometry import (
    corridor_rectangle,
    first_distance_where,
    point_in_rotated_rect,
    point_polyline_distance,
    region_center,
    route_polyline,
    sample_polyline,
)


def _entry_time(distance_m: float, speed_mps: float, preroll_s: float) -> float | None:
    if not math.isfinite(distance_m):
        return None
    return float(distance_m) / max(float(speed_mps), 1e-3) - float(preroll_s)


def _feature_score(feature: dict, tx_ids: list[str], target_window: tuple[float, float]) -> float:
    """Diagnostic-only route score.

    This score is no longer used for active plan accept/reject/ranking. Keep it
    weak and motion-oriented so the feature cache remains useful for review
    without smuggling TX corridor/core heuristics back into decisions.
    """
    del tx_ids, target_window
    return float(min(float(feature["route_length_m"]), 100.0))


def _window_error(value: float, window: tuple[float, float]) -> float:
    if window[0] <= value <= window[1]:
        return 0.0
    return min(abs(value - window[0]), abs(value - window[1]))


def _canonical_junction_center(scene_meta: dict) -> tuple[np.ndarray, str]:
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), dict) else {}
    center = info.get("junction_center") if isinstance(info.get("junction_center"), dict) else None
    if center is not None:
        return np.array([float(center["x"]), float(center["y"])], dtype=np.float64), "scene_info.junction_center"
    return region_center(scene_meta["valid_crop"]), "valid_crop.center"


def _route_conflicts(features: list[dict], focus_radius_m: float, conflict_distance_m: float, scene_center: np.ndarray) -> list[dict]:
    rows: list[dict[str, Any]] = []
    samples_by_id = {row["route_id"]: np.array(row["_samples_xy"], dtype=np.float64) for row in features}
    s_by_id = {row["route_id"]: np.array(row["_sample_s_m"], dtype=np.float64) for row in features}
    for idx, route_a in enumerate(features):
        for route_b in features[idx + 1 :]:
            rid_a = str(route_a["route_id"])
            rid_b = str(route_b["route_id"])
            sample_a = samples_by_id[rid_a]
            sample_b = samples_by_id[rid_b]
            focus_a = np.linalg.norm(sample_a - scene_center[None, :], axis=1) <= float(focus_radius_m)
            focus_b = np.linalg.norm(sample_b - scene_center[None, :], axis=1) <= float(focus_radius_m)
            idx_a = np.where(focus_a)[0]
            idx_b = np.where(focus_b)[0]
            if idx_a.size == 0 or idx_b.size == 0:
                continue
            dists = np.linalg.norm(sample_a[idx_a][:, None, :] - sample_b[idx_b][None, :, :], axis=2)
            flat = int(np.argmin(dists))
            local_a, local_b = np.unravel_index(flat, dists.shape)
            min_dist = float(dists[local_a, local_b])
            close_count = int(np.sum(dists <= float(conflict_distance_m)))
            rows.append(
                {
                    "route_a": rid_a,
                    "route_b": rid_b,
                    "min_distance_m": min_dist,
                    "close_sample_count": close_count,
                    "distance_a_m": float(s_by_id[rid_a][idx_a[local_a]]),
                    "distance_b_m": float(s_by_id[rid_b][idx_b[local_b]]),
                    "conflict_risk": float(max(0.0, conflict_distance_m - min_dist) + 0.01 * close_count),
                }
            )
    return rows


def build_route_library(config: dict) -> dict:
    dataset_root = resolve_repo_path(config["dataset"]["root"])
    output_dir = dataset_root / "route_library"
    output_dir.mkdir(parents=True, exist_ok=True)

    route_source = resolve_repo_path(config["routes"]["source_dir"])
    scene_meta = load_json(route_source / "scene_meta.json")
    routes = load_json(route_source / "routes.json")["routes"]
    local_tx_catalog = dataset_root / "scene_static" / "tx_catalog.json"
    configured_tx_catalog = resolve_repo_path(config["scene"]["static_dir"]) / "tx_catalog.json"
    tx_catalog = load_json(local_tx_catalog if local_tx_catalog.exists() else configured_tx_catalog)["tx_catalog"]

    label_center, center_source = _canonical_junction_center(scene_meta)
    tx_ids = [str(tx["tx_id"]) for tx in tx_catalog]
    duration_s = float(config["traffic"]["duration_s"])
    fps = float(config["traffic"]["fps"])
    speed_mps = float(config["traffic"].get("estimated_speed_mps", 6.8))
    preroll_s = float(config["traffic"].get("traffic_preroll_s", 2.0))
    core_radius = float(config["scene"]["junction_core_radius"])
    corridor_width = float(config["tx"]["corridor_width_m"])
    clearance_reference = float(config.get("diagnostics", {}).get("tx_clearance_reference_m", 2.5))
    target_window = tuple(config["plans"].get("target_event_window_s", [2.0, 6.0]))
    sample_step = float(config["routes"].get("sample_step_m", 0.5))

    features: list[dict[str, Any]] = []
    for route in routes:
        poly = route_polyline(route)
        samples, sample_s = sample_polyline(poly, sample_step)
        length_to_core = first_distance_where(samples, sample_s, lambda p: float(np.linalg.norm(p - label_center)) <= core_radius)
        feature: dict[str, Any] = {
            "route_id": str(route["route_id"]),
            "turn_type": str(route.get("turn_type", "unknown")),
            "polyline": route.get("polyline", []),
            "route_length_m": float(sample_s[-1] if sample_s.size else 0.0),
            "spawn_start_xy": [float(poly[0][0]), float(poly[0][1])] if len(poly) else None,
            "route_end_xy": [float(poly[-1][0]), float(poly[-1][1])] if len(poly) else None,
            "length_to_core_m": None if not math.isfinite(length_to_core) else float(length_to_core),
            "min_distance_to_scene_center_m": float(np.min(np.linalg.norm(samples - label_center[None, :], axis=1))),
            "estimated_core_entry_s": _entry_time(length_to_core, speed_mps, preroll_s),
            "estimated_frames_after_core": 0,
            "min_distance_to_tx_m": {},
            "hits_tx_corridor": {},
            "estimated_tx_corridor_entry_s": {},
            "tx_clearance_margin_m": {},
            "_samples_xy": samples.tolist(),
            "_sample_s_m": sample_s.tolist(),
        }
        if feature["estimated_core_entry_s"] is not None:
            feature["estimated_frames_after_core"] = max(0, int(round((duration_s - float(feature["estimated_core_entry_s"])) * fps)))
        for tx in tx_catalog:
            tx_id = str(tx["tx_id"])
            tx_xy = np.array([float(tx["position"]["x"]), float(tx["position"]["y"])], dtype=np.float64)
            center, extent, yaw = corridor_rectangle(tx_xy, label_center, corridor_width)
            hit_dist = first_distance_where(samples, sample_s, lambda p, c=center, e=extent, y=yaw: point_in_rotated_rect(p, c, e, y))
            min_tx_dist = point_polyline_distance(tx_xy, poly)
            feature["min_distance_to_tx_m"][tx_id] = float(min_tx_dist)
            feature["hits_tx_corridor"][tx_id] = bool(math.isfinite(hit_dist))
            feature["estimated_tx_corridor_entry_s"][tx_id] = _entry_time(hit_dist, speed_mps, preroll_s)
            feature["tx_clearance_margin_m"][tx_id] = float(min_tx_dist - clearance_reference)
        feature["candidate_score"] = _feature_score(feature, tx_ids, (float(target_window[0]), float(target_window[1])))
        features.append(feature)

    conflicts = _route_conflicts(
        features,
        float(config["routes"].get("conflict_focus_radius_m", 28.0)),
        float(config["plans"].get("conflict_distance_m", 3.2)),
        label_center,
    )
    for row in features:
        row.pop("_samples_xy", None)
        row.pop("_sample_s_m", None)
    save_json(output_dir / "route_features.json", {"schema": "route_library_v1", "features": features})
    save_json(output_dir / "route_conflicts.json", {"schema": "route_conflicts_v1", "conflicts": conflicts})
    shutil.copy2(route_source / "routes.json", output_dir / "routes.json")
    summary = {
        "route_count": len(features),
        "conflict_pair_count": len(conflicts),
        "turn_type_histogram": _histogram(row.get("turn_type", "unknown") for row in features),
        "route_length_summary": _summary([float(row.get("route_length_m", 0.0)) for row in features]),
        "route_conflict_density": float(len(conflicts) / max(len(features), 1)),
        "tx_ids": tx_ids,
        "canonical_junction_center_xy": [float(label_center[0]), float(label_center[1])],
        "canonical_junction_center_source": center_source,
        "tx_clearance_reference_m": clearance_reference,
        "tx_clearance_policy": "diagnostic_only_not_used_for_plan_accept_reject_or_index",
    }
    save_json(output_dir / "route_library_summary.json", summary)
    return summary


def _histogram(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return result


def _summary(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {"count": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}
