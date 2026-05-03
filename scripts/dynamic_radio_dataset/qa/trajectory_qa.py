from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, resolve_repo_path
from dynamic_radio_dataset.plans.schemas import canonical_vehicle_role, role_is_required
from dynamic_radio_dataset.routes.geometry import corridor_rectangle, point_in_rotated_rect, region_center
from dynamic_radio_dataset.radio_dataset_utils import (
    actor_center_xy,
    actor_tracks_from_states,
    building_collision_metrics,
    idle_metrics,
    jump_metrics,
    lane_adherence_metrics,
    route_lookup,
    vehicle_type_mix_metrics,
    vehicle_vehicle_collision_metrics,
)


def evaluate_attempt_trajectory(attempt_dir: Path, config: dict) -> dict[str, Any]:
    report = _evaluate(attempt_dir, config)
    save_json(attempt_dir / "trajectory_qa.json", report)
    return report


def _evaluate(attempt_dir: Path, config: dict) -> dict[str, Any]:
    required = [
        attempt_dir / "scene_meta.json",
        attempt_dir / "routes.json",
        attempt_dir / "frames" / "actor_states.jsonl",
        attempt_dir / "plan.json",
    ]
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        return _reject("missing_carla_outputs", {"missing": missing})

    scene_meta = load_json(attempt_dir / "scene_meta.json")
    validation_path = attempt_dir / "validation_report.json"
    validation_report = load_json(validation_path) if validation_path.exists() else {}
    plan = load_json(attempt_dir / "plan.json")
    tracks = actor_tracks_from_states(attempt_dir / "frames" / "actor_states.jsonl")
    plan_consistency = _plan_consistency_metrics(tracks, plan)
    frame_count = _frame_count(attempt_dir / "frames" / "actor_states.jsonl")
    expected_frames = int(scene_meta.get("frames_requested", int(float(config["traffic"]["duration_s"]) * float(config["traffic"]["fps"]))))
    clip_complete = frame_count >= expected_frames
    actor_state_completeness = _actor_state_completeness_metrics(tracks, scene_meta, expected_frames)
    fps = float(scene_meta.get("fps", config["traffic"]["fps"]))
    qa = config.get("qa", {})
    manifest = _load_reference_manifest(config)
    routes_by_id = route_lookup(attempt_dir / "routes.json")

    lane_violations, lane_details = lane_adherence_metrics(
        tracks,
        routes_by_id,
        float(qa.get("lane_adherence_max_offset_m", 1.75)),
    )
    lane_warning_rows = [
        row
        for row in lane_violations
        if int(row.get("longest_consecutive_violation_frames", 0))
        > int(qa.get("lane_adherence_max_consecutive_frames", 20))
    ]
    vehicle_collisions = vehicle_vehicle_collision_metrics(tracks, float(qa.get("vehicle_collision_shrink_m", 0.10)))
    role_by_actor = _role_by_actor(tracks)
    collision_by_role = _collision_by_role_pair(vehicle_collisions, role_by_actor)
    building_collisions = building_collision_metrics(tracks, manifest) if manifest is not None else []
    jumps = jump_metrics(tracks, float(qa.get("max_jump_per_frame_m", 3.5)))
    idle = idle_metrics(
        tracks,
        float(qa.get("idle_speed_threshold_mps", 0.5)),
        float(qa.get("max_all_slow_duration_s", 2.0)),
        fps,
    )
    vehicle_mix = vehicle_type_mix_metrics(tracks, str(qa.get("large_vehicle_token", "fusorosa")))
    role_metrics = _role_metrics(tracks, plan, scene_meta, config)
    core_region = role_metrics.pop("core_region")
    core_comparison = role_metrics.pop("core_definition_comparison")

    blockers: list[str] = []
    warnings: list[str] = []
    if not clip_complete:
        blockers.append("clip_incomplete")
    if not bool(actor_state_completeness["complete"]):
        blockers.append("actor_states_incomplete")
    if not bool(plan_consistency["required_controlled_present"]):
        blockers.append(str(plan_consistency["failure_code"]))
    if vehicle_collisions:
        blockers.append("vehicle_vehicle_collision")
    if building_collisions:
        blockers.append("vehicle_building_collision")
    if jumps:
        blockers.append("frame_jump")
    if float(idle.get("all_slow_longest_s", 0.0)) > float(qa.get("max_all_slow_duration_s", 2.0)):
        blockers.append("long_all_stop")
    if int(vehicle_mix.get("large_vehicle_count", 0)) > int(qa.get("max_large_vehicle_count", 1)):
        blockers.append("vehicle_mix_violation")
    primary_failures = [row for row in role_metrics["primary_vehicle_metrics"] if not row["primary_qc_pass"]]
    if not role_metrics["primary_vehicle_metrics"]:
        blockers.append("missing_primary_vehicle")
    elif primary_failures:
        blockers.append("required_primary_not_moving_enough")
    if plan_consistency.get("missing_optional_controlled_vehicles"):
        warnings.append("optional_auxiliary_controlled_missing")
    if lane_warning_rows:
        warnings.append("lane_adherence_near_junction_soft_warning")
    if manifest is None:
        warnings.append("building_collision_manifest_unavailable")
    if not bool(scene_meta.get("warmup_result", {}).get("settled", True)):
        warnings.append("warmup_not_settled")
    if not validation_path.exists():
        warnings.append("old_validation_report_missing_after_post_clip_carla_failure")

    return {
        "schema": "trajectory_qa_v1",
        "trajectory_qc_pass": not blockers,
        "failure_code": blockers[0] if blockers else None,
        "blockers": blockers,
        "warnings": warnings,
        "plan_id": plan.get("plan_id"),
        "target_tx_id": plan.get("target_tx_id"),
        "clip_complete": bool(clip_complete),
        "frame_count": int(frame_count),
        "expected_frames": int(expected_frames),
        "warmup_settled": bool(scene_meta.get("warmup_result", {}).get("settled", True)),
        "old_validation_valid_clip": bool(validation_report.get("valid_clip", False)),
        "old_passed_target_count": int(validation_report.get("passed_target_count", 0)),
        "plan_consistency": plan_consistency,
        "vehicle_role_counts": plan_consistency.get("vehicle_role_counts", {}),
        "actor_state_completeness": actor_state_completeness,
        "core_region": core_region,
        "core_definition_comparison": core_comparison,
        "collision_summary": {
            "vehicle_vehicle_count": len(vehicle_collisions),
            "vehicle_building_count": len(building_collisions),
            "building_check_available": manifest is not None,
            "vehicle_vehicle_by_role_pair": collision_by_role,
        },
        "frame_jump": {"violations": jumps},
        "idle_stop": idle,
        "vehicle_mix": vehicle_mix,
        "lane_adherence": {"details": lane_details, "soft_violations": lane_warning_rows},
        **role_metrics,
    }


