from __future__ import annotations

import shutil
import socket
import subprocess
import sys
import time
import os
import random
from collections import Counter
from glob import glob
from pathlib import Path
from typing import Any, Mapping, Sequence

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, ensure_dataset_dirs, repo_root
from dynamic_radio_dataset.plans.catalog import read_plan_catalog
from dynamic_radio_dataset.plans.schemas import TrafficPlan
from dynamic_radio_dataset.qa.trajectory_qa import evaluate_attempt_trajectory


def collect_from_plan_catalog(
    config: dict,
    max_attempts: int | None = None,
    target_accepted: int | None = None,
    resume: bool = True,
    bucket_targets: Mapping[int | str, int] | None = None,
    selection_manifest: Path | None = None,
) -> dict:
    dirs = ensure_dataset_dirs(config)
    plans = read_plan_catalog(dirs["plan_catalog"] / "plans.jsonl")
    max_retries = int(config.get("carla", {}).get("max_retries_per_plan", 3))
    selection_manifest_path = _selection_manifest_path(config, selection_manifest)
    selection_data = load_json(selection_manifest_path) if selection_manifest_path is not None else None
    collection_bucket_targets = {} if selection_data else _collection_bucket_targets(config, bucket_targets)
    if max_attempts is not None:
        attempt_budget = int(max_attempts)
    elif selection_data:
        attempt_budget = int(
            _selection_target_total(selection_data)
            * max(1, int(config.get("collection", {}).get("attempts_per_target_accepted", max_retries)))
        )
    elif collection_bucket_targets:
        attempt_budget = int(
            sum(collection_bucket_targets.values())
            * max(1, int(config.get("collection", {}).get("attempts_per_target_accepted", max_retries)))
        )
    elif target_accepted is not None:
        attempt_budget = len(plans) * max(1, max_retries)
    else:
        attempt_budget = len(plans)
    if selection_data:
        target_accepted_count = int(_selection_target_total(selection_data))
    elif collection_bucket_targets:
        target_accepted_count = int(sum(collection_bucket_targets.values()))
    else:
        target_accepted_count = int(target_accepted) if target_accepted is not None else None
    if target_accepted_count is not None and target_accepted_count <= 0:
        raise ValueError("--target-accepted must be a positive integer when provided.")
    completed_plan_ids = _completed_plan_ids(dirs["episodes"]) if resume else set()
    existing_accepted = _trajectory_accepted_episode_count(dirs["episodes"])
    if selection_data:
        return _collect_selection_aware(
            config=config,
            dirs=dirs,
            plans=plans,
            max_retries=max_retries,
            attempt_budget=attempt_budget,
            target_accepted_count=target_accepted_count,
            selection_manifest=selection_data,
            selection_manifest_path=selection_manifest_path,
            resume=resume,
            completed_plan_ids=completed_plan_ids,
            existing_accepted=existing_accepted,
        )
    if collection_bucket_targets:
        return _collect_bucket_aware(
            config=config,
            dirs=dirs,
            plans=plans,
            max_retries=max_retries,
            attempt_budget=attempt_budget,
            target_accepted_count=target_accepted_count,
            bucket_targets=collection_bucket_targets,
            resume=resume,
            completed_plan_ids=completed_plan_ids,
            existing_accepted=existing_accepted,
        )
    accepted = 0
    attempted = 0
    skipped_completed = 0
    stopped_reason = "plan_catalog_exhausted"

    def target_reached() -> bool:
        if target_accepted_count is None:
            return False
        base = existing_accepted if resume else 0
        return base + accepted >= target_accepted_count

    if target_reached():
        summary = _collection_summary(
            config=config,
            dirs=dirs,
            attempted=attempted,
            accepted=accepted,
            skipped_completed=skipped_completed,
            existing_accepted=existing_accepted,
            target_accepted=target_accepted_count,
            attempt_budget=attempt_budget,
            plan_catalog_count=len(plans),
            resume=resume,
            stopped_reason="target_accepted_reached",
        )
        _write_collection_summary(dirs["root"], summary)
        return summary

    for plan_index, plan in enumerate(plans):
        if target_reached():
            stopped_reason = "target_accepted_reached"
            break
        if resume and plan.plan_id in completed_plan_ids:
            skipped_completed += 1
            continue
        if attempted >= attempt_budget:
            stopped_reason = "attempt_budget_exhausted"
            break
        for retry_index in range(max_retries):
            if target_reached():
                stopped_reason = "target_accepted_reached"
                break
            if attempted >= attempt_budget:
                stopped_reason = "attempt_budget_exhausted"
                break
            server = check_carla_server(config)
            if not server["available"]:
                summary = _collection_summary(
                    config=config,
                    dirs=dirs,
                    attempted=attempted,
                    accepted=accepted,
                    skipped_completed=skipped_completed,
                    existing_accepted=existing_accepted,
                    target_accepted=target_accepted_count,
                    attempt_budget=attempt_budget,
                    plan_catalog_count=len(plans),
                    resume=resume,
                    stopped_reason="carla_server_unavailable",
                    server_check=server,
                )
                _write_collection_summary(dirs["root"], summary)
                return summary
            attempt_id = _next_id(dirs["attempts"], "attempt")
            attempt_dir = dirs["attempts"] / attempt_id
            attempt_dir.mkdir(parents=True, exist_ok=True)
            attempted += 1
            result = collect_attempt(config, plan, attempt_dir, plan_index, retry_index)
            if result["status"] == "TRAJECTORY_ACCEPTED":
                episode_id = _next_id(dirs["episodes"], "episode")
                episode_dir = dirs["episodes"] / episode_id
                _materialize_episode(attempt_dir, episode_dir, episode_id)
                accepted += 1
                completed_plan_ids.add(plan.plan_id)
                break
            _archive_failed_attempt(config, attempt_dir, result)
    if target_reached():
        stopped_reason = "target_accepted_reached"
    elif attempted >= attempt_budget:
        stopped_reason = "attempt_budget_exhausted"
    summary = _collection_summary(
        config=config,
        dirs=dirs,
        attempted=attempted,
        accepted=accepted,
        skipped_completed=skipped_completed,
        existing_accepted=existing_accepted,
        target_accepted=target_accepted_count,
        attempt_budget=attempt_budget,
        plan_catalog_count=len(plans),
        resume=resume,
        stopped_reason=stopped_reason,
    )
    _write_collection_summary(dirs["root"], summary)
    return summary


