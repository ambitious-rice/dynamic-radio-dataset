from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.carla.runner import collect_from_plan_catalog
from dynamic_radio_dataset.indexing.global_finalize import finalize_global_index
from dynamic_radio_dataset.json_utils import iter_jsonl, load_json, save_json
from dynamic_radio_dataset.multi_scene.config import (
    candidate_catalog_path,
    load_multi_scene_config,
    load_yaml_subset,
    make_single_scene_config,
    multi_scene_root,
    scene_dataset_root,
    selected_scene_manifest_ids,
    validate_multi_scene_config,
)
from dynamic_radio_dataset.multi_scene.carla_server import configure_worker_env, ensure_worker_carla, restart_worker_carla, terminate_worker_carla
from dynamic_radio_dataset.paths import repo_root, resolve_repo_path
from dynamic_radio_dataset.pipeline.stages import finalize
from dynamic_radio_dataset.plans.sampler import generate_plan_bank
from dynamic_radio_dataset.rf.processing import process_rf
from dynamic_radio_dataset.rf.static_cache import prepare_rf_static_cache
from dynamic_radio_dataset.routes.route_library import build_route_library
from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments
from dynamic_radio_dataset.tx.placement import generate_tx_catalog


class CarlaUnavailable(RuntimeError): pass
class PlanCatalogEmpty(RuntimeError): pass


def validate_multi_scene(config_path: Path) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    result = validate_multi_scene_config(config, require_scenes=False)
    root = multi_scene_root(config)
    root.mkdir(parents=True, exist_ok=True)
    save_json(root / "configs" / "resolved_multi_scene_config.json", dict(config))
    return {"config": str(config_path), "dataset_root": str(root), **result}


def prepare_multi_scene(config_path: Path, *, max_scenes: int | None = None) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    validate_multi_scene_config(config)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    root = multi_scene_root(config)
    save_json(root / "configs" / "resolved_multi_scene_config.json", dict(config))
    results = []
    for scene in scenes:
        results.append(_run_scene_step(config, scene, _prepare_one_scene))
    summary = _scene_summary(config, results)
    save_json(root / "indexes" / "scene_summary.json", summary)
    return {"dataset_root": str(root), "scene_results": results, "scene_summary": str(root / "indexes" / "scene_summary.json")}


def prepare_multi_scene_rf_cache(config_path: Path, *, force: bool = False) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    scenes = list(config.get("scenes", []))
    results = []
    for scene in scenes:
        results.append(_run_scene_step(config, scene, lambda cfg, sc: prepare_rf_static_cache(cfg, force=force)))
    return {"dataset_root": str(multi_scene_root(config)), "scene_results": results}


def process_multi_scene_rf(
    config_path: Path,
    *,
    max_scenes: int | None = None,
    max_episodes: int | None = None,
    rf_policy: str | None = None,
    use_gpu: bool | None = None,
    gpu_ids: list[str] | None = None,
    workers: int | None = None,
) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    results = []
    for scene in scenes:
        results.append(
            _run_scene_step(
                config,
                scene,
                lambda cfg, sc: process_rf(
                    cfg,
                    max_episodes=max_episodes,
                    rf_policy=rf_policy,
                    use_gpu=use_gpu,
                    gpu_ids=gpu_ids,
                    workers=workers,
                ),
            )
        )
    failed_scene_count = sum(1 for row in results if row.get("status") != "ok")
    summary = {
        "schema": "multi_scene_rf_process_summary_v1",
        "dataset_root": str(multi_scene_root(config)),
        "scene_count": int(len(scenes)),
        "ok_scene_count": int(len(scenes) - failed_scene_count),
        "failed_scene_count": int(failed_scene_count),
        "max_episodes": max_episodes,
        "use_gpu": use_gpu,
        "gpu_ids": gpu_ids,
        "worker_count": workers,
        "scene_results": results,
    }
    output = multi_scene_root(config) / "indexes" / "multi_scene_rf_process_summary.json"
    save_json(output, summary)
    if failed_scene_count:
        raise RuntimeError(f"Multi-scene RF failed for {failed_scene_count} scene(s); see {output}")
    return summary