def _plan_consistency_metrics(tracks: dict[int, list[dict]], plan: dict) -> dict[str, Any]:
    plan_vehicles = [dict(vehicle) for vehicle in plan.get("vehicles", []) if isinstance(vehicle, dict)]
    required = [_vehicle_signature(vehicle) for vehicle in plan_vehicles if _plan_vehicle_required(vehicle)]
    optional = [_vehicle_signature(vehicle) for vehicle in plan_vehicles if not _plan_vehicle_required(vehicle)]
    observed_controlled = [
        _vehicle_signature(rows[0])
        for _, rows in sorted(tracks.items())
        if rows and _actor_role(rows[0]) in {"primary_controlled", "auxiliary_controlled"}
    ]
    observed_background = [
        _vehicle_signature(rows[0])
        for _, rows in sorted(tracks.items())
        if rows and _actor_role(rows[0]) == "background_tm"
    ]
    required_counter = Counter(required)
    optional_counter = Counter(optional)
    observed_controlled_counter = Counter(observed_controlled)
    observed_background_counter = Counter(observed_background)
    missing_required = _counter_elements(required_counter - observed_controlled_counter)
    missing_optional = _counter_elements(optional_counter - observed_controlled_counter)
    required_present = not missing_required
    failure_code = None
    if not required_present:
        failure_code = "missing_required_controlled_vehicle"
    requested_background_count = _requested_background_count(plan)
    actual_required = _counter_total(required_counter & observed_controlled_counter)
    actual_optional = _counter_total(optional_counter & observed_controlled_counter)
    actual_background = len(observed_background)
    role_counts = {
        "planned_required_controlled_count": len(required),
        "planned_optional_controlled_count": len(optional),
        "requested_background_count": int(requested_background_count),
        "actual_required_controlled_count": int(actual_required),
        "actual_optional_controlled_count": int(actual_optional),
        "actual_background_count": int(actual_background),
        "actual_total_vehicle_count": int(len(observed_controlled) + actual_background),
    }
    return {
        "matches_plan": bool(required_present),
        "required_controlled_present": bool(required_present),
        "failure_code": failure_code,
        "background_tm_route_matching": "disabled",
        "planned_vehicle_count": int(len(required) + len(optional) + requested_background_count),
        "planned_controlled_vehicle_count": int(len(required) + len(optional)),
        "observed_track_count": int(len(observed_controlled) + actual_background),
        "missing_required_controlled_vehicles": missing_required,
        "missing_optional_controlled_vehicles": missing_optional,
        "missing_planned_vehicles": missing_required,
        "unexpected_vehicle_tracks": [],
        "planned_required_signature_counts": _counter_rows(required_counter),
        "planned_optional_signature_counts": _counter_rows(optional_counter),
        "observed_controlled_signature_counts": _counter_rows(observed_controlled_counter),
        "observed_background_signature_counts": _counter_rows(observed_background_counter),
        "vehicle_role_counts": role_counts,
    }


