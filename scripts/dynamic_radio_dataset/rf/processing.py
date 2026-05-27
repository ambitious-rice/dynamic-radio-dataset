from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Sequence

import numpy as np

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, resolve_repo_path
from dynamic_radio_dataset.rf.runtime import run_command, sionna_env, sionna_python, sionna_runtime_key


def process_rf(
    config: dict,
    max_episodes: int | None = None,
    rf_policy: str | None = None,
    use_gpu: bool | None = None,
    gpu_ids: Sequence[str] | None = None,
    workers: int | None = None,
) -> dict:
    policy = rf_policy or str(config.get("sionna", {}).get("rf_policy", "all_tx"))
    if policy != "all_tx":
        raise ValueError("Only rf_policy=all_tx is wired for formal processing; target_tx_first remains debug-only.")
    root = dataset_root(config)
    assignment_summary = _ensure_selected_tx_assignments_if_configured(config)
    episode_dirs = trajectory_accepted_episode_dirs(root)
    if max_episodes is not None:
        episode_dirs = episode_dirs[: int(max_episodes)]
    sionna_cfg = config.get("sionna", {})
    gpu_enabled = bool(sionna_cfg.get("use_gpu", False) if use_gpu is None else use_gpu)
    configured_gpu_ids = gpu_ids if gpu_ids is not None else sionna_cfg.get("gpu_ids", [])
    gpu_id_list = [str(item) for item in configured_gpu_ids] if isinstance(configured_gpu_ids, list) else []
    if gpu_enabled and not gpu_id_list:
        gpu_id_list = ["0"]
    worker_count = int(workers if workers is not None else sionna_cfg.get("rf_workers", len(gpu_id_list) if gpu_enabled else 1))
    worker_count = max(1, worker_count)
    if not gpu_enabled:
        gpu_id_list = []
        worker_count = 1 if workers is None else worker_count

    tasks = []
    skipped_complete = 0
    for episode_dir in episode_dirs:
        complete, _details = rf_episode_complete(config, episode_dir)
        if complete:
            skipped_complete += 1
            continue
        tasks.append(episode_dir)

    processed = 0
    failed: list[dict[str, Any]] = []
    if tasks and gpu_enabled and gpu_id_list:
        slot_count = max(1, worker_count)
        with ThreadPoolExecutor(max_workers=slot_count) as executor:
            futures = {}
            for worker_index in range(slot_count):
                slot_tasks = tasks[worker_index::slot_count]
                if not slot_tasks:
                    continue
                gpu_id = gpu_id_list[worker_index % len(gpu_id_list)]
                future = executor.submit(
                    process_rf_episode_slot,
                    config,
                    slot_tasks,
                    gpu_enabled,
                    gpu_id,
                    worker_index,
                )
                futures[future] = worker_index
            for future in as_completed(futures):
                worker_index = futures[future]
                try:
                    slot_results = future.result()
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {
                            "worker_index": int(worker_index),
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    continue
                for result in slot_results:
                    if result.get("status") == "processed":
                        processed += 1
                    elif result.get("status") == "failed":
                        failed.append(result)
    elif tasks:
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {}
            for index, episode_dir in enumerate(tasks):
                gpu_id = gpu_id_list[index % len(gpu_id_list)] if gpu_enabled and gpu_id_list else None
                future = executor.submit(
                    process_one_rf_episode,
                    config,
                    episode_dir,
                    gpu_enabled,
                    gpu_id,
                    index % worker_count,
                )
                futures[future] = episode_dir
            for future in as_completed(futures):
                episode_dir = futures[future]
                try:
                    result = future.result()
                except Exception as exc:  # noqa: BLE001
                    failed.append(
                        {
                            "episode_id": episode_dir.name,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                else:
                    if result.get("status") == "processed":
                        processed += 1
                    elif result.get("status") == "failed":
                        failed.append(result)
    summary = {
        "episode_candidates": len(episode_dirs),
        "complete_skipped_episode_count": int(skipped_complete),
        "queued_episode_count": int(len(tasks)),
        "processed_episode_count": int(processed),
        "failed_episode_count": int(len(failed)),
        "use_gpu": bool(gpu_enabled),
        "gpu_ids": gpu_id_list,
        "worker_count": int(worker_count),
        "tx_assignment": assignment_summary,
        "failures": failed,
    }
    if failed:
        save_json(root / "rf_failure_summary.json", {"schema": "rf_failure_summary_v1", **summary})
        raise RuntimeError(f"RF processing failed for {len(failed)} episode(s); see {root / 'rf_failure_summary.json'}")
    return summary


def trajectory_accepted_episode_dirs(root: Path) -> list[Path]:
    result = []
    for episode_dir in sorted((root / "episodes").glob("episode_*")):
        if trajectory_qc_pass(episode_dir):
            result.append(episode_dir)
    return result


def trajectory_qc_pass(episode_dir: Path) -> bool:
    qa_path = episode_dir / "trajectory_qa.json"
    if not qa_path.exists():
        return False
    report = load_json(qa_path)
    return bool(report.get("trajectory_qc_pass"))


def process_one_rf_episode(
    config: dict,
    episode_dir: Path,
    use_gpu: bool,
    gpu_id: str | None,
    worker_index: int,
) -> dict:
    started = time.time()
    complete_before, before_details = rf_episode_complete(config, episode_dir)
    if complete_before:
        return {"episode_id": episode_dir.name, "status": "complete_skipped", "completeness": before_details}
    try:
        run_process_episode_subprocess(
            config,
            episode_dir,
            use_gpu=use_gpu,
            gpu_id=gpu_id,
            worker_index=worker_index,
        )
        complete_after, after_details = rf_episode_complete(config, episode_dir)
        if not complete_after:
            raise RuntimeError(f"RF output incomplete after processing: {after_details}")
    except Exception as exc:
        meta = {
            "schema": "rf_process_meta_v1",
            "episode_id": episode_dir.name,
            "status": "failed",
            "elapsed_s": float(time.time() - started),
            "error": f"{type(exc).__name__}: {exc}",
            "use_gpu": bool(use_gpu),
            "gpu_id": gpu_id,
            "worker_index": int(worker_index),
            "mitsuba_variant": str(config.get("sionna", {}).get("mitsuba_variant", "cuda_ad_rgb" if use_gpu else "llvm_ad_rgb")),
            "completeness_before": before_details,
        }
        save_json(episode_dir / "rf_process_meta.json", meta)
        return meta
    meta = {
        "schema": "rf_process_meta_v1",
        "episode_id": episode_dir.name,
        "status": "processed",
        "elapsed_s": float(time.time() - started),
        "use_gpu": bool(use_gpu),
        "gpu_id": gpu_id,
        "worker_index": int(worker_index),
        "mitsuba_variant": str(config.get("sionna", {}).get("mitsuba_variant", "cuda_ad_rgb" if use_gpu else "llvm_ad_rgb")),
        "completeness_before": before_details,
        "completeness_after": after_details,
    }
    save_json(episode_dir / "rf_process_meta.json", meta)
    return meta


def process_rf_episode_slot(
    config: dict,
    episode_dirs: Sequence[Path],
    use_gpu: bool,
    gpu_id: str | None,
    worker_index: int,
) -> list[dict[str, Any]]:
    return [
        process_one_rf_episode(
            config,
            episode_dir,
            use_gpu=use_gpu,
            gpu_id=gpu_id,
            worker_index=worker_index,
        )
        for episode_dir in episode_dirs
    ]


def rf_episode_complete(config: dict, episode_dir: Path) -> tuple[bool, dict[str, Any]]:
    tx_ids = expected_tx_ids(config, episode_dir=episode_dir)
    expected_frames = int(round(float(config["traffic"]["duration_s"]) * float(config["traffic"]["fps"])))
    resolution = int(config.get("sionna", {}).get("resolution", 128))
    required = [
        episode_dir / "qa_report.json",
        episode_dir / "episode_meta.json",
        episode_dir / "traffic_grid_uint8.npz",
        episode_dir / "rss_dynamic_dbm.npz",
        episode_dir / "rss_delta_from_static_db.npz",
    ]
    missing = [str(path.name) for path in required if not path.exists()]
    missing.extend(str(Path(tx_id) / "rss_maps.npz") for tx_id in tx_ids if not (episode_dir / tx_id / "rss_maps.npz").exists())
    if missing:
        return False, {"complete": False, "failure_code": "missing_rf_artifacts", "missing": missing}
    try:
        with np.load(episode_dir / "rss_dynamic_dbm.npz") as data:
            if "dynamic_rss_dbm" not in data.files:
                return False, {"complete": False, "failure_code": "missing_dynamic_rss_key"}
            dynamic = np.asarray(data["dynamic_rss_dbm"])
            frame_indices = np.asarray(data["frame_indices"]) if "frame_indices" in data.files else np.asarray([])
        expected_shape = (len(tx_ids), expected_frames, resolution, resolution)
        if tuple(dynamic.shape) != expected_shape:
            return False, {
                "complete": False,
                "failure_code": "dynamic_rss_shape_mismatch",
                "actual_shape": list(dynamic.shape),
                "expected_shape": list(expected_shape),
            }
        if int(frame_indices.size) != expected_frames:
            return False, {
                "complete": False,
                "failure_code": "dynamic_frame_indices_mismatch",
                "actual_frame_count": int(frame_indices.size),
                "expected_frame_count": int(expected_frames),
            }
        tx_shapes = {}
        for tx_id in tx_ids:
            with np.load(episode_dir / tx_id / "rss_maps.npz") as tx_data:
                rss = np.asarray(tx_data["rss_dbm"])
            tx_shapes[tx_id] = list(rss.shape)
            if tuple(rss.shape) != (expected_frames, resolution, resolution):
                return False, {
                    "complete": False,
                    "failure_code": "tx_rss_shape_mismatch",
                    "tx_id": tx_id,
                    "actual_shape": list(rss.shape),
                    "expected_shape": [expected_frames, resolution, resolution],
                }
        qa_report = load_json(episode_dir / "qa_report.json")
        tx_reports = qa_report.get("tx_reports", []) if isinstance(qa_report.get("tx_reports"), list) else []
        if len(tx_reports) != len(tx_ids):
            return False, {
                "complete": False,
                "failure_code": "tx_report_count_mismatch",
                "actual_tx_report_count": int(len(tx_reports)),
                "expected_tx_count": int(len(tx_ids)),
            }
    except Exception as exc:  # noqa: BLE001
        return False, {"complete": False, "failure_code": "rf_artifact_read_failed", "error": f"{type(exc).__name__}: {exc}"}
    return True, {
        "complete": True,
        "expected_shape": [len(tx_ids), expected_frames, resolution, resolution],
        "tx_ids": tx_ids,
        "tx_shapes": tx_shapes,
    }


def expected_tx_ids(config: dict, episode_dir: Path | None = None) -> list[str]:
    if episode_dir is not None and (episode_dir / "tx_assignment.json").exists():
        assignment = load_json(episode_dir / "tx_assignment.json")
        return [str(tx_id) for tx_id in assignment.get("selected_tx_ids", [])]
    tx_catalog_path = dataset_root(config) / "scene_static" / "tx_catalog.json"
    tx_catalog = load_json(tx_catalog_path)["tx_catalog"]
    tx_ids = [str(tx["tx_id"]) for tx in tx_catalog]
    selected_count = int(config.get("tx", {}).get("selected_tx_per_episode", 0)) if isinstance(config.get("tx"), dict) else 0
    if selected_count > 0:
        return tx_ids[:selected_count]
    return tx_ids


def expected_tx_count(config: dict) -> int:
    selected_count = int(config.get("tx", {}).get("selected_tx_per_episode", 0)) if isinstance(config.get("tx"), dict) else 0
    if selected_count > 0:
        return selected_count
    return len(expected_tx_ids(config))


def _ensure_selected_tx_assignments_if_configured(config: dict) -> dict[str, Any]:
    selected_count = int(config.get("tx", {}).get("selected_tx_per_episode", 0)) if isinstance(config.get("tx"), dict) else 0
    if selected_count <= 0:
        return {"mode": "all_tx"}
    from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments

    return ensure_tx_assignments(config)


def run_process_episode_subprocess(
    config: dict,
    episode_dir: Path,
    *,
    use_gpu: bool = False,
    gpu_id: str | None = None,
    worker_index: int = 0,
) -> None:
    root = dataset_root(config)
    sionna = config.get("sionna", {})
    qa = config.get("qa", {})
    cmd: list[object] = [
        sionna_python(config),
        "-m",
        "dynamic_radio_dataset.pipeline.process_rf",
        "process-episode",
        "--dataset-root",
        root,
        "--episode-dir",
        episode_dir,
        "--scene-static-dir",
        root / "scene_static",
        "--reference-export-dir",
        reference_export_dir(config),
        "--label-fps",
        float(config["traffic"]["fps"]),
        "--rss-resolution",
        int(sionna.get("resolution", 128)),
        "--frequency",
        float(sionna.get("frequency_hz", 5.89e9)),
        "--bandwidth",
        float(sionna.get("bandwidth_hz", 10e6)),
        "--tx-power-dbm",
        float(sionna.get("tx_power_dbm", 23.0)),
        "--max-depth",
        int(sionna.get("max_depth", 3)),
        "--num-samples",
        int(sionna.get("num_samples", 250000)),
        "--mitsuba-variant",
        str(sionna.get("mitsuba_variant", "cuda_ad_rgb" if use_gpu else "llvm_ad_rgb")),
        "--large-vehicle-token",
        str(qa.get("large_vehicle_token", "fusorosa")),
        "--max-large-vehicle-count",
        int(qa.get("max_large_vehicle_count", config.get("traffic", {}).get("max_large_vehicle_count", 1))),
        "--diagnostic-clearance-reference-m",
        float(config.get("diagnostics", {}).get("tx_clearance_reference_m", 2.5)),
        "--max-all-slow-duration-s",
        float(qa.get("max_all_slow_duration_s", 2.0)),
        "--max-idle-fraction",
        float(qa.get("max_idle_fraction", 0.70)),
    ]
    if use_gpu:
        cmd.append("--use-gpu")
    env = sionna_env(runtime_key=sionna_runtime_key(gpu_id=gpu_id, worker_index=worker_index) if use_gpu else None)
    if use_gpu and gpu_id is not None:
        env.setdefault("CUDA_DEVICE_ORDER", "PCI_BUS_ID")
        env["CUDA_VISIBLE_DEVICES"] = str(gpu_id)
    elif not use_gpu:
        env["CUDA_VISIBLE_DEVICES"] = ""
    run_command(cmd, env=env)


def reference_export_dir(config: dict) -> Path:
    root = dataset_root(config)
    local_reference_export = root / "reference_scene" / "sionna_export"
    if local_reference_export.exists():
        return local_reference_export
    return resolve_repo_path(config["scene"]["reference_export_dir"])