def run_multi_scene_supervised(config_path: Path, *, max_scenes: int | None = None, max_episodes: int | None = None) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    results = []
    for scene in scenes:
        def step(scene_config: dict, scene_row: Mapping[str, Any]) -> dict[str, Any]:
            target = int(scene_row.get("target_accepted", config.get("dataset", {}).get("accepted_trajectories_per_scene", 0) or 0))
            result: dict[str, Any] = {}
            result["collect"] = collect_from_plan_catalog(scene_config, target_accepted=target if target > 0 else None, bucket_targets={})
            result["tx_assignment"] = ensure_tx_assignments(scene_config)
            result["process_rf"] = process_rf(scene_config, max_episodes=max_episodes)
            result["finalize"] = finalize(scene_config)
            return result
        results.append(_run_scene_step(config, scene, step))
    save_json(multi_scene_root(config) / "indexes" / "scene_summary.json", _scene_summary(config, results))
    return {"dataset_root": str(multi_scene_root(config)), "scene_results": results}


def finalize_multi_scene(config_path: Path) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    scene_results = []
    for scene in config.get("scenes", []):
        scene_results.append(_run_scene_step(config, scene, lambda cfg, sc: finalize(cfg)))
    global_result = finalize_global_index(config)
    return {"dataset_root": str(multi_scene_root(config)), "scene_results": scene_results, "global_finalize": global_result}


def validate_selected_scenes(manifest_path: Path) -> dict[str, Any]:
    manifest = load_yaml_subset(manifest_path)
    config_path = manifest.get("multi_scene_config") or manifest.get("config")
    if not config_path:
        raise ValueError("selected scene manifest requires multi_scene_config")
    config = load_multi_scene_config(resolve_repo_path(str(config_path)))
    catalog_raw = manifest.get("source_candidate_catalog") or str(candidate_catalog_path(config))
    catalog_path = resolve_repo_path(str(catalog_raw)) if not Path(str(catalog_raw)).is_absolute() else Path(str(catalog_raw))
    candidates = {str(row["candidate_id"]): row for row in iter_jsonl(catalog_path)}
    selected_ids, backup_ids = selected_scene_manifest_ids(manifest)
    target_count = int(manifest.get("target_scene_count", len(selected_ids)))
    queue = _order_candidate_ids(selected_ids, candidates, manifest)
    queue += _order_candidate_ids([item for item in backup_ids if item not in selected_ids], candidates, manifest)
    accepted: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    transient_failures: list[dict[str, Any]] = []
    auto_manage_carla = bool(manifest.get("auto_manage_carla", True))
    max_carla_restarts = int(manifest.get("max_carla_restarts", 8))
    validation_gpu_id = manifest.get("carla_gpu_id", manifest.get("gpu_id"))
    if auto_manage_carla:
        configure_worker_env(None if validation_gpu_id in (None, "") else str(validation_gpu_id), worker_id=0)
    carla_restarts = 0
    retry_counts: dict[str, int] = {}
    carla_proc = None
    carla_log_handles: list[Any] = []
    index = 0
    try:
        while index < len(queue):
            if len(accepted) >= target_count:
                break
            candidate_id = queue[index]
            if candidate_id not in candidates:
                failed.append({"candidate_id": candidate_id, "status": "failed", "failure_code": "candidate_id_not_in_catalog"})
                index += 1
                continue
            scene = _scene_from_candidate(candidates[candidate_id], manifest)
            if auto_manage_carla:
                scene_config = make_single_scene_config(config, scene)
                carla_proc, carla_log_handles, restarted = _ensure_or_restart_carla(
                    scene_config,
                    carla_proc,
                    carla_log_handles,
                    carla_restarts=carla_restarts,
                    max_carla_restarts=max_carla_restarts,
                )
                carla_restarts += int(restarted)
            result = _run_scene_step(config, scene, _prepare_one_scene)
            if result.get("status") == "ok":
                _remove_optional(scene_dataset_root(config, scene) / "scene_failure.json")
                accepted.append(scene)
                index += 1
            else:
                if result.get("failure_code") == "carla_unavailable" and auto_manage_carla:
                    attempts = retry_counts.get(candidate_id, 0)
                    if attempts < 1 and carla_restarts < max_carla_restarts:
                        transient_failures.append(result)
                        retry_counts[candidate_id] = attempts + 1
                        carla_restarts += 1
                        scene_config = make_single_scene_config(config, scene)
                        carla_proc, carla_log_handles = _restart_owned_carla(scene_config, carla_proc, carla_log_handles)
                        continue
                failed.append(result)
                index += 1
                if result.get("fatal"):
                    break
    finally:
        if carla_proc is not None:
            try:
                terminate_worker_carla(carla_proc, carla_log_handles, scene_config if "scene_config" in locals() else None)
            except Exception:  # noqa: BLE001
                pass
    resolved_config = dict(config)
    resolved_config["scenes"] = accepted
    root = multi_scene_root(config)
    report = {
        "schema": "selected_scene_validation_report_v1",
        "source_manifest": str(manifest_path),
        "source_candidate_catalog": str(catalog_path),
        "target_scene_count": int(target_count),
        "accepted_scene_count": len(accepted),
        "accepted_scenes": accepted,
        "failed": failed,
        "transient_failures": transient_failures,
        "carla_restarts": int(carla_restarts),
    }
    save_json(root / "configs" / "selected_scene_manifest_resolved.json", resolved_config)
    save_json(root / "scene_validation_report.json", report)
    if len(accepted) < target_count:
        raise RuntimeError(f"Only validated {len(accepted)}/{target_count} scenes; see {root / 'scene_validation_report.json'}")
    return report


