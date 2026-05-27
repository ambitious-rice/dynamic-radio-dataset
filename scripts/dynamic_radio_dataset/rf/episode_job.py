from __future__ import annotations

import argparse
import shutil
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.regions import scene_signature, scene_signature_matches
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.qa.rf_policy import (
    EpisodeQAConfig,
    TxQAConfig,
    evaluate_episode_scene_qa,
    evaluate_episode_tx_qa,
)
from dynamic_radio_dataset.raster.traffic_grid import build_traffic_grid_from_motion
from dynamic_radio_dataset.rf.artifacts import load_frame_indices_from_rss, load_motion_rows_by_frame
from dynamic_radio_dataset.rf.runtime import manual_sionna_env, run_command as runtime_run_command


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def episode_qa_config_from_args(args: argparse.Namespace) -> EpisodeQAConfig:
    return EpisodeQAConfig(
        lane_adherence_max_offset_m=float(args.lane_adherence_max_offset_m),
        lane_adherence_max_consecutive_frames=int(args.lane_adherence_max_consecutive_frames),
        max_jump_per_frame_m=float(args.max_jump_per_frame_m),
        idle_speed_threshold_mps=float(args.idle_speed_threshold_mps),
        max_idle_fraction=float(args.max_idle_fraction),
        max_all_slow_duration_s=float(args.max_all_slow_duration_s),
        min_displacement_m=float(args.min_displacement_m),
        large_vehicle_token=str(args.large_vehicle_token),
        max_large_vehicle_count=int(args.max_large_vehicle_count),
    )


def tx_qa_config_from_args(args: argparse.Namespace) -> TxQAConfig:
    return TxQAConfig(
        diagnostic_clearance_reference_m=float(args.diagnostic_clearance_reference_m),
        corridor_width_m=float(args.corridor_width_m),
        min_p95_temporal_range_db=float(args.min_p95_temporal_range_db),
        min_max_temporal_range_db=float(args.min_max_temporal_range_db),
    )


def run_command(cmd: Sequence[object], env: Optional[Dict[str, str]] = None) -> None:
    str_cmd = [str(item) for item in cmd]
    print(f"[RUN] {' '.join(str_cmd)}")
    runtime_run_command(str_cmd, env=env)


def sionna_env(args: argparse.Namespace) -> Dict[str, str]:
    return manual_sionna_env(use_gpu=bool(getattr(args, "use_gpu", False)))


def scene_static_dir(args: argparse.Namespace) -> Path:
    return args.scene_static_dir if getattr(args, "scene_static_dir", None) else (args.dataset_root / "scene_static")


def resolve_reference_export_dir(args: argparse.Namespace, static_dir: Optional[Path] = None) -> Optional[Path]:
    if getattr(args, "reference_export_dir", None):
        return Path(args.reference_export_dir)
    if getattr(args, "reuse_building_proxy_from_export", None):
        return Path(args.reuse_building_proxy_from_export)
    meta_path = (static_dir or scene_static_dir(args)) / "scene_static_meta.json"
    if meta_path.exists():
        meta = load_json(meta_path)
        value = meta.get("reference_export_dir")
        if value:
            return Path(str(value))
    return None


def export_dataset_dir(
    args: argparse.Namespace,
    dataset_dir: Path,
    export_dir: Path,
    reuse_building_proxy_from_export: Optional[Path],
) -> None:
    cmd: List[object] = [
        args.carla_python,
        "-m",
        "dynamic_radio_dataset.sionna.export",
        "--dataset-dir",
        dataset_dir,
        "--output-dir",
        export_dir,
        "--snapshot-frame",
        int(args.snapshot_frame),
        "--vehicle-geometry",
        args.vehicle_geometry,
        "--building-geometry",
        args.building_geometry,
        "--vehicle-asset-catalog",
        args.vehicle_asset_catalog,
        "--asset-tool-dir",
        args.asset_tool_dir,
        "--asset-cache-dir",
        args.asset_cache_dir,
        "--carla-host",
        args.carla_host,
        "--carla-port",
        int(args.carla_port),
    ]
    if args.include_buildings:
        cmd.append("--include-buildings")
    if reuse_building_proxy_from_export is not None:
        cmd.extend(["--reuse-building-proxy-from-export", reuse_building_proxy_from_export])
    run_command(cmd)