def _collect_bucket_aware(
    *,
    config: dict,
    dirs: dict[str, Path],
    plans: Sequence[TrafficPlan],
    max_retries: int,
    attempt_budget: int,
    target_accepted_count: int | None,
    bucket_targets: dict[int, int],
    resume: bool,
    completed_plan_ids: set[str],
    existing_accepted: int,
) -> dict:
    order = _bucket_order(config, bucket_targets)
    plan_groups = _bucket_plan_entries(config, plans, order)
    existing_bucket_counts = _accepted_bucket_counts(dirs["episodes"]) if resume else Counter()
    accepted_by_bucket: Counter[str] = Counter()
    attempted_by_bucket: Counter[str] = Counter()
    accepted = 0
    attempted = 0
    skipped_completed = 0
    stopped_reason = "bucket_targets_reached"

    def bucket_total(bucket: int) -> int:
        return int(existing_bucket_counts.get(str(bucket), 0) + accepted_by_bucket.get(str(bucket), 0))

    def bucket_reached(bucket: int) -> bool:
        return bucket_total(bucket) >= int(bucket_targets[bucket])

    def all_buckets_reached() -> bool:
        return all(bucket_reached(bucket) for bucket in order)

    if all_buckets_reached():
        summary = _collection_summary(
            config=config,
            dirs=dirs,
            attempted=attempted,
            accepted=accepted,
            skipped_completed=skipped_completed,
            existing_accepted=existing_accepted,
            target_accepted=target_accepted_count,
            attempt_budget=attempt_budget,
            plan_catalog_count=len(plans),
            resume=resume,
            stopped_reason="bucket_targets_reached",
            bucket_targets=bucket_targets,
            existing_bucket_counts=existing_bucket_counts,
            accepted_by_bucket=accepted_by_bucket,
            attempted_by_bucket=attempted_by_bucket,
        )
        _write_collection_summary(dirs["root"], summary)
        return summary

    for bucket in order:
        if bucket_reached(bucket):
            continue
        bucket_plans = plan_groups.get(bucket, [])
        if not bucket_plans:
            stopped_reason = f"bucket_{bucket}_plan_catalog_exhausted"
            break
        for plan_index, plan in bucket_plans:
            if bucket_reached(bucket):
                break
            if resume and plan.plan_id in completed_plan_ids:
                skipped_completed += 1
                continue
            if attempted >= attempt_budget:
                stopped_reason = "attempt_budget_exhausted"
                break
            for retry_index in range(max_retries):
                if bucket_reached(bucket):
                    break
                if attempted >= attempt_budget:
                    stopped_reason = "attempt_budget_exhausted"
                    break
                server = check_carla_server(config)
                if not server["available"]:
                    summary = _collection_summary(
                        config=config,
                        dirs=dirs,
                        attempted=attempted,
                        accepted=accepted,
                        skipped_completed=skipped_completed,
                        existing_accepted=existing_accepted,
                        target_accepted=target_accepted_count,
                        attempt_budget=attempt_budget,
                        plan_catalog_count=len(plans),
                        resume=resume,
                        stopped_reason="carla_server_unavailable",
                        server_check=server,
                        bucket_targets=bucket_targets,
                        existing_bucket_counts=existing_bucket_counts,
                        accepted_by_bucket=accepted_by_bucket,
                        attempted_by_bucket=attempted_by_bucket,
                    )
                    _write_collection_summary(dirs["root"], summary)
                    return summary
                attempt_id = _next_id(dirs["attempts"], "attempt")
                attempt_dir = dirs["attempts"] / attempt_id
                attempt_dir.mkdir(parents=True, exist_ok=True)
                attempted += 1
                attempted_by_bucket[str(bucket)] += 1
                result = collect_attempt(config, plan, attempt_dir, plan_index, retry_index)
                if result["status"] == "TRAJECTORY_ACCEPTED":
                    episode_id = _next_id(dirs["episodes"], "episode")
                    episode_dir = dirs["episodes"] / episode_id
                    _materialize_episode(attempt_dir, episode_dir, episode_id)
                    accepted += 1
                    accepted_by_bucket[str(bucket)] += 1
                    completed_plan_ids.add(plan.plan_id)
                    break
                _archive_failed_attempt(config, attempt_dir, result)
            if stopped_reason == "attempt_budget_exhausted":
                break
        if stopped_reason == "attempt_budget_exhausted":
            break
        if not bucket_reached(bucket):
            stopped_reason = f"bucket_{bucket}_target_not_reached"
            break

    if all_buckets_reached():
        stopped_reason = "bucket_targets_reached"

    summary = _collection_summary(
        config=config,
        dirs=dirs,
        attempted=attempted,
        accepted=accepted,
        skipped_completed=skipped_completed,
        existing_accepted=existing_accepted,
        target_accepted=target_accepted_count,
        attempt_budget=attempt_budget,
        plan_catalog_count=len(plans),
        resume=resume,
        stopped_reason=stopped_reason,
        bucket_targets=bucket_targets,
        existing_bucket_counts=existing_bucket_counts,
        accepted_by_bucket=accepted_by_bucket,
        attempted_by_bucket=attempted_by_bucket,
    )
    _write_collection_summary(dirs["root"], summary)
    return summary