def _run_scene_step(config: Mapping[str, Any], scene: Mapping[str, Any], fn) -> dict[str, Any]:
    scene_id = str(scene.get("scene_id", "unknown"))
    started = time.time()
    scene_config = make_single_scene_config(config, scene)
    try:
        result = fn(scene_config, scene)
        return {
            "scene_id": scene_id,
            "candidate_id": str(scene.get("candidate_id", "")),
            "status": "ok",
            "elapsed_s": float(time.time() - started),
            "result": result,
        }
    except CarlaUnavailable as exc:
        failure = {
            "scene_id": scene_id,
            "candidate_id": str(scene.get("candidate_id", "")),
            "status": "failed",
            "elapsed_s": float(time.time() - started),
            "failure_code": "carla_unavailable",
            "fatal": True,
            "error": f"{type(exc).__name__}: {exc}",
        }
        save_json(scene_dataset_root(config, scene) / "scene_failure.json", failure)
        return failure
    except PlanCatalogEmpty as exc:
        failure = {
            "scene_id": scene_id,
            "candidate_id": str(scene.get("candidate_id", "")),
            "status": "failed",
            "elapsed_s": float(time.time() - started),
            "failure_code": "plan_catalog_empty",
            "error": f"{type(exc).__name__}: {exc}",
        }
        save_json(scene_dataset_root(config, scene) / "scene_failure.json", failure)
        return failure
    except Exception as exc:  # noqa: BLE001
        failure = {
            "scene_id": scene_id,
            "candidate_id": str(scene.get("candidate_id", "")),
            "status": "failed",
            "elapsed_s": float(time.time() - started),
            "failure_code": "scene_step_failed",
            "error": f"{type(exc).__name__}: {exc}",
        }
        save_json(scene_dataset_root(config, scene) / "scene_failure.json", failure)
        return failure


