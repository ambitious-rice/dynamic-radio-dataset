from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.collision import polygons_from_rectangle, polygons_intersect_sat
from dynamic_radio_dataset.geometry.regions import corridor_rectangle, region_center
from dynamic_radio_dataset.qa.metrics import (
    actor_tracks_from_states,
    building_collision_metrics,
    dynamic_rss_summary,
    idle_metrics,
    jump_metrics,
    lane_adherence_metrics,
    route_lookup,
    route_mix_metrics,
    vehicle_type_mix_metrics,
    vehicle_vehicle_collision_metrics,
)


JsonDict = Dict[str, Any]


@dataclass
class EpisodeQAConfig:
    lane_adherence_max_offset_m: float = 1.75
    lane_adherence_max_consecutive_frames: int = 20
    max_jump_per_frame_m: float = 3.5
    idle_speed_threshold_mps: float = 0.5
    max_idle_fraction: float = 0.70
    max_all_slow_duration_s: float = 2.0
    min_displacement_m: float = 20.0
    min_vehicles_crossing_label: int = 2
    require_turning_vehicle: bool = True
    require_straight_vehicle: bool = True
    large_vehicle_token: str = "fusorosa"
    max_large_vehicle_count: int = 1
    vehicle_collision_shrink_m: float = 0.10


@dataclass
class TxQAConfig:
    diagnostic_clearance_reference_m: float = 2.5
    corridor_width_m: float = 10.0
    min_p95_temporal_range_db: float = 1.0
    min_max_temporal_range_db: float = 4.0


def evaluate_episode_scene_qa(
    actor_states_path: Path,
    routes_path: Path,
    manifest: JsonDict,
    fps: float,
    config: EpisodeQAConfig,
) -> JsonDict:
    tracks = actor_tracks_from_states(actor_states_path)
    routes_by_id = route_lookup(routes_path)
    lane_violations, lane_details = lane_adherence_metrics(tracks, routes_by_id, config.lane_adherence_max_offset_m)
    lane_blockers = [
        row
        for row in lane_violations
        if int(row["longest_consecutive_violation_frames"]) > int(config.lane_adherence_max_consecutive_frames)
    ]
    building_collisions = building_collision_metrics(tracks, manifest)
    vehicle_collisions = vehicle_vehicle_collision_metrics(tracks, config.vehicle_collision_shrink_m)
    jumps = jump_metrics(tracks, config.max_jump_per_frame_m)
    idle = idle_metrics(tracks, config.idle_speed_threshold_mps, config.max_all_slow_duration_s, fps)
    route_mix = route_mix_metrics(tracks, routes_by_id, config.min_displacement_m)
    vehicle_mix = vehicle_type_mix_metrics(tracks, config.large_vehicle_token)

    idle_actor_blockers = [
        row for row in idle.get("per_actor", []) if float(row["idle_fraction"]) > float(config.max_idle_fraction)
    ]
    all_slow_blocker = float(idle.get("all_slow_longest_s", 0.0)) > float(config.max_all_slow_duration_s)
    crossing_ok = int(route_mix["actors_crossing_label_region"]) >= int(config.min_vehicles_crossing_label)
    straight_ok = (not config.require_straight_vehicle) or int(route_mix["straight_actor_count"]) >= 1
    turning_ok = (not config.require_turning_vehicle) or int(route_mix["turning_actor_count"]) >= 1
    displacement_ok = int(route_mix["actors_over_min_displacement"]) >= int(config.min_vehicles_crossing_label)
    large_vehicle_ok = int(vehicle_mix["large_vehicle_count"]) <= int(config.max_large_vehicle_count)

    hard_blockers = []
    warnings = []
    traffic_visual_blockers = []
    propagation_blockers = []
    if lane_blockers:
        warnings.append("lane_adherence")
    if building_collisions:
        hard_blockers.append("building_collision")
        traffic_visual_blockers.append("building_collision")
    if vehicle_collisions:
        hard_blockers.append("vehicle_collision")
        traffic_visual_blockers.append("vehicle_collision")
    if jumps:
        hard_blockers.append("frame_jump")
        traffic_visual_blockers.append("frame_jump")
    if idle_actor_blockers:
        warnings.append("idle_fraction")
    if all_slow_blocker:
        hard_blockers.append("all_slow")
        traffic_visual_blockers.append("all_slow")
    if not large_vehicle_ok:
        hard_blockers.append("too_many_large_vehicles")

    return {
        "config": asdict(config),
        "scene_qc_pass": not hard_blockers,
        "traffic_visual_pass": not traffic_visual_blockers,
        "propagation_candidate_pass": not propagation_blockers,
        "blockers": hard_blockers,
        "warnings": warnings,
        "traffic_visual_blockers": traffic_visual_blockers,
        "propagation_blockers": propagation_blockers,
        "lane_adherence": {
            "details": lane_details,
            "violations": lane_blockers,
        },
        "building_collision": {
            "violations": building_collisions,
        },
        "vehicle_collision": {
            "shrink_m": float(config.vehicle_collision_shrink_m),
            "violations": vehicle_collisions,
        },
        "frame_jump": {
            "violations": jumps,
        },
        "idle_stop": {
            "details": idle,
            "actor_idle_fraction_violations": idle_actor_blockers,
            "all_slow_blocker": bool(all_slow_blocker),
        },
        "route_mix": {
            **route_mix,
            "crossing_ok": bool(crossing_ok),
            "straight_ok": bool(straight_ok),
            "turning_ok": bool(turning_ok),
            "displacement_ok": bool(displacement_ok),
            "decision_policy": "diagnostic_only_not_used_for_accept_reject_or_index",
        },
        "vehicle_mix": {
            **vehicle_mix,
            "large_vehicle_ok": bool(large_vehicle_ok),
        },
    }