def run_dynamic_rss_for_tx(
    args: argparse.Namespace,
    export_dir: Path,
    tx_entry: Dict[str, object],
    output_dir: Path,
    fps: float,
) -> Path:
    tx = tx_entry["position"]
    stride = max(1, int(round(float(fps) / float(args.label_fps))))
    cmd: List[object] = [
        args.sionna_python,
        "-m",
        "dynamic_radio_dataset.rf.rss_compute",
        "--export-dir",
        export_dir,
        "--output-dir",
        output_dir,
        "--start-frame",
        0,
        "--end-frame",
        -1,
        "--stride",
        int(stride),
        "--resolution",
        int(args.rss_resolution),
        "--frequency",
        float(args.frequency),
        "--bandwidth",
        float(args.bandwidth),
        "--tx-power-dbm",
        float(args.tx_power_dbm),
        "--tx-height",
        float(args.tx_height),
        "--rx-height",
        float(args.rx_height),
        "--tx-placement",
        "custom",
        "--tx-x",
        float(tx["x"]),
        "--tx-y",
        float(tx["y"]),
        "--tx-z",
        float(tx["z"]),
        "--max-depth",
        int(args.max_depth),
        "--num-samples",
        int(args.num_samples),
        "--num-runs",
        int(args.num_runs),
        "--allow-flat-rss",
        "--no-auto-skip-unstable-start",
    ]
    cmd.extend(["--mitsuba-variant", str(args.mitsuba_variant)])
    if bool(args.use_gpu):
        cmd.append("--use-gpu")
    if args.mask_building_cells:
        cmd.append("--mask-building-cells")
    run_command(cmd, env=sionna_env(args))
    return output_dir / "rss_maps.npz"


