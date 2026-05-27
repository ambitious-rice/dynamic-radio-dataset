from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any, Iterable, Mapping

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.multi_scene.config import (
    load_multi_scene_config,
    make_single_scene_config,
    multi_scene_root,
    scene_dataset_root,
    validate_multi_scene_config,
)
from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments


def repair_multi_scene_metadata(config_path: Path) -> dict[str, Any]:
    """Repair recoverable metadata gaps after an interrupted CARLA collection.

    This only uses already-recorded trajectory artifacts. It does not rerun CARLA,
    does not relax QA, and does not create RF/Sionna outputs.
    """

    config = load_multi_scene_config(config_path)
    validate_multi_scene_config(config)
    root = multi_scene_root(config)
    scene_results = []
    for scene in config.get("scenes", []):
        scene_results.append(_repair_scene(config, scene))
    report = {
        "schema": "multi_scene_metadata_repair_v1",
        "config": str(config_path),
        "dataset_root": str(root),
        "created_unix_s": time.time(),
        "scene_results": scene_results,
        "totals": _totals(scene_results),
    }
    stamp = time.strftime("%Y%m%d_%H%M%S", time.localtime())
    save_json(root / "indexes" / f"metadata_repair_{stamp}.json", report)
    save_json(root / "indexes" / "metadata_repair_latest.json", report)
    return report


