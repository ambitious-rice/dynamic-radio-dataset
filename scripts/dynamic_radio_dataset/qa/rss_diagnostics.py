from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import numpy as np

from dynamic_radio_dataset.json_utils import save_json


def diagnose_episode_rss(
    episode_dir: Path,
    scene_static_dir: Path,
    output_path: Path | None = None,
    floor_dbm: float = -200.0,
) -> dict[str, Any]:
    dynamic_npz = np.load(episode_dir / "rss_dynamic_dbm.npz")
    delta_npz = np.load(episode_dir / "rss_delta_from_static_db.npz")
    dynamic = np.asarray(dynamic_npz["dynamic_rss_dbm"], dtype=np.float64)
    delta = np.asarray(delta_npz["delta_from_static_db"], dtype=np.float64)
    tx_ids = [str(item) for item in dynamic_npz["tx_ids"].tolist()]
    building_mask = _load_building_mask(episode_dir, scene_static_dir).astype(bool)

    tx_reports = []
    for tx_index, tx_id in enumerate(tx_ids):
        static = np.asarray(np.load(scene_static_dir / tx_id / "static_rss_dbm.npy"), dtype=np.float64)
        report = _diagnose_tx(
            tx_id=tx_id,
            dynamic=dynamic[tx_index],
            delta=delta[tx_index],
            static=static,
            building_mask=building_mask,
            floor_dbm=floor_dbm,
        )
        tx_reports.append(report)

    result = {
        "schema": "rss_common_mask_diagnostics_v1",
        "episode_dir": str(episode_dir),
        "scene_static_dir": str(scene_static_dir),
        "floor_dbm": float(floor_dbm),
        "mask_policy": (
            "valid crop grid, non-building cells, finite static RSS, finite dynamic RSS for all frames, "
            "and static/dynamic values above floor_dbm"
        ),
        "duplicate_static_sanity": _duplicate_static_sanity(scene_static_dir, tx_ids, building_mask, floor_dbm),
        "tx_reports": tx_reports,
    }
    if output_path is not None:
        save_json(output_path, result)
    return result


def _diagnose_tx(
    tx_id: str,
    dynamic: np.ndarray,
    delta: np.ndarray,
    static: np.ndarray,
    building_mask: np.ndarray,
    floor_dbm: float,
) -> dict[str, Any]:
    static_valid = np.isfinite(static) & (static > floor_dbm)
    dynamic_finite_all = np.all(np.isfinite(dynamic), axis=0)
    dynamic_above_floor_all = np.all(dynamic > floor_dbm, axis=0)
    common = (~building_mask) & static_valid & dynamic_finite_all & dynamic_above_floor_all

    with np.errstate(invalid="ignore"):
        temporal_range = np.max(dynamic[:, common], axis=0) - np.min(dynamic[:, common], axis=0)
        delta_common = delta[:, common]
    return {
        "tx_id": tx_id,
        "grid_shape": list(dynamic.shape[1:]),
        "frame_count": int(dynamic.shape[0]),
        "non_building_cell_count": int(np.sum(~building_mask)),
        "static_valid_cell_count": int(np.sum((~building_mask) & static_valid)),
        "dynamic_all_frames_valid_cell_count": int(np.sum((~building_mask) & dynamic_finite_all & dynamic_above_floor_all)),
        "common_mask_cell_count": int(np.sum(common)),
        "excluded_nan_or_inf_cell_count": int(np.sum((~building_mask) & (~dynamic_finite_all | ~np.isfinite(static)))),
        "excluded_clipped_or_floor_cell_count": int(
            np.sum((~building_mask) & ~(static_valid & dynamic_above_floor_all) & dynamic_finite_all & np.isfinite(static))
        ),
        "temporal_range_db": _stats_1d(temporal_range),
        "delta_from_static_db": _stats_1d(delta_common.reshape(-1)),
        "abs_delta_from_static_db": _stats_1d(np.abs(delta_common.reshape(-1))),
    }


def _duplicate_static_sanity(scene_static_dir: Path, tx_ids: list[str], building_mask: np.ndarray, floor_dbm: float) -> dict[str, Any]:
    reports = []
    for tx_id in tx_ids:
        static = np.asarray(np.load(scene_static_dir / tx_id / "static_rss_dbm.npy"), dtype=np.float64)
        mask = (~building_mask) & np.isfinite(static) & (static > floor_dbm)
        repeated = np.repeat(static[None, :, :], 80, axis=0)
        temporal_range = np.max(repeated[:, mask], axis=0) - np.min(repeated[:, mask], axis=0)
        reports.append(
            {
                "tx_id": tx_id,
                "common_mask_cell_count": int(np.sum(mask)),
                "max_temporal_range_db": float(np.max(temporal_range)) if temporal_range.size else None,
                "max_abs_delta_db": 0.0 if temporal_range.size else None,
            }
        )
    return {
        "policy": "repeat each static RSS map for 80 frames; expected temporal range and delta are exactly zero",
        "reports": reports,
    }


def _stats_1d(values: np.ndarray) -> dict[str, Any]:
    finite = values[np.isfinite(values)]
    if finite.size == 0:
        return {"count": 0}
    return {
        "count": int(finite.size),
        "p50": float(np.percentile(finite, 50)),
        "p90": float(np.percentile(finite, 90)),
        "p95": float(np.percentile(finite, 95)),
        "p99": float(np.percentile(finite, 99)),
        "min": float(np.min(finite)),
        "max": float(np.max(finite)),
    }


def _load_building_mask(episode_dir: Path, scene_static_dir: Path) -> np.ndarray:
    tx_dirs = sorted(episode_dir.glob("tx_*"))
    for tx_dir in tx_dirs:
        npz_path = tx_dir / "rss_maps.npz"
        if npz_path.exists():
            data = np.load(npz_path)
            if "building_mask" in data.files:
                return np.asarray(data["building_mask"], dtype=bool)
    return np.asarray(np.load(scene_static_dir / "building_mask_uint8.npy"), dtype=bool)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check RSS statistics on a valid non-building common mask.")
    parser.add_argument("--episode-dir", type=Path, required=True)
    parser.add_argument("--scene-static-dir", type=Path, required=True)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--floor-dbm", type=float, default=-200.0)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    result = diagnose_episode_rss(args.episode_dir, args.scene_static_dir, args.output_json, args.floor_dbm)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