def process_episode(args: argparse.Namespace) -> int:
    static_dir = scene_static_dir(args)
    episode_dir = args.episode_dir
    episode_id = args.episode_id or episode_dir.name
    from dynamic_radio_dataset.tx.assignment import selected_tx_catalog

    tx_selection = selected_tx_catalog(static_dir, episode_dir)
    reference_export_dir = resolve_reference_export_dir(args, static_dir)
    if reference_export_dir is None:
        raise RuntimeError("Reference export dir could not be resolved. Run prepare-scene first or pass --reference-export-dir.")

    export_dir = episode_dir / "sionna_export"
    if not _episode_export_ready(export_dir):
        if export_dir.exists():
            shutil.rmtree(export_dir)
        export_dataset_dir(args, dataset_dir=episode_dir, export_dir=export_dir, reuse_building_proxy_from_export=reference_export_dir)

    scene_meta = load_json(episode_dir / "scene_meta.json")
    expected_signature = load_json(static_dir / "scene_signature.json")
    actual_signature = scene_signature(scene_meta)
    if not scene_signature_matches(expected_signature, actual_signature):
        raise RuntimeError(
            f"Episode {episode_id} scene signature does not match the prepared fixed scene. "
            f"expected={expected_signature} actual={actual_signature}"
        )

    manifest = load_json(export_dir / "manifest.json")
    motion_path = export_dir / "motion.jsonl"
    motion_rows_by_frame = load_motion_rows_by_frame(motion_path)

    tx_reports = []
    first_frame_indices: Optional[List[int]] = None
    rss_stacks: List[np.ndarray] = []
    static_rss_stacks: List[np.ndarray] = []
    for tx in tx_selection["tx_catalog"]:
        tx_dir = episode_dir / tx["tx_id"]
        ensure_dir(tx_dir)
        run_dynamic_rss_for_tx(args, export_dir, tx, tx_dir, fps=float(scene_meta["fps"]))
        frame_indices = load_frame_indices_from_rss(tx_dir)
        if first_frame_indices is None:
            first_frame_indices = frame_indices
        elif frame_indices != first_frame_indices:
            raise RuntimeError(f"Frame indices differ across TX runs for episode {episode_id}.")
        with np.load(tx_dir / "rss_maps.npz") as data:
            rss_stack = np.asarray(data["rss_dbm"], dtype=np.float32)
        rss_stacks.append(rss_stack)
        static_map = np.load(static_dir / tx["tx_id"] / "static_rss_dbm.npy").astype(np.float32)
        static_rss_stacks.append(static_map)
        tx_reports.append(
            evaluate_episode_tx_qa(
                tx,
                manifest,
                motion_rows_by_frame,
                frame_indices,
                tx_dir,
                tx_qa_config_from_args(args),
                static_rss_path=static_dir / tx["tx_id"] / "static_rss_dbm.npy",
            )
        )

    if first_frame_indices is None:
        raise RuntimeError(f"No TX outputs were produced for episode {episode_id}.")

    traffic_grid, building_mask = build_traffic_grid_from_motion(
        manifest,
        motion_rows_by_frame,
        first_frame_indices,
        manifest["valid_crop"],
        int(args.rss_resolution),
    )
    rss_dynamic = np.stack(rss_stacks, axis=0)
    static_rss = np.stack(static_rss_stacks, axis=0)
    rss_delta = rss_dynamic - static_rss[:, None, :, :]

    np.savez_compressed(
        episode_dir / "traffic_grid_uint8.npz",
        traffic_grid_uint8=traffic_grid.astype(np.uint8),
        frame_indices=np.asarray(first_frame_indices, dtype=np.int32),
    )
    np.savez_compressed(
        episode_dir / "rss_dynamic_dbm.npz",
        dynamic_rss_dbm=rss_dynamic.astype(np.float32),
        frame_indices=np.asarray(first_frame_indices, dtype=np.int32),
        tx_ids=np.asarray([tx["tx_id"] for tx in tx_selection["tx_catalog"]]),
        tx_candidate_ids=np.asarray([tx.get("tx_candidate_id", tx["tx_id"]) for tx in tx_selection["tx_catalog"]]),
    )
    np.savez_compressed(
        episode_dir / "rss_delta_from_static_db.npz",
        delta_from_static_db=rss_delta.astype(np.float32),
        frame_indices=np.asarray(first_frame_indices, dtype=np.int32),
        tx_ids=np.asarray([tx["tx_id"] for tx in tx_selection["tx_catalog"]]),
        tx_candidate_ids=np.asarray([tx.get("tx_candidate_id", tx["tx_id"]) for tx in tx_selection["tx_catalog"]]),
    )

    qa_scene = evaluate_episode_scene_qa(
        actor_states_path=episode_dir / "frames" / "actor_states.jsonl",
        routes_path=episode_dir / "routes.json",
        manifest=manifest,
        fps=float(scene_meta["fps"]),
        config=episode_qa_config_from_args(args),
    )
    accepted_tx_ids = [report["tx_id"] for report in tx_reports if report["pair_qc_pass"]]
    validation_report = load_json(episode_dir / "validation_report.json") if (episode_dir / "validation_report.json").exists() else {}
    first_core_frame_by_actor = {
        str(actor_id): target.get("first_core_frame")
        for actor_id, target in dict(validation_report.get("targets", {})).items()
    }
    corridor_hit_frame_by_tx = {
        str(report["tx_id"]): (
            min(int(row["frame_index"]) for row in report.get("corridor_crossings", []))
            if report.get("corridor_crossings")
            else None
        )
        for report in tx_reports
    }
    expected_carla_frames = int(scene_meta.get("frames_requested", len(motion_rows_by_frame)))
    carla_clip_complete = len(motion_rows_by_frame) >= expected_carla_frames
    vehicle_role_counts = dict(scene_meta.get("vehicle_role_counts", {})) if isinstance(scene_meta.get("vehicle_role_counts"), dict) else {}
    if not vehicle_role_counts and (episode_dir / "trajectory_qa.json").exists():
        trajectory_qa = load_json(episode_dir / "trajectory_qa.json")
        vehicle_role_counts = dict(trajectory_qa.get("vehicle_role_counts", {})) if isinstance(trajectory_qa.get("vehicle_role_counts"), dict) else {}
    sampler_feedback = {}
    plan_meta = load_json(episode_dir / "plan.json") if (episode_dir / "plan.json").exists() else {}
    if (episode_dir / "demo_scenario.json").exists():
        scenario_meta = load_json(episode_dir / "demo_scenario.json")
        sampler_feedback = {
            "traffic_plan_path": scenario_meta.get("traffic_plan_path"),
            "offline_plan": scenario_meta.get("offline_plan", {}),
            "candidate_selection": scenario_meta.get("candidate_selection", {}),
            "collection_acceptance": scenario_meta.get("collection_acceptance", {}),
        }
    qa_report = {
        "schema": "single_scene_episode_qa_v1",
        "episode_id": episode_id,
        "scene_signature": actual_signature,
        "carla_clip_complete": bool(carla_clip_complete),
        "traffic_visual_pass": bool(qa_scene.get("traffic_visual_pass")),
        "propagation_candidate_pass": bool(qa_scene.get("propagation_candidate_pass")),
        "first_core_frame_by_actor": first_core_frame_by_actor,
        "corridor_hit_frame_by_tx": corridor_hit_frame_by_tx,
        "collision_summary": {
            "vehicle_vehicle_count": int(len(qa_scene.get("vehicle_collision", {}).get("violations", []))),
            "vehicle_building_count": int(len(qa_scene.get("building_collision", {}).get("violations", []))),
        },
        "sampler_feedback": sampler_feedback,
        "selected_tx_assignment": tx_selection.get("selected_tx_assignment"),
        "scene_qc_pass": bool(qa_scene["scene_qc_pass"]),
        "accepted_tx_ids": accepted_tx_ids if qa_scene["scene_qc_pass"] else [],
        "scene_qa": qa_scene,
        "tx_reports": tx_reports,
        "summary": {
            "accepted_pair_count": int(sum(bool(report["pair_qc_pass"]) for report in tx_reports if qa_scene["scene_qc_pass"])),
            "tx_count": int(len(tx_reports)),
            "frame_count": int(len(first_frame_indices)),
            "vehicle_role_counts": vehicle_role_counts,
        },
    }
    save_json(episode_dir / "qa_report.json", qa_report)

    episode_meta = {
        "schema": "single_scene_radio_episode_v1",
        "episode_id": episode_id,
        "source_episode_dir": str(episode_dir),
        "town": scene_meta.get("town"),
        "scene_id": plan_meta.get("scene_id") or scene_meta.get("scene_id") or actual_signature.get("scene_info", {}).get("scene_id"),
        "scene_mode": scene_meta.get("scene_mode"),
        "scene_type": plan_meta.get("scene_type") or scene_meta.get("scene_type"),
        "fps": float(scene_meta["fps"]),
        "label_fps": float(args.label_fps),
        "label_stride": max(1, int(round(float(scene_meta["fps"]) / float(args.label_fps)))),
        "frame_count": int(len(first_frame_indices)),
        "frame_indices": first_frame_indices,
        "tx_ids": [tx["tx_id"] for tx in tx_selection["tx_catalog"]],
        "tx_candidate_ids": [tx.get("tx_candidate_id", tx["tx_id"]) for tx in tx_selection["tx_catalog"]],
        "selected_tx_assignment": tx_selection.get("selected_tx_assignment"),
        "source_tx_catalog_count": tx_selection.get("source_tx_catalog_count", len(tx_selection["tx_catalog"])),
        "accepted_tx_ids": qa_report["accepted_tx_ids"],
        "scene_qc_pass": bool(qa_scene["scene_qc_pass"]),
        "vehicle_role_counts": vehicle_role_counts,
        "actual_total_vehicle_count": int(vehicle_role_counts.get("actual_total_vehicle_count", manifest.get("snapshot_vehicle_count", 0))),
        "actual_background_count": int(vehicle_role_counts.get("actual_background_count", 0)),
        "planned_required_controlled_count": int(vehicle_role_counts.get("planned_required_controlled_count", 0)),
        "actual_required_controlled_count": int(vehicle_role_counts.get("actual_required_controlled_count", 0)),
        "support_region": scene_meta["support_region"],
        "valid_crop": scene_meta["valid_crop"],
        "export_dir": str(export_dir),
        "building_mask_cells": int(np.sum(building_mask)),
    }
    save_json(episode_dir / "episode_meta.json", episode_meta)

    if (episode_dir / "topdown.mp4").exists():
        shutil.copy2(episode_dir / "topdown.mp4", episode_dir / "topdown_preview.mp4")
    print(
        f"[OK] Processed episode {episode_id}: scene_qc_pass={qa_scene['scene_qc_pass']} "
        f"accepted_tx_ids={qa_report['accepted_tx_ids']}"
    )
    return 0


def _episode_export_ready(export_dir: Path) -> bool:
    required = [export_dir / "manifest.json", export_dir / "scene.xml", export_dir / "motion.jsonl"]
    if not all(path.exists() and path.stat().st_size > 0 for path in required):
        return False
    try:
        manifest = load_json(export_dir / "manifest.json")
    except Exception:  # noqa: BLE001
        return False
    if "valid_crop" not in manifest or "support_region" not in manifest:
        return False
    try:
        with (export_dir / "motion.jsonl").open("r", encoding="utf-8") as f:
            return any(line.strip() for line in f)
    except OSError:
        return False