def _prepare_one_scene(scene_config: dict, scene: Mapping[str, Any]) -> dict[str, Any]:
    root = scene_dataset_root({"dataset": {"root": scene_config["multi_scene"]["parent_root"]}}, scene)
    root.mkdir(parents=True, exist_ok=True)
    save_json(root / "configs" / "resolved_single_scene_config.json", scene_config)
    reference = _prepare_reference_scene(scene_config, scene, root)
    tx = generate_tx_catalog(scene_config)
    route_library = build_route_library(scene_config)
    plans = generate_plan_bank(scene_config)
    if int(plans.get("accepted_plan_count", 0)) <= 0:
        raise PlanCatalogEmpty(
            f"{scene.get('scene_id')} produced 0 accepted traffic plans; "
            f"rejections={plans.get('rejection_reason_histogram', {})}"
        )
    return {"reference_scene": reference, "tx_catalog": tx, "route_library": route_library, "plan_catalog": plans}


def _prepare_reference_scene(scene_config: dict, scene: Mapping[str, Any], root: Path) -> dict[str, Any]:
    reference_dir = root / "reference_scene"
    source = scene.get("source_reference_scene") or scene.get("reference_scene")
    if source:
        src = resolve_repo_path(str(source))
        if not src.exists():
            raise FileNotFoundError(f"source reference scene does not exist: {src}")
        shutil.copytree(src, reference_dir, dirs_exist_ok=True)
    elif not _reference_capture_ready(reference_dir):
        _capture_reference_scene(scene_config, scene, reference_dir)
    if not (reference_dir / "sionna_export" / "manifest.json").exists():
        _export_reference_scene(scene_config, reference_dir)
    return {"reference_scene_dir": str(reference_dir), "reference_export_dir": str(reference_dir / "sionna_export")}


def _reference_capture_ready(reference_dir: Path) -> bool:
    return (reference_dir / "scene_meta.json").exists() and (reference_dir / "frames" / "actor_states.jsonl").exists()


def _capture_reference_scene(scene_config: dict, scene: Mapping[str, Any], output_dir: Path) -> None:
    carla_cfg = scene_config.get("carla", {})
    _require_carla_ready(scene_config, stage="before_reference_capture")
    selector = scene.get("selector", {}) if isinstance(scene.get("selector"), Mapping) else {}
    cmd: list[object] = [
        carla_cfg.get("python", "python3"),
        "-m",
        "dynamic_radio_dataset.carla.collect",
        "--host", carla_cfg.get("host", "localhost"),
        "--port", int(carla_cfg.get("port", 2000)),
        "--town", scene.get("town", scene_config.get("scene", {}).get("town", "Town10HD_Opt")),
        "--scene-mode", selector.get("mode", scene_config.get("scene", {}).get("scene_mode", "junction")),
        "--scene-id", scene.get("scene_id", ""),
        "--scene-type", scene.get("scene_type", ""),
        "--seed", int(scene_config.get("dataset", {}).get("seed", 17)),
        "--frames", 1,
        "--fps", float(scene_config.get("traffic", {}).get("fps", 10.0)),
        "--traffic-preroll-s", 0.0,
        "--valid-size", float(scene_config.get("scene", {}).get("valid_size", 96.0)),
        "--support-size", float(scene_config.get("scene", {}).get("support_size", 192.0)),
        "--route-step", float(carla_cfg.get("route_step", 2.0)),
        "--route-approach", float(carla_cfg.get("route_approach", 25.0)),
        "--route-exit", float(carla_cfg.get("route_exit", 25.0)),
        "--min-approach", float(carla_cfg.get("min_approach", 18.0)),
        "--min-exit", float(carla_cfg.get("min_exit", 18.0)),
        "--junction-core-radius", float(scene_config.get("scene", {}).get("junction_core_radius", 12.0)),
        "--num-target-vehicles", 1,
        "--num-background-vehicles", 0,
        "--min-passed-targets", 0,
        "--vehicle-size-preset", carla_cfg.get("vehicle_size_preset", "mixed"),
        "--output-dir", output_dir,
        "--keep-existing",
        "--no-video",
        "--clear-existing-vehicles",
        "--clear-existing-sensors",
    ]
    if carla_cfg.get("traffic_manager_port") is not None:
        cmd.extend(["--traffic-manager-port", int(carla_cfg["traffic_manager_port"])])
    if selector.get("junction_id") is not None:
        cmd.extend(["--scene-junction-id", int(selector["junction_id"])])
    if selector.get("corridor_index") is not None:
        cmd.extend(["--scene-corridor-index", int(selector["corridor_index"])])
    try:
        _run(cmd)
    except subprocess.CalledProcessError as exc:
        ok, details = _carla_health_status(scene_config, timeout_s=5.0)
        if not ok:
            raise CarlaUnavailable(f"CARLA became unavailable after reference capture failed: {details}") from exc
        raise