def _collect_selection_aware(
    *,
    config: dict,
    dirs: dict[str, Path],
    plans: Sequence[TrafficPlan],
    max_retries: int,
    attempt_budget: int,
    target_accepted_count: int | None,
    selection_manifest: dict[str, Any],
    selection_manifest_path: Path,
    resume: bool,
    completed_plan_ids: set[str],
    existing_accepted: int,
) -> dict:
    cells = _selection_cells(selection_manifest)
    plan_by_id = {str(plan.plan_id): (index, plan) for index, plan in enumerate(plans)}
    existing_cell_counts = _accepted_selection_cell_counts(dirs["episodes"]) if resume else Counter()
    accepted_by_cell: Counter[str] = Counter()
    attempted_by_cell: Counter[str] = Counter()
    accepted = 0
    attempted = 0
    skipped_completed = 0
    missing_manifest_plan_ids: list[str] = []
    stopped_reason = "selection_targets_reached"

    def cell_total(cell: Mapping[str, Any]) -> int:
        cell_id = str(cell["cell_id"])
        return int(existing_cell_counts.get(cell_id, 0) + accepted_by_cell.get(cell_id, 0))

    def cell_reached(cell: Mapping[str, Any]) -> bool:
        return cell_total(cell) >= int(cell["target_accepted"])

    def all_cells_reached() -> bool:
        return all(cell_reached(cell) for cell in cells)

    if all_cells_reached():
        summary = _collection_summary(
            config=config,
            dirs=dirs,
            attempted=attempted,
            accepted=accepted,
            skipped_completed=skipped_completed,
            existing_accepted=existing_accepted,
            target_accepted=target_accepted_count,
            attempt_budget=attempt_budget,
            plan_catalog_count=len(plans),
            resume=resume,
            stopped_reason="selection_targets_reached",
            selection_manifest_path=selection_manifest_path,
            selection_cells=cells,
            existing_selection_cell_counts=existing_cell_counts,
            accepted_by_selection_cell=accepted_by_cell,
            attempted_by_selection_cell=attempted_by_cell,
        )
        _write_collection_summary(dirs["root"], summary)
        return summary

    for cell in cells:
        cell_id = str(cell["cell_id"])
        if cell_reached(cell):
            continue
        candidate_ids = [str(plan_id) for plan_id in cell.get("candidate_plan_ids", [])]
        if not candidate_ids:
            stopped_reason = f"selection_cell_{cell_id}_plan_catalog_exhausted"
            break
        for plan_id in candidate_ids:
            if cell_reached(cell):
                break
            plan_entry = plan_by_id.get(plan_id)
            if plan_entry is None:
                missing_manifest_plan_ids.append(plan_id)
                continue
            plan_index, plan = plan_entry
            if resume and plan.plan_id in completed_plan_ids:
                skipped_completed += 1
                continue
            if attempted >= attempt_budget:
                stopped_reason = "attempt_budget_exhausted"
                break
            for retry_index in range(max_retries):
                if cell_reached(cell):
                    break
                if attempted >= attempt_budget:
                    stopped_reason = "attempt_budget_exhausted"
                    break
                server = check_carla_server(config)
                if not server["available"]:
                    summary = _collection_summary(
                        config=config,
                        dirs=dirs,
                        attempted=attempted,
                        accepted=accepted,
                        skipped_completed=skipped_completed,
                        existing_accepted=existing_accepted,
                        target_accepted=target_accepted_count,
                        attempt_budget=attempt_budget,
                        plan_catalog_count=len(plans),
                        resume=resume,
                        stopped_reason="carla_server_unavailable",
                        server_check=server,
                        selection_manifest_path=selection_manifest_path,
                        selection_cells=cells,
                        existing_selection_cell_counts=existing_cell_counts,
                        accepted_by_selection_cell=accepted_by_cell,
                        attempted_by_selection_cell=attempted_by_cell,
                        missing_manifest_plan_ids=missing_manifest_plan_ids,
                    )
                    _write_collection_summary(dirs["root"], summary)
                    return summary
                attempt_id = _next_id(dirs["attempts"], "attempt")
                attempt_dir = dirs["attempts"] / attempt_id
                attempt_dir.mkdir(parents=True, exist_ok=True)
                attempted += 1
                attempted_by_cell[cell_id] += 1
                result = collect_attempt(config, plan, attempt_dir, plan_index, retry_index)
                if result["status"] == "TRAJECTORY_ACCEPTED":
                    episode_id = _next_id(dirs["episodes"], "episode")
                    episode_dir = dirs["episodes"] / episode_id
                    _materialize_episode(attempt_dir, episode_dir, episode_id)
                    accepted += 1
                    accepted_by_cell[cell_id] += 1
                    completed_plan_ids.add(plan.plan_id)
                    break
                _archive_failed_attempt(config, attempt_dir, result)
            if stopped_reason == "attempt_budget_exhausted":
                break
        if stopped_reason == "attempt_budget_exhausted":
            break
        if not cell_reached(cell):
            stopped_reason = f"selection_cell_{cell_id}_target_not_reached"
            break

    if all_cells_reached():
        stopped_reason = "selection_targets_reached"

    summary = _collection_summary(
        config=config,
        dirs=dirs,
        attempted=attempted,
        accepted=accepted,
        skipped_completed=skipped_completed,
        existing_accepted=existing_accepted,
        target_accepted=target_accepted_count,
        attempt_budget=attempt_budget,
        plan_catalog_count=len(plans),
        resume=resume,
        stopped_reason=stopped_reason,
        selection_manifest_path=selection_manifest_path,
        selection_cells=cells,
        existing_selection_cell_counts=existing_cell_counts,
        accepted_by_selection_cell=accepted_by_cell,
        attempted_by_selection_cell=attempted_by_cell,
        missing_manifest_plan_ids=missing_manifest_plan_ids,
    )
    _write_collection_summary(dirs["root"], summary)
    return summary


def collect_attempt(
    config: dict,
    plan: TrafficPlan,
    attempt_dir: Path,
    plan_index: int,
    retry_index: int,
) -> dict:
    save_json(attempt_dir / "plan.json", plan.to_dict())
    save_json(attempt_dir / "carla_plan.json", plan.to_carla_plan())
    server = check_carla_server(config)
    if not server["available"]:
        meta = {
            "schema": "carla_attempt_meta_v1",
            "plan_id": plan.plan_id,
            "target_tx_id": plan.target_tx_id,
            "plan_bucket": _plan_bucket_meta(plan),
            "attempt_dir": str(attempt_dir),
            "retry_index": int(retry_index),
            "command": [],
            "returncode": None,
            "elapsed_s": 0.0,
            "status": "CARLA_FAILED",
            "failure_code": "carla_server_unavailable",
            "server_check": server,
        }
        save_json(attempt_dir / "attempt_meta.json", meta)
        return meta
    cmd = build_collect_command(config, plan, attempt_dir, seed_offset=plan_index * 100 + retry_index)
    started = time.time()
    with (attempt_dir / "carla_stdout.log").open("w", encoding="utf-8") as stdout_f, (
        attempt_dir / "carla_stderr.log"
    ).open("w", encoding="utf-8") as stderr_f:
        proc = subprocess.run(
            [str(item) for item in cmd],
            cwd=str(repo_root()),
            stdout=stdout_f,
            stderr=stderr_f,
            env=_with_package_path(),
        )
    meta = {
        "schema": "carla_attempt_meta_v1",
        "plan_id": plan.plan_id,
        "target_tx_id": plan.target_tx_id,
        "plan_bucket": _plan_bucket_meta(plan),
        "attempt_dir": str(attempt_dir),
        "retry_index": int(retry_index),
        "command": [str(item) for item in cmd],
        "returncode": int(proc.returncode),
        "elapsed_s": float(time.time() - started),
        "status": "CARLA_COLLECTED" if proc.returncode == 0 else "CARLA_FAILED",
    }
    if proc.returncode != 0:
        carla_failure_code = _classify_carla_failure(attempt_dir, config)
        meta["failure_code"] = carla_failure_code
        meta["server_check_after_failure"] = check_carla_server(config)
        if _has_complete_clip_outputs(attempt_dir, config):
            report = evaluate_attempt_trajectory(attempt_dir, config)
            status = "TRAJECTORY_ACCEPTED" if report["trajectory_qc_pass"] else "TRAJECTORY_REJECTED"
            meta["status"] = status
            meta["post_clip_carla_failure_code"] = carla_failure_code
            meta["post_clip_carla_failure_note"] = (
                "CARLA subprocess failed after complete trajectory outputs were written; "
                "trajectory QA was evaluated from saved actor states."
            )
            meta["failure_code"] = report.get("failure_code")
        save_json(attempt_dir / "attempt_meta.json", meta)
        return meta
    report = evaluate_attempt_trajectory(attempt_dir, config)
    status = "TRAJECTORY_ACCEPTED" if report["trajectory_qc_pass"] else "TRAJECTORY_REJECTED"
    meta["status"] = status
    meta["failure_code"] = report.get("failure_code")
    save_json(attempt_dir / "attempt_meta.json", meta)
    return meta


