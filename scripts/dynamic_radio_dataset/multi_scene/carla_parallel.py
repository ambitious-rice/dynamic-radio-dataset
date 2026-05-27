from __future__ import annotations

import concurrent.futures
import hashlib
import multiprocessing as mp
import os
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

from dynamic_radio_dataset.carla.runner import collect_from_plan_catalog
from dynamic_radio_dataset.json_utils import save_json
from dynamic_radio_dataset.multi_scene.carla_server import (
    close_handles,
    configure_worker_env,
    ensure_worker_carla,
    restart_worker_carla,
    sleep_restart_backoff,
    summarize_worker_carla_stderr,
    terminate_worker_carla,
)
from dynamic_radio_dataset.multi_scene.config import (
    load_multi_scene_config,
    make_single_scene_config,
    multi_scene_root,
    scene_dataset_root,
    validate_multi_scene_config,
)
from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments


def collect_multi_scene_carla_parallel(
    config_path: Path,
    *,
    carla_workers: int = 4,
    gpu_id: str | None = "1",
    gpu_ids: Sequence[str] | None = None,
    rpc_ports: Sequence[int] | None = None,
    traffic_manager_ports: Sequence[int] | None = None,
    target_accepted_per_scene: int | None = None,
    max_scenes: int | None = None,
    max_attempts_per_scene: int | None = None,
    resume: bool = True,
    keep_carla_running: bool = False,
    jitter_scene_seeds: bool = True,
    max_carla_restarts_per_scene: int = 120,
    no_rendering_mode: bool = True,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run CARLA-only collection for multi-scene configs with scene-level workers.

    This intentionally stays outside the Sionna/RF path. Each worker owns at
    most one CARLA server port and processes its assigned scenes serially.
    """

    config = load_multi_scene_config(config_path)
    validate_multi_scene_config(config)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    if not scenes:
        raise ValueError("No scenes selected for multi-scene CARLA collection.")
    scenes = _order_scenes_for_collection(config, scenes, target_accepted_per_scene)

    worker_count = max(1, min(int(carla_workers), len(scenes)))
    rpc = _ports(rpc_ports, default_start=2000, count=worker_count)
    tm = _ports(traffic_manager_ports, default_start=8000, count=worker_count)
    worker_gpus = _worker_gpus(gpu_ids, gpu_id=gpu_id, count=worker_count)
    root = multi_scene_root(config)
    indexes = root / "indexes"
    indexes.mkdir(parents=True, exist_ok=True)

    assignments = _chunk_scenes(scenes, worker_count)
    plan = {
        "schema": "multi_scene_carla_parallel_plan_v1",
        "config": str(config_path),
        "dataset_root": str(root),
        "worker_count": int(worker_count),
        "gpu_id": None if gpu_id is None else str(gpu_id),
        "gpu_ids": worker_gpus,
        "resume": bool(resume),
        "keep_carla_running": bool(keep_carla_running),
        "jitter_scene_seeds": bool(jitter_scene_seeds),
        "max_carla_restarts_per_scene": int(max_carla_restarts_per_scene),
        "no_rendering_mode": bool(no_rendering_mode),
        "dry_run": bool(dry_run),
        "workers": [
            {
                "worker_id": idx,
                "gpu_id": worker_gpus[idx],
                "rpc_port": int(rpc[idx]),
                "traffic_manager_port": int(tm[idx]),
                "scene_count": len(chunk),
                "scenes": [_scene_plan_row(config, scene, target_accepted_per_scene) for scene in chunk],
            }
            for idx, chunk in enumerate(assignments)
        ],
    }
    save_json(indexes / "multi_scene_collection_plan.json", plan)
    if dry_run:
        return {"status": "dry_run", "plan": str(indexes / "multi_scene_collection_plan.json"), **plan}

    futures = []
    started = time.time()
    mp_context = mp.get_context("spawn")
    with concurrent.futures.ProcessPoolExecutor(max_workers=worker_count, mp_context=mp_context) as pool:
        for idx, chunk in enumerate(assignments):
            futures.append(
                pool.submit(
                    _worker_collect,
                    dict(config),
                    [dict(scene) for scene in chunk],
                    idx,
                    int(rpc[idx]),
                    int(tm[idx]),
                    worker_gpus[idx],
                    target_accepted_per_scene,
                    max_attempts_per_scene,
                    bool(resume),
                    bool(keep_carla_running),
                    bool(jitter_scene_seeds),
                    int(max_carla_restarts_per_scene),
                    bool(no_rendering_mode),
                )
            )

        worker_results: list[dict[str, Any]] = []
        for future in concurrent.futures.as_completed(futures):
            worker_results.append(future.result())

    worker_results.sort(key=lambda row: int(row.get("worker_id", 0)))
    summary = {
        "schema": "multi_scene_carla_parallel_summary_v1",
        "status": "ok" if all(row.get("status") == "ok" for row in worker_results) else "partial_or_failed",
        "config": str(config_path),
        "dataset_root": str(root),
        "elapsed_s": float(time.time() - started),
        "worker_count": int(worker_count),
        "gpu_id": None if gpu_id is None else str(gpu_id),
        "gpu_ids": worker_gpus,
        "target_accepted_per_scene_override": target_accepted_per_scene,
        "max_attempts_per_scene": max_attempts_per_scene,
        "resume": bool(resume),
        "worker_results": worker_results,
        "totals": _totals(worker_results),
    }
    save_json(indexes / "multi_scene_collection_summary.json", summary)
    return {"summary": str(indexes / "multi_scene_collection_summary.json"), **summary}


def _worker_collect(
    config: dict[str, Any],
    scenes: list[dict[str, Any]],
    worker_id: int,
    rpc_port: int,
    traffic_manager_port: int,
    gpu_id: str | None,
    target_override: int | None,
    max_attempts_per_scene: int | None,
    resume: bool,
    keep_carla_running: bool,
    jitter_scene_seeds: bool,
    max_carla_restarts_per_scene: int,
    no_rendering_mode: bool,
) -> dict[str, Any]:
    configure_worker_env(gpu_id, worker_id)
    _stagger_worker_start(worker_id)
    proc = None
    handles: list[Any] = []
    owned_carla = False
    results: list[dict[str, Any]] = []
    started = time.time()
    try:
        for scene in scenes:
            scene_config = make_single_scene_config(config, scene)
            _apply_runtime_overrides(
                scene_config,
                scene,
                rpc_port=rpc_port,
                traffic_manager_port=traffic_manager_port,
                jitter_scene_seeds=jitter_scene_seeds,
                no_rendering_mode=no_rendering_mode,
            )
            scene_root = scene_dataset_root(config, scene)
            save_json(scene_root / "configs" / f"collection_runtime_worker_{worker_id}.json", scene_config)
            target = _target_for_scene(config, scene, target_override)
            scene_result: dict[str, Any] = {
                "scene_id": str(scene.get("scene_id")),
                "scene_type": str(scene.get("scene_type", "unknown")),
                "town": str(scene.get("town", "")),
                "worker_id": int(worker_id),
                "gpu_id": gpu_id,
                "rpc_port": int(rpc_port),
                "traffic_manager_port": int(traffic_manager_port),
                "target_accepted": target,
                "plan_count": _plan_count(scene_root),
                "initial_accepted": _accepted_count(scene_root),
            }
            try:
                if int(scene_result["plan_count"]) <= 0:
                    scene_result.update(
                        {
                            "status": "plan_catalog_empty",
                            "failure_code": "plan_catalog_empty",
                            "final_accepted": _accepted_count(scene_root),
                        }
                    )
                    save_json(scene_root / "collection_worker_summary.json", scene_result)
                    results.append(scene_result)
                    continue
                collection_runs: list[dict[str, Any]] = []
                backoff_history: list[dict[str, Any]] = []
                restart_count = 0
                collection: dict[str, Any] = {}
                last_restart_reason: str | None = None
                while True:
                    if target is not None and _accepted_count(scene_root) >= int(target):
                        collection = {"target_reached": True, "stopped_reason": "target_accepted_reached"}
                        break
                    try:
                        proc, handles, started_now = ensure_worker_carla(
                            scene_config, proc, handles, worker_id, startup_retries=2
                        )
                    except Exception as exc:  # noqa: BLE001
                        restart_count += 1
                        last_restart_reason = "carla_startup_failed"
                        proc = None
                        handles = []
                        collection = {
                            "target_reached": False,
                            "stopped_reason": "carla_startup_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        collection_runs.append(_compact_collection_run(collection))
                        if restart_count > int(max_carla_restarts_per_scene):
                            break
                        slept = sleep_restart_backoff(scene_config, restart_count)
                        backoff_history.append(
                            {"restart_count": int(restart_count), "reason": last_restart_reason, "sleep_s": slept}
                        )
                        continue
                    owned_carla = owned_carla or started_now
                    collection = collect_from_plan_catalog(
                        scene_config,
                        max_attempts=max_attempts_per_scene,
                        target_accepted=target,
                        resume=resume,
                        bucket_targets={},
                    )
                    collection_runs.append(_compact_collection_run(collection))
                    if bool(collection.get("target_reached")):
                        break
                    if not _needs_carla_restart(collection):
                        break
                    restart_count += 1
                    last_restart_reason = str(collection.get("stopped_reason", "carla_unavailable"))
                    if restart_count > int(max_carla_restarts_per_scene):
                        break
                    try:
                        slept = sleep_restart_backoff(scene_config, restart_count)
                        backoff_history.append(
                            {"restart_count": int(restart_count), "reason": last_restart_reason, "sleep_s": slept}
                        )
                        proc, handles = restart_worker_carla(
                            scene_config, proc, handles, worker_id, startup_retries=2
                        )
                        owned_carla = True
                    except Exception as exc:  # noqa: BLE001
                        last_restart_reason = "carla_restart_failed"
                        proc = None
                        handles = []
                        collection = {
                            "target_reached": False,
                            "stopped_reason": "carla_restart_failed",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                        collection_runs.append(_compact_collection_run(collection))
                        if restart_count > int(max_carla_restarts_per_scene):
                            break
                        slept = sleep_restart_backoff(scene_config, restart_count)
                        backoff_history.append(
                            {"restart_count": int(restart_count), "reason": last_restart_reason, "sleep_s": slept}
                        )
                scene_result["collection"] = collection
                scene_result["collection_runs"] = collection_runs
                scene_result["carla_restart_count"] = int(restart_count)
                scene_result["carla_backoff_history_s"] = backoff_history
                if last_restart_reason:
                    scene_result["last_restart_reason"] = last_restart_reason
                scene_result["tx_assignment"] = ensure_tx_assignments(scene_config)
                scene_result["final_accepted"] = _accepted_count(scene_root)
                if bool(collection.get("target_reached")):
                    scene_result["status"] = "ok"
                elif restart_count > int(max_carla_restarts_per_scene):
                    scene_result["status"] = "failed"
                    scene_result["failure_code"] = "carla_restart_budget_exhausted"
                else:
                    scene_result["status"] = "target_not_reached"
            except Exception as exc:  # noqa: BLE001
                scene_result.update(
                    {
                        "status": "failed",
                        "failure_code": "multi_scene_carla_collect_failed",
                        "error": f"{type(exc).__name__}: {exc}",
                        "final_accepted": _accepted_count(scene_root),
                    }
                )
            save_json(scene_root / "collection_worker_summary.json", scene_result)
            results.append(scene_result)
    finally:
        if proc is not None and owned_carla and not keep_carla_running:
            terminate_worker_carla(proc, handles, scene_config if "scene_config" in locals() else None)
        else:
            close_handles(handles)

    return {
        "schema": "multi_scene_carla_worker_summary_v1",
        "worker_id": int(worker_id),
        "status": _worker_status(results),
        "rpc_port": int(rpc_port),
        "traffic_manager_port": int(traffic_manager_port),
        "gpu_id": gpu_id,
        "elapsed_s": float(time.time() - started),
        "carla_crash_signature_summary": summarize_worker_carla_stderr(
            scene_config if "scene_config" in locals() else {"dataset": config.get("dataset", {})},
            worker_id,
        ),
        "scene_results": results,
    }


def _stagger_worker_start(worker_id: int) -> None:
    """Avoid launching all UE4 servers at the exact same moment."""

    try:
        step_s = float(os.environ.get("DRD_CARLA_WORKER_START_STAGGER_S", "15.0"))
    except ValueError:
        step_s = 15.0
    delay_s = max(0.0, step_s) * max(0, int(worker_id))
    if delay_s > 0.0:
        time.sleep(delay_s)


def _apply_runtime_overrides(
    scene_config: dict[str, Any],
    scene: Mapping[str, Any],
    *,
    rpc_port: int,
    traffic_manager_port: int,
    jitter_scene_seeds: bool,
    no_rendering_mode: bool,
) -> None:
    carla_cfg = scene_config.setdefault("carla", {})
    carla_cfg["host"] = "localhost"
    carla_cfg["port"] = int(rpc_port)
    carla_cfg["traffic_manager_port"] = int(traffic_manager_port)
    if no_rendering_mode:
        raw_timeout = carla_cfg.get("collect_subprocess_timeout_s", 180.0)
        carla_cfg["collect_subprocess_timeout_s"] = min(float(raw_timeout), 180.0)
    else:
        carla_cfg.setdefault("collect_subprocess_timeout_s", 600.0)
    carla_cfg["stop_on_collect_infra_failure"] = True
    carla_cfg["no_rendering_mode"] = bool(no_rendering_mode)
    supervisor = scene_config.setdefault("supervisor", {})
    supervisor.setdefault("sanitize_display_env", True)
    supervisor.setdefault("disable_core_dumps", True)
    supervisor.setdefault("use_nullrhi_for_no_rendering", False)
    nullrhi_expected = bool(supervisor.get("use_nullrhi", False)) or (
        bool(no_rendering_mode) and bool(supervisor.get("use_nullrhi_for_no_rendering", True))
    )
    supervisor.setdefault("gpu_start_health_check", False if no_rendering_mode else not nullrhi_expected)
    supervisor.setdefault("gpu_start_max_util_pct", 95.0)
    supervisor.setdefault("gpu_start_max_temp_c", 88.0)
    supervisor.setdefault("restart_backoff_s", [30.0, 60.0, 120.0, 300.0])
    collection = scene_config.setdefault("collection", {})
    collection["bucket_targets"] = {}
    collection["bucket_order"] = []
    collection.pop("selection_matrix", None)
    collection["selection_manifest"] = None
    if jitter_scene_seeds:
        base_seed = int(scene_config.get("dataset", {}).get("seed", 1701))
        scene_seed = base_seed + _stable_scene_offset(str(scene.get("scene_id", "")))
        scene_config.setdefault("dataset", {})["seed"] = int(scene_seed)
        scene_config.setdefault("collection", {})["bucket_shuffle_seed"] = int(scene_seed)


def _needs_carla_restart(collection: Mapping[str, Any]) -> bool:
    restart_reasons = {
        "carla_server_unavailable",
        "carla_attempt_infrastructure_failure",
        "carla_post_clip_infrastructure_failure",
    }
    if str(collection.get("stopped_reason")) in restart_reasons:
        return True
    server = collection.get("server_check")
    return isinstance(server, Mapping) and not bool(server.get("available", True))


def _compact_collection_run(collection: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "stopped_reason": collection.get("stopped_reason"),
        "target_reached": bool(collection.get("target_reached")),
        "total_trajectory_accepted": int(collection.get("total_trajectory_accepted", 0) or 0),
        "attempted": int(collection.get("attempted", 0) or 0),
        "server_check": collection.get("server_check"),
        "error": collection.get("error"),
    }


def _worker_status(results: Sequence[Mapping[str, Any]]) -> str:
    if any(str(row.get("status")) == "failed" for row in results):
        return "failed"
    if all(str(row.get("status")) == "ok" for row in results):
        return "ok"
    return "partial"


def _ports(values: Sequence[int] | None, *, default_start: int, count: int) -> list[int]:
    if values is None:
        return [default_start + 10 * idx for idx in range(count)]
    ports = [int(item) for item in values]
    if len(ports) < count:
        raise ValueError(f"Need at least {count} ports, got {len(ports)}")
    if len(set(ports[:count])) != count:
        raise ValueError(f"Ports must be unique: {ports[:count]}")
    return ports[:count]


def _worker_gpus(gpu_ids: Sequence[str] | None, *, gpu_id: str | None, count: int) -> list[str | None]:
    if gpu_ids:
        values = [str(item).strip() for item in gpu_ids if str(item).strip()]
        if not values:
            return [None] * int(count)
        return [values[idx % len(values)] for idx in range(int(count))]
    if gpu_id is None or not str(gpu_id).strip():
        return [None] * int(count)
    return [str(gpu_id).strip()] * int(count)


def _order_scenes_for_collection(config: Mapping[str, Any], scenes: Sequence[Mapping[str, Any]], target_override: int | None) -> list[Mapping[str, Any]]:
    return sorted(
        scenes,
        key=lambda scene: (
            -_remaining_for_scene(config, scene, target_override),
            str(scene.get("scene_id", "")),
        ),
    )


def _chunk_scenes(scenes: Sequence[Mapping[str, Any]], worker_count: int) -> list[list[Mapping[str, Any]]]:
    chunks: list[list[Mapping[str, Any]]] = [[] for _ in range(worker_count)]
    for idx, scene in enumerate(scenes):
        chunks[idx % worker_count].append(scene)
    return chunks


def _remaining_for_scene(config: Mapping[str, Any], scene: Mapping[str, Any], target_override: int | None) -> int:
    target = _target_for_scene(config, scene, target_override)
    return 1 if target is None else max(0, int(target) - _accepted_count(scene_dataset_root(config, scene)))


def _target_for_scene(config: Mapping[str, Any], scene: Mapping[str, Any], override: int | None) -> int | None:
    if override is not None:
        return int(override)
    dataset = config.get("dataset", {}) if isinstance(config.get("dataset"), Mapping) else {}
    raw = scene.get("target_accepted", dataset.get("accepted_trajectories_per_scene"))
    return int(raw) if raw not in (None, "") else None


def _scene_plan_row(config: Mapping[str, Any], scene: Mapping[str, Any], target_override: int | None) -> dict[str, Any]:
    scene_root = scene_dataset_root(config, scene)
    return {
        "scene_id": str(scene.get("scene_id")),
        "town": str(scene.get("town", "")),
        "scene_type": str(scene.get("scene_type", "unknown")),
        "target_accepted": _target_for_scene(config, scene, target_override),
        "plan_count": _plan_count(scene_root),
        "accepted_count": _accepted_count(scene_root),
    }


def _plan_count(scene_root: Path) -> int:
    path = scene_root / "plan_catalog" / "plans.jsonl"
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


def _accepted_count(scene_root: Path) -> int:
    count = 0
    for qa_path in sorted((scene_root / "episodes").glob("episode_*/trajectory_qa.json")):
        try:
            import json

            count += bool(json.loads(qa_path.read_text(encoding="utf-8")).get("trajectory_qc_pass"))
        except Exception:  # noqa: BLE001
            continue
    return int(count)


def _stable_scene_offset(scene_id: str) -> int:
    digest = hashlib.sha1(scene_id.encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % 100_000


def _totals(worker_results: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    scene_rows = [row for worker in worker_results for row in worker.get("scene_results", [])]
    return {
        "scene_count": len(scene_rows),
        "ok_scene_count": sum(str(row.get("status")) == "ok" for row in scene_rows),
        "target_not_reached_scene_count": sum(str(row.get("status")) == "target_not_reached" for row in scene_rows),
        "plan_catalog_empty_scene_count": sum(str(row.get("status")) == "plan_catalog_empty" for row in scene_rows),
        "failed_scene_count": sum(str(row.get("status")) == "failed" for row in scene_rows),
        "initial_accepted": sum(int(row.get("initial_accepted", 0)) for row in scene_rows),
        "final_accepted": sum(int(row.get("final_accepted", row.get("initial_accepted", 0))) for row in scene_rows),
    }