def _repair_scene(config: Mapping[str, Any], scene: Mapping[str, Any]) -> dict[str, Any]:
    scene_config = make_single_scene_config(config, scene)
    root = scene_dataset_root(config, scene)
    repaired_validation = 0
    validation_errors: list[dict[str, Any]] = []
    accepted_dirs = _accepted_episode_dirs(root)
    for episode_dir in accepted_dirs:
        validation_path = episode_dir / "validation_report.json"
        if validation_path.exists() and _json_readable(validation_path):
            continue
        try:
            save_json(validation_path, _reconstruct_validation_report(episode_dir, scene_config))
            repaired_validation += 1
        except Exception as exc:  # noqa: BLE001
            validation_errors.append(
                {
                    "episode_id": episode_dir.name,
                    "failure_code": "validation_report_repair_failed",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )

    tx_before = _missing_tx_count(accepted_dirs)
    tx_result: dict[str, Any]
    try:
        tx_result = ensure_tx_assignments(scene_config)
    except Exception as exc:  # noqa: BLE001
        tx_result = {
            "status": "failed",
            "failure_code": "tx_assignment_repair_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }

    accepted_dirs_after = _accepted_episode_dirs(root)
    return {
        "scene_id": str(scene.get("scene_id")),
        "scene_type": str(scene.get("scene_type", "unknown")),
        "accepted_episode_count": len(accepted_dirs_after),
        "validation_report_repaired_count": int(repaired_validation),
        "validation_report_missing_after": _missing_validation_count(accepted_dirs_after),
        "validation_report_error_count": len(validation_errors),
        "validation_report_errors": validation_errors[:20],
        "tx_assignment_missing_before": int(tx_before),
        "tx_assignment": tx_result,
        "tx_assignment_missing_after": _missing_tx_count(accepted_dirs_after),
    }


def _accepted_episode_dirs(scene_root: Path) -> list[Path]:
    rows: list[Path] = []
    for episode_dir in sorted((scene_root / "episodes").glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        if not qa_path.exists():
            continue
        try:
            if bool(load_json(qa_path).get("trajectory_qc_pass")):
                rows.append(episode_dir)
        except Exception:  # noqa: BLE001
            continue
    return rows


def _json_readable(path: Path) -> bool:
    try:
        load_json(path)
        return True
    except Exception:  # noqa: BLE001
        return False


def _missing_validation_count(episode_dirs: Iterable[Path]) -> int:
    return sum(not (episode_dir / "validation_report.json").exists() for episode_dir in episode_dirs)


def _missing_tx_count(episode_dirs: Iterable[Path]) -> int:
    return sum(not (episode_dir / "tx_assignment.json").exists() for episode_dir in episode_dirs)


def _reconstruct_validation_report(episode_dir: Path, scene_config: Mapping[str, Any]) -> dict[str, Any]:
    scene_meta = load_json(episode_dir / "scene_meta.json")
    qa = load_json(episode_dir / "trajectory_qa.json")
    routes = _routes_by_id(episode_dir / "routes.json")
    policy = scene_meta.get("target_validation_policy", {})
    if not isinstance(policy, Mapping):
        policy = {}
    min_passed = int(policy.get("min_passed_targets", 0))
    min_frames_after_core = int(policy.get("min_frames_after_core", 3))
    core_exit_buffer_m = float(policy.get("core_exit_buffer_m", 6.0))
    min_displacement_m = float(policy.get("min_target_displacement_m", 12.0))
    core_center = _core_center_xy(scene_meta, qa)
    core_radius = float(
        policy.get(
            "junction_core_radius_m",
            scene_meta.get("junction_core_radius_m", qa.get("core_region", {}).get("radius_m", 12.0)),
        )
    )
    states = _target_states_from_actor_jsonl(
        episode_dir / "frames" / "actor_states.jsonl",
        routes,
        core_center,
        core_radius,
        core_exit_buffer_m,
    )
    passed = 0
    targets = {}
    for actor_id, state in sorted(states.items(), key=lambda item: int(item[0])):
        state["passed"] = _target_passed(state, min_frames_after_core, min_displacement_m)
        passed += int(bool(state["passed"]))
        targets[str(actor_id)] = state
    support = scene_meta.get("support_region", {}) if isinstance(scene_meta.get("support_region"), Mapping) else {}
    valid = scene_meta.get("valid_crop", {}) if isinstance(scene_meta.get("valid_crop"), Mapping) else {}
    return {
        "schema": "carla_validation_report_reconstructed_v1",
        "reconstructed": True,
        "reconstruction_reason": "missing_or_unreadable_after_interrupted_collection",
        "source_trajectory_qa_pass": bool(qa.get("trajectory_qc_pass")),
        "min_passed_targets_required": min_passed,
        "min_frames_after_core_required": min_frames_after_core,
        "core_exit_buffer_m": core_exit_buffer_m,
        "min_target_displacement_m": min_displacement_m,
        "passed_target_count": int(passed),
        "valid_clip": int(passed) >= int(min_passed),
        "targets": targets,
        "vehicle_role_counts": scene_meta.get("vehicle_role_counts", qa.get("vehicle_role_counts", {})),
        "background_tm": scene_meta.get("background_spawn_report", {}),
        "support_valid_check": {
            "support_size_m": support.get("width_m"),
            "valid_size_m": valid.get("width_m"),
            "edge_buffer_m_each_side": scene_meta.get("edge_buffer_m_each_side"),
            "support_larger_than_valid": (
                float(support.get("width_m", 0.0) or 0.0) > float(valid.get("width_m", 0.0) or 0.0)
            ),
        },
        "repair_context": {
            "scene_id": scene_config.get("scene", {}).get("scene_id") if isinstance(scene_config.get("scene"), Mapping) else None,
            "episode_id": episode_dir.name,
        },
    }


def _target_states_from_actor_jsonl(
    path: Path,
    routes: Mapping[str, Mapping[str, Any]],
    core_center_xy: tuple[float, float],
    core_radius: float,
    core_exit_buffer_m: float,
) -> dict[str, dict[str, Any]]:
    states: dict[str, dict[str, Any]] = {}
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            frame = json.loads(line)
            frame_index = int(frame.get("frame_index", 0))
            for actor in frame.get("actors", []):
                if str(actor.get("role", "")) == "background_tm" or not actor.get("route_id"):
                    continue
                actor_id = str(actor["actor_id"])
                xy = _actor_xy(actor)
                state = states.setdefault(actor_id, _new_target_state(actor, routes, xy))
                d = math.hypot(xy[0] - core_center_xy[0], xy[1] - core_center_xy[1])
                state["min_center_distance_m"] = min(float(state["min_center_distance_m"]), d)
                state["ever_in_support"] = bool(state["ever_in_support"] or actor.get("in_support_region"))
                state["ever_in_valid"] = bool(state["ever_in_valid"] or actor.get("in_valid_crop"))
                if d <= core_radius:
                    state["visited_core"] = True
                    if state["first_core_frame"] is None:
                        state["first_core_frame"] = frame_index
                    state["last_core_frame"] = frame_index
                if state["visited_core"] and d > core_radius + core_exit_buffer_m:
                    state["frames_after_core"] = int(state["frames_after_core"]) + 1
                start = state["start_location"]
                state["max_displacement_m"] = max(
                    float(state["max_displacement_m"]),
                    math.hypot(xy[0] - float(start["x"]), xy[1] - float(start["y"])),
                )
    return states


def _new_target_state(
    actor: Mapping[str, Any],
    routes: Mapping[str, Mapping[str, Any]],
    xy: tuple[float, float],
) -> dict[str, Any]:
    route_id = str(actor.get("route_id", ""))
    tf = actor.get("transform", {}) if isinstance(actor.get("transform"), Mapping) else {}
    traffic_plan = actor.get("traffic_plan", {}) if isinstance(actor.get("traffic_plan"), Mapping) else {}
    return {
        "actor_id": int(actor["actor_id"]),
        "route_id": route_id,
        "turn_type": routes.get(route_id, {}).get("turn_type", traffic_plan.get("turn_type", "unknown")),
        "start_location": {
            "x": float(tf.get("x", xy[0])),
            "y": float(tf.get("y", xy[1])),
            "z": float(tf.get("z", 0.0)),
        },
        "visited_core": False,
        "first_core_frame": None,
        "last_core_frame": None,
        "frames_after_core": 0,
        "ever_in_support": False,
        "ever_in_valid": False,
        "min_center_distance_m": float("inf"),
        "max_displacement_m": 0.0,
        "traffic_plan": dict(traffic_plan),
    }


def _actor_xy(actor: Mapping[str, Any]) -> tuple[float, float]:
    tf = actor.get("transform", {}) if isinstance(actor.get("transform"), Mapping) else {}
    return float(tf.get("x", 0.0)), float(tf.get("y", 0.0))


def _target_passed(state: Mapping[str, Any], min_frames_after_core: int, min_displacement_m: float) -> bool:
    return bool(
        state.get("visited_core")
        and state.get("ever_in_valid")
        and int(state.get("frames_after_core", 0)) >= int(min_frames_after_core)
        and float(state.get("max_displacement_m", 0.0)) >= float(min_displacement_m)
    )


def _core_center_xy(scene_meta: Mapping[str, Any], qa: Mapping[str, Any]) -> tuple[float, float]:
    core = qa.get("core_region", {}) if isinstance(qa.get("core_region"), Mapping) else {}
    center = core.get("center_xy")
    if isinstance(center, list) and len(center) >= 2:
        return float(center[0]), float(center[1])
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), Mapping) else {}
    junction = info.get("junction_center", {}) if isinstance(info.get("junction_center"), Mapping) else {}
    if "x" in junction and "y" in junction:
        return float(junction["x"]), float(junction["y"])
    valid = scene_meta.get("valid_crop", {}) if isinstance(scene_meta.get("valid_crop"), Mapping) else {}
    center_map = valid.get("center", {}) if isinstance(valid.get("center"), Mapping) else {}
    return float(center_map.get("x", 0.0)), float(center_map.get("y", 0.0))


def _routes_by_id(path: Path) -> dict[str, dict[str, Any]]:
    if not path.exists():
        return {}
    data = load_json(path)
    routes = data.get("routes", []) if isinstance(data, Mapping) else data
    if not isinstance(routes, list):
        return {}
    return {str(route.get("route_id")): dict(route) for route in routes if isinstance(route, Mapping)}


def _totals(scene_results: list[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "scene_count": len(scene_results),
        "accepted_episode_count": sum(int(row.get("accepted_episode_count", 0)) for row in scene_results),
        "validation_report_repaired_count": sum(
            int(row.get("validation_report_repaired_count", 0)) for row in scene_results
        ),
        "validation_report_missing_after": sum(int(row.get("validation_report_missing_after", 0)) for row in scene_results),
        "tx_assignment_missing_before": sum(int(row.get("tx_assignment_missing_before", 0)) for row in scene_results),
        "tx_assignment_missing_after": sum(int(row.get("tx_assignment_missing_after", 0)) for row in scene_results),
    }
