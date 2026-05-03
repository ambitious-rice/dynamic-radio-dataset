#!/usr/bin/env python3
"""
Engineering-oriented entrypoint for single-scene dynamic radio-map dataset generation.

This script reuses the existing CARLA collection, Sionna export, and RSS scripts,
and adds the missing orchestration needed for formal dataset collection:
- scene-static preparation with fixed TX placements;
- zero-vehicle static RSS baselines;
- per-episode dynamic RSS generation for all TXs;
- traffic-grid rasterization;
- trajectory/TX QA;
- dataset indexing and deterministic splits.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Dict, List, Optional, Sequence

import numpy as np

from dynamic_radio_dataset.radio_dataset_utils import (
    TxSearchConfig,
    deterministic_split,
    ensure_dir,
    load_carla_map_for_scene,
    load_json,
    plot_tx_selection_overlay,
    rasterize_building_mask,
    save_json,
    scene_signature,
    select_tx_candidates,
    summarize_episode_for_index,
    write_jsonl,
)
from dynamic_radio_dataset.rf.episode_job import process_episode
from dynamic_radio_dataset.rf.runtime import DEFAULT_SIONNA_PYTHON, manual_sionna_env, run_command as runtime_run_command


PACKAGE_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PACKAGE_ROOT / "assets"
DEFAULT_VEHICLE_ALLOWLIST = ",".join(
    [
        "vehicle.mini.cooper_s",
        "vehicle.nissan.micra",
        "vehicle.tesla.model3",
        "vehicle.chevrolet.impala",
        "vehicle.seat.leon",
        "vehicle.mitsubishi.fusorosa",
    ]
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a formal single-scene CARLA + Sionna radio dataset.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    prepare = subparsers.add_parser("prepare-scene", help="Prepare fixed TX placements and static RSS baselines from a reference clip.")
    add_common_paths_args(prepare)
    add_export_args(prepare)
    add_rss_args(prepare)
    add_tx_search_args(prepare)
    prepare.add_argument("--reference-dataset-dir", type=Path, required=True)
    prepare.add_argument("--reference-export-dir", type=Path, default=None)
    prepare.add_argument("--tx-catalog-name", type=str, default="tx_catalog.json")

    process = subparsers.add_parser("process-episode", help="Export one existing episode, generate dynamic RSS, traffic grids, and QA artifacts.")
    add_common_paths_args(process)
    add_export_args(process)
    add_rss_args(process)
    add_episode_qa_args(process)
    process.add_argument("--episode-dir", type=Path, required=True)
    process.add_argument("--episode-id", type=str, default=None)
    process.add_argument("--scene-static-dir", type=Path, default=None)
    process.add_argument("--reference-export-dir", type=Path, default=None)

    collect = subparsers.add_parser("collect-dataset", help="Deprecated random collector; use scripts/drd.py collect.")
    add_common_paths_args(collect)
    add_export_args(collect)
    add_rss_args(collect)
    add_episode_qa_args(collect)
    add_collect_args(collect)
    collect.add_argument("--scene-static-dir", type=Path, default=None)
    collect.add_argument("--reference-export-dir", type=Path, default=None)
    collect.add_argument("--start-episode-index", type=int, default=0)
    collect.add_argument("--raw-episode-budget", type=int, default=150)
    collect.add_argument("--target-accepted-episodes", type=int, default=120)
    collect.add_argument("--accepted-ratio-stop-threshold", type=float, default=0.0)
    collect.add_argument(
        "--per-tx-target-count",
        type=int,
        default=0,
        help=(
            "If >0, keep collecting until each fixed TX has this many uniquely assigned accepted episodes. "
            "Each episode is assigned to at most one TX."
        ),
    )

    finalize = subparsers.add_parser("finalize-index", help="Regenerate episode_index.jsonl and splits.json from processed episodes.")
    finalize.add_argument("--dataset-root", type=Path, required=True)
    finalize.add_argument("--split-seed", type=int, default=7)

    return parser.parse_args()


def add_common_paths_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--dataset-root", type=Path, required=True)
    parser.add_argument("--carla-python", type=str, default=sys.executable)
    parser.add_argument("--sionna-python", type=Path, default=DEFAULT_SIONNA_PYTHON)


def add_export_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--snapshot-frame", type=int, default=40)
    parser.add_argument("--vehicle-geometry", choices=["bbox", "catalog_mesh", "mixed"], default="catalog_mesh")
    parser.add_argument("--building-geometry", choices=["bbox", "environment_mesh", "mixed"], default="mixed")
    parser.add_argument("--vehicle-asset-catalog", type=Path, default=ASSETS_DIR / "vehicle_asset_catalog.json")
    parser.add_argument("--asset-tool-dir", type=Path, default=Path("/share1/fzj/tools/src/UEViewer"))
    parser.add_argument("--asset-cache-dir", type=Path, default=Path("/share1/fzj/tools/umodel_cache"))
    parser.add_argument("--reuse-building-proxy-from-export", type=Path, default=None)
    parser.add_argument("--include-buildings", action="store_true", default=True)
    parser.add_argument("--carla-host", type=str, default="localhost")
    parser.add_argument("--carla-port", type=int, default=2000)


def add_rss_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--rss-resolution", type=int, default=128)
    parser.add_argument("--tx-height", type=float, default=1.5)
    parser.add_argument("--rx-height", type=float, default=1.0)
    parser.add_argument("--frequency", type=float, default=5.89e9)
    parser.add_argument("--bandwidth", type=float, default=10e6)
    parser.add_argument("--tx-power-dbm", type=float, default=23.0)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--num-samples", type=int, default=250000)
    parser.add_argument("--num-runs", type=int, default=1)
    parser.add_argument("--label-fps", type=float, default=10.0)
    parser.add_argument("--mask-building-cells", action="store_true", default=True)
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")


def add_tx_search_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--tx-scene-radius-min", type=float, default=10.0)
    parser.add_argument("--tx-scene-radius-max", type=float, default=20.0)
    parser.add_argument("--tx-lane-distance-min", type=float, default=2.0)
    parser.add_argument("--tx-lane-distance-max", type=float, default=5.5)
    parser.add_argument("--tx-min-spacing", type=float, default=12.0)
    parser.add_argument("--tx-grid-step", type=float, default=1.0)
    parser.add_argument("--tx-top-k-candidates", type=int, default=40)
    parser.add_argument("--tx-count", type=int, default=3)
    parser.add_argument("--corridor-width-m", type=float, default=10.0)
    parser.add_argument("--allow-offline-tx-search", action="store_true", default=False)
    parser.add_argument("--tx-require-sidewalk-lane", dest="tx_require_sidewalk_lane", action="store_true", default=True)
    parser.add_argument("--no-tx-require-sidewalk-lane", dest="tx_require_sidewalk_lane", action="store_false")


def add_episode_qa_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--lane-adherence-max-offset-m", type=float, default=1.75)
    parser.add_argument("--lane-adherence-max-consecutive-frames", type=int, default=20)
    parser.add_argument("--max-jump-per-frame-m", type=float, default=3.5)
    parser.add_argument("--idle-speed-threshold-mps", type=float, default=0.5)
    parser.add_argument("--max-idle-fraction", type=float, default=0.70)
    parser.add_argument("--max-all-slow-duration-s", type=float, default=2.0)
    parser.add_argument("--min-displacement-m", type=float, default=20.0)
    parser.add_argument("--diagnostic-clearance-reference-m", type=float, default=2.5)
    parser.add_argument("--min-p95-temporal-range-db", type=float, default=1.0)
    parser.add_argument("--min-max-temporal-range-db", type=float, default=4.0)
    parser.add_argument("--large-vehicle-token", type=str, default="fusorosa")
    parser.add_argument("--max-large-vehicle-count", type=int, default=1)
    parser.add_argument("--corridor-width-m", type=float, default=10.0)


def add_collect_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--town", type=str, default="Town10HD_Opt")
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument("--scene-mode", choices=["junction", "corridor"], default="junction")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--support-size", type=float, default=192.0)
    parser.add_argument("--valid-size", type=float, default=96.0)
    parser.add_argument("--route-step", type=float, default=2.0)
    parser.add_argument("--route-approach", type=float, default=55.0)
    parser.add_argument("--route-exit", type=float, default=55.0)
    parser.add_argument("--min-approach", type=float, default=18.0)
    parser.add_argument("--min-exit", type=float, default=18.0)
    parser.add_argument("--junction-core-radius", type=float, default=12.0)
    parser.add_argument("--episode-duration-s", type=float, default=8.0)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--traffic-preroll-s", type=float, default=2.0)
    parser.add_argument("--vehicle-allowlist", type=str, default=DEFAULT_VEHICLE_ALLOWLIST)
    parser.add_argument("--vehicle-size-preset", choices=["mixed", "passenger"], default="mixed")
    parser.add_argument("--min-passed-targets", type=int, default=2)
    parser.add_argument("--num-background-vehicles", type=int, default=0)
    parser.add_argument("--turn-ratio", type=float, default=0.3, help="Probability of collecting 4 vehicles instead of 3.")
    parser.add_argument("--target-speed-diff", type=float, default=-20.0)
    parser.add_argument("--background-speed-diff", type=float, default=0.0)
    parser.add_argument("--clear-existing-vehicles", action="store_true", default=True)
    parser.add_argument("--clear-existing-sensors", action="store_true", default=True)


def tx_search_config_from_args(args: argparse.Namespace) -> TxSearchConfig:
    return TxSearchConfig(
        min_scene_radius_m=float(args.tx_scene_radius_min),
        max_scene_radius_m=float(args.tx_scene_radius_max),
        lane_distance_min_m=float(args.tx_lane_distance_min),
        lane_distance_max_m=float(args.tx_lane_distance_max),
        min_tx_spacing_m=float(args.tx_min_spacing),
        grid_step_m=float(args.tx_grid_step),
        top_k_candidates=int(args.tx_top_k_candidates),
        selected_count=int(args.tx_count),
        corridor_width_m=float(args.corridor_width_m),
        require_sidewalk_lane=bool(args.tx_require_sidewalk_lane),
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


def find_topdown_reference_frame(reference_dataset_dir: Path) -> Optional[Path]:
    frame_dir = reference_dataset_dir / "frames" / "topdown_rgb"
    frames = sorted(frame_dir.glob("*.png"))
    return frames[0] if frames else None


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


def run_static_baseline_for_tx(
    args: argparse.Namespace,
    export_dir: Path,
    tx_entry: Dict[str, object],
    output_dir: Path,
) -> Path:
    tx = tx_entry["position"]
    cmd: List[object] = [
        args.sionna_python,
        "-m",
        "dynamic_radio_dataset.render.rss_video",
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
        "--allow-zero-vehicles",
        "--allow-flat-rss",
        "--no-auto-skip-unstable-start",
        "--skip-frame-rendering",
    ]
    cmd.extend(["--mitsuba-variant", str(args.mitsuba_variant)])
    if bool(args.use_gpu):
        cmd.append("--use-gpu")
    if args.mask_building_cells:
        cmd.append("--mask-building-cells")
    run_command(cmd, env=sionna_env(args))
    return output_dir / "rss_maps.npz"


def prepare_scene(args: argparse.Namespace) -> int:
    static_dir = scene_static_dir(args)
    ensure_dir(static_dir)
    reference_export_dir = args.reference_export_dir or (args.reference_dataset_dir / "sionna_export")
    if not reference_export_dir.exists():
        export_dataset_dir(
            args,
            dataset_dir=args.reference_dataset_dir,
            export_dir=reference_export_dir,
            reuse_building_proxy_from_export=args.reuse_building_proxy_from_export,
        )

    reference_scene_meta = load_json(args.reference_dataset_dir / "scene_meta.json")
    reference_routes = load_json(args.reference_dataset_dir / "routes.json")
    reference_manifest = load_json(reference_export_dir / "manifest.json")
    tx_config = tx_search_config_from_args(args)
    carla_map = None
    if tx_config.require_sidewalk_lane:
        try:
            carla_map = load_carla_map_for_scene(
                host=str(args.carla_host),
                port=int(args.carla_port),
                town=str(reference_scene_meta.get("town") or ""),
            )
        except Exception as exc:
            if args.allow_offline_tx_search:
                print(
                    "[WARN] Falling back to offline TX search because CARLA map could not be loaded. "
                    "This is debug-only and may place TX on the road."
                )
            else:
                raise RuntimeError(
                    "Failed to load the live CARLA map for sidewalk-constrained TX search. "
                    "Start CARLA with the correct town or pass --allow-offline-tx-search only for debugging."
                ) from exc
    tx_selection = select_tx_candidates(
        reference_scene_meta,
        {str(route["route_id"]): route for route in reference_routes.get("routes", [])},
        reference_manifest,
        tx_config,
        carla_map=carla_map,
    )

    for tx in tx_selection["tx_catalog"]:
        tx["position"]["z"] = float(args.tx_height)

    save_json(static_dir / args.tx_catalog_name, tx_selection)
    if args.tx_catalog_name != "tx_catalog.json":
        save_json(static_dir / "tx_catalog.json", tx_selection)
    save_json(static_dir / "scene_signature.json", scene_signature(reference_scene_meta))

    building_mask = rasterize_building_mask(reference_manifest, reference_manifest["valid_crop"], int(args.rss_resolution))
    np.save(static_dir / "building_mask_uint8.npy", building_mask.astype(np.uint8))
    np.save(static_dir / "loss_mask_uint8.npy", (~building_mask).astype(np.uint8))

    diagnostics_dir = static_dir / "diagnostics"
    ensure_dir(diagnostics_dir)
    plot_tx_selection_overlay(
        reference_scene_meta,
        {str(route["route_id"]): route for route in reference_routes.get("routes", [])},
        reference_manifest,
        tx_selection,
        diagnostics_dir / "tx_candidates_overlay.png",
        find_topdown_reference_frame(args.reference_dataset_dir),
    )

    static_meta: Dict[str, object] = {
        "schema": "single_scene_static_bundle_v1",
        "reference_dataset_dir": str(args.reference_dataset_dir),
        "reference_export_dir": str(reference_export_dir),
        "scene_signature": scene_signature(reference_scene_meta),
        "tx_search_config": asdict(tx_config),
        "tx_search_semantics": {
            "used_live_carla_map": bool(carla_map is not None),
            "require_sidewalk_lane": bool(tx_config.require_sidewalk_lane),
            "allow_offline_tx_search": bool(args.allow_offline_tx_search),
            "carla_host": str(args.carla_host),
            "carla_port": int(args.carla_port),
        },
        "rss_config": {
            "resolution": int(args.rss_resolution),
            "tx_height_m": float(args.tx_height),
            "rx_height_m": float(args.rx_height),
            "frequency_hz": float(args.frequency),
            "bandwidth_hz": float(args.bandwidth),
            "tx_power_dbm": float(args.tx_power_dbm),
            "max_depth": int(args.max_depth),
            "num_samples": int(args.num_samples),
            "num_runs": int(args.num_runs),
        },
    }

    for tx in tx_selection["tx_catalog"]:
        tx_dir = static_dir / tx["tx_id"]
        ensure_dir(tx_dir)
        rss_dir = tx_dir / "rss_static"
        run_static_baseline_for_tx(args, reference_export_dir, tx, rss_dir)
        with np.load(rss_dir / "rss_maps.npz") as data:
            rss_dbm = np.asarray(data["rss_dbm"], dtype=np.float32)
        if rss_dbm.shape[0] < 1:
            raise RuntimeError(f"Static RSS output for {tx['tx_id']} is empty.")
        np.save(tx_dir / "static_rss_dbm.npy", rss_dbm[0])
        shutil.copy2(rss_dir / "rss_heatmap_meta.json", tx_dir / "rss_heatmap_meta.json")

    save_json(static_dir / "scene_static_meta.json", static_meta)
    print(f"[OK] Prepared scene-static bundle at: {static_dir}")
    return 0


def finalize_index(dataset_root: Path, split_seed: int) -> int:
    episodes_dir = dataset_root / "episodes"
    rows = []
    accepted_episode_ids = []
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "qa_report.json"
        meta_path = episode_dir / "episode_meta.json"
        if not qa_path.exists() or not meta_path.exists():
            continue
        qa_report = load_json(qa_path)
        episode_meta = load_json(meta_path)
        tx_reports = qa_report.get("tx_reports", [])
        frame_count = int(episode_meta.get("frame_count", 0))
        episode_rows = summarize_episode_for_index(episode_dir.name, episode_meta, qa_report["scene_qa"], tx_reports, frame_count)
        rows.extend(row for row in episode_rows if bool(row.get("accepted")))
        if qa_report.get("accepted_tx_ids"):
            accepted_episode_ids.append(episode_dir.name)
    write_jsonl(dataset_root / "episode_index.jsonl", rows)
    save_json(dataset_root / "splits.json", deterministic_split(sorted(set(accepted_episode_ids)), seed=split_seed))
    print(f"[OK] Wrote dataset index: {dataset_root / 'episode_index.jsonl'}")
    print(f"[OK] Wrote splits: {dataset_root / 'splits.json'}")
    return 0


def collect_dataset(args: argparse.Namespace) -> int:
    raise RuntimeError(
        "collect-dataset is deprecated for formal runs. "
        "Use python3 scripts/drd.py collect with a generated plan_catalog instead. "
        "prepare-scene, process-episode, and finalize-index remain supported here."
    )


def main() -> int:
    args = parse_args()
    args.dataset_root = args.dataset_root.resolve()
    ensure_dir(args.dataset_root)
    if args.command == "prepare-scene":
        return prepare_scene(args)
    if args.command == "process-episode":
        return process_episode(args)
    if args.command == "collect-dataset":
        return collect_dataset(args)
    if args.command == "finalize-index":
        return finalize_index(args.dataset_root.resolve(), split_seed=int(args.split_seed))
    raise ValueError(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
