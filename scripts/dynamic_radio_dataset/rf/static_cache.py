from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, resolve_repo_path
from dynamic_radio_dataset.rf.runtime import run_command, sionna_env, sionna_python


def prepare_rf_static_cache(config: dict, *, force: bool = False) -> dict[str, Any]:
    root = dataset_root(config)
    static_dir = root / "scene_static"
    tx_catalog = load_json(static_dir / "tx_catalog.json").get("tx_catalog", [])
    reference_export = _reference_export_dir(config)
    resolution = int(config.get("sionna", {}).get("resolution", 128)) if isinstance(config.get("sionna"), Mapping) else 128
    started = time.time()
    processed = 0
    skipped = 0
    invalid_existing: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for tx in tx_catalog:
        tx_id = str(tx["tx_id"])
        tx_dir = static_dir / tx_id
        target = tx_dir / "static_rss_dbm.npy"
        if target.exists() and not force:
            valid, reason = _existing_static_cache_valid(tx_dir, tx_id, resolution)
            if valid:
                skipped += 1
                continue
            invalid_existing.append({"tx_id": tx_id, "reason": reason})
        tx_dir.mkdir(parents=True, exist_ok=True)
        rss_dir = tx_dir / "rss_static"
        try:
            _run_static_for_tx(config, reference_export, tx, rss_dir)
            with np.load(rss_dir / "rss_maps.npz") as data:
                rss_dbm = np.asarray(data["rss_dbm"], dtype=np.float32)
            if rss_dbm.shape[0] < 1:
                raise RuntimeError(f"Static RSS output for {tx_id} is empty")
            if tuple(rss_dbm[0].shape) != (resolution, resolution):
                raise RuntimeError(
                    f"Static RSS output for {tx_id} has shape {tuple(rss_dbm[0].shape)}, "
                    f"expected {(resolution, resolution)}"
                )
            _atomic_save_npy(target, rss_dbm[0])
            meta = rss_dir / "rss_heatmap_meta.json"
            if not meta.exists():
                raise RuntimeError(f"Static RSS metadata is missing for {tx_id}: {meta}")
            meta_data = load_json(meta)
            _validate_zero_vehicle_static_meta(meta_data, tx_id)
            save_json(tx_dir / "rss_heatmap_meta.json", meta_data)
            processed += 1
        except Exception as exc:  # noqa: BLE001
            failures.append({"tx_id": tx_id, "failure_code": "static_rss_failed", "error": f"{type(exc).__name__}: {exc}"})
    summary = {
        "schema": "rf_static_cache_summary_v1",
        "dataset_root": str(root),
        "tx_catalog_count": int(len(tx_catalog)),
        "processed_tx_count": int(processed),
        "skipped_existing_tx_count": int(skipped),
        "invalid_existing_tx_count": int(len(invalid_existing)),
        "invalid_existing": invalid_existing,
        "failed_tx_count": int(len(failures)),
        "elapsed_s": float(time.time() - started),
        "failures": failures,
    }
    save_json(static_dir / "rf_static_cache_summary.json", summary)
    if failures:
        raise RuntimeError(f"RF static cache failed for {len(failures)} TX(s); see {static_dir / 'rf_static_cache_summary.json'}")
    return summary