def build_collect_command(config: dict, plan: TrafficPlan, attempt_dir: Path, seed_offset: int) -> list[object]:
    carla_cfg = config.get("carla", {})
    traffic = config["traffic"]
    scene = config["scene"]
    frames = int(round(float(traffic["duration_s"]) * float(traffic["fps"])))
    route_ids = ",".join(vehicle.route_id for vehicle in plan.vehicles)
    background_tm = plan.background_tm if isinstance(plan.background_tm, dict) else {}
    background_types = {
        str(item)
        for item in background_tm.get("vehicle_types", [])
        if str(item).strip()
    }
    allowlist = ",".join(sorted({vehicle.vehicle_type for vehicle in plan.vehicles} | background_types))
    requested_background_count = int(background_tm.get("requested_count", 0))
    cmd: list[object] = [
        carla_cfg.get("python", sys.executable),
        "-m",
        "dynamic_radio_dataset.carla.collect",
        "--host",
        carla_cfg.get("host", "localhost"),
        "--port",
        int(carla_cfg.get("port", 2000)),
        "--town",
        scene.get("town", "Town10HD_Opt"),
        "--scene-mode",
        scene.get("scene_mode", "junction"),
        "--seed",
        int(config.get("dataset", {}).get("seed", 7)) + int(seed_offset),
        "--frames",
        frames,
        "--fps",
        float(traffic["fps"]),
        "--traffic-preroll-s",
        float(traffic.get("traffic_preroll_s", 2.0)),
        "--valid-size",
        float(scene.get("valid_size", 96.0)),
        "--support-size",
        float(scene.get("support_size", 192.0)),
        "--route-step",
        float(carla_cfg.get("route_step", 2.0)),
        "--route-approach",
        float(carla_cfg.get("route_approach", 25.0)),
        "--route-exit",
        float(carla_cfg.get("route_exit", 25.0)),
        "--min-approach",
        float(carla_cfg.get("min_approach", 18.0)),
        "--min-exit",
        float(carla_cfg.get("min_exit", 18.0)),
        "--junction-core-radius",
        float(scene.get("junction_core_radius", 12.0)),
        "--num-target-vehicles",
        len(plan.vehicles),
        "--num-background-vehicles",
        requested_background_count,
        "--background-speed-diff",
        float(background_tm.get("speed_diff", carla_cfg.get("background_speed_diff", 0.0))),
        "--min-passed-targets",
        0,
        "--vehicle-size-preset",
        carla_cfg.get("vehicle_size_preset", "mixed"),
        "--vehicle-allowlist",
        allowlist,
        "--target-route-ids",
        route_ids,
        "--traffic-plan-json",
        attempt_dir / "carla_plan.json",
        "--output-dir",
        attempt_dir,
        "--keep-existing",
        "--no-video",
    ]
    if bool(carla_cfg.get("clear_existing_vehicles", True)):
        cmd.append("--clear-existing-vehicles")
    if bool(carla_cfg.get("clear_existing_sensors", True)):
        cmd.append("--clear-existing-sensors")
    return cmd


def check_carla_server(config: dict) -> dict:
    carla_cfg = config.get("carla", {})
    host = str(carla_cfg.get("host", "localhost"))
    port = int(carla_cfg.get("port", 2000))
    timeout_s = float(carla_cfg.get("server_check_timeout_s", 1.0))
    started = time.time()
    try:
        with socket.create_connection((host, port), timeout=timeout_s):
            pass
    except OSError as exc:
        return {
            "available": False,
            "host": host,
            "port": port,
            "timeout_s": timeout_s,
            "elapsed_s": float(time.time() - started),
            "stage": "socket",
            "error": str(exc),
        }
    rpc_timeout_s = float(carla_cfg.get("server_rpc_timeout_s", max(2.0, timeout_s)))
    try:
        carla = _import_carla_api()
        client = carla.Client(host, port)
        client.set_timeout(rpc_timeout_s)
        world = client.get_world()
        world_name = ""
        try:
            world_name = world.get_map().name
        except Exception:  # noqa: BLE001
            world_name = ""
        return {
            "available": True,
            "host": host,
            "port": port,
            "timeout_s": timeout_s,
            "rpc_timeout_s": rpc_timeout_s,
            "elapsed_s": float(time.time() - started),
            "stage": "rpc",
            "world": world_name,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "available": False,
            "host": host,
            "port": port,
            "timeout_s": timeout_s,
            "rpc_timeout_s": rpc_timeout_s,
            "elapsed_s": float(time.time() - started),
            "stage": "rpc",
            "error": str(exc),
        }


def _materialize_episode(attempt_dir: Path, episode_dir: Path, episode_id: str) -> None:
    shutil.copytree(attempt_dir, episode_dir, dirs_exist_ok=True)
    meta_path = episode_dir / "attempt_meta.json"
    meta = {}
    if meta_path.exists():
        from dynamic_radio_dataset.json_utils import load_json

        meta = load_json(meta_path)
    meta["episode_id"] = episode_id
    meta["status"] = "TRAJECTORY_ACCEPTED"
    save_json(meta_path, meta)


def _archive_failed_attempt(config: dict, attempt_dir: Path, result: dict) -> None:
    failed_dir = dataset_root(config) / "failed_attempts" / attempt_dir.name
    failed_dir.mkdir(parents=True, exist_ok=True)
    for name in (
        "plan.json",
        "carla_plan.json",
        "attempt_meta.json",
        "trajectory_qa.json",
        "collection_stage.json",
        "scene_meta.json",
        "validation_report.json",
    ):
        src = attempt_dir / name
        if src.exists():
            shutil.copy2(src, failed_dir / name)
    logs_dir = failed_dir / "logs"
    logs_dir.mkdir(exist_ok=True)
    for name in ("carla_stdout.log", "carla_stderr.log"):
        src = attempt_dir / name
        if src.exists():
            shutil.copy2(src, logs_dir / name)
    save_json(
        failed_dir / "failure.json",
        {
            "schema": "failed_attempt_v1",
            "attempt_id": attempt_dir.name,
            "status": result.get("status"),
            "failure_code": result.get("failure_code"),
            "post_clip_carla_failure_code": result.get("post_clip_carla_failure_code"),
            "collection_stage": _load_optional_json(attempt_dir / "collection_stage.json"),
        },
    )


