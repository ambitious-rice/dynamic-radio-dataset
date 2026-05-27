from __future__ import annotations

import argparse
import json
import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.regions import region_axes, region_center, region_size
from dynamic_radio_dataset.raster.traffic_grid import rasterize_building_mask
from dynamic_radio_dataset.sionna.rt_compat import (
    compute_radio_map_rss_watt,
    configure_scene_arrays,
    configure_scene_frequency,
    load_scene_preserving_names,
    make_backend_info,
    radio_map_request_from_region,
)


JsonDict = Dict[str, Any]


@dataclass
class RssComputeConfig:
    scene_file: str = "scene.xml"
    motion_file: str = "motion.jsonl"
    start_frame: int = 0
    end_frame: int = -1
    stride: int = 5
    resolution: int = 128
    frequency: float = 5.89e9
    bandwidth: float = 10e6
    tx_power_dbm: float = 23.0
    tx_height: float = 1.5
    rx_height: float = 1.5
    rx_region: str = "valid_crop"
    tx_placement: str = "north_edge"
    tx_edge_offset: float = 4.0
    tx_x: float | None = None
    tx_y: float | None = None
    tx_z: float | None = None
    max_depth: int = 3
    num_samples: int = 50000
    num_runs: int = 1
    include_vehicles: str = ""
    exclude_vehicles: str = ""
    allow_zero_vehicles: bool = False
    no_los: bool = False
    no_reflection: bool = False
    diffraction: bool = False
    scattering: bool = False
    edge_diffraction: bool = False
    mask_building_cells: bool = True
    require_buildings: bool = True
    require_dynamic_rss_change: bool = True
    min_p95_temporal_range_db: float = 1.0
    min_max_temporal_range_db: float = 4.0
    dynamic_stat_floor_dbm: float = -200.0
    auto_skip_unstable_start: bool = True
    stability_window: int = 3
    stability_max_abs_vz: float = 0.5
    stability_max_z_range: float = 0.15
    mitsuba_variant: str = "llvm_ad_rgb"
    use_gpu: bool = False

    @classmethod
    def from_namespace(cls, args: argparse.Namespace) -> "RssComputeConfig":
        return cls(
            scene_file=str(getattr(args, "scene_file", "scene.xml")),
            motion_file=str(getattr(args, "motion_file", "motion.jsonl")),
            start_frame=int(getattr(args, "start_frame", 0)),
            end_frame=int(getattr(args, "end_frame", -1)),
            stride=int(getattr(args, "stride", 5)),
            resolution=int(getattr(args, "resolution", 128)),
            frequency=float(getattr(args, "frequency", 5.89e9)),
            bandwidth=float(getattr(args, "bandwidth", 10e6)),
            tx_power_dbm=float(getattr(args, "tx_power_dbm", 23.0)),
            tx_height=float(getattr(args, "tx_height", 1.5)),
            rx_height=float(getattr(args, "rx_height", 1.5)),
            rx_region=str(getattr(args, "rx_region", "valid_crop")),
            tx_placement=str(getattr(args, "tx_placement", "north_edge")),
            tx_edge_offset=float(getattr(args, "tx_edge_offset", 4.0)),
            tx_x=getattr(args, "tx_x", None),
            tx_y=getattr(args, "tx_y", None),
            tx_z=getattr(args, "tx_z", None),
            max_depth=int(getattr(args, "max_depth", 3)),
            num_samples=int(getattr(args, "num_samples", 50000)),
            num_runs=int(getattr(args, "num_runs", 1)),
            include_vehicles=str(getattr(args, "include_vehicles", "")),
            exclude_vehicles=str(getattr(args, "exclude_vehicles", "")),
            allow_zero_vehicles=bool(getattr(args, "allow_zero_vehicles", False)),
            no_los=bool(getattr(args, "no_los", False)),
            no_reflection=bool(getattr(args, "no_reflection", False)),
            diffraction=bool(getattr(args, "diffraction", False)),
            scattering=bool(getattr(args, "scattering", False)),
            edge_diffraction=bool(getattr(args, "edge_diffraction", False)),
            mask_building_cells=bool(getattr(args, "mask_building_cells", True)),
            require_buildings=bool(getattr(args, "require_buildings", True)),
            require_dynamic_rss_change=bool(getattr(args, "require_dynamic_rss_change", True)),
            min_p95_temporal_range_db=float(getattr(args, "min_p95_temporal_range_db", 1.0)),
            min_max_temporal_range_db=float(getattr(args, "min_max_temporal_range_db", 4.0)),
            dynamic_stat_floor_dbm=float(getattr(args, "dynamic_stat_floor_dbm", -200.0)),
            auto_skip_unstable_start=bool(getattr(args, "auto_skip_unstable_start", True)),
            stability_window=int(getattr(args, "stability_window", 3)),
            stability_max_abs_vz=float(getattr(args, "stability_max_abs_vz", 0.5)),
            stability_max_z_range=float(getattr(args, "stability_max_z_range", 0.15)),
            mitsuba_variant=str(getattr(args, "mitsuba_variant", "llvm_ad_rgb")),
            use_gpu=bool(getattr(args, "use_gpu", False)),
        )


