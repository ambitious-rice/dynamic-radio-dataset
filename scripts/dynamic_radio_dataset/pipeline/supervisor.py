from __future__ import annotations

import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.carla.runner import check_carla_server, collect_from_plan_catalog
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import ensure_dataset_dirs, repo_root
from dynamic_radio_dataset.pipeline.reports import write_timing_report
from dynamic_radio_dataset.pipeline.stages import finalize, prepare_scene, process_rf
from dynamic_radio_dataset.plans.sampler import generate_plan_bank
from dynamic_radio_dataset.routes.route_library import build_route_library


def run_supervised(
    config: dict,
    *,
    num_plans: int | None = None,
    max_attempts: int | None = None,
    target_accepted: int | None = None,
    max_episodes: int | None = None,
    resume: bool = True,
    render_sample: bool | None = None,
    render_sample_count: int | None = None,
    restart_every_accepted: int | None = None,
    selection_manifest: Path | None = None,
) -> dict:
    dirs = ensure_dataset_dirs(config)
    root = dirs["root"]
    started = time.time()
    supervisor_cfg = config.get("supervisor", {})
    target = int(
        target_accepted
        if target_accepted is not None
        else config.get("profile", {}).get(
            "target_accepted_episodes",
            _selection_matrix_target(config) or sum(int(value) for value in _bucket_targets(config).values()) or 0,
        )
    )
    if target <= 0:
        raise ValueError("run-supervised needs --target-accepted, profile.target_accepted_episodes, or collection.bucket_targets.")
    restart_every = int(
        restart_every_accepted
        if restart_every_accepted is not None
        else supervisor_cfg.get("restart_every_accepted", 0)
    )
    render_enabled = bool(
        config.get("visualization", {}).get("auto_render_sample", True)
        if render_sample is None
        else render_sample
    )
    sample_count = int(render_sample_count if render_sample_count is not None else config.get("visualization", {}).get("sample_count", 15))
    results: dict[str, Any] = {}
    carla_proc: subprocess.Popen | None = None
    carla_log_handles: list[Any] = []

    try:
        _write_status(root, "starting", started, target_accepted=target)
        if not (dirs["scene_static"] / "tx_catalog.json").exists() or not (dirs["reference_scene"] / "sionna_export").exists():
            _write_status(root, "prepare_scene", started)
            results["prepare_scene"] = prepare_scene(config)
        if not (dirs["route_library"] / "route_features.json").exists():
            _write_status(root, "build_route_library", started)
            results["route_library"] = build_route_library(config)
        if not (dirs["plan_catalog"] / "plans.jsonl").exists():
            _write_status(root, "generate_plans", started)
            results["plan_catalog"] = generate_plan_bank(config, num_plans=num_plans)
        resolved_selection_manifest = _ensure_selection_manifest(config, selection_manifest)
        if resolved_selection_manifest is not None:
            results["selection_manifest"] = str(resolved_selection_manifest)

        accepted = _accepted_trajectory_count(root)
        next_restart_target = accepted + restart_every if restart_every > 0 else None
        while accepted < target:
            _write_heartbeat(root, "collection", started, accepted_trajectories=accepted, target_accepted=target)
            carla_proc, carla_log_handles = _ensure_carla(config, carla_proc, carla_log_handles)
            batch_target = target
            if resolved_selection_manifest is None and next_restart_target is not None:
                batch_target = min(target, next_restart_target)
            batch_bucket_targets = None if resolved_selection_manifest is not None else _batch_bucket_targets(config, batch_target)
            collect_result = collect_from_plan_catalog(
                config,
                max_attempts=max_attempts,
                target_accepted=batch_target,
                resume=resume,
                bucket_targets=batch_bucket_targets,
                selection_manifest=resolved_selection_manifest,
            )
            results["collect"] = collect_result
            accepted = int(collect_result.get("total_trajectory_accepted", _accepted_trajectory_count(root)))
            _write_status(root, "collection_batch_completed", started, accepted_trajectories=accepted, collect=collect_result)
            if accepted >= target:
                break
            stopped_reason = str(collect_result.get("stopped_reason", ""))
            if stopped_reason == "carla_server_unavailable":
                carla_proc, carla_log_handles = _restart_owned_carla(config, carla_proc, carla_log_handles)
                continue
            if next_restart_target is not None and accepted >= next_restart_target:
                carla_proc, carla_log_handles = _restart_owned_carla(config, carla_proc, carla_log_handles)
                next_restart_target = accepted + restart_every
                continue
            _write_failure_summary(root, collect_result)
            break

        if accepted < target:
            raise RuntimeError(f"Supervised collection stopped before target: accepted={accepted} target={target}")

        if carla_proc is not None and not bool(supervisor_cfg.get("keep_carla_running", False)):
            _terminate_carla(carla_proc, carla_log_handles)
            carla_proc = None
            carla_log_handles = []

        _write_status(root, "process_rf", started)
        results["process_rf"] = process_rf(config, max_episodes=max_episodes)
        _write_status(root, "finalize", started)
        results["finalize"] = finalize(config)
        if render_enabled:
            from dynamic_radio_dataset.render.sample import render_green_absolute_sample

            _write_status(root, "render_sample", started, sample_count=sample_count)
            results["render_sample"] = render_green_absolute_sample(
                config,
                sample_count=sample_count,
                seed=config.get("visualization", {}).get("sample_seed"),
                allow_partial=bool(config.get("visualization", {}).get("allow_partial", False)),
            )
        _write_status(root, "timing_report", started)
        results["timing_report"] = write_timing_report(config)
        _write_status(root, "completed", started, results=results)
        return results
    except Exception as exc:
        _write_status(root, "failed", started, error=f"{type(exc).__name__}: {exc}")
        raise
    finally:
        if carla_proc is not None and not bool(supervisor_cfg.get("keep_carla_running", False)):
            _terminate_carla(carla_proc, carla_log_handles)