def _classify_carla_failure(attempt_dir: Path, config: dict) -> str:
    stderr_path = attempt_dir / "carla_stderr.log"
    stdout_path = attempt_dir / "carla_stdout.log"
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace") if stderr_path.exists() else ""
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace") if stdout_path.exists() else ""
    text = f"{stdout}\n{stderr}".lower()
    stage = _load_optional_json(attempt_dir / "collection_stage.json")
    if "traffic_plan_spawn_incomplete" in text or (
        isinstance(stage, dict)
        and stage.get("stage") == "spawn_actors_failed"
        and stage.get("details", {}).get("failure_code") == "traffic_plan_spawn_incomplete"
    ):
        return "traffic_plan_spawn_incomplete"
    if "time-out" in text:
        server = check_carla_server(config)
        if not server["available"]:
            if server.get("stage") == "rpc":
                return "carla_rpc_unavailable_after_attempt"
            return "carla_server_unavailable_after_attempt"
        if "load_world" in text:
            return "carla_load_world_timeout"
        return "carla_rpc_timeout"
    if "segmentation fault" in text or "signal 11" in text:
        return "carla_server_segfault"
    return "carla_subprocess_failed"


def _has_complete_clip_outputs(attempt_dir: Path, config: dict) -> bool:
    required = [
        attempt_dir / "scene_meta.json",
        attempt_dir / "routes.json",
        attempt_dir / "plan.json",
        attempt_dir / "frames" / "actor_states.jsonl",
    ]
    if any(not path.exists() for path in required):
        return False
    frames_path = attempt_dir / "frames" / "actor_states.jsonl"
    expected_frames = int(round(float(config["traffic"]["duration_s"]) * float(config["traffic"]["fps"])))
    try:
        frame_count = sum(1 for line in frames_path.read_text(encoding="utf-8").splitlines() if line.strip())
    except OSError:
        return False
    return frame_count >= expected_frames