@dataclass
class RssComputeResult:
    rss_npz: Path
    metadata_json: Path
    frame_stats_jsonl: Path
    frame_indices: list[int]
    rss_shape: tuple[int, int, int]


def compute_rss_maps(
    export_dir: Path,
    output_dir: Path,
    tx_position: dict[str, Any] | Sequence[float] | None,
    config: RssComputeConfig,
) -> RssComputeResult:
    scene_path = export_dir / config.scene_file
    motion_path = export_dir / config.motion_file
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
    if config.require_buildings and building_count <= 0:
        raise RuntimeError(
            "No exported building geometry found. Re-run export_sionna_scene_from_carla.py with --include-buildings "
            "or pass --allow-missing-buildings for a debug-only run."
        )

    rx_region = manifest[config.rx_region]
    support_region = manifest["support_region"]
    valid_crop = manifest["valid_crop"]
    tx_position_list = resolve_tx_position(tx_position, config, valid_crop)
    output_dir.mkdir(parents=True, exist_ok=True)

    from sionna.rt import PlanarArray, Transmitter
    import mitsuba as mi

    rows = select_motion_frames(motion_path, config.start_frame, config.end_frame, config.stride)
    building_mask = rasterize_building_mask(manifest, rx_region, config.resolution) if config.mask_building_cells else None

    scene = load_scene_preserving_names(str(scene_path))
    configure_scene_frequency(scene, frequency=float(config.frequency), bandwidth=float(config.bandwidth))
    configure_scene_arrays(scene, planar_array_cls=PlanarArray)
    sionna_backend = make_backend_info(scene)
    scene.add(
        Transmitter(
            "tx_valid_edge",
            position=tx_position_list,
            power_dbm=float(config.tx_power_dbm),
            look_at=[
                float(rx_region["center"]["x"]),
                float(rx_region["center"]["y"]),
                float(config.rx_height),
            ],
        )
    )

    object_names = list(scene.objects.keys())
    car_names = sorted(
        [name for name in object_names if name.startswith("car_")],
        key=lambda item: int(item.split("_")[1]),
    )
    selected_car_names = select_car_names(car_names, config)
    selected_car_set = set(selected_car_names)
    rows, stability_filter = skip_unstable_start_rows(rows, selected_car_set, config)
    if config.require_buildings and scene.get("static_buildings_proxy") is None:
        raise RuntimeError("Manifest has buildings, but scene object 'static_buildings_proxy' is missing.")

    center = region_center(support_region)
    width, height = region_size(support_region)
    hidden_position = [float(center[0] + 10.0 * width), float(center[1] + 10.0 * height), -1000.0]

    rss_maps: list[np.ndarray] = []
    frame_indices: list[int] = []
    stats_rows: list[JsonDict] = []
    print(f"[INFO] Loaded scene: {scene_path}")
    print(f"[INFO] Mitsuba variant: {mi.variant()}")
    print(f"[INFO] Sionna RT backend: {sionna_backend.package_version}, API={sionna_backend.radio_map_api}")
    print(f"[INFO] Buildings in propagation scene: {building_count}")
    print(f"[INFO] Cars in scene: {len(car_names)} total; {len(selected_car_names)} kept in propagation: {selected_car_names}")
    if stability_filter.get("enabled"):
        print(f"[INFO] Startup stability filter: {stability_filter}")
    print(f"[INFO] TX position: {tx_position_list}, power={config.tx_power_dbm} dBm")
    print(
        f"[INFO] RSS grid: {config.resolution}x{config.resolution} over {config.rx_region} "
        f"{region_size(rx_region)} m"
    )
    print(f"[INFO] Selected motion frames: {len(rows)} (stride={config.stride})")

    for seq, row in enumerate(rows):
        frame_index = int(row["frame_index"])
        moving_vehicles = apply_motion_frame(scene, car_names, row, hidden_position, selected_car_set)
        rss_dbm = compute_rss_map(scene, config, rx_region).astype(np.float32)
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
    dynamic_summary = summarize_dynamic_rss_change(rss_stack, building_mask, config.dynamic_stat_floor_dbm)
    p95_range = dynamic_summary["p95_temporal_range_db"]
    max_range = dynamic_summary["max_temporal_range_db"]
    print(f"[INFO] Dynamic RSS temporal range summary: {dynamic_summary}")
    require_dynamic_change = bool(config.require_dynamic_rss_change and not config.allow_zero_vehicles)
    if require_dynamic_change:
        if p95_range is None or max_range is None:
            raise RuntimeError("No finite RSS cells were available for dynamic-change validation.")
        if float(p95_range) < config.min_p95_temporal_range_db and float(max_range) < config.min_max_temporal_range_db:
            raise RuntimeError(
                "RSS maps are too flat over time; vehicle-induced propagation change is not obvious. "
                f"p95 temporal range={p95_range:.2f} dB, max temporal range={max_range:.2f} dB. "
                "Try lowering TX/RX height, moving TX to another valid-crop edge, increasing selected frames, "
                "or collecting a clip where vehicles pass through the TX-to-grid paths. "
                "Pass --allow-flat-rss only for debugging."
            )

    rss_npz = output_dir / "rss_maps.npz"
    np.savez_compressed(
        rss_npz,
        rss_dbm=rss_stack,
        frame_indices=np.asarray(frame_indices, dtype=np.int32),
        tx_position=np.asarray(tx_position_list, dtype=np.float32),
        building_mask=(building_mask.astype(np.uint8) if building_mask is not None else np.zeros((config.resolution, config.resolution), dtype=np.uint8)),
    )
    frame_stats_jsonl = output_dir / "frame_stats.jsonl"
    with frame_stats_jsonl.open("w", encoding="utf-8") as f:
        for stats in stats_rows:
            f.write(json.dumps(stats, ensure_ascii=False) + "\n")

    meta = build_metadata(
        export_dir=export_dir,
        scene_path=scene_path,
        motion_path=motion_path,
        output_dir=output_dir,
        config=config,
        tx_position=tx_position_list,
        rx_region=rx_region,
        building_count=building_count,
        car_names=car_names,
        selected_car_names=selected_car_names,
        dynamic_summary=dynamic_summary,
        require_dynamic_change=require_dynamic_change,
        stability_filter=stability_filter,
        frame_indices=frame_indices,
        row_count=len(rows),
        sionna_backend=sionna_backend.__dict__,
    )
    metadata_json = output_dir / "rss_heatmap_meta.json"
    with metadata_json.open("w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    print(f"[OK] Wrote RSS maps: {rss_npz}")
    print(f"[OK] Wrote metadata: {metadata_json}")
    return RssComputeResult(
        rss_npz=rss_npz,
        metadata_json=metadata_json,
        frame_stats_jsonl=frame_stats_jsonl,
        frame_indices=frame_indices,
        rss_shape=tuple(int(item) for item in rss_stack.shape),
    )


def build_metadata(
    *,
    export_dir: Path,
    scene_path: Path,
    motion_path: Path,
    output_dir: Path,
    config: RssComputeConfig,
    tx_position: Sequence[float],
    rx_region: JsonDict,
    building_count: int,
    car_names: Sequence[str],
    selected_car_names: Sequence[str],
    dynamic_summary: JsonDict,
    require_dynamic_change: bool,
    stability_filter: JsonDict,
    frame_indices: Sequence[int],
    row_count: int,
    sionna_backend: JsonDict | None = None,
) -> JsonDict:
    return {
        "schema": "sionna_dynamic_rss_heatmap_v1",
        "source_export_dir": str(export_dir),
        "scene_xml": str(scene_path),
        "motion_jsonl": str(motion_path),
        "rss_npz": str(output_dir / "rss_maps.npz"),
        "frequency_hz": float(config.frequency),
        "bandwidth_hz": float(config.bandwidth),
        "tx": {
            "name": "tx_valid_edge",
            "position": list(tx_position),
            "power_dbm": float(config.tx_power_dbm),
            "placement": config.tx_placement,
            "edge_offset_m": float(config.tx_edge_offset),
        },
        "rx_grid": {
            "region": config.rx_region,
            "resolution": [int(config.resolution), int(config.resolution)],
            "height_m": float(config.rx_height),
            "cell_size_m": [
                float(rx_region["width_m"]) / float(config.resolution),
                float(rx_region["height_m"]) / float(config.resolution),
            ],
        },
        "propagation": {
            "sionna_backend": dict(sionna_backend or {}),
            "max_depth": int(config.max_depth),
            "num_samples": int(config.num_samples),
            "num_runs": int(config.num_runs),
            "los": not config.no_los,
            "reflection": not config.no_reflection,
            "diffraction": bool(config.diffraction),
            "scattering": bool(config.scattering),
            "edge_diffraction": bool(config.edge_diffraction),
            "buildings_in_scene": int(building_count),
            "building_cells_masked_in_output": bool(config.mask_building_cells),
            "total_vehicle_objects_in_scene": len(car_names),
            "active_vehicle_objects": list(selected_car_names),
            "allow_zero_vehicles": bool(config.allow_zero_vehicles),
            "materials_follow_van3twin_style": {
                "buildings": "itu_concrete proxy in current export",
                "road": "itu_concrete",
                "vehicles": "itu_metal",
            },
        },
        "dynamic_rss_change_validation": dynamic_summary,
        "dynamic_rss_change_config": {
            "floor_dbm": float(config.dynamic_stat_floor_dbm),
            "required": require_dynamic_change,
        },
        "startup_stability_filter": stability_filter,
        "frames": {
            "source": "fresh_simulation",
            "start_frame": int(config.start_frame),
            "end_frame": int(config.end_frame),
            "stride": int(config.stride),
            "count": int(row_count),
            "frame_indices": [int(item) for item in frame_indices],
        },
    }


def resolve_tx_position(
    tx_position: dict[str, Any] | Sequence[float] | None,
    config: RssComputeConfig,
    valid_crop: JsonDict,
) -> list[float]:
    if tx_position is not None:
        if isinstance(tx_position, dict):
            source = tx_position.get("position") if isinstance(tx_position.get("position"), dict) else tx_position
            z = source.get("z", None)
            return [
                float(source["x"]),
                float(source["y"]),
                float(z if z is not None else config.tx_height),
            ]
        return [float(tx_position[0]), float(tx_position[1]), float(tx_position[2])]
    return tx_position_from_config(config, valid_crop)


def tx_position_from_config(config: RssComputeConfig, valid_crop: JsonDict) -> list[float]:
    center = region_center(valid_crop)
    width, height = region_size(valid_crop)
    x_axis, y_axis = region_axes(valid_crop)

    if config.tx_placement == "custom":
        if config.tx_x is None or config.tx_y is None:
            raise ValueError("--tx-placement custom requires --tx-x and --tx-y")
        return [
            float(config.tx_x),
            float(config.tx_y),
            float(config.tx_z if config.tx_z is not None else config.tx_height),
        ]

    if config.tx_placement == "north_edge":
        xy = center + y_axis * (height / 2.0 + config.tx_edge_offset)
    elif config.tx_placement == "south_edge":
        xy = center - y_axis * (height / 2.0 + config.tx_edge_offset)
    elif config.tx_placement == "east_edge":
        xy = center + x_axis * (width / 2.0 + config.tx_edge_offset)
    elif config.tx_placement == "west_edge":
        xy = center - x_axis * (width / 2.0 + config.tx_edge_offset)
    else:
        raise ValueError(f"Unsupported TX placement: {config.tx_placement}")

    return [float(xy[0]), float(xy[1]), float(config.tx_z if config.tx_z is not None else config.tx_height)]


def load_json(path: Path) -> JsonDict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_motion(path: Path) -> Iterable[JsonDict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def select_motion_frames(path: Path, start: int, end: int, stride: int) -> list[JsonDict]:
    frames: list[JsonDict] = []
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


def vehicle_filter_requests_all(text: str) -> bool:
    return any(item.strip().lower() in {"*", "all"} for item in text.split(",") if item.strip())


def select_car_names(car_names: Sequence[str], config: RssComputeConfig) -> list[str]:
    available = set(car_names)
    include = parse_car_set(config.include_vehicles)
    exclude = parse_car_set(config.exclude_vehicles)
    if vehicle_filter_requests_all(config.include_vehicles):
        selected = set(available)
    elif include:
        selected = include & available
        missing = sorted(include - available)
        if missing:
            print(f"[WARN] Requested vehicles not found in scene and will be ignored: {missing}")
    else:
        selected = set(available)
    if vehicle_filter_requests_all(config.exclude_vehicles):
        selected.clear()
    else:
        selected -= exclude
    if not selected:
        if config.allow_zero_vehicles:
            return []
        raise RuntimeError("Vehicle filtering removed all dynamic cars. Check --include-vehicles/--exclude-vehicles.")
    return sorted(selected, key=lambda item: int(item.split("_")[1]))


def motion_window_stability(
    rows: Sequence[JsonDict],
    selected_car_names: set[str],
) -> JsonDict:
    by_name: dict[str, list[tuple[float, float]]] = {}
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
    rows: Sequence[JsonDict],
    selected_car_names: set[str],
    config: RssComputeConfig,
) -> tuple[list[JsonDict], JsonDict]:
    if not selected_car_names:
        return list(rows), {
            "enabled": bool(config.auto_skip_unstable_start),
            "skipped_count": 0,
            "reason": "no_selected_vehicles",
        }
    if not config.auto_skip_unstable_start:
        return list(rows), {"enabled": False, "skipped_count": 0}

    window = max(1, int(config.stability_window))
    if len(rows) < window:
        return list(rows), {"enabled": True, "skipped_count": 0, "reason": "not_enough_rows", "window": window}

    last_stats: JsonDict = {}
    for start_index in range(0, len(rows) - window + 1):
        stats = motion_window_stability(rows[start_index : start_index + window], selected_car_names)
        last_stats = stats
        if (
            float(stats["max_abs_vz_mps"]) <= float(config.stability_max_abs_vz)
            and float(stats["max_z_range_m"]) <= float(config.stability_max_z_range)
        ):
            return list(rows[start_index:]), {
                "enabled": True,
                "skipped_count": start_index,
                "window": window,
                "thresholds": {
                    "max_abs_vz_mps": float(config.stability_max_abs_vz),
                    "max_z_range_m": float(config.stability_max_z_range),
                },
                "first_kept_frame": int(rows[start_index]["frame_index"]),
                "stability_stats": stats,
            }

    return [rows[-1]], {
        "enabled": True,
        "skipped_count": max(0, len(rows) - 1),
        "window": window,
        "thresholds": {
            "max_abs_vz_mps": float(config.stability_max_abs_vz),
            "max_z_range_m": float(config.stability_max_z_range),
        },
        "first_kept_frame": int(rows[-1]["frame_index"]),
        "stability_stats": last_stats,
        "warning": "No stable startup window was found; kept only the last selected row.",
    }


def dbm_from_watt(values: np.ndarray) -> np.ndarray:
    values = np.asarray(values, dtype=np.float64)
    safe = np.maximum(values, 1e-30)
    return 10.0 * np.log10(safe) + 30.0


def apply_motion_frame(
    scene,
    car_names: Sequence[str],
    row: JsonDict,
    hidden_position: Sequence[float],
    selected_car_names: set[str],
) -> list[JsonDict]:
    present = set()
    vehicles: list[JsonDict] = []
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


def compute_rss_map(scene, config: RssComputeConfig, rx_region: JsonDict) -> np.ndarray:
    request = radio_map_request_from_region(
        rx_region,
        resolution=int(config.resolution),
        rx_height=float(config.rx_height),
        max_depth=int(config.max_depth),
        num_samples=int(config.num_samples),
        num_runs=int(config.num_runs),
        los=not config.no_los,
        reflection=not config.no_reflection,
        diffraction=bool(config.diffraction),
        scattering=bool(config.scattering),
        edge_diffraction=bool(config.edge_diffraction),
    )
    rss_watt = compute_radio_map_rss_watt(scene, request)
    if rss_watt.ndim != 3 or rss_watt.shape[0] < 1:
        raise RuntimeError(f"Unexpected RSS tensor shape: {rss_watt.shape}")
    return dbm_from_watt(rss_watt[0])


def summarize_dynamic_rss_change(
    rss_stack: np.ndarray,
    building_mask: np.ndarray | None,
    floor_dbm: float,
) -> JsonDict:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compute Sionna RSS maps for an exported dynamic scene.")
    parser.add_argument("--export-dir", type=Path, required=True)
    parser.add_argument("--scene-file", type=str, default="scene.xml")
    parser.add_argument("--motion-file", type=str, default="motion.jsonl")
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--end-frame", type=int, default=-1)
    parser.add_argument("--stride", type=int, default=5)
    parser.add_argument("--resolution", type=int, default=128)
    parser.add_argument("--frequency", type=float, default=5.89e9)
    parser.add_argument("--bandwidth", type=float, default=10e6)
    parser.add_argument("--tx-power-dbm", type=float, default=23.0)
    parser.add_argument("--tx-height", type=float, default=1.5)
    parser.add_argument("--rx-height", type=float, default=1.5)
    parser.add_argument("--rx-region", choices=["valid_crop", "support_region"], default="valid_crop")
    parser.add_argument("--tx-placement", choices=["north_edge", "south_edge", "east_edge", "west_edge", "custom"], default="north_edge")
    parser.add_argument("--tx-edge-offset", type=float, default=4.0)
    parser.add_argument("--tx-x", type=float, default=None)
    parser.add_argument("--tx-y", type=float, default=None)
    parser.add_argument("--tx-z", type=float, default=None)
    parser.add_argument("--max-depth", type=int, default=3)
    parser.add_argument("--num-samples", type=int, default=50000)
    parser.add_argument("--num-runs", type=int, default=1)
    parser.add_argument("--include-vehicles", type=str, default="")
    parser.add_argument("--exclude-vehicles", type=str, default="")
    parser.add_argument("--allow-zero-vehicles", action="store_true")
    parser.add_argument("--no-los", action="store_true")
    parser.add_argument("--no-reflection", action="store_true")
    parser.add_argument("--diffraction", action="store_true")
    parser.add_argument("--scattering", action="store_true")
    parser.add_argument("--edge-diffraction", action="store_true")
    parser.add_argument("--mask-building-cells", action="store_true", default=True)
    parser.add_argument("--no-mask-building-cells", action="store_false", dest="mask_building_cells")
    parser.add_argument("--require-buildings", action="store_true", default=True)
    parser.add_argument("--allow-missing-buildings", action="store_false", dest="require_buildings")
    parser.add_argument("--require-dynamic-rss-change", action="store_true", default=True)
    parser.add_argument("--allow-flat-rss", action="store_false", dest="require_dynamic_rss_change")
    parser.add_argument("--min-p95-temporal-range-db", type=float, default=1.0)
    parser.add_argument("--min-max-temporal-range-db", type=float, default=4.0)
    parser.add_argument("--dynamic-stat-floor-dbm", type=float, default=-200.0)
    parser.add_argument("--auto-skip-unstable-start", action="store_true", default=True)
    parser.add_argument("--no-auto-skip-unstable-start", action="store_false", dest="auto_skip_unstable_start")
    parser.add_argument("--stability-window", type=int, default=3)
    parser.add_argument("--stability-max-abs-vz", type=float, default=0.5)
    parser.add_argument("--stability-max-z-range", type=float, default=0.15)
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("MI_DEFAULT_VARIANT", args.mitsuba_variant)
    if not args.use_gpu:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
    config = RssComputeConfig.from_namespace(args)
    compute_rss_maps(args.export_dir, args.output_dir, None, config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