def _run_static_for_tx(config: Mapping[str, Any], export_dir: Path, tx_entry: Mapping[str, Any], output_dir: Path) -> None:
    sionna = config.get("sionna", {}) if isinstance(config.get("sionna"), Mapping) else {}
    tx_cfg = config.get("tx", {}) if isinstance(config.get("tx"), Mapping) else {}
    pos = tx_entry["position"]
    cmd: list[object] = [
        sionna_python(dict(config)),
        "-m",
        "dynamic_radio_dataset.rf.rss_compute",
        "--export-dir",
        export_dir,
        "--output-dir",
        output_dir,
        "--start-frame",
        0,
        "--end-frame",
        0,
        "--stride",
        1,
        "--resolution",
        int(sionna.get("resolution", 128)),
        "--frequency",
        float(sionna.get("frequency_hz", 5.89e9)),
        "--bandwidth",
        float(sionna.get("bandwidth_hz", 10e6)),
        "--tx-power-dbm",
        float(tx_entry.get("tx_power_dbm", sionna.get("tx_power_dbm", 23.0))),
        "--tx-height",
        float(tx_cfg.get("tx_height_m", tx_cfg.get("height_m", 1.5))),
        "--rx-height",
        float(sionna.get("rx_height_m", 1.0)),
        "--tx-placement",
        "custom",
        "--tx-x",
        float(pos["x"]),
        "--tx-y",
        float(pos["y"]),
        "--tx-z",
        float(pos["z"]),
        "--max-depth",
        int(sionna.get("max_depth", 3)),
        "--num-samples",
        int(sionna.get("num_samples", 250000)),
        "--num-runs",
        int(sionna.get("num_runs", 1)),
        "--allow-zero-vehicles",
        "--exclude-vehicles",
        "all",
        "--allow-flat-rss",
        "--no-auto-skip-unstable-start",
        "--mitsuba-variant",
        str(sionna.get("mitsuba_variant", "llvm_ad_rgb")),
    ]
    if bool(sionna.get("use_gpu", False)):
        cmd.append("--use-gpu")
    if bool(sionna.get("mask_building_cells", True)):
        cmd.append("--mask-building-cells")
    env = sionna_env(None)
    if bool(sionna.get("use_gpu", False)):
        configured_gpu_ids = sionna.get("gpu_ids", [])
        if not env.get("CUDA_VISIBLE_DEVICES") and isinstance(configured_gpu_ids, list) and configured_gpu_ids:
            env["CUDA_VISIBLE_DEVICES"] = str(configured_gpu_ids[0])
    else:
        env["CUDA_VISIBLE_DEVICES"] = ""
    run_command(cmd, env=env)


def _existing_static_cache_valid(tx_dir: Path, tx_id: str, resolution: int) -> tuple[bool, str | None]:
    target = tx_dir / "static_rss_dbm.npy"
    meta_path = tx_dir / "rss_heatmap_meta.json"
    try:
        arr = np.load(target, mmap_mode="r")
        if tuple(arr.shape) != (int(resolution), int(resolution)):
            return False, f"shape_mismatch:{tuple(arr.shape)}"
        if not np.issubdtype(arr.dtype, np.number):
            return False, f"non_numeric_dtype:{arr.dtype}"
    except Exception as exc:  # noqa: BLE001
        return False, f"npy_read_failed:{type(exc).__name__}:{exc}"
    if not meta_path.exists():
        return False, "metadata_missing"
    try:
        meta = load_json(meta_path)
        _validate_zero_vehicle_static_meta(meta, tx_id)
    except Exception as exc:  # noqa: BLE001
        return False, f"metadata_invalid:{type(exc).__name__}:{exc}"
    return True, None


def _atomic_save_npy(path: Path, array: np.ndarray) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    with tmp.open("wb") as f:
        np.save(f, array)
    tmp.replace(path)


def _validate_zero_vehicle_static_meta(meta: Mapping[str, Any], tx_id: str) -> None:
    propagation = meta.get("propagation", {}) if isinstance(meta.get("propagation"), Mapping) else {}
    active = propagation.get("active_vehicle_objects", [])
    if active:
        raise RuntimeError(f"Static RSS cache for {tx_id} unexpectedly kept active vehicle objects: {active}")


def _reference_export_dir(config: dict) -> Path:
    root = dataset_root(config)
    local = root / "reference_scene" / "sionna_export"
    if local.exists():
        return local
    raw = config.get("scene", {}).get("reference_export_dir") if isinstance(config.get("scene"), dict) else None
    if raw:
        return resolve_repo_path(str(raw))
    raise FileNotFoundError(f"reference export is missing for {root}")