def _load_optional_json(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        value = load_json(path)
        return value if isinstance(value, dict) else {"value": value}
    except Exception:  # noqa: BLE001
        return None


def _import_carla_api():
    root = repo_root()
    egg_glob = str(root / "PythonAPI" / "carla" / "dist" / "carla-*-py3.*-linux-x86_64.egg")
    eggs = sorted(glob(egg_glob))
    if eggs:
        sys.path.insert(0, eggs[-1])
    sys.path.insert(0, str(root / "PythonAPI" / "carla"))
    import carla  # type: ignore  # noqa: PLC0415

    return carla


def _collection_bucket_targets(config: dict, override: Mapping[int | str, int] | None = None) -> dict[int, int]:
    raw = override
    if raw is None:
        collection = config.get("collection", {})
        raw = collection.get("bucket_targets") if isinstance(collection, dict) else None
    if not isinstance(raw, Mapping):
        return {}
    result = {int(bucket): int(target) for bucket, target in raw.items() if int(target) > 0}
    if not result:
        return {}
    return dict(sorted(result.items()))


def _selection_manifest_path(config: dict, override: Path | None = None) -> Path | None:
    raw: Path | str | None = override
    if raw is None:
        collection = config.get("collection", {})
        raw = collection.get("selection_manifest") if isinstance(collection, dict) else None
    if raw is None or not str(raw).strip():
        return None
    path = Path(str(raw))
    if path.is_absolute():
        return path
    root = dataset_root(config)
    if len(path.parts) == 1:
        return root / "plan_catalog" / path
    return root / path


def _selection_cells(selection_manifest: dict[str, Any]) -> list[dict[str, Any]]:
    raw_cells = selection_manifest.get("cells", [])
    if not isinstance(raw_cells, list):
        raise ValueError("Selection manifest is invalid: cells must be a list.")
    cells: list[dict[str, Any]] = []
    for order, raw in enumerate(raw_cells):
        if not isinstance(raw, Mapping):
            raise ValueError(f"Selection manifest has invalid cell: {raw!r}")
        vehicle_count = int(raw["vehicle_count"])
        target_large = int(raw["target_large_vehicle_count"])
        cell = {
            "cell_id": str(raw.get("cell_id") or _selection_cell_id(vehicle_count, target_large)),
            "order": int(raw.get("order", order)),
            "vehicle_count": int(vehicle_count),
            "target_large_vehicle_count": int(target_large),
            "target_accepted": int(raw["target_accepted"]),
            "candidate_plan_ids": [str(plan_id) for plan_id in raw.get("candidate_plan_ids", [])],
        }
        if cell["target_accepted"] <= 0:
            continue
        cells.append(cell)
    if not cells:
        raise ValueError("Selection manifest has no positive target cells.")
    return sorted(cells, key=lambda cell: int(cell["order"]))


def _selection_target_total(selection_manifest: dict[str, Any]) -> int:
    return int(sum(int(cell["target_accepted"]) for cell in _selection_cells(selection_manifest)))


def _bucket_order(config: dict, bucket_targets: Mapping[int, int]) -> list[int]:
    configured = config.get("collection", {}).get("bucket_order", []) if isinstance(config.get("collection"), dict) else []
    order = [int(item) for item in configured if int(item) in bucket_targets]
    for bucket in sorted(int(item) for item in bucket_targets):
        if bucket not in order:
            order.append(bucket)
    return order


def _bucket_plan_entries(
    config: dict,
    plans: Sequence[TrafficPlan],
    order: Sequence[int],
) -> dict[int, list[tuple[int, TrafficPlan]]]:
    groups: dict[int, list[tuple[int, TrafficPlan]]] = {int(bucket): [] for bucket in order}
    for index, plan in enumerate(plans):
        bucket = _plan_vehicle_count_bucket(plan)
        if bucket in groups:
            groups[bucket].append((index, plan))
    seed = int(config.get("collection", {}).get("bucket_shuffle_seed", config.get("plans", {}).get("seed", 17)))
    rng = random.Random(seed)
    for bucket in order:
        rng.shuffle(groups[int(bucket)])
    return groups


def _plan_vehicle_count_bucket(plan: TrafficPlan | dict[str, Any]) -> int:
    if isinstance(plan, TrafficPlan):
        bucket = plan.bucket if isinstance(plan.bucket, dict) else {}
        return int(bucket.get("vehicle_count", plan.vehicle_count))
    bucket = plan.get("bucket", {}) if isinstance(plan.get("bucket"), dict) else {}
    return int(bucket.get("vehicle_count", plan.get("vehicle_count", 0)))


def _plan_target_large_vehicle_count(plan: TrafficPlan | dict[str, Any]) -> int:
    if isinstance(plan, TrafficPlan):
        bucket = plan.bucket if isinstance(plan.bucket, dict) else {}
        return int(
            bucket.get(
                "target_large_vehicle_count",
                plan.expected_metrics.get("target_large_vehicle_count", _planned_large_vehicle_count(plan.to_dict())),
            )
        )
    bucket = plan.get("bucket", {}) if isinstance(plan.get("bucket"), dict) else {}
    metrics = plan.get("expected_metrics", {}) if isinstance(plan.get("expected_metrics"), dict) else {}
    return int(bucket.get("target_large_vehicle_count", metrics.get("target_large_vehicle_count", _planned_large_vehicle_count(plan))))


def _selection_cell_id_from_plan(plan: TrafficPlan | dict[str, Any]) -> str:
    return _selection_cell_id(_plan_vehicle_count_bucket(plan), _plan_target_large_vehicle_count(plan))


def _selection_cell_id(vehicle_count: int, target_large_vehicle_count: int) -> str:
    return f"vehicles_{int(vehicle_count)}_large_{int(target_large_vehicle_count)}"


def _planned_large_vehicle_count(plan: dict[str, Any]) -> int:
    vehicles = plan.get("vehicles", []) if isinstance(plan.get("vehicles"), list) else []
    background = plan.get("background_tm", {}) if isinstance(plan.get("background_tm"), dict) else {}
    controlled = sum("fusorosa" in str(vehicle.get("vehicle_type", "")).lower() for vehicle in vehicles)
    background_count = sum("fusorosa" in str(item).lower() for item in background.get("vehicle_types", []))
    return int(controlled + background_count)


def _plan_bucket_meta(plan: TrafficPlan) -> dict[str, Any]:
    bucket = dict(plan.bucket) if isinstance(plan.bucket, dict) else {}
    if not bucket:
        bucket = {
            "vehicle_count": int(plan.vehicle_count),
            "target_large_vehicle_count": int(plan.expected_metrics.get("target_large_vehicle_count", 0)),
            "bucket_key": f"vehicle_count_{int(plan.vehicle_count)}",
        }
    return bucket


def _completed_plan_ids(episodes_dir: Path) -> set[str]:
    completed: set[str] = set()
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        plan_path = episode_dir / "plan.json"
        if not qa_path.exists() or not plan_path.exists():
            continue
        try:
            qa = load_json(qa_path)
            plan = load_json(plan_path)
        except Exception:  # noqa: BLE001
            continue
        if bool(qa.get("trajectory_qc_pass")):
            completed.add(str(plan.get("plan_id")))
    return completed


def _accepted_bucket_counts(episodes_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        plan_path = episode_dir / "plan.json"
        if not qa_path.exists() or not plan_path.exists():
            continue
        try:
            qa = load_json(qa_path)
            plan = load_json(plan_path)
        except Exception:  # noqa: BLE001
            continue
        if bool(qa.get("trajectory_qc_pass")):
            counts[str(_plan_vehicle_count_bucket(plan))] += 1
    return counts


def _accepted_selection_cell_counts(episodes_dir: Path) -> Counter[str]:
    counts: Counter[str] = Counter()
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        plan_path = episode_dir / "plan.json"
        if not qa_path.exists() or not plan_path.exists():
            continue
        try:
            qa = load_json(qa_path)
            plan = load_json(plan_path)
        except Exception:  # noqa: BLE001
            continue
        if bool(qa.get("trajectory_qc_pass")):
            counts[_selection_cell_id_from_plan(plan)] += 1
    return counts


def _trajectory_accepted_episode_count(episodes_dir: Path) -> int:
    count = 0
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        if not qa_path.exists():
            continue
        try:
            qa = load_json(qa_path)
        except Exception:  # noqa: BLE001
            continue
        if bool(qa.get("trajectory_qc_pass")):
            count += 1
    return count


def _collection_summary(
    *,
    config: dict,
    dirs: dict[str, Path],
    attempted: int,
    accepted: int,
    skipped_completed: int,
    existing_accepted: int,
    target_accepted: int | None,
    attempt_budget: int,
    plan_catalog_count: int,
    resume: bool,
    stopped_reason: str,
    server_check: dict | None = None,
    bucket_targets: Mapping[int, int] | None = None,
    existing_bucket_counts: Counter[str] | None = None,
    accepted_by_bucket: Counter[str] | None = None,
    attempted_by_bucket: Counter[str] | None = None,
    selection_manifest_path: Path | None = None,
    selection_cells: list[dict[str, Any]] | None = None,
    existing_selection_cell_counts: Counter[str] | None = None,
    accepted_by_selection_cell: Counter[str] | None = None,
    attempted_by_selection_cell: Counter[str] | None = None,
    missing_manifest_plan_ids: list[str] | None = None,
) -> dict:
    total_accepted = _trajectory_accepted_episode_count(dirs["episodes"])
    status_histogram, failure_histogram = _attempt_histograms(dirs["attempts"])
    bucket_status_histogram, bucket_failure_histogram, bucket_elapsed_s = _attempt_bucket_histograms(dirs["attempts"])
    selection_status_histogram, selection_failure_histogram, selection_elapsed_s = _attempt_selection_cell_histograms(dirs["attempts"])
    summary = {
        "schema": "plan_catalog_collection_summary_v2",
        "attempted": int(attempted),
        "trajectory_accepted": int(accepted),
        "new_trajectory_accepted": int(accepted),
        "initial_trajectory_accepted": int(existing_accepted),
        "total_trajectory_accepted": int(total_accepted),
        "target_accepted": target_accepted,
        "target_reached": bool(target_accepted is not None and total_accepted >= target_accepted),
        "skipped_completed_plans": int(skipped_completed),
        "plan_catalog_count": int(plan_catalog_count),
        "attempt_budget": int(attempt_budget),
        "resume": bool(resume),
        "stopped_reason": stopped_reason,
        "status_histogram": dict(status_histogram),
        "failure_code_histogram": dict(failure_histogram),
        "bucket_attempt_status_histogram": bucket_status_histogram,
        "bucket_attempt_failure_code_histogram": bucket_failure_histogram,
        "bucket_attempt_elapsed_s": bucket_elapsed_s,
        "selection_cell_attempt_status_histogram": selection_status_histogram,
        "selection_cell_attempt_failure_code_histogram": selection_failure_histogram,
        "selection_cell_attempt_elapsed_s": selection_elapsed_s,
    }
    if bucket_targets:
        existing_counts = existing_bucket_counts or Counter()
        new_counts = accepted_by_bucket or Counter()
        total_counts = Counter(existing_counts)
        total_counts.update(new_counts)
        ordered_buckets = _bucket_order(config, bucket_targets)
        summary["bucket_mode"] = "vehicle_count_ordered"
        summary["bucket_targets"] = {str(key): int(value) for key, value in bucket_targets.items()}
        summary["bucket_order"] = ordered_buckets
        summary["initial_accepted_by_bucket"] = {str(key): int(existing_counts.get(str(key), 0)) for key in bucket_targets}
        summary["new_accepted_by_bucket"] = {str(key): int(new_counts.get(str(key), 0)) for key in bucket_targets}
        summary["total_accepted_by_bucket"] = {str(key): int(total_counts.get(str(key), 0)) for key in bucket_targets}
        summary["attempted_by_bucket"] = {
            str(key): int((attempted_by_bucket or Counter()).get(str(key), 0)) for key in bucket_targets
        }
        summary["bucket_targets_reached"] = all(
            int(total_counts.get(str(key), 0)) >= int(target) for key, target in bucket_targets.items()
        )
    if selection_cells:
        existing_counts = existing_selection_cell_counts or Counter()
        new_counts = accepted_by_selection_cell or Counter()
        total_counts = Counter(existing_counts)
        total_counts.update(new_counts)
        cells = sorted(selection_cells, key=lambda cell: int(cell.get("order", 0)))
        summary["selection_mode"] = "vehicle_count_target_large_matrix"
        summary["selection_manifest"] = str(selection_manifest_path) if selection_manifest_path is not None else None
        summary["selection_targets"] = {str(cell["cell_id"]): int(cell["target_accepted"]) for cell in cells}
        summary["selection_matrix"] = [
            {
                "cell_id": str(cell["cell_id"]),
                "order": int(cell.get("order", 0)),
                "vehicle_count": int(cell["vehicle_count"]),
                "target_large_vehicle_count": int(cell["target_large_vehicle_count"]),
                "target_accepted": int(cell["target_accepted"]),
                "candidate_plan_count": int(len(cell.get("candidate_plan_ids", []))),
            }
            for cell in cells
        ]
        summary["initial_accepted_by_selection_cell"] = {
            str(cell["cell_id"]): int(existing_counts.get(str(cell["cell_id"]), 0)) for cell in cells
        }
        summary["new_accepted_by_selection_cell"] = {
            str(cell["cell_id"]): int(new_counts.get(str(cell["cell_id"]), 0)) for cell in cells
        }
        summary["total_accepted_by_selection_cell"] = {
            str(cell["cell_id"]): int(total_counts.get(str(cell["cell_id"]), 0)) for cell in cells
        }
        summary["attempted_by_selection_cell"] = {
            str(cell["cell_id"]): int((attempted_by_selection_cell or Counter()).get(str(cell["cell_id"]), 0))
            for cell in cells
        }
        summary["missing_manifest_plan_ids"] = list(missing_manifest_plan_ids or [])
        summary["selection_targets_reached"] = all(
            int(total_counts.get(str(cell["cell_id"]), 0)) >= int(cell["target_accepted"]) for cell in cells
        )
        summary["accepted_selection_validation"] = _accepted_selection_validation(dirs["episodes"], cells)
    if server_check is not None:
        summary["server_check"] = server_check
    if selection_cells and not bool(summary.get("selection_targets_reached")):
        summary["blocker"] = {
            "failure_code": "selection_targets_not_reached",
            "selection_targets": summary.get("selection_targets", {}),
            "total_accepted_by_selection_cell": summary.get("total_accepted_by_selection_cell", {}),
            "stopped_reason": stopped_reason,
        }
    elif bucket_targets and not bool(summary.get("bucket_targets_reached")):
        summary["blocker"] = {
            "failure_code": "bucket_targets_not_reached",
            "bucket_targets": summary.get("bucket_targets", {}),
            "total_accepted_by_bucket": summary.get("total_accepted_by_bucket", {}),
            "stopped_reason": stopped_reason,
        }
    elif target_accepted is not None and total_accepted < int(target_accepted):
        summary["blocker"] = {
            "failure_code": "target_accepted_not_reached",
            "needed_trajectory_accepted": int(target_accepted),
            "actual_trajectory_accepted": int(total_accepted),
            "stopped_reason": stopped_reason,
        }
    return summary


def _attempt_histograms(attempts_dir: Path) -> tuple[Counter[str], Counter[str]]:
    status_histogram: Counter[str] = Counter()
    failure_histogram: Counter[str] = Counter()
    for meta_path in sorted(attempts_dir.glob("attempt_*/attempt_meta.json")):
        try:
            meta = load_json(meta_path)
        except Exception:  # noqa: BLE001
            continue
        status = str(meta.get("status") or "unknown")
        status_histogram[status] += 1
        failure_code = meta.get("failure_code")
        if status != "TRAJECTORY_ACCEPTED" and failure_code:
            failure_histogram[str(failure_code)] += 1
    return status_histogram, failure_histogram


def _accepted_selection_validation(episodes_dir: Path, selection_cells: list[dict[str, Any]]) -> dict[str, Any]:
    targets = {str(cell["cell_id"]): int(cell["target_accepted"]) for cell in selection_cells}
    matrix_cells = set(targets)
    rows: list[dict[str, Any]] = []
    cell_counts: Counter[str] = Counter()
    vehicle_counts: Counter[str] = Counter()
    target_large_counts: Counter[str] = Counter()
    actual_large_counts: Counter[str] = Counter()
    background_shortfalls: list[dict[str, Any]] = []
    actual_large_mismatches: list[dict[str, Any]] = []
    actual_vehicle_count_mismatches: list[dict[str, Any]] = []
    out_of_matrix: list[dict[str, Any]] = []
    invalid_three_large_below_six: list[dict[str, Any]] = []
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "trajectory_qa.json"
        plan_path = episode_dir / "plan.json"
        if not qa_path.exists() or not plan_path.exists():
            continue
        try:
            qa = load_json(qa_path)
            plan = load_json(plan_path)
        except Exception:  # noqa: BLE001
            continue
        if not bool(qa.get("trajectory_qc_pass")):
            continue
        vehicle_count = _plan_vehicle_count_bucket(plan)
        target_large = _plan_target_large_vehicle_count(plan)
        cell_id = _selection_cell_id(vehicle_count, target_large)
        role_counts = qa.get("vehicle_role_counts", {}) if isinstance(qa.get("vehicle_role_counts"), dict) else {}
        vehicle_mix = qa.get("vehicle_mix", {}) if isinstance(qa.get("vehicle_mix"), dict) else {}
        requested_background = int(role_counts.get("requested_background_count", _requested_background_count_from_plan(plan)))
        actual_background = int(role_counts.get("actual_background_count", 0))
        actual_total = int(role_counts.get("actual_total_vehicle_count", 0))
        actual_large = int(vehicle_mix.get("large_vehicle_count", 0))
        planned_large = _planned_large_vehicle_count(plan)
        row = {
            "episode_id": episode_dir.name,
            "plan_id": str(plan.get("plan_id", "")),
            "cell_id": cell_id,
            "vehicle_count": int(vehicle_count),
            "target_large_vehicle_count": int(target_large),
            "planned_large_vehicle_count": int(planned_large),
            "actual_total_vehicle_count": int(actual_total),
            "actual_large_vehicle_count": int(actual_large),
            "requested_background_count": int(requested_background),
            "actual_background_count": int(actual_background),
            "background_shortfall": int(max(0, requested_background - actual_background)),
            "in_selection_matrix": bool(cell_id in matrix_cells),
        }
        rows.append(row)
        cell_counts[cell_id] += 1
        vehicle_counts[str(vehicle_count)] += 1
        target_large_counts[str(target_large)] += 1
        actual_large_counts[str(actual_large)] += 1
        if cell_id not in matrix_cells:
            out_of_matrix.append(row)
        if target_large >= 3 and vehicle_count < 6:
            invalid_three_large_below_six.append(row)
        if requested_background > actual_background:
            background_shortfalls.append(row)
        if actual_large != target_large:
            actual_large_mismatches.append(row)
        if actual_total and actual_total != vehicle_count:
            actual_vehicle_count_mismatches.append(row)
    return {
        "schema": "validation_selection_accepted_outcome_v1",
        "accepted_episode_count": int(len(rows)),
        "target_by_cell": targets,
        "accepted_by_cell": {key: int(cell_counts.get(key, 0)) for key in sorted(targets)},
        "vehicle_count_histogram": {key: int(vehicle_counts[key]) for key in sorted(vehicle_counts)},
        "target_large_vehicle_count_histogram": {key: int(target_large_counts[key]) for key in sorted(target_large_counts)},
        "actual_large_vehicle_count_histogram": {key: int(actual_large_counts[key]) for key in sorted(actual_large_counts)},
        "matrix_targets_reached": all(int(cell_counts.get(key, 0)) >= int(value) for key, value in targets.items()),
        "out_of_matrix_episode_count": int(len(out_of_matrix)),
        "invalid_three_large_below_six_episode_count": int(len(invalid_three_large_below_six)),
        "background_shortfall_episode_count": int(len(background_shortfalls)),
        "actual_large_mismatch_episode_count": int(len(actual_large_mismatches)),
        "actual_vehicle_count_mismatch_episode_count": int(len(actual_vehicle_count_mismatches)),
        "background_shortfall_episodes": background_shortfalls,
        "actual_large_mismatch_episodes": actual_large_mismatches,
        "actual_vehicle_count_mismatch_episodes": actual_vehicle_count_mismatches,
        "out_of_matrix_episodes": out_of_matrix,
        "invalid_three_large_below_six_episodes": invalid_three_large_below_six,
        "episodes": rows,
    }


def _requested_background_count_from_plan(plan: dict[str, Any]) -> int:
    background = plan.get("background_tm", {}) if isinstance(plan.get("background_tm"), dict) else {}
    metrics = plan.get("expected_metrics", {}) if isinstance(plan.get("expected_metrics"), dict) else {}
    return int(background.get("requested_count", metrics.get("requested_background_count", 0)))


def _attempt_bucket_histograms(attempts_dir: Path) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], dict[str, float]]:
    status: dict[str, Counter[str]] = {}
    failures: dict[str, Counter[str]] = {}
    elapsed: Counter[str] = Counter()
    for meta_path in sorted(attempts_dir.glob("attempt_*/attempt_meta.json")):
        try:
            meta = load_json(meta_path)
        except Exception:  # noqa: BLE001
            continue
        bucket_meta = meta.get("plan_bucket", {}) if isinstance(meta.get("plan_bucket"), dict) else {}
        bucket = str(bucket_meta.get("vehicle_count", "unknown"))
        status.setdefault(bucket, Counter())[str(meta.get("status") or "unknown")] += 1
        elapsed[bucket] += float(meta.get("elapsed_s", 0.0))
        failure_code = meta.get("failure_code")
        if str(meta.get("status") or "") != "TRAJECTORY_ACCEPTED" and failure_code:
            failures.setdefault(bucket, Counter())[str(failure_code)] += 1
    return (
        {bucket: dict(counter) for bucket, counter in sorted(status.items())},
        {bucket: dict(counter) for bucket, counter in sorted(failures.items())},
        {bucket: float(value) for bucket, value in sorted(elapsed.items())},
    )