def _export_reference_scene(scene_config: dict, reference_dir: Path) -> None:
    carla_cfg = scene_config.get("carla", {})
    sionna = scene_config.get("sionna", {})
    cmd: list[object] = [
        carla_cfg.get("python", "python3"),
        "-m",
        "dynamic_radio_dataset.sionna.export",
        "--dataset-dir", reference_dir,
        "--output-dir", reference_dir / "sionna_export",
        "--snapshot-frame", 0,
        "--vehicle-geometry", "bbox",
        "--building-geometry", "mixed",
        "--carla-host", carla_cfg.get("host", "localhost"),
        "--carla-port", int(carla_cfg.get("port", 2000)),
    ]
    if bool(sionna.get("include_buildings", True)):
        cmd.append("--include-buildings")
    _run(cmd)


def _scene_from_candidate(candidate: Mapping[str, Any], manifest: Mapping[str, Any]) -> dict[str, Any]:
    overrides = manifest.get("scene_type_overrides", {}) if isinstance(manifest.get("scene_type_overrides"), Mapping) else {}
    scene_id = str(candidate["scene_id"])
    return {
        "candidate_id": str(candidate["candidate_id"]),
        "scene_id": scene_id,
        "town": str(candidate["town"]),
        "scene_type": str(overrides.get(candidate["candidate_id"], candidate.get("scene_type_hint", "unknown"))),
        "selector": dict(candidate.get("selector", {})),
        "target_accepted": int(manifest.get("target_accepted_per_scene", 0) or 0),
    }


def _order_candidate_ids(
    candidate_ids: list[str],
    candidates: Mapping[str, Mapping[str, Any]],
    manifest: Mapping[str, Any],
) -> list[str]:
    order = str(manifest.get("validation_order", "manifest")).strip().lower()
    if order not in {"grouped_by_town", "town"}:
        return list(candidate_ids)
    positions = {candidate_id: idx for idx, candidate_id in enumerate(candidate_ids)}
    return sorted(
        candidate_ids,
        key=lambda candidate_id: (
            str(candidates.get(candidate_id, {}).get("town", "")),
            positions[candidate_id],
        ),
    )


def _scene_summary(config: Mapping[str, Any], results: list[dict[str, Any]]) -> dict[str, Any]:
    rows = []
    for scene in config.get("scenes", []):
        scene_root = scene_dataset_root(config, scene)
        route_summary = _load_optional(scene_root / "route_library" / "route_library_summary.json") or {}
        tx_summary = _load_optional(scene_root / "scene_static" / "tx_placement_summary.json") or {}
        collection = _load_optional(scene_root / "collection_summary.json") or {}
        rows.append({
            "scene_id": str(scene.get("scene_id")),
            "town": str(scene.get("town")),
            "scene_type": str(scene.get("scene_type", "unknown")),
            "route_count": int(route_summary.get("route_count", 0)),
            "route_turn_type_histogram": route_summary.get("turn_type_histogram", {}),
            "route_length_summary": route_summary.get("route_length_summary", {}),
            "route_conflict_density": route_summary.get("route_conflict_density"),
            "tx_candidate_count": int(tx_summary.get("candidate_count", 0)),
            "selected_tx_per_episode": int(config.get("dataset", {}).get("selected_tx_per_episode", 5)),
            "planned_target_accepted": int(scene.get("target_accepted", config.get("dataset", {}).get("accepted_trajectories_per_scene", 0) or 0)),
            "collection_status": collection.get("stopped_reason"),
        })
    return {"schema": "multi_scene_summary_v1", "scene_count": len(rows), "scenes": rows, "step_results": results}