def evaluate_episode_tx_qa(
    tx_entry: JsonDict,
    manifest: JsonDict,
    motion_rows_by_frame: dict[int, JsonDict],
    frame_indices: Sequence[int],
    tx_rss_dir: Path,
    config: TxQAConfig,
    static_rss_path: Path | None = None,
) -> JsonDict:
    tx_xy = np.array([float(tx_entry["position"]["x"]), float(tx_entry["position"]["y"])], dtype=np.float64)
    label_center = region_center(manifest["valid_crop"])
    corridor_center, corridor_extent, corridor_yaw_deg = corridor_rectangle(tx_xy, label_center, config.corridor_width_m)
    corridor_poly = polygons_from_rectangle(corridor_center, corridor_extent, corridor_yaw_deg)
    min_clearance = float("inf")
    corridor_hits: list[JsonDict] = []
    for frame_index in frame_indices:
        row = motion_rows_by_frame[int(frame_index)]
        for vehicle in row.get("vehicles", []):
            position = vehicle.get("position") or [0.0, 0.0, 0.0]
            center_xy = np.array([float(position[0]), float(position[1])], dtype=np.float64)
            min_clearance = min(min_clearance, float(np.linalg.norm(center_xy - tx_xy)))
            yaw = float(vehicle.get("mesh_reference_yaw_deg", 0.0)) + float(vehicle.get("yaw_delta_deg_sionna", 0.0))
            bbox = vehicle.get("bbox_extent") or {"x": 0.0, "y": 0.0}
            vehicle_poly = polygons_from_rectangle(center_xy, [float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))], yaw)
            if polygons_intersect_sat(vehicle_poly, corridor_poly):
                corridor_hits.append(
                    {
                        "frame_index": int(frame_index),
                        "actor_id": int(vehicle["actor_id"]),
                        "sionna_object": str(vehicle["sionna_object"]),
                    }
                )
    rss_summary = dynamic_rss_summary(tx_rss_dir, static_rss_path=static_rss_path)
    p95_range = rss_summary.get("p95_temporal_range_db")
    max_range = rss_summary.get("max_temporal_range_db")
    dynamic_ok = False
    if p95_range is not None and max_range is not None:
        dynamic_ok = (
            float(p95_range) >= float(config.min_p95_temporal_range_db)
            or float(max_range) >= float(config.min_max_temporal_range_db)
        )
    blockers = []
    if not dynamic_ok:
        blockers.append("dynamic_rss_change")
    return {
        "tx_id": str(tx_entry["tx_id"]),
        "config": asdict(config),
        "pair_qc_pass": not blockers,
        "blockers": blockers,
        "min_vehicle_center_clearance_m": None if np.isinf(min_clearance) else float(min_clearance),
        "diagnostic_clearance_reference_m": float(config.diagnostic_clearance_reference_m),
        "corridor_crossings": corridor_hits,
        "geometry_diagnostics_policy": "clearance_and_corridor_crossings_not_used_for_accept_reject_or_index",
        "dynamic_rss_change": rss_summary,
    }