def _vehicle_signature(row: dict) -> tuple[str, str, str]:
    plan_row = row.get("traffic_plan", {}) if isinstance(row.get("traffic_plan"), dict) else {}
    return (
        str(row.get("route_id") or plan_row.get("route_id") or ""),
        canonical_vehicle_role(plan_row.get("role") or row.get("role")),
        str(row.get("vehicle_type", "")),
    )


def _plan_vehicle_required(row: dict) -> bool:
    return bool(row.get("required", role_is_required(row.get("role"))))


def _requested_background_count(plan: dict) -> int:
    background = plan.get("background_tm", {}) if isinstance(plan.get("background_tm"), dict) else {}
    metrics = plan.get("expected_metrics", {}) if isinstance(plan.get("expected_metrics"), dict) else {}
    return int(background.get("requested_count", metrics.get("requested_background_count", 0)))


def _counter_total(counter: Counter[tuple[str, str, str]]) -> int:
    return int(sum(counter.values()))


def _counter_elements(counter: Counter[tuple[str, str, str]]) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for signature, count in sorted(counter.items()):
        rows.append(_signature_row(signature, count))
    return rows


def _counter_rows(counter: Counter[tuple[str, str, str]]) -> list[dict[str, object]]:
    return [_signature_row(signature, count) for signature, count in sorted(counter.items())]


def _signature_row(signature: tuple[str, str, str], count: int) -> dict[str, object]:
    route_id, role, vehicle_type = signature
    return {
        "route_id": route_id,
        "role": role,
        "vehicle_type": vehicle_type,
        "count": int(count),
    }