def _ensure_carla(
    config: dict,
    carla_proc: subprocess.Popen | None,
    log_handles: list[Any],
) -> tuple[subprocess.Popen | None, list[Any]]:
    server = check_carla_server(config)
    if server["available"]:
        return carla_proc, log_handles
    supervisor_cfg = config.get("supervisor", {})
    if not bool(supervisor_cfg.get("start_carla", True)):
        raise RuntimeError(f"CARLA server unavailable and supervisor.start_carla=false: {server}")
    carla_proc, log_handles = _start_carla(config, carla_proc, log_handles)
    timeout_s = float(supervisor_cfg.get("carla_startup_timeout_s", 90.0))
    poll_s = float(supervisor_cfg.get("carla_startup_poll_s", 2.0))
    deadline = time.time() + timeout_s
    last_check = server
    while time.time() < deadline:
        if carla_proc is not None and carla_proc.poll() is not None:
            raise RuntimeError(f"CARLA process exited during startup with code {carla_proc.returncode}")
        time.sleep(poll_s)
        last_check = check_carla_server(config)
        if last_check["available"]:
            return carla_proc, log_handles
    raise RuntimeError(f"CARLA did not become ready within {timeout_s:.1f}s: {last_check}")


def _start_carla(
    config: dict,
    carla_proc: subprocess.Popen | None,
    log_handles: list[Any],
) -> tuple[subprocess.Popen | None, list[Any]]:
    if carla_proc is not None and carla_proc.poll() is None:
        return carla_proc, log_handles
    _close_handles(log_handles)
    root = ensure_dataset_dirs(config)["root"]
    log_dir = root / "supervisor_logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    carla_cfg = config.get("carla", {})
    supervisor_cfg = config.get("supervisor", {})
    executable = Path(str(supervisor_cfg.get("carla_executable", repo_root() / "CarlaUE4.sh")))
    if not executable.is_absolute():
        executable = repo_root() / executable
    port = int(carla_cfg.get("port", 2000))
    cmd = [
        str(executable),
        "-RenderOffScreen",
        "-nosound",
        "-quality-level=Low",
        f"-carla-rpc-port={port}",
    ]
    extra_args = supervisor_cfg.get("carla_args", [])
    if isinstance(extra_args, list):
        cmd.extend(str(item) for item in extra_args)
    stdout_f = (log_dir / "carla_stdout.log").open("a", encoding="utf-8")
    stderr_f = (log_dir / "carla_stderr.log").open("a", encoding="utf-8")
    proc = subprocess.Popen(cmd, cwd=str(repo_root()), stdout=stdout_f, stderr=stderr_f)
    return proc, [stdout_f, stderr_f]


def _restart_owned_carla(
    config: dict,
    carla_proc: subprocess.Popen | None,
    log_handles: list[Any],
) -> tuple[subprocess.Popen | None, list[Any]]:
    if carla_proc is not None:
        _terminate_carla(carla_proc, log_handles)
        carla_proc = None
        log_handles = []
    return _ensure_carla(config, carla_proc, log_handles)