def _attempt_selection_cell_histograms(attempts_dir: Path) -> tuple[dict[str, dict[str, int]], dict[str, dict[str, int]], dict[str, float]]:
    status: dict[str, Counter[str]] = {}
    failures: dict[str, Counter[str]] = {}
    elapsed: Counter[str] = Counter()
    for meta_path in sorted(attempts_dir.glob("attempt_*/attempt_meta.json")):
        try:
            meta = load_json(meta_path)
        except Exception:  # noqa: BLE001
            continue
        bucket_meta = meta.get("plan_bucket", {}) if isinstance(meta.get("plan_bucket"), dict) else {}
        cell = _selection_cell_id(
            int(bucket_meta.get("vehicle_count", 0)),
            int(bucket_meta.get("target_large_vehicle_count", 0)),
        )
        status.setdefault(cell, Counter())[str(meta.get("status") or "unknown")] += 1
        elapsed[cell] += float(meta.get("elapsed_s", 0.0))
        failure_code = meta.get("failure_code")
        if str(meta.get("status") or "") != "TRAJECTORY_ACCEPTED" and failure_code:
            failures.setdefault(cell, Counter())[str(failure_code)] += 1
    return (
        {cell: dict(counter) for cell, counter in sorted(status.items())},
        {cell: dict(counter) for cell, counter in sorted(failures.items())},
        {cell: float(value) for cell, value in sorted(elapsed.items())},
    )


def _write_collection_summary(root: Path, summary: dict) -> None:
    save_json(root / "collection_summary.json", summary)


def _with_package_path() -> dict[str, str]:
    env = os.environ.copy()
    scripts_dir = str(repo_root() / "scripts")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = scripts_dir if not existing else f"{scripts_dir}:{existing}"
    return env


def _next_id(parent: Path, prefix: str) -> str:
    existing = sorted(path.name for path in parent.glob(f"{prefix}_*"))
    if not existing:
        return f"{prefix}_000000"
    numbers = []
    for name in existing:
        try:
            numbers.append(int(name.rsplit("_", 1)[1]))
        except ValueError:
            continue
    return f"{prefix}_{(max(numbers) + 1 if numbers else 0):06d}"