def _role_metrics(tracks: dict[int, list[dict]], plan: dict, scene_meta: dict, config: dict) -> dict[str, Any]:
    qa = config.get("qa", {})
    target_tx_id = str(plan.get("target_tx_id"))
    tx_catalog = load_json(dataset_root(config) / "scene_static" / "tx_catalog.json")["tx_catalog"]
    tx_by_id = {str(tx["tx_id"]): tx for tx in tx_catalog}
    tx = tx_by_id.get(target_tx_id)
    label_center = region_center(scene_meta["valid_crop"])
    core_center = _junction_core_center(scene_meta, label_center)
    core_radius_m = float(scene_meta.get("junction_core_radius_m", config["scene"].get("junction_core_radius", 12.0)))
    corridor = None
    if tx is not None:
        tx_xy = np.array([float(tx["position"]["x"]), float(tx["position"]["y"])], dtype=np.float64)
        corridor = corridor_rectangle(tx_xy, label_center, float(config["tx"].get("corridor_width_m", 10.0)))

    primary_rows = []
    auxiliary_rows = []
    background_rows = []
    core_comparison_rows = []
    for actor_id, rows in sorted(tracks.items()):
        if not rows:
            continue
        role = _actor_role(rows[0])
        first_xy = actor_center_xy(rows[0])
        last_xy = actor_center_xy(rows[-1])
        displacement = float(np.linalg.norm(last_xy - first_xy))
        core_diag = _track_core_diagnostics(rows, core_center, core_radius_m)
        visited_core = bool(core_diag["new_junction_center_visited_core"])
        hit_target_corridor = False
        if corridor is not None:
            center, extent, yaw = corridor
            hit_target_corridor = any(point_in_rotated_rect(actor_center_xy(row), center, extent, yaw) for row in rows)
        row = {
            "actor_id": int(actor_id),
            "role": role,
            "route_id": rows[0].get("route_id"),
            "vehicle_type": rows[0].get("vehicle_type"),
            "displacement_m": displacement,
            "visited_core": bool(visited_core),
            "new_junction_center_visited_core": bool(visited_core),
            "hit_target_tx_corridor": bool(hit_target_corridor),
            "entered_valid_crop": any(bool(item.get("in_valid_crop")) for item in rows),
            **core_diag,
        }
        if bool(core_diag["old_new_core_disagreement"]):
            core_comparison_rows.append(
                {
                    "actor_id": int(actor_id),
                    "role": role,
                    "route_id": rows[0].get("route_id"),
                    "old_route_local_visited_core": bool(core_diag["old_route_local_visited_core"]),
                    "new_junction_center_visited_core": bool(core_diag["new_junction_center_visited_core"]),
                    "old_route_local_first_core_frame": core_diag["old_route_local_first_core_frame"],
                    "first_core_frame": core_diag["first_core_frame"],
                    "min_distance_to_core_center_m": core_diag["min_distance_to_core_center_m"],
                }
            )
        if role == "primary_controlled":
            row["primary_qc_pass"] = bool(
                displacement >= float(qa.get("min_main_displacement_m", 12.0))
                and bool(row["entered_valid_crop"])
            )
            row["primary_qc_policy"] = "displacement_and_valid_crop_only_core_corridor_are_diagnostics"
            primary_rows.append(row)
        elif role == "auxiliary_controlled":
            row["auxiliary_qc_policy"] = "optional_presence_only; route/core/corridor are diagnostics"
            auxiliary_rows.append(row)
        else:
            row["background_qc_policy"] = "traffic_manager_background_not_route_matched; collisions/jumps/all-stop are global hard QA"
            background_rows.append(row)
    return {
        "core_region": {
            "center_xy": [float(core_center[0]), float(core_center[1])],
            "radius_m": float(core_radius_m),
            "source": "scene_meta.scene_info.junction_center",
            "fallback_source": "valid_crop.center",
        },
        "core_definition_comparison": {
            "old_route_local_source": "actor_states.inside_core_region / validation_report passed_target_count",
            "new_junction_center_source": "distance(actor center, scene_meta.scene_info.junction_center) <= junction_core_radius_m",
            "old_route_local_visit_actor_count": int(
                sum(1 for rows in tracks.values() if any(bool(row.get("inside_core_region")) for row in rows))
            ),
            "new_junction_center_visit_actor_count": int(
                sum(
                    1
                    for rows in tracks.values()
                    if any(float(np.linalg.norm(actor_center_xy(row) - core_center)) <= core_radius_m for row in rows)
                )
            ),
            "actor_disagreement_count": len(core_comparison_rows),
            "actors_with_old_new_disagreement": core_comparison_rows,
        },
        "primary_vehicle_metrics": primary_rows,
        "auxiliary_vehicle_metrics": auxiliary_rows,
        "background_vehicle_metrics": background_rows,
        "context_vehicle_metrics": auxiliary_rows + background_rows,
    }