def _terminate_carla(proc: subprocess.Popen, log_handles: list[Any]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=20.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=20.0)
    _close_handles(log_handles)


def _close_handles(handles: list[Any]) -> None:
    for handle in handles:
        try:
            handle.close()
        except Exception:  # noqa: BLE001
            pass


def _accepted_trajectory_count(root: Path) -> int:
    count = 0
    for qa_path in sorted((root / "episodes").glob("*/trajectory_qa.json")):
        try:
            if bool(load_json(qa_path).get("trajectory_qc_pass")):
                count += 1
        except Exception:  # noqa: BLE001
            continue
    return count


def _bucket_targets(config: dict) -> Mapping[Any, Any]:
    collection = config.get("collection", {})
    if isinstance(collection, dict) and isinstance(collection.get("bucket_targets"), dict):
        return collection["bucket_targets"]
    return {}


def _selection_matrix_target(config: dict) -> int:
    collection = config.get("collection", {})
    raw = collection.get("selection_matrix") if isinstance(collection, dict) else None
    if isinstance(raw, list):
        return int(sum(int(row.get("target_accepted", row.get("accepted_episodes", row.get("quota", 0)))) for row in raw if isinstance(row, dict)))
    if isinstance(raw, dict):
        return int(sum(int(value) for value in raw.values()))
    return 0


def _bucket_order(config: dict, targets: Mapping[Any, Any]) -> list[int]:
    configured = config.get("collection", {}).get("bucket_order", []) if isinstance(config.get("collection"), dict) else []
    order = [int(item) for item in configured if int(item) in {int(key) for key in targets}]
    for bucket in sorted(int(key) for key in targets):
        if bucket not in order:
            order.append(bucket)
    return order


def _batch_bucket_targets(config: dict, batch_target_total: int) -> dict[int, int] | None:
    final_targets = {int(key): int(value) for key, value in _bucket_targets(config).items() if int(value) > 0}
    if not final_targets:
        return None
    remaining = max(0, min(int(batch_target_total), sum(final_targets.values())))
    result: dict[int, int] = {}
    for bucket in _bucket_order(config, final_targets):
        if remaining <= 0:
            break
        target = min(int(final_targets[bucket]), remaining)
        if target > 0:
            result[int(bucket)] = int(target)
        remaining -= target
    return result or None


def _ensure_selection_manifest(config: dict, override: Path | None) -> Path | None:
    from dynamic_radio_dataset.plans.validation_selector import (
        configured_selection_manifest_path,
        select_validation_plans,
    )

    if override is not None:
        path = override if override.is_absolute() else _dataset_relative_path(config, override)
    else:
        path = configured_selection_manifest_path(config)
    has_matrix = bool(config.get("collection", {}).get("selection_matrix")) if isinstance(config.get("collection"), dict) else False
    if path is None and not has_matrix:
        return None
    if path is not None and path.exists():
        return path
    result = select_validation_plans(config, output_path=path)
    return Path(str(result["selection_manifest"]))


def _dataset_relative_path(config: dict, path: Path) -> Path:
    root = ensure_dataset_dirs(config)["root"]
    if len(path.parts) == 1:
        return root / "plan_catalog" / path
    return root / path


def _write_status(root: Path, stage: str, started: float, **extra: Any) -> None:
    save_json(
        root / "supervisor_status.json",
        {
            "schema": "dynamic_radio_supervisor_status_v1",
            "stage": stage,
            "elapsed_s": float(time.time() - started),
            **extra,
        },
    )


def _write_heartbeat(root: Path, stage: str, started: float, **extra: Any) -> None:
    save_json(
        root / "heartbeat.json",
        {
            "schema": "dynamic_radio_supervisor_heartbeat_v1",
            "stage": stage,
            "elapsed_s": float(time.time() - started),
            **extra,
        },
    )


def _write_failure_summary(root: Path, collect_result: dict) -> None:
    save_json(
        root / "failure_summary.json",
        {
            "schema": "dynamic_radio_supervisor_failure_summary_v1",
            "stage": "collection",
            "stopped_reason": collect_result.get("stopped_reason"),
            "blocker": collect_result.get("blocker"),
            "failure_code_histogram": collect_result.get("failure_code_histogram", {}),
            "bucket_attempt_failure_code_histogram": collect_result.get("bucket_attempt_failure_code_histogram", {}),
        },
    )