def _load_optional(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        return load_json(path)
    except Exception:  # noqa: BLE001
        return None


def _remove_optional(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def _ensure_or_restart_carla(
    scene_config: dict,
    carla_proc,
    carla_log_handles: list[Any],
    *,
    carla_restarts: int,
    max_carla_restarts: int,
):
    ok, _details = _carla_health_status(scene_config, timeout_s=5.0)
    if ok:
        return carla_proc, carla_log_handles, False
    if carla_proc is not None and carla_restarts < max_carla_restarts:
        _apply_validation_carla_defaults(scene_config)
        proc, handles = restart_worker_carla(scene_config, carla_proc, carla_log_handles, worker_id=0, startup_retries=2)
        return proc, handles, True
    _apply_validation_carla_defaults(scene_config)
    proc, handles, started = ensure_worker_carla(scene_config, carla_proc, carla_log_handles, worker_id=0, startup_retries=2)
    return proc, handles, started


def _apply_validation_carla_defaults(scene_config: dict[str, Any]) -> None:
    supervisor = scene_config.setdefault("supervisor", {})
    supervisor.setdefault("sanitize_display_env", True)
    supervisor.setdefault("disable_core_dumps", True)
    supervisor.setdefault("restart_backoff_s", [30.0, 60.0, 120.0, 300.0])


def _run(cmd: list[object]) -> None:
    subprocess.run([str(item) for item in cmd], cwd=str(repo_root()), check=True, env=_env())


def _require_carla_ready(scene_config: Mapping[str, Any], *, stage: str) -> None:
    ok, details = _carla_health_status(scene_config, timeout_s=5.0)
    if not ok:
        raise CarlaUnavailable(f"{stage}: {details}")


def _carla_health_status(scene_config: Mapping[str, Any], *, timeout_s: float) -> tuple[bool, str]:
    carla_cfg = scene_config.get("carla", {}) if isinstance(scene_config.get("carla", {}), Mapping) else {}
    python = str(carla_cfg.get("python", "python3"))
    host = str(carla_cfg.get("host", "localhost"))
    port = int(carla_cfg.get("port", 2000))
    client_timeout_s = max(1.0, float(timeout_s))
    code = f"""
import glob
import sys
from pathlib import Path
repo = Path({str(repo_root())!r})
eggs = sorted(glob.glob(str(repo / "PythonAPI" / "carla" / "dist" / "carla-*-py3.*-linux-x86_64.egg")))
if eggs:
    sys.path.insert(0, eggs[-1])
sys.path.insert(0, str(repo / "PythonAPI" / "carla"))
import carla
client = carla.Client({host!r}, {port})
client.set_timeout({client_timeout_s!r})
world = client.get_world()
print(world.get_map().name)
"""
    try:
        proc = subprocess.run(
            [python, "-c", code],
            cwd=str(repo_root()),
            env=_env(),
            text=True,
            capture_output=True,
            timeout=client_timeout_s + 3.0,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        return False, f"health check subprocess timeout after {exc.timeout}s"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip().splitlines()
        return False, detail[-1] if detail else f"health check returned {proc.returncode}"
    return True, (proc.stdout or "").strip()


def _env() -> dict[str, str]:
    import os

    env = os.environ.copy()
    scripts = str(repo_root() / "scripts")
    env["PYTHONPATH"] = scripts if not env.get("PYTHONPATH") else f"{scripts}:{env['PYTHONPATH']}"
    return env
