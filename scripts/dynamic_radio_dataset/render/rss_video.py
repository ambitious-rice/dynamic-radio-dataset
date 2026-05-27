#!/usr/bin/env python3
"""
Render a dynamic Sionna coverage-map/RSS heatmap video.

The script loads a CARLA-to-Sionna export, keeps buildings and vehicles in the
Sionna scene, places one transmitter near an edge of the valid crop, computes a
128x128 RSS coverage map over the valid crop for selected motion frames, and
writes both numeric arrays and a top-down heatmap video.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import warnings
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from dynamic_radio_dataset.sionna.rt_compat import (
    compute_radio_map_rss_watt,
    configure_scene_arrays,
    configure_scene_frequency,
    load_scene_preserving_names,
    make_backend_info,
    radio_map_request_from_region,
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a dynamic Sionna RSS heatmap video.")
    parser.add_argument("--export-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo/sionna_export"))
    parser.add_argument("--scene-file", type=str, default="scene.xml")
    parser.add_argument("--motion-file", type=str, default="motion.jsonl")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument("--output-video", type=Path, default=None)
    parser.add_argument("--frames-dir", type=Path, default=None)
    parser.add_argument(
        "--reuse-rss-dir",
        type=Path,
        default=None,
        help=(
            "Optional existing RSS output directory containing rss_maps.npz and optional frame_stats.jsonl. "
            "When provided, skip Sionna RT recomputation and render frames/video from the cached RSS arrays."
        ),
    )
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--end-frame", type=int, default=-1, help="-1 means through the final motion frame.")
    parser.add_argument("--stride", type=int, default=5)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--resolution", type=int, default=128, help="RSS grid resolution per axis.")
    parser.add_argument("--figure-dpi", type=int, default=120)
    parser.add_argument("--frequency", type=float, default=5.89e9)
    parser.add_argument("--bandwidth", type=float, default=10e6)
    parser.add_argument("--tx-power-dbm", type=float, default=23.0)
    parser.add_argument("--tx-height", type=float, default=1.5)
    parser.add_argument("--rx-height", type=float, default=1.5)
    parser.add_argument(
        "--rx-region",
        choices=["valid_crop", "support_region"],
        default="valid_crop",
        help="Region where RSS grid is sampled. valid_crop is default for dataset labels; support_region is for showcase/debug.",
    )
    parser.add_argument(
        "--tx-placement",
        choices=["north_edge", "south_edge", "east_edge", "west_edge", "custom"],
        default="north_edge",
        help="Default places the TX just outside the top edge of the valid crop.",
    )
    parser.add_argument("--tx-edge-offset", type=float, default=4.0)
    parser.add_argument("--tx-x", type=float, default=None)
    parser.add_argument("--tx-y", type=float, default=None)
    parser.add_argument("--tx-z", type=float, default=None)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--num-samples", type=int, default=50000)
    parser.add_argument("--num-runs", type=int, default=1)
    parser.add_argument(
        "--include-vehicles",
        type=str,
        default="",
        help="Comma-separated vehicle objects to keep in propagation, e.g. 'car_58,59'. Empty keeps all cars.",
    )
    parser.add_argument(
        "--exclude-vehicles",
        type=str,
        default="",
        help="Comma-separated vehicle objects to hide from propagation.",
    )
    parser.add_argument(
        "--allow-zero-vehicles",
        action="store_true",
        help="Allow an empty selected vehicle set. Useful for static no-vehicle baseline RSS maps.",
    )
    parser.add_argument("--no-los", action="store_true")
    parser.add_argument("--no-reflection", action="store_true")
    parser.add_argument("--diffraction", action="store_true")
    parser.add_argument("--scattering", action="store_true")
    parser.add_argument("--edge-diffraction", action="store_true")
    parser.add_argument("--mask-building-cells", action="store_true", default=True)
    parser.add_argument("--no-mask-building-cells", action="store_false", dest="mask_building_cells")
    parser.add_argument("--require-buildings", action="store_true", default=True)
    parser.add_argument("--allow-missing-buildings", action="store_false", dest="require_buildings")
    parser.add_argument("--vmin-dbm", type=float, default=-115.0)
    parser.add_argument("--vmax-dbm", type=float, default=-35.0)
    parser.add_argument("--require-dynamic-rss-change", action="store_true", default=True)
    parser.add_argument("--allow-flat-rss", action="store_false", dest="require_dynamic_rss_change")
    parser.add_argument("--min-p95-temporal-range-db", type=float, default=1.0)
    parser.add_argument("--min-max-temporal-range-db", type=float, default=4.0)
    parser.add_argument("--plot-style", choices=["annotated", "clean"], default="annotated")
    parser.add_argument(
        "--display-mode",
        choices=["absolute", "delta_first", "delta_median"],
        default="absolute",
        help="Visualization mode only. Raw rss_maps.npz is always absolute RSS.",
    )
    parser.add_argument("--view-region", choices=["support", "valid"], default=None)
    parser.add_argument("--hide-tx-marker", action="store_true")
    parser.add_argument("--vehicle-overlay", choices=["none", "light", "dark"], default=None)
    parser.add_argument("--interpolation", type=str, default=None)
    parser.add_argument("--clean-background-color", type=str, default=None)
    parser.add_argument("--building-facecolor", type=str, default=None)
    parser.add_argument("--building-edgecolor", type=str, default=None)
    parser.add_argument("--vehicle-facecolor", type=str, default=None)
    parser.add_argument("--vehicle-edgecolor", type=str, default=None)
    parser.add_argument("--tx-marker-color", type=str, default=None)
    parser.add_argument(
        "--visual-fill-threshold-dbm",
        type=float,
        default=-200.0,
        help="For plotting only, threshold used when --visual-fill is enabled.",
    )
    parser.add_argument(
        "--visual-fill",
        action="store_true",
        help="For plotting only, opt in to legacy nearest-fill of no-hit/low-RSS cells.",
    )
    parser.add_argument(
        "--no-visual-fill",
        action="store_true",
        help="Deprecated compatibility flag; visual fill is disabled unless --visual-fill is passed.",
    )
    parser.add_argument("--visual-smooth-sigma", type=float, default=0.0, help="For plotting only, Gaussian smoothing sigma in grid cells.")
    parser.add_argument(
        "--dynamic-stat-floor-dbm",
        type=float,
        default=-200.0,
        help="Ignore cells below this floor when computing temporal-range diagnostics.",
    )
    parser.add_argument("--auto-skip-unstable-start", action="store_true", default=True)
    parser.add_argument("--no-auto-skip-unstable-start", action="store_false", dest="auto_skip_unstable_start")
    parser.add_argument("--stability-window", type=int, default=3, help="Selected motion-frame window for startup stability checks.")
    parser.add_argument("--stability-max-abs-vz", type=float, default=0.5)
    parser.add_argument("--stability-max-z-range", type=float, default=0.15)
    parser.add_argument("--cmap", type=str, default="turbo")
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--keep-frames", action="store_true")
    parser.add_argument("--skip-existing-frames", action="store_true")
    parser.add_argument(
        "--skip-frame-rendering",
        action="store_true",
        help="Generate rss_maps.npz and metadata only; skip PNG/MP4 rendering for dataset collection.",
    )
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_motion(path: Path) -> Iterable[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def select_motion_frames(path: Path, start: int, end: int, stride: int) -> List[Dict[str, object]]:
    frames: List[Dict[str, object]] = []
    for row in iter_motion(path):
        frame_index = int(row["frame_index"])
        if frame_index < start:
            continue
        if end >= 0 and frame_index > end:
            break
        if (frame_index - start) % stride == 0:
            frames.append(row)
    if not frames:
        raise RuntimeError(f"No motion frames selected from {path}")
    return frames


def ensure_empty_dir(path: Path, keep_existing: bool) -> None:
    if path.exists() and not keep_existing:
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg
    raise RuntimeError("ffmpeg not found. Install imageio-ffmpeg in the active environment.")


def encode_video(frames_dir: Path, output_video: Path, fps: float) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        find_ffmpeg(),
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frames_dir / "frame_%06d.png"),
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(output_video),
    ]
    subprocess.run(cmd, check=True)


def normalize_car_name(token: str) -> str:
    token = token.strip()
    if not token:
        return token
    if token.startswith("car_"):
        return token
    if token.isdigit():
        return f"car_{token}"
    return token


def parse_car_set(text: str) -> set[str]:
    if not text.strip():
        return set()
    return {normalize_car_name(item) for item in text.split(",") if item.strip()}


def select_car_names(car_names: Sequence[str], args: argparse.Namespace) -> List[str]:
    available = set(car_names)
    include = parse_car_set(args.include_vehicles)
    exclude = parse_car_set(args.exclude_vehicles)
    if include:
        selected = include & available
        missing = sorted(include - available)
        if missing:
            print(f"[WARN] Requested vehicles not found in scene and will be ignored: {missing}")
    else:
        selected = set(available)
    selected -= exclude
    if not selected:
        if args.allow_zero_vehicles:
            return []
        raise RuntimeError("Vehicle filtering removed all dynamic cars. Check --include-vehicles/--exclude-vehicles.")
    return sorted(selected, key=lambda item: int(item.split("_")[1]))


def motion_window_stability(
    rows: Sequence[Dict[str, object]],
    selected_car_names: set[str],
) -> Dict[str, object]:
    by_name: Dict[str, List[Tuple[float, float]]] = {}
    for row in rows:
        for vehicle in row["vehicles"]:
            name = str(vehicle["sionna_object"])
            if name not in selected_car_names:
                continue
            position = vehicle.get("position", [0.0, 0.0, 0.0])
            velocity = vehicle.get("velocity", [0.0, 0.0, 0.0])
            by_name.setdefault(name, []).append((float(position[2]), float(velocity[2])))

    max_abs_vz = 0.0
    max_z_range = 0.0
    for values in by_name.values():
        if not values:
            continue
        zs = [item[0] for item in values]
        vzs = [item[1] for item in values]
        max_abs_vz = max(max_abs_vz, max(abs(vz) for vz in vzs))
        max_z_range = max(max_z_range, max(zs) - min(zs))
    return {
        "vehicle_count": len(by_name),
        "max_abs_vz_mps": float(max_abs_vz),
        "max_z_range_m": float(max_z_range),
    }


def skip_unstable_start_rows(
    rows: Sequence[Dict[str, object]],
    selected_car_names: set[str],
    args: argparse.Namespace,
) -> Tuple[List[Dict[str, object]], Dict[str, object]]:
    if not selected_car_names:
        return list(rows), {
            "enabled": bool(args.auto_skip_unstable_start),
            "skipped_count": 0,
            "reason": "no_selected_vehicles",
        }
    if not args.auto_skip_unstable_start:
        return list(rows), {"enabled": False, "skipped_count": 0}

    window = max(1, int(args.stability_window))
    if len(rows) < window:
        return list(rows), {"enabled": True, "skipped_count": 0, "reason": "not_enough_rows", "window": window}

    last_stats: Dict[str, object] = {}
    for start_index in range(0, len(rows) - window + 1):
        stats = motion_window_stability(rows[start_index : start_index + window], selected_car_names)
        last_stats = stats
        if (
            float(stats["max_abs_vz_mps"]) <= float(args.stability_max_abs_vz)
            and float(stats["max_z_range_m"]) <= float(args.stability_max_z_range)
        ):
            return list(rows[start_index:]), {
                "enabled": True,
                "skipped_count": start_index,
                "window": window,
                "thresholds": {
                    "max_abs_vz_mps": float(args.stability_max_abs_vz),
                    "max_z_range_m": float(args.stability_max_z_range),
                },
                "first_kept_frame": int(rows[start_index]["frame_index"]),
                "stability_stats": stats,
            }

    return [rows[-1]], {
        "enabled": True,
        "skipped_count": max(0, len(rows) - 1),
        "window": window,
        "thresholds": {
            "max_abs_vz_mps": float(args.stability_max_abs_vz),
            "max_z_range_m": float(args.stability_max_z_range),
        },
        "first_kept_frame": int(rows[-1]["frame_index"]),
        "stability_stats": last_stats,
        "warning": "No stable startup window was found; kept only the last selected row.",
    }


def region_axes(region: Dict[str, object]) -> Tuple[np.ndarray, np.ndarray]:
    yaw = math.radians(float(region.get("yaw_deg", 0.0)))
    x_axis = np.array([math.cos(yaw), math.sin(yaw)], dtype=np.float64)
    y_axis = np.array([-math.sin(yaw), math.cos(yaw)], dtype=np.float64)
    return x_axis, y_axis


def region_center(region: Dict[str, object]) -> np.ndarray:
    center = region["center"]
    return np.array([float(center["x"]), float(center["y"])], dtype=np.float64)


def region_size(region: Dict[str, object]) -> Tuple[float, float]:
    return float(region["width_m"]), float(region["height_m"])


def tx_position_from_args(args: argparse.Namespace, valid_crop: Dict[str, object]) -> List[float]:
    center = region_center(valid_crop)
    width, height = region_size(valid_crop)
    x_axis, y_axis = region_axes(valid_crop)

    if args.tx_placement == "custom":
        if args.tx_x is None or args.tx_y is None:
            raise ValueError("--tx-placement custom requires --tx-x and --tx-y")
        return [
            float(args.tx_x),
            float(args.tx_y),
            float(args.tx_z if args.tx_z is not None else args.tx_height),
        ]

    if args.tx_placement == "north_edge":
        xy = center + y_axis * (height / 2.0 + args.tx_edge_offset)
    elif args.tx_placement == "south_edge":
        xy = center - y_axis * (height / 2.0 + args.tx_edge_offset)
    elif args.tx_placement == "east_edge":
        xy = center + x_axis * (width / 2.0 + args.tx_edge_offset)
    elif args.tx_placement == "west_edge":
        xy = center - x_axis * (width / 2.0 + args.tx_edge_offset)
    else:
        raise ValueError(f"Unsupported TX placement: {args.tx_placement}")

    return [float(xy[0]), float(xy[1]), float(args.tx_z if args.tx_z is not None else args.tx_height)]


def region_extent_for_plot(region: Dict[str, object]) -> Tuple[float, float, float, float]:
    center = region_center(region)
    width, height = region_size(region)
    if abs(float(region.get("yaw_deg", 0.0))) > 1e-6:
        raise ValueError("Plot extent currently assumes axis-aligned valid crops.")
    return (
        float(center[0] - width / 2.0),
        float(center[0] + width / 2.0),
        float(center[1] - height / 2.0),
        float(center[1] + height / 2.0),
    )


def cell_centers_from_region(region: Dict[str, object], resolution: int) -> np.ndarray:
    center = region_center(region)
    width, height = region_size(region)
    x_axis, y_axis = region_axes(region)
    xs = (np.arange(resolution, dtype=np.float64) + 0.5) / resolution * width - width / 2.0
    ys = (np.arange(resolution, dtype=np.float64) + 0.5) / resolution * height - height / 2.0
    xx, yy = np.meshgrid(xs, ys)
    flat = center[None, :] + xx.reshape(-1, 1) * x_axis[None, :] + yy.reshape(-1, 1) * y_axis[None, :]
    return flat.reshape(resolution, resolution, 2)


def building_mask_from_manifest(
    manifest: Dict[str, object],
    valid_crop: Dict[str, object],
    resolution: int,
) -> np.ndarray:
    buildings = manifest.get("buildings") or []
    if not buildings:
        return np.zeros((resolution, resolution), dtype=bool)

    centers = cell_centers_from_region(valid_crop, resolution).reshape(-1, 2)
    mask = np.zeros((centers.shape[0],), dtype=bool)
    for building in buildings:
        center = building["center"]
        extent = building["extent"]
        rotation = building.get("rotation", {})
        cx = float(center["x"])
        cy = float(center["y"])
        ex = float(extent["x"])
        ey = float(extent["y"])
        yaw = math.radians(float(rotation.get("yaw", 0.0)))
        c = math.cos(-yaw)
        s = math.sin(-yaw)
        dx = centers[:, 0] - cx
        dy = centers[:, 1] - cy
        local_x = c * dx - s * dy
        local_y = s * dx + c * dy
        mask |= (np.abs(local_x) <= ex) & (np.abs(local_y) <= ey)
    return mask.reshape(resolution, resolution)


def rectangle_corners(
    center_xy: Sequence[float],
    extent_xy: Sequence[float],
    yaw_deg: float,
) -> np.ndarray:
    ex, ey = float(extent_xy[0]), float(extent_xy[1])
    corners = np.array(
        [
            [-ex, -ey],
            [ex, -ey],
            [ex, ey],
            [-ex, ey],
            [-ex, -ey],
        ],
        dtype=np.float64,
    )
    yaw = math.radians(float(yaw_deg))
    rot = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]], dtype=np.float64)
    return corners @ rot.T + np.array(center_xy, dtype=np.float64)[None, :]


def dbm_from_watt(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    safe = np.maximum(values, 1e-30)
    return 10.0 * np.log10(safe) + 30.0


def apply_motion_frame(
    scene,
    car_names: Sequence[str],
    row: Dict[str, object],
    hidden_position: Sequence[float],
    selected_car_names: set[str],
) -> List[Dict[str, object]]:
    present = set()
    vehicles: List[Dict[str, object]] = []
    for vehicle in row["vehicles"]:
        name = str(vehicle["sionna_object"])
        if name not in selected_car_names:
            continue
        obj = scene.get(name)
        if obj is None:
            continue
        obj.position = vehicle["position"]
        obj.orientation = [vehicle["yaw_rad_sionna_like"], 0.0, 0.0]
        obj.velocity = vehicle["velocity"]
        present.add(name)
        vehicles.append(vehicle)

    for name in car_names:
        if name in present:
            continue
        obj = scene.get(name)
        if obj is not None:
            obj.position = hidden_position
    return vehicles


def compute_rss_map(scene, args: argparse.Namespace, rx_region: Dict[str, object]) -> np.ndarray:
    request = radio_map_request_from_region(
        rx_region,
        resolution=int(args.resolution),
        rx_height=float(args.rx_height),
        max_depth=int(args.max_depth),
        num_samples=int(args.num_samples),
        num_runs=int(args.num_runs),
        los=not args.no_los,
        reflection=not args.no_reflection,
        diffraction=bool(args.diffraction),
        scattering=bool(args.scattering),
        edge_diffraction=bool(args.edge_diffraction),
    )
    rss_watt = compute_radio_map_rss_watt(scene, request)
    if rss_watt.ndim != 3 or rss_watt.shape[0] < 1:
        raise RuntimeError(f"Unexpected RSS tensor shape: {rss_watt.shape}")
    return dbm_from_watt(rss_watt[0])


def prepare_plot_values(
    display_db: np.ndarray,
    building_mask: Optional[np.ndarray],
    args: argparse.Namespace,
) -> np.ndarray:
    plot_values = np.array(display_db, dtype=np.float64)
    building = np.zeros(plot_values.shape, dtype=bool) if building_mask is None else building_mask.astype(bool)

    if args.plot_style == "clean":
        finite = np.isfinite(plot_values)
        source = finite & (~building) & (plot_values > float(args.visual_fill_threshold_dbm))
        fill_target = (~building) & (~source)
        if visual_fill_enabled(args) and np.any(fill_target) and np.any(source):
            try:
                from scipy.ndimage import distance_transform_edt

                indices = distance_transform_edt(~source, return_distances=False, return_indices=True)
                nearest = plot_values[tuple(indices)]
                plot_values[fill_target] = nearest[fill_target]
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Visual no-hit fill failed and will be skipped: {exc}")

        if float(args.visual_smooth_sigma) > 0.0:
            try:
                from scipy.ndimage import gaussian_filter

                valid = np.isfinite(plot_values) & (~building)
                weights = valid.astype(np.float64)
                weighted_values = np.where(valid, plot_values, 0.0)
                numerator = gaussian_filter(weighted_values, sigma=float(args.visual_smooth_sigma), mode="nearest")
                denominator = gaussian_filter(weights, sigma=float(args.visual_smooth_sigma), mode="nearest")
                smoothed = np.divide(numerator, denominator, out=np.array(plot_values), where=denominator > 1e-6)
                plot_values[valid] = smoothed[valid]
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] Visual smoothing failed and will be skipped: {exc}")

    if building_mask is not None:
        plot_values = plot_values.copy()
        plot_values[building_mask] = np.nan
    return plot_values


def visual_fill_enabled(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "visual_fill", False)) and not bool(getattr(args, "no_visual_fill", False))


def build_display_stack(rss_stack: np.ndarray, args: argparse.Namespace) -> np.ndarray:
    values = np.array(rss_stack, dtype=np.float64)
    if args.display_mode == "absolute":
        return values
    if args.display_mode == "delta_first":
        return values - values[0:1]
    if args.display_mode == "delta_median":
        baseline = np.nanmedian(values, axis=0, keepdims=True)
        return values - baseline
    raise ValueError(f"Unsupported display mode: {args.display_mode}")


def resolve_overlay_colors(args: argparse.Namespace, vehicle_overlay: str) -> Dict[str, object]:
    if args.plot_style == "clean":
        background_color = args.clean_background_color or "#210a33"
        building_face = args.building_facecolor or "#171b35"
        building_edge = args.building_edgecolor or building_face
        if vehicle_overlay == "dark":
            vehicle_face = args.vehicle_facecolor or "#171b35"
            vehicle_edge = args.vehicle_edgecolor or vehicle_face
            vehicle_linewidth = 0.0
            vehicle_alpha = 0.96
        else:
            vehicle_face = args.vehicle_facecolor or "#fff4cf"
            vehicle_edge = args.vehicle_edgecolor or "#7a3d00"
            vehicle_linewidth = 0.6
            vehicle_alpha = 0.94
        tx_color = args.tx_marker_color or "#f6f74d"
        return {
            "background_color": background_color,
            "building_facecolor": building_face,
            "building_edgecolor": building_edge,
            "building_linewidth": 0.0,
            "building_alpha": 0.82,
            "vehicle_facecolor": vehicle_face,
            "vehicle_edgecolor": vehicle_edge,
            "vehicle_linewidth": vehicle_linewidth,
            "vehicle_alpha": vehicle_alpha,
            "tx_marker_color": tx_color,
            "nan_color": background_color,
        }

    background_color = "#f4f4f2"
    building_face = args.building_facecolor or "#303030"
    building_edge = args.building_edgecolor or "#101010"
    if vehicle_overlay == "dark":
        vehicle_face = args.vehicle_facecolor or "#3f7194"
        vehicle_edge = args.vehicle_edgecolor or "#163145"
    else:
        vehicle_face = args.vehicle_facecolor or "#ffffff"
        vehicle_edge = args.vehicle_edgecolor or "#111111"
    return {
        "background_color": background_color,
        "building_facecolor": building_face,
        "building_edgecolor": building_edge,
        "building_linewidth": 0.35,
        "building_alpha": 0.42,
        "vehicle_facecolor": vehicle_face,
        "vehicle_edgecolor": vehicle_edge,
        "vehicle_linewidth": 0.45,
        "vehicle_alpha": 0.88,
        "tx_marker_color": args.tx_marker_color or "#ff2020",
        "nan_color": building_face,
    }


def draw_heatmap_frame(
    frame_path: Path,
    display_db: np.ndarray,
    frame_index: int,
    tx_position: Sequence[float],
    manifest: Dict[str, object],
    moving_vehicles: Sequence[Dict[str, object]],
    building_mask: Optional[np.ndarray],
    rx_region: Dict[str, object],
    args: argparse.Namespace,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon, Rectangle

    support_region = manifest["support_region"]
    extent = region_extent_for_plot(rx_region)
    support_extent = region_extent_for_plot(support_region)
    view_region = args.view_region or ("valid" if args.plot_style == "clean" else "support")
    view_extent = extent if view_region == "valid" else support_extent
    interpolation = args.interpolation or "nearest"
    vehicle_overlay = args.vehicle_overlay or ("dark" if args.plot_style == "clean" else "light")
    palette = resolve_overlay_colors(args, vehicle_overlay)

    plot_values = prepare_plot_values(display_db, building_mask, args)

    if args.plot_style == "clean":
        bg_color = str(palette["background_color"])
        fig, ax = plt.subplots(figsize=(6.4, 6.4), dpi=args.figure_dpi)
        fig.patch.set_facecolor(bg_color)
        ax.set_facecolor(bg_color)
    else:
        fig, ax = plt.subplots(figsize=(7.0, 7.0), dpi=args.figure_dpi)
        fig.patch.set_facecolor("white")
        ax.set_facecolor("#f4f4f2")

    if args.plot_style != "clean":
        support_rect = Rectangle(
            (support_extent[0], support_extent[2]),
            support_extent[1] - support_extent[0],
            support_extent[3] - support_extent[2],
            fill=False,
            linewidth=0.8,
            linestyle="--",
            edgecolor="#808080",
            alpha=0.7,
        )
        ax.add_patch(support_rect)

    cmap = plt.get_cmap(args.cmap).copy()
    cmap.set_bad(str(palette["nan_color"]))

    image = ax.imshow(
        plot_values,
        extent=extent,
        origin="lower",
        cmap=cmap,
        vmin=args.vmin_dbm,
        vmax=args.vmax_dbm,
        interpolation=interpolation,
        alpha=1.0 if args.plot_style == "clean" else 0.96,
    )

    for building in manifest.get("buildings") or []:
        center = building["center"]
        extent_b = building["extent"]
        rotation = building.get("rotation", {})
        corners = rectangle_corners(
            [float(center["x"]), float(center["y"])],
            [float(extent_b["x"]), float(extent_b["y"])],
            float(rotation.get("yaw", 0.0)),
        )
        ax.add_patch(
            Polygon(
                corners,
                closed=True,
                facecolor=str(palette["building_facecolor"]),
                edgecolor=str(palette["building_edgecolor"]),
                linewidth=float(palette["building_linewidth"]),
                alpha=float(palette["building_alpha"]),
            )
        )

    if vehicle_overlay != "none":
        for vehicle in moving_vehicles:
            pos = vehicle["position"]
            bbox = vehicle.get("bbox_extent", {})
            ref_yaw = float(vehicle.get("mesh_reference_yaw_deg", 0.0))
            yaw_delta = float(vehicle.get("yaw_delta_deg_sionna", 0.0))
            yaw = ref_yaw + yaw_delta
            extent_v = [float(bbox.get("x", 1.8)), float(bbox.get("y", 0.9))]
            corners = rectangle_corners([float(pos[0]), float(pos[1])], extent_v, yaw)
            ax.add_patch(
                Polygon(
                    corners,
                    closed=True,
                    facecolor=str(palette["vehicle_facecolor"]),
                    edgecolor=str(palette["vehicle_edgecolor"]),
                    linewidth=float(palette["vehicle_linewidth"]),
                    alpha=float(palette["vehicle_alpha"]),
                )
            )

    if not args.hide_tx_marker:
        if args.plot_style == "clean":
            ax.scatter(
                [tx_position[0]],
                [tx_position[1]],
                marker="o",
                s=28,
                c=str(palette["tx_marker_color"]),
                edgecolors="none",
                zorder=10,
            )
        else:
            ax.scatter(
                [tx_position[0]],
                [tx_position[1]],
                marker="*",
                s=150,
                c=str(palette["tx_marker_color"]),
                edgecolors="white",
                linewidths=0.8,
                zorder=10,
            )
            ax.text(
                tx_position[0],
                tx_position[1] + 1.7,
                "TX",
                ha="center",
                va="bottom",
                color=str(palette["tx_marker_color"]),
                fontsize=9,
                weight="bold",
            )

    ax.set_xlim(view_extent[0], view_extent[1])
    ax.set_ylim(view_extent[2], view_extent[3])
    ax.set_aspect("equal", adjustable="box")
    if args.plot_style == "clean":
        ax.set_axis_off()
        fig.subplots_adjust(left=0.0, right=1.0, bottom=0.0, top=1.0)
    else:
        if args.display_mode == "absolute":
            title_mode = "RSS"
            cbar_label = "RSS [dBm]"
        elif args.display_mode == "delta_first":
            title_mode = "Delta RSS (vs first)"
            cbar_label = "RSS change [dB]"
        else:
            title_mode = "Delta RSS (vs temporal median)"
            cbar_label = "RSS change [dB]"
        ax.set_title(f"Sionna {title_mode} | frame {frame_index}", fontsize=11)
        ax.set_xlabel("CARLA world x [m]")
        ax.set_ylabel("CARLA world y [m]")
        cbar = fig.colorbar(image, ax=ax, fraction=0.046, pad=0.03)
        cbar.set_label(cbar_label)
        fig.tight_layout()
    frame_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(frame_path)
    plt.close(fig)


def summarize_dynamic_rss_change(
    rss_stack: np.ndarray,
    building_mask: Optional[np.ndarray],
    floor_dbm: float,
) -> Dict[str, object]:
    values = np.array(rss_stack, dtype=np.float64)
    if building_mask is not None:
        values[:, building_mask] = np.nan
    values[values <= float(floor_dbm)] = np.nan
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", category=RuntimeWarning, message="All-NaN slice encountered")
        temporal_range = np.nanmax(values, axis=0) - np.nanmin(values, axis=0)
    finite = np.isfinite(temporal_range)
    finite_values = temporal_range[finite]
    if finite_values.size == 0:
        return {
            "finite_cell_count": 0,
            "p50_temporal_range_db": None,
            "p90_temporal_range_db": None,
            "p95_temporal_range_db": None,
            "p99_temporal_range_db": None,
            "max_temporal_range_db": None,
            "cells_over_1db": 0,
            "cells_over_3db": 0,
            "cells_over_6db": 0,
        }
    return {
        "finite_cell_count": int(finite_values.size),
        "p50_temporal_range_db": float(np.percentile(finite_values, 50)),
        "p90_temporal_range_db": float(np.percentile(finite_values, 90)),
        "p95_temporal_range_db": float(np.percentile(finite_values, 95)),
        "p99_temporal_range_db": float(np.percentile(finite_values, 99)),
        "max_temporal_range_db": float(np.max(finite_values)),
        "cells_over_1db": int(np.sum(finite_values > 1.0)),
        "cells_over_3db": int(np.sum(finite_values > 3.0)),
        "cells_over_6db": int(np.sum(finite_values > 6.0)),
    }


def build_rows_from_frame_indices(motion_path: Path, frame_indices: Sequence[int]) -> List[Dict[str, object]]:
    wanted = {int(item) for item in frame_indices}
    rows_by_index = {int(row["frame_index"]): row for row in iter_motion(motion_path) if int(row["frame_index"]) in wanted}
    missing = [int(item) for item in frame_indices if int(item) not in rows_by_index]
    if missing:
        raise RuntimeError(f"Missing motion rows for cached RSS frame indices: {missing[:10]}")
    return [rows_by_index[int(item)] for item in frame_indices]


def load_stats_rows(path: Path) -> List[Dict[str, object]]:
    rows: List[Dict[str, object]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                rows.append(json.loads(line))
    return rows


def main() -> int:
    args = parse_args()
    if args.reuse_rss_dir is None:
        print(
            "[WARN] render.rss_video fresh Sionna RSS computation is deprecated; "
            "formal RF generation uses dynamic_radio_dataset.rf.rss_compute."
        )
    if args.display_mode != "absolute" and args.cmap == "turbo":
        args.cmap = "coolwarm"
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("MI_DEFAULT_VARIANT", args.mitsuba_variant)
    if not args.use_gpu:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    export_dir = args.export_dir
    scene_path = export_dir / args.scene_file
    motion_path = export_dir / args.motion_file
    manifest_path = export_dir / "manifest.json"
    if not scene_path.exists():
        raise FileNotFoundError(scene_path)
    if not motion_path.exists():
        raise FileNotFoundError(motion_path)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)

    manifest = load_json(manifest_path)
    static_geometry = manifest.get("static_geometry", {})
    building_count = int(static_geometry.get("building_count", len(manifest.get("buildings") or []))) if isinstance(static_geometry, dict) else len(manifest.get("buildings") or [])
    if args.require_buildings and building_count <= 0:
        raise RuntimeError(
            "No exported building geometry found. Re-run export_sionna_scene_from_carla.py with --include-buildings "
            "or pass --allow-missing-buildings for a debug-only run."
        )

    valid_crop = manifest["valid_crop"]
    support_region = manifest["support_region"]
    rx_region = manifest[args.rx_region]

    output_dir = args.output_dir or (export_dir / "rss_heatmap")
    frames_dir = args.frames_dir or (output_dir / "frames")
    output_video = args.output_video or (output_dir / "rss_heatmap_video.mp4")
    reuse_rss_dir = args.reuse_rss_dir.resolve() if args.reuse_rss_dir is not None else None
    reuse_meta: Optional[Dict[str, object]] = None
    if reuse_rss_dir is not None:
        rss_npz_path = reuse_rss_dir / "rss_maps.npz"
        if not rss_npz_path.exists():
            raise FileNotFoundError(rss_npz_path)
        reuse_meta_path = reuse_rss_dir / "rss_heatmap_meta.json"
        if reuse_meta_path.exists():
            reuse_meta = load_json(reuse_meta_path)

    if reuse_meta is not None and args.tx_placement != "custom" and args.tx_x is None and args.tx_y is None:
        reused_tx = ((reuse_meta.get("tx") or {}).get("position")) if isinstance(reuse_meta.get("tx"), dict) else None
        if reused_tx is not None:
            tx_position = [float(reused_tx[0]), float(reused_tx[1]), float(reused_tx[2])]
        else:
            tx_position = tx_position_from_args(args, valid_crop)
    else:
        tx_position = tx_position_from_args(args, valid_crop)

    output_dir.mkdir(parents=True, exist_ok=True)
    if not args.skip_frame_rendering:
        ensure_empty_dir(frames_dir, keep_existing=args.keep_frames or args.skip_existing_frames)

    if reuse_rss_dir is not None:
        with np.load(reuse_rss_dir / "rss_maps.npz") as data:
            rss_stack = np.asarray(data["rss_dbm"], dtype=np.float32)
            frame_indices = [int(item) for item in data["frame_indices"].tolist()]
            if rss_stack.ndim != 3:
                raise RuntimeError(f"Cached RSS stack must have shape [T,H,W], got {rss_stack.shape}")
            cached_resolution = int(rss_stack.shape[-1])
            if cached_resolution != int(args.resolution):
                print(
                    f"[WARN] Overriding requested resolution {args.resolution} with cached RSS resolution {cached_resolution}."
                )
                args.resolution = cached_resolution
            if args.mask_building_cells and "building_mask" in data:
                building_mask = np.asarray(data["building_mask"], dtype=np.uint8).astype(bool)
            else:
                building_mask = building_mask_from_manifest(manifest, rx_region, args.resolution) if args.mask_building_cells else None
        rows = build_rows_from_frame_indices(motion_path, frame_indices)
        moving_vehicles_rows = [list(row.get("vehicles", [])) for row in rows]
        car_names = sorted({str(vehicle["sionna_object"]) for row in rows for vehicle in row.get("vehicles", [])}, key=lambda item: int(item.split("_")[1]))
        if args.include_vehicles.strip() or args.exclude_vehicles.strip():
            print("[WARN] --include-vehicles/--exclude-vehicles are ignored when --reuse-rss-dir is used.")
        selected_car_names = car_names
        stability_filter = reuse_meta.get("startup_stability_filter", {}) if reuse_meta is not None else {
            "enabled": False,
            "reused_cached_rss": True,
        }
        stats_rows = load_stats_rows(reuse_rss_dir / "frame_stats.jsonl")
        print(f"[INFO] Reusing cached RSS stack from: {reuse_rss_dir}")
        print(f"[INFO] Buildings in propagation scene: {building_count}")
        print(f"[INFO] Cars represented in cached RSS: {len(selected_car_names)}")
        sionna_backend_info: Dict[str, object] = (
            ((reuse_meta.get("propagation") or {}).get("sionna_backend") or {})
            if isinstance(reuse_meta, dict)
            else {}
        )
    else:
        from sionna.rt import PlanarArray, Transmitter
        import mitsuba as mi

        rows = select_motion_frames(motion_path, args.start_frame, args.end_frame, args.stride)
        building_mask = building_mask_from_manifest(manifest, rx_region, args.resolution) if args.mask_building_cells else None

        scene = load_scene_preserving_names(str(scene_path))
        configure_scene_frequency(scene, frequency=float(args.frequency), bandwidth=float(args.bandwidth))
        configure_scene_arrays(scene, planar_array_cls=PlanarArray)
        sionna_backend = make_backend_info(scene)
        sionna_backend_info = dict(sionna_backend.__dict__)
        scene.add(
            Transmitter(
                "tx_valid_edge",
                position=tx_position,
                power_dbm=float(args.tx_power_dbm),
                look_at=[
                    float(rx_region["center"]["x"]),
                    float(rx_region["center"]["y"]),
                    float(args.rx_height),
                ],
            )
        )

        object_names = list(scene.objects.keys())
        car_names = sorted(
            [name for name in object_names if name.startswith("car_")],
            key=lambda item: int(item.split("_")[1]),
        )
        selected_car_names = select_car_names(car_names, args)
        selected_car_set = set(selected_car_names)
        rows, stability_filter = skip_unstable_start_rows(rows, selected_car_set, args)
        if args.require_buildings and scene.get("static_buildings_proxy") is None:
            raise RuntimeError("Manifest has buildings, but scene object 'static_buildings_proxy' is missing.")

        center = region_center(support_region)
        width, height = region_size(support_region)
        hidden_position = [float(center[0] + 10.0 * width), float(center[1] + 10.0 * height), -1000.0]

        rss_maps: List[np.ndarray] = []
        frame_indices = []
        stats_rows = []
        moving_vehicles_rows = []
        print(f"[INFO] Loaded scene: {scene_path}")
        print(f"[INFO] Mitsuba variant: {mi.variant()}")
        print(f"[INFO] Sionna RT backend: {sionna_backend.package_version}, API={sionna_backend.radio_map_api}")
        print(f"[INFO] Buildings in propagation scene: {building_count}")
        print(f"[INFO] Cars in scene: {len(car_names)} total; {len(selected_car_names)} kept in propagation: {selected_car_names}")
        if stability_filter.get("enabled"):
            print(f"[INFO] Startup stability filter: {stability_filter}")
        print(f"[INFO] TX position: {tx_position}, power={args.tx_power_dbm} dBm")
        print(
            f"[INFO] RSS grid: {args.resolution}x{args.resolution} over {args.rx_region} "
            f"{region_size(rx_region)} m, display_mode={args.display_mode}"
        )
        print(f"[INFO] Selected motion frames: {len(rows)} (stride={args.stride})")

        for seq, row in enumerate(rows):
            frame_index = int(row["frame_index"])
            moving_vehicles = apply_motion_frame(scene, car_names, row, hidden_position, selected_car_set)
            moving_vehicles_rows.append(moving_vehicles)
            frame_path = frames_dir / f"frame_{seq:06d}.png"
            if (not args.skip_frame_rendering) and args.skip_existing_frames and frame_path.exists():
                rss_dbm = np.full((args.resolution, args.resolution), np.nan, dtype=np.float32)
            else:
                rss_dbm = compute_rss_map(scene, args, rx_region).astype(np.float32)
            rss_maps.append(rss_dbm)
            frame_indices.append(frame_index)
            finite = np.isfinite(rss_dbm)
            if building_mask is not None:
                finite &= ~building_mask
            values = rss_dbm[finite]
            stats = {
                "sequence_index": seq,
                "frame_index": frame_index,
                "vehicle_count": len(moving_vehicles),
                "rss_dbm_min": float(np.min(values)) if values.size else None,
                "rss_dbm_mean": float(np.mean(values)) if values.size else None,
                "rss_dbm_max": float(np.max(values)) if values.size else None,
            }
            stats_rows.append(stats)
            if seq == 0 or (seq + 1) % 5 == 0 or seq == len(rows) - 1:
                print(
                    "[INFO] RSS frame "
                    f"{seq + 1}/{len(rows)} (motion frame {frame_index}): "
                    f"min={stats['rss_dbm_min']:.1f} mean={stats['rss_dbm_mean']:.1f} max={stats['rss_dbm_max']:.1f} dBm"
                    if values.size
                    else f"[INFO] RSS frame {seq + 1}/{len(rows)} has no finite cells"
                )

        rss_stack = np.stack(rss_maps, axis=0)

    if not stats_rows or len(stats_rows) != len(frame_indices):
        stats_rows = []
        for seq, (frame_index, rss_dbm, moving_vehicles) in enumerate(zip(frame_indices, rss_stack, moving_vehicles_rows)):
            finite = np.isfinite(rss_dbm)
            if building_mask is not None:
                finite &= ~building_mask
            values = rss_dbm[finite]
            stats_rows.append(
                {
                    "sequence_index": seq,
                    "frame_index": int(frame_index),
                    "vehicle_count": len(moving_vehicles),
                    "rss_dbm_min": float(np.min(values)) if values.size else None,
                    "rss_dbm_mean": float(np.mean(values)) if values.size else None,
                    "rss_dbm_max": float(np.max(values)) if values.size else None,
                }
            )
    if reuse_rss_dir is not None:
        print(f"[INFO] TX position: {tx_position}, power={args.tx_power_dbm} dBm")
        print(
            f"[INFO] RSS grid: {args.resolution}x{args.resolution} over {args.rx_region} "
            f"{region_size(rx_region)} m, display_mode={args.display_mode}"
        )
        print(f"[INFO] Cached motion frames reused: {len(frame_indices)}")

    display_stack = build_display_stack(rss_stack, args).astype(np.float32)
    if not args.skip_frame_rendering:
        for seq, row in enumerate(rows):
            frame_path = frames_dir / f"frame_{seq:06d}.png"
            if args.skip_existing_frames and frame_path.exists():
                continue
            draw_heatmap_frame(
                frame_path=frame_path,
                display_db=display_stack[seq],
                frame_index=int(row["frame_index"]),
                tx_position=tx_position,
                manifest=manifest,
                moving_vehicles=moving_vehicles_rows[seq],
                building_mask=building_mask,
                rx_region=rx_region,
                args=args,
            )

    dynamic_summary = summarize_dynamic_rss_change(rss_stack, building_mask, args.dynamic_stat_floor_dbm)
    p95_range = dynamic_summary["p95_temporal_range_db"]
    max_range = dynamic_summary["max_temporal_range_db"]
    print(f"[INFO] Dynamic RSS temporal range summary: {dynamic_summary}")
    require_dynamic_change = bool(args.require_dynamic_rss_change and not args.allow_zero_vehicles)
    if require_dynamic_change:
        if p95_range is None or max_range is None:
            raise RuntimeError("No finite RSS cells were available for dynamic-change validation.")
        if float(p95_range) < args.min_p95_temporal_range_db and float(max_range) < args.min_max_temporal_range_db:
            raise RuntimeError(
                "RSS maps are too flat over time; vehicle-induced propagation change is not obvious. "
                f"p95 temporal range={p95_range:.2f} dB, max temporal range={max_range:.2f} dB. "
                "Try lowering TX/RX height, moving TX to another valid-crop edge, increasing selected frames, "
                "or collecting a clip where vehicles pass through the TX-to-grid paths. "
                "Pass --allow-flat-rss only for debugging."
            )

    np.savez_compressed(
        output_dir / "rss_maps.npz",
        rss_dbm=rss_stack,
        frame_indices=np.asarray(frame_indices, dtype=np.int32),
        tx_position=np.asarray(tx_position, dtype=np.float32),
        building_mask=(building_mask.astype(np.uint8) if building_mask is not None else np.zeros((args.resolution, args.resolution), dtype=np.uint8)),
    )
    with (output_dir / "frame_stats.jsonl").open("w", encoding="utf-8") as f:
        for stats in stats_rows:
            f.write(json.dumps(stats, ensure_ascii=False) + "\n")
    meta = {
        "schema": "sionna_dynamic_rss_heatmap_v1",
        "source_export_dir": str(export_dir),
        "reuse_rss_dir": str(reuse_rss_dir) if reuse_rss_dir is not None else None,
        "scene_xml": str(scene_path),
        "motion_jsonl": str(motion_path),
        "output_video": str(output_video),
        "rss_npz": str(output_dir / "rss_maps.npz"),
        "frequency_hz": float(args.frequency),
        "bandwidth_hz": float(args.bandwidth),
        "tx": {
            "name": "tx_valid_edge",
            "position": tx_position,
            "power_dbm": float(args.tx_power_dbm),
            "placement": args.tx_placement,
            "edge_offset_m": float(args.tx_edge_offset),
        },
        "rx_grid": {
            "region": args.rx_region,
            "resolution": [int(args.resolution), int(args.resolution)],
            "height_m": float(args.rx_height),
            "cell_size_m": [
                float(rx_region["width_m"]) / float(args.resolution),
                float(rx_region["height_m"]) / float(args.resolution),
            ],
        },
        "propagation": {
            "max_depth": int(args.max_depth),
            "num_samples": int(args.num_samples),
            "num_runs": int(args.num_runs),
            "los": not args.no_los,
            "reflection": not args.no_reflection,
            "diffraction": bool(args.diffraction),
            "scattering": bool(args.scattering),
            "edge_diffraction": bool(args.edge_diffraction),
            "buildings_in_scene": int(building_count),
            "building_cells_masked_in_output": bool(args.mask_building_cells),
            "total_vehicle_objects_in_scene": len(car_names),
            "active_vehicle_objects": selected_car_names,
            "allow_zero_vehicles": bool(args.allow_zero_vehicles),
            "sionna_backend": sionna_backend_info,
            "materials_follow_van3twin_style": {
                "buildings": "itu_concrete proxy in current export",
                "road": "itu_concrete",
                "vehicles": "itu_metal",
            },
        },
        "visualization": {
            "plot_style": args.plot_style,
            "view_region": args.view_region or ("valid" if args.plot_style == "clean" else "support"),
            "colormap": args.cmap,
            "vmin_dbm": float(args.vmin_dbm),
            "vmax_dbm": float(args.vmax_dbm),
            "vehicle_overlay": args.vehicle_overlay or ("dark" if args.plot_style == "clean" else "light"),
            "tx_marker_visible": not bool(args.hide_tx_marker),
            "visual_fill_threshold_dbm": float(args.visual_fill_threshold_dbm),
            "visual_fill_enabled": visual_fill_enabled(args),
            "visual_smooth_sigma": float(args.visual_smooth_sigma),
            "interpolation": args.interpolation or "nearest",
            "display_mode": args.display_mode,
            "style_preset": (
                "faithful_cached_128"
                if (
                    args.plot_style == "clean"
                    and args.display_mode == "absolute"
                    and args.cmap == "viridis"
                    and math.isclose(float(args.vmin_dbm), -108.0)
                    and math.isclose(float(args.vmax_dbm), -42.0)
                    and not visual_fill_enabled(args)
                    and math.isclose(float(args.visual_smooth_sigma), 0.0)
                    and (args.interpolation is None or args.interpolation == "nearest")
                    and bool(args.hide_tx_marker)
                )
                else "custom"
            ),
            "clean_background_color": args.clean_background_color,
            "building_facecolor": args.building_facecolor,
            "building_edgecolor": args.building_edgecolor,
            "vehicle_facecolor": args.vehicle_facecolor,
            "vehicle_edgecolor": args.vehicle_edgecolor,
            "tx_marker_color": args.tx_marker_color,
        },
        "dynamic_rss_change_validation": dynamic_summary,
        "dynamic_rss_change_config": {
            "floor_dbm": float(args.dynamic_stat_floor_dbm),
            "required": require_dynamic_change,
        },
        "startup_stability_filter": stability_filter,
        "frames": {
            "source": "cached_rss" if reuse_rss_dir is not None else "fresh_simulation",
            "start_frame": int(args.start_frame),
            "end_frame": int(args.end_frame),
            "stride": int(args.stride),
            "count": len(rows),
            "frame_indices": frame_indices,
        },
    }
    with (output_dir / "rss_heatmap_meta.json").open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"[OK] Wrote RSS maps: {output_dir / 'rss_maps.npz'}")
    print(f"[OK] Wrote metadata: {output_dir / 'rss_heatmap_meta.json'}")
    if not args.skip_frame_rendering:
        encode_video(frames_dir, output_video, args.fps / max(args.stride, 1))
        print(f"[OK] Wrote video: {output_video}")
        print(f"[INFO] Kept frames at: {frames_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