def _track_core_diagnostics(rows: list[dict], core_center: np.ndarray, core_radius_m: float) -> dict[str, Any]:
    distances = [float(np.linalg.norm(actor_center_xy(row) - core_center)) for row in rows]
    new_flags = [distance <= float(core_radius_m) for distance in distances]
    old_flags = [bool(row.get("inside_core_region")) for row in rows]
    return {
        "new_junction_center_visited_core": bool(any(new_flags)),
        "first_core_frame": _first_flagged_frame(rows, new_flags),
        "core_visit_frame_count": int(sum(new_flags)),
        "min_distance_to_core_center_m": float(min(distances)) if distances else None,
        "old_route_local_visited_core": bool(any(old_flags)),
        "old_route_local_first_core_frame": _first_flagged_frame(rows, old_flags),
        "old_route_local_core_frame_count": int(sum(old_flags)),
        "old_new_core_disagreement": bool(any(old_flags) != any(new_flags)),
    }


def _first_flagged_frame(rows: list[dict], flags: list[bool]) -> int | None:
    for row, flag in zip(rows, flags):
        if flag:
            return int(row.get("_frame_index", -1))
    return None


def _actor_role(row: dict) -> str:
    plan_row = row.get("traffic_plan", {}) if isinstance(row.get("traffic_plan"), dict) else {}
    role = str(plan_row.get("role") or row.get("role") or "context")
    return canonical_vehicle_role(role)


def _role_by_actor(tracks: dict[int, list[dict]]) -> dict[int, str]:
    return {int(actor_id): _actor_role(rows[0]) for actor_id, rows in tracks.items() if rows}


def _collision_by_role_pair(collisions: list[dict], role_by_actor: dict[int, str]) -> dict[str, int]:
    result: dict[str, int] = {}
    seen_pairs: set[tuple[int, int]] = set()
    for row in collisions:
        actor_a = int(row.get("actor_a", -1))
        actor_b = int(row.get("actor_b", -1))
        pair = tuple(sorted((actor_a, actor_b)))
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        role_a = role_by_actor.get(actor_a, "unknown")
        role_b = role_by_actor.get(actor_b, "unknown")
        key = "+".join(sorted([role_a, role_b]))
        result[key] = result.get(key, 0) + 1
    return result


def _actor_state_completeness_metrics(tracks: dict[int, list[dict]], scene_meta: dict, expected_frames: int) -> dict[str, Any]:
    spawned = scene_meta.get("spawned_actor_ids", {}) if isinstance(scene_meta.get("spawned_actor_ids"), dict) else {}
    expected_ids = [int(item) for item in spawned.get("all", [])] if isinstance(spawned.get("all"), list) else []
    if not expected_ids:
        expected_ids = sorted(int(actor_id) for actor_id in tracks)
    missing_ids = [actor_id for actor_id in expected_ids if actor_id not in tracks]
    short_tracks = [
        {
            "actor_id": int(actor_id),
            "observed_frame_count": int(len(tracks.get(actor_id, []))),
            "expected_frame_count": int(expected_frames),
            "role": _actor_role(tracks[actor_id][0]) if actor_id in tracks and tracks[actor_id] else None,
        }
        for actor_id in expected_ids
        if actor_id in tracks and len(tracks.get(actor_id, [])) < int(expected_frames)
    ]
    return {
        "complete": bool(not missing_ids and not short_tracks),
        "expected_actor_ids": expected_ids,
        "observed_actor_ids": sorted(int(actor_id) for actor_id in tracks),
        "missing_actor_ids": missing_ids,
        "short_tracks": short_tracks,
    }


def _load_reference_manifest(config: dict) -> dict | None:
    value = config.get("scene", {}).get("reference_export_dir")
    if not value:
        return None
    path = resolve_repo_path(value) / "manifest.json"
    return load_json(path) if path.exists() else None


def _junction_core_center(scene_meta: dict, fallback: np.ndarray) -> np.ndarray:
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), dict) else {}
    center = info.get("junction_center") if isinstance(info.get("junction_center"), dict) else None
    if center is None:
        return fallback
    return np.array([float(center["x"]), float(center["y"])], dtype=np.float64)


def _frame_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip())


def _reject(failure_code: str, extra: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema": "trajectory_qa_v1",
        "trajectory_qc_pass": False,
        "failure_code": failure_code,
        "blockers": [failure_code],
        **extra,
    }
