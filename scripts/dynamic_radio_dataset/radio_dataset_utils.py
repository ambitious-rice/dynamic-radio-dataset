#!/usr/bin/env python3
"""
Helpers for building a formal single-scene CARLA + Sionna radio-map dataset.

This module intentionally stays lightweight and reusable:
- scene signature checks;
- TX candidate search and overlay visualization;
- traffic-grid rasterization;
- episode QA for trajectory geometry and per-TX usefulness;
- dataset indexing and deterministic splits.
"""

from __future__ import annotations

import json
import math
import random
import sys
import time
from glob import glob
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

import numpy as np


JsonDict = Dict[str, object]


@dataclass
class TxSearchConfig:
    min_scene_radius_m: float = 10.0
    max_scene_radius_m: float = 20.0
    lane_distance_min_m: float = 2.0
    lane_distance_max_m: float = 5.5
    min_tx_spacing_m: float = 12.0
    grid_step_m: float = 1.0
    top_k_candidates: int = 40
    selected_count: int = 3
    corridor_width_m: float = 10.0
    require_sidewalk_lane: bool = True


@dataclass
class EpisodeQAConfig:
    lane_adherence_max_offset_m: float = 1.75
    lane_adherence_max_consecutive_frames: int = 20
    max_jump_per_frame_m: float = 3.5
    idle_speed_threshold_mps: float = 0.5
    max_idle_fraction: float = 0.70
    max_all_slow_duration_s: float = 2.0
    min_displacement_m: float = 20.0
    min_vehicles_crossing_label: int = 2
    require_turning_vehicle: bool = True
    require_straight_vehicle: bool = True
    large_vehicle_token: str = "fusorosa"
    max_large_vehicle_count: int = 1
    vehicle_collision_shrink_m: float = 0.10


@dataclass
class TxQAConfig:
    diagnostic_clearance_reference_m: float = 2.5
    corridor_width_m: float = 10.0
    min_p95_temporal_range_db: float = 1.0
    min_max_temporal_range_db: float = 4.0


def load_json(path: Path) -> JsonDict:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path: Path, data: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def iter_jsonl(path: Path) -> Iterator[JsonDict]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def write_jsonl(path: Path, rows: Iterable[JsonDict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def region_center(region: JsonDict) -> np.ndarray:
    center = region["center"]
    return np.array([float(center["x"]), float(center["y"])], dtype=np.float64)


def region_size(region: JsonDict) -> Tuple[float, float]:
    return float(region["width_m"]), float(region["height_m"])


def region_axes(region: JsonDict) -> Tuple[np.ndarray, np.ndarray]:
    yaw = math.radians(float(region.get("yaw_deg", 0.0)))
    x_axis = np.array([math.cos(yaw), math.sin(yaw)], dtype=np.float64)
    y_axis = np.array([-math.sin(yaw), math.cos(yaw)], dtype=np.float64)
    return x_axis, y_axis


def region_local_xy(points_xy: np.ndarray, region: JsonDict) -> np.ndarray:
    center = region_center(region)
    x_axis, y_axis = region_axes(region)
    delta = points_xy - center[None, :]
    return np.stack([delta @ x_axis, delta @ y_axis], axis=-1)


def region_extent_for_plot(region: JsonDict) -> Tuple[float, float, float, float]:
    center = region_center(region)
    width, height = region_size(region)
    return (
        float(center[0] - width / 2.0),
        float(center[0] + width / 2.0),
        float(center[1] - height / 2.0),
        float(center[1] + height / 2.0),
    )


def cell_centers_from_region(region: JsonDict, resolution: int) -> np.ndarray:
    center = region_center(region)
    width, height = region_size(region)
    x_axis, y_axis = region_axes(region)
    xs = (np.arange(resolution, dtype=np.float64) + 0.5) / resolution * width - width / 2.0
    ys = (np.arange(resolution, dtype=np.float64) + 0.5) / resolution * height - height / 2.0
    xx, yy = np.meshgrid(xs, ys)
    flat = center[None, :] + xx.reshape(-1, 1) * x_axis[None, :] + yy.reshape(-1, 1) * y_axis[None, :]
    return flat.reshape(resolution, resolution, 2)


def scene_signature(scene_meta: JsonDict) -> JsonDict:
    support_region = scene_meta["support_region"]
    valid_crop = scene_meta["valid_crop"]
    scene_info = scene_meta.get("scene_info", {})
    center = support_region["center"]
    return {
        "town": scene_meta.get("town"),
        "scene_mode": scene_meta.get("scene_mode"),
        "scene_info": scene_info,
        "support_region": {
            "center": {
                "x": float(center["x"]),
                "y": float(center["y"]),
                "z": float(center["z"]),
            },
            "width_m": float(support_region["width_m"]),
            "height_m": float(support_region["height_m"]),
            "yaw_deg": float(support_region.get("yaw_deg", 0.0)),
        },
        "valid_crop": {
            "width_m": float(valid_crop["width_m"]),
            "height_m": float(valid_crop["height_m"]),
            "yaw_deg": float(valid_crop.get("yaw_deg", 0.0)),
        },
    }


def scene_signature_matches(expected: JsonDict, actual: JsonDict, tol: float = 1e-3) -> bool:
    if expected.get("town") != actual.get("town"):
        return False
    if expected.get("scene_mode") != actual.get("scene_mode"):
        return False
    for key in ("width_m", "height_m", "yaw_deg"):
        if abs(float(expected["support_region"][key]) - float(actual["support_region"][key])) > tol:
            return False
    for key in ("width_m", "height_m", "yaw_deg"):
        if abs(float(expected["valid_crop"][key]) - float(actual["valid_crop"][key])) > tol:
            return False
    for axis in ("x", "y", "z"):
        if abs(float(expected["support_region"]["center"][axis]) - float(actual["support_region"]["center"][axis])) > tol:
            return False
    if expected.get("scene_info") != actual.get("scene_info"):
        return False
    return True


def bootstrap_carla_api() -> Path:
    repo_root = _find_repo_root(Path(__file__).resolve())
    egg_glob = str(repo_root / "PythonAPI" / "carla" / "dist" / "carla-*-py3.*-linux-x86_64.egg")
    eggs = sorted(glob(egg_glob))
    if eggs:
        sys.path.insert(0, eggs[-1])
    sys.path.insert(0, str(repo_root / "PythonAPI" / "carla"))
    return repo_root


def _find_repo_root(path: Path) -> Path:
    for parent in [path.parent, *path.parents]:
        if (parent / "CarlaUE4").exists() and (parent / "PythonAPI").exists():
            return parent
    raise RuntimeError(f"Could not locate CARLA repo root from {path}")


def load_carla_map_for_scene(host: str, port: int, town: str, timeout: float = 30.0):
    bootstrap_carla_api()
    import carla  # type: ignore  # noqa: PLC0415

    client = carla.Client(host, int(port))
    client.set_timeout(float(timeout))
    world = client.get_world()
    wanted_town = Path(str(town)).name if town else ""
    current_town = Path(world.get_map().name).name
    if wanted_town and current_town != wanted_town:
        world = client.load_world(wanted_town)
        time.sleep(3.0)
    return world.get_map()


def route_lookup(routes_path: Path) -> Dict[str, JsonDict]:
    data = load_json(routes_path)
    return {str(route["route_id"]): route for route in data.get("routes", [])}


def route_polylines(routes: Dict[str, JsonDict]) -> List[np.ndarray]:
    polylines = []
    for route in routes.values():
        polyline = route.get("polyline") or []
        if len(polyline) < 2:
            continue
        polylines.append(
            np.array([[float(item["x"]), float(item["y"])] for item in polyline], dtype=np.float64)
        )
    return polylines


def normalize_angle_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def rectangle_corners(center_xy: Sequence[float], extent_xy: Sequence[float], yaw_deg: float) -> np.ndarray:
    ex, ey = float(extent_xy[0]), float(extent_xy[1])
    corners = np.array([[-ex, -ey], [ex, -ey], [ex, ey], [-ex, ey]], dtype=np.float64)
    yaw = math.radians(float(yaw_deg))
    rot = np.array([[math.cos(yaw), -math.sin(yaw)], [math.sin(yaw), math.cos(yaw)]], dtype=np.float64)
    return corners @ rot.T + np.array(center_xy, dtype=np.float64)[None, :]


def rectangle_mask(
    centers_xy: np.ndarray,
    center_xy: Sequence[float],
    extent_xy: Sequence[float],
    yaw_deg: float,
) -> np.ndarray:
    center_xy_np = np.array(center_xy, dtype=np.float64)
    yaw = math.radians(float(yaw_deg))
    c = math.cos(-yaw)
    s = math.sin(-yaw)
    delta = centers_xy - center_xy_np[None, :]
    local_x = delta[:, 0] * c - delta[:, 1] * s
    local_y = delta[:, 0] * s + delta[:, 1] * c
    return (np.abs(local_x) <= float(extent_xy[0])) & (np.abs(local_y) <= float(extent_xy[1]))


def point_segment_distance(point_xy: np.ndarray, seg_start: np.ndarray, seg_end: np.ndarray) -> float:
    delta = seg_end - seg_start
    denom = float(np.dot(delta, delta))
    if denom <= 1e-9:
        return float(np.linalg.norm(point_xy - seg_start))
    t = float(np.dot(point_xy - seg_start, delta) / denom)
    t = max(0.0, min(1.0, t))
    proj = seg_start + t * delta
    return float(np.linalg.norm(point_xy - proj))


def point_polyline_distance(point_xy: np.ndarray, polyline_xy: np.ndarray) -> float:
    if polyline_xy.shape[0] == 1:
        return float(np.linalg.norm(point_xy - polyline_xy[0]))
    return min(
        point_segment_distance(point_xy, polyline_xy[idx], polyline_xy[idx + 1])
        for idx in range(polyline_xy.shape[0] - 1)
    )


def min_distance_to_polylines(point_xy: np.ndarray, polylines_xy: Sequence[np.ndarray]) -> float:
    if not polylines_xy:
        return float("inf")
    return min(point_polyline_distance(point_xy, polyline) for polyline in polylines_xy)


def building_rectangles_from_manifest(manifest: JsonDict) -> List[JsonDict]:
    result: List[JsonDict] = []
    for building in manifest.get("buildings") or []:
        center = building["center"]
        extent = building["extent"]
        rotation = building.get("rotation", {})
        result.append(
            {
                "id": building.get("name") or building.get("object_id"),
                "center_xy": [float(center["x"]), float(center["y"])],
                "extent_xy": [float(extent["x"]), float(extent["y"])],
                "yaw_deg": float(rotation.get("yaw", 0.0)),
            }
        )
    return result


def min_distance_to_rectangle(point_xy: np.ndarray, center_xy: Sequence[float], extent_xy: Sequence[float], yaw_deg: float) -> float:
    center_xy_np = np.array(center_xy, dtype=np.float64)
    yaw = math.radians(float(yaw_deg))
    c = math.cos(-yaw)
    s = math.sin(-yaw)
    delta = point_xy - center_xy_np
    local_x = delta[0] * c - delta[1] * s
    local_y = delta[0] * s + delta[1] * c
    dx = max(abs(local_x) - float(extent_xy[0]), 0.0)
    dy = max(abs(local_y) - float(extent_xy[1]), 0.0)
    return float(math.hypot(dx, dy))


def inside_any_building(point_xy: np.ndarray, building_rects: Sequence[JsonDict]) -> bool:
    for building in building_rects:
        if min_distance_to_rectangle(point_xy, building["center_xy"], building["extent_xy"], building["yaw_deg"]) <= 1e-6:
            return True
    return False


def min_building_clearance(point_xy: np.ndarray, building_rects: Sequence[JsonDict]) -> float:
    if not building_rects:
        return float("inf")
    return min(
        min_distance_to_rectangle(point_xy, building["center_xy"], building["extent_xy"], building["yaw_deg"])
        for building in building_rects
    )


def select_tx_candidates(
    scene_meta: JsonDict,
    routes: Dict[str, JsonDict],
    manifest: JsonDict,
    config: TxSearchConfig,
    carla_map=None,
) -> JsonDict:
    support_region = scene_meta["support_region"]
    valid_crop = scene_meta["valid_crop"]
    center_xy = region_center(support_region)
    width, height = region_size(support_region)
    polylines = route_polylines(routes)
    building_rects = building_rectangles_from_manifest(manifest)

    half_w = width / 2.0
    half_h = height / 2.0
    xs = np.arange(center_xy[0] - half_w, center_xy[0] + half_w + 1e-6, float(config.grid_step_m))
    ys = np.arange(center_xy[1] - half_h, center_xy[1] + half_h + 1e-6, float(config.grid_step_m))
    candidates: List[JsonDict] = []
    scene_radius_mid = 0.5 * (float(config.min_scene_radius_m) + float(config.max_scene_radius_m))
    lane_distance_mid = 0.5 * (float(config.lane_distance_min_m) + float(config.lane_distance_max_m))

    if bool(config.require_sidewalk_lane) and carla_map is None:
        raise RuntimeError(
            "TX search requires a live CARLA map when require_sidewalk_lane=True. "
            "Start the CARLA server and provide the correct town instead of falling back to offline geometry guesses."
        )

    if carla_map is not None:
        import carla  # type: ignore  # noqa: PLC0415

        probe_z = float((support_region.get("center") or {}).get("z", 0.0)) + 0.5

        def lane_probe(x: float, y: float) -> Dict[str, object]:
            loc = carla.Location(x=float(x), y=float(y), z=probe_z)
            on_sidewalk_wp = carla_map.get_waypoint(loc, project_to_road=False, lane_type=carla.LaneType.Sidewalk)
            on_driving_wp = carla_map.get_waypoint(loc, project_to_road=False, lane_type=carla.LaneType.Driving)
            driving_wp = carla_map.get_waypoint(loc, project_to_road=True, lane_type=carla.LaneType.Driving)
            sidewalk_wp = carla_map.get_waypoint(loc, project_to_road=True, lane_type=carla.LaneType.Sidewalk)
            driving_distance = float("inf")
            sidewalk_distance = float("inf")
            if driving_wp is not None:
                driving_distance = float(math.hypot(driving_wp.transform.location.x - x, driving_wp.transform.location.y - y))
            if sidewalk_wp is not None:
                sidewalk_distance = float(math.hypot(sidewalk_wp.transform.location.x - x, sidewalk_wp.transform.location.y - y))
            return {
                "on_sidewalk": on_sidewalk_wp is not None,
                "on_driving": on_driving_wp is not None,
                "driving_distance_m": driving_distance,
                "sidewalk_distance_m": sidewalk_distance,
            }
    else:
        lane_probe = None

    for x in xs:
        for y in ys:
            point_xy = np.array([x, y], dtype=np.float64)
            radial_distance = float(np.linalg.norm(point_xy - center_xy))
            if radial_distance < float(config.min_scene_radius_m) or radial_distance > float(config.max_scene_radius_m):
                continue
            if inside_any_building(point_xy, building_rects):
                continue
            lane_distance = min_distance_to_polylines(point_xy, polylines)
            sidewalk_distance = float("inf")
            on_sidewalk = False
            on_driving = False
            if lane_probe is not None:
                lane_info = lane_probe(float(x), float(y))
                on_sidewalk = bool(lane_info["on_sidewalk"])
                on_driving = bool(lane_info["on_driving"])
                lane_distance = float(lane_info["driving_distance_m"])
                sidewalk_distance = float(lane_info["sidewalk_distance_m"])
                if bool(config.require_sidewalk_lane) and not on_sidewalk:
                    continue
                if on_driving:
                    continue
            if lane_distance < float(config.lane_distance_min_m) or lane_distance > float(config.lane_distance_max_m):
                continue
            building_clearance = min_building_clearance(point_xy, building_rects)
            azimuth_deg = float(math.degrees(math.atan2(point_xy[1] - center_xy[1], point_xy[0] - center_xy[0])))
            score = 0.0
            score -= abs(radial_distance - scene_radius_mid) * 0.6
            score -= abs(lane_distance - lane_distance_mid) * 1.0
            if sidewalk_distance != float("inf"):
                score -= abs(sidewalk_distance) * 0.5
            score += min(building_clearance, 10.0) * 0.08
            score += max(0.0, 6.0 - abs(normalize_angle_deg(azimuth_deg))) * 0.02
            candidates.append(
                {
                    "x": float(x),
                    "y": float(y),
                    "z": None,
                    "scene_radius_m": radial_distance,
                    "lane_distance_m": lane_distance,
                    "sidewalk_distance_m": None if sidewalk_distance == float("inf") else sidewalk_distance,
                    "on_sidewalk": bool(on_sidewalk),
                    "on_driving": bool(on_driving),
                    "building_clearance_m": building_clearance,
                    "azimuth_deg": azimuth_deg,
                    "score": float(score),
                }
            )

    candidates.sort(key=lambda item: float(item["score"]), reverse=True)
    trimmed_candidates = candidates[: max(int(config.top_k_candidates), int(config.selected_count))]
    selected: List[JsonDict] = []
    for candidate in trimmed_candidates:
        point_xy = np.array([float(candidate["x"]), float(candidate["y"])], dtype=np.float64)
        if any(
            float(np.linalg.norm(point_xy - np.array([float(item["x"]), float(item["y"])], dtype=np.float64)))
            < float(config.min_tx_spacing_m)
            for item in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= int(config.selected_count):
            break

    if len(selected) < int(config.selected_count):
        raise RuntimeError(
            f"Only found {len(selected)} TX candidates after spacing constraints; need {config.selected_count}."
        )

    center_valid = region_center(valid_crop)
    tx_catalog = []
    for index, candidate in enumerate(selected):
        tx_catalog.append(
            {
                "tx_id": f"tx_{index:02d}",
                "position": {
                    "x": float(candidate["x"]),
                    "y": float(candidate["y"]),
                    "z": None,
                },
                "scene_radius_m": float(candidate["scene_radius_m"]),
                "lane_distance_m": float(candidate["lane_distance_m"]),
                "building_clearance_m": float(candidate["building_clearance_m"]),
                "selection_score": float(candidate["score"]),
                "surface_semantics": {
                    "require_sidewalk_lane": bool(config.require_sidewalk_lane),
                    "on_sidewalk_lane": bool(candidate["on_sidewalk"]),
                    "on_driving_lane": bool(candidate["on_driving"]),
                    "sidewalk_distance_m": candidate.get("sidewalk_distance_m"),
                },
                "corridor_to_label_center": {
                    "x0": float(candidate["x"]),
                    "y0": float(candidate["y"]),
                    "x1": float(center_valid[0]),
                    "y1": float(center_valid[1]),
                    "width_m": float(config.corridor_width_m),
                },
            }
        )

    return {
        "schema": "single_scene_tx_catalog_v1",
        "selection_mode": "carla_sidewalk_semantic_search" if bool(config.require_sidewalk_lane) else "offline_geometry_search",
        "search_config": asdict(config),
        "scene_signature": scene_signature(scene_meta),
        "candidate_count": len(candidates),
        "selected_count": len(tx_catalog),
        "candidates": trimmed_candidates,
        "tx_catalog": tx_catalog,
    }


def plot_tx_selection_overlay(
    scene_meta: JsonDict,
    routes: Dict[str, JsonDict],
    manifest: JsonDict,
    tx_selection: JsonDict,
    output_path: Path,
    topdown_image_path: Optional[Path] = None,
) -> None:
    try:
        import matplotlib
    except ModuleNotFoundError as exc:  # pragma: no cover - depends on caller environment
        raise RuntimeError(
            "matplotlib is required for TX overlay generation. "
            "Run the dataset builder with an environment that already supports the Sionna visualization stack."
        ) from exc

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib.patches import Polygon, Rectangle
    from matplotlib import image as mpimg

    support_region = scene_meta["support_region"]
    valid_crop = scene_meta["valid_crop"]
    support_extent = region_extent_for_plot(support_region)
    valid_extent = region_extent_for_plot(valid_crop)
    fig, ax = plt.subplots(figsize=(8.0, 8.0), dpi=140)
    ax.set_facecolor("#f6f6f3")

    if topdown_image_path is not None and topdown_image_path.exists():
        image = mpimg.imread(str(topdown_image_path))
        ax.imshow(image, extent=support_extent, origin="lower", alpha=0.55)

    ax.add_patch(
        Rectangle(
            (support_extent[0], support_extent[2]),
            support_extent[1] - support_extent[0],
            support_extent[3] - support_extent[2],
            fill=False,
            linestyle="--",
            linewidth=1.0,
            edgecolor="#666666",
        )
    )
    ax.add_patch(
        Rectangle(
            (valid_extent[0], valid_extent[2]),
            valid_extent[1] - valid_extent[0],
            valid_extent[3] - valid_extent[2],
            fill=False,
            linestyle="-",
            linewidth=1.0,
            edgecolor="#00aa55",
        )
    )

    for route in routes.values():
        polyline = route.get("polyline") or []
        if len(polyline) < 2:
            continue
        xs = [float(item["x"]) for item in polyline]
        ys = [float(item["y"]) for item in polyline]
        ax.plot(xs, ys, color="#1177cc", linewidth=1.2, alpha=0.65)

    for building in building_rectangles_from_manifest(manifest):
        corners = rectangle_corners(building["center_xy"], building["extent_xy"], building["yaw_deg"])
        ax.add_patch(
            Polygon(
                corners,
                closed=True,
                facecolor="#404040",
                edgecolor="#202020",
                linewidth=0.4,
                alpha=0.35,
            )
        )

    for candidate in tx_selection.get("candidates", []):
        ax.scatter(float(candidate["x"]), float(candidate["y"]), s=10, c="#6f2cff", alpha=0.45)

    label_center = region_center(valid_crop)
    for tx in tx_selection.get("tx_catalog", []):
        pos = tx["position"]
        ax.scatter(float(pos["x"]), float(pos["y"]), s=50, c="#d92525", edgecolors="white", linewidths=0.7, zorder=10)
        ax.text(float(pos["x"]) + 0.8, float(pos["y"]) + 0.8, str(tx["tx_id"]), fontsize=8, color="#a00000")
        ax.plot([float(pos["x"]), float(label_center[0])], [float(pos["y"]), float(label_center[1])], color="#d92525", linewidth=1.0, alpha=0.75)

    ax.scatter(float(label_center[0]), float(label_center[1]), s=28, c="#00aa55", edgecolors="white", linewidths=0.7, zorder=8)
    ax.set_title("TX candidate search and selected placements")
    ax.set_xlabel("CARLA world x [m]")
    ax.set_ylabel("CARLA world y [m]")
    ax.set_xlim(support_extent[0], support_extent[1])
    ax.set_ylim(support_extent[2], support_extent[3])
    ax.set_aspect("equal", adjustable="box")
    fig.tight_layout()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(output_path)
    plt.close(fig)


def load_motion_rows_by_frame(motion_path: Path) -> Dict[int, JsonDict]:
    return {int(row["frame_index"]): row for row in iter_jsonl(motion_path)}


def rasterize_building_mask(manifest: JsonDict, region: JsonDict, resolution: int) -> np.ndarray:
    centers = cell_centers_from_region(region, resolution).reshape(-1, 2)
    mask = np.zeros((centers.shape[0],), dtype=bool)
    for building in building_rectangles_from_manifest(manifest):
        mask |= rectangle_mask(centers, building["center_xy"], building["extent_xy"], building["yaw_deg"])
    return mask.reshape(resolution, resolution)


def rasterize_vehicle_mask_from_motion_row(row: JsonDict, region: JsonDict, resolution: int) -> np.ndarray:
    centers = cell_centers_from_region(region, resolution).reshape(-1, 2)
    mask = np.zeros((centers.shape[0],), dtype=bool)
    for vehicle in row.get("vehicles", []):
        if not bool(vehicle.get("in_support_region", True)):
            continue
        position = vehicle.get("position") or [0.0, 0.0, 0.0]
        bbox = vehicle.get("bbox_extent") or {"x": 0.0, "y": 0.0}
        yaw = float(vehicle.get("mesh_reference_yaw_deg", 0.0)) + float(vehicle.get("yaw_delta_deg_sionna", 0.0))
        mask |= rectangle_mask(
            centers,
            [float(position[0]), float(position[1])],
            [float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))],
            yaw,
        )
    return mask.reshape(resolution, resolution)


def build_traffic_grid_from_motion(
    manifest: JsonDict,
    motion_rows_by_frame: Dict[int, JsonDict],
    frame_indices: Sequence[int],
    region: JsonDict,
    resolution: int,
) -> Tuple[np.ndarray, np.ndarray]:
    building_mask = rasterize_building_mask(manifest, region, resolution)
    traffic = np.zeros((len(frame_indices), resolution, resolution), dtype=np.uint8)
    for idx, frame_index in enumerate(frame_indices):
        row = motion_rows_by_frame[int(frame_index)]
        vehicle_mask = rasterize_vehicle_mask_from_motion_row(row, region, resolution)
        traffic[idx, building_mask] = 1
        traffic[idx, vehicle_mask] = 2
    return traffic, building_mask


def speed_magnitude(actor_row: JsonDict) -> float:
    velocity = actor_row.get("velocity", {})
    return float(
        math.sqrt(
            float(velocity.get("x", 0.0)) ** 2
            + float(velocity.get("y", 0.0)) ** 2
            + float(velocity.get("z", 0.0)) ** 2
        )
    )


def actor_tracks_from_states(actor_states_path: Path) -> Dict[int, List[JsonDict]]:
    tracks: Dict[int, List[JsonDict]] = {}
    for frame_row in iter_jsonl(actor_states_path):
        frame_index = int(frame_row["frame_index"])
        timestamp = float(frame_row["timestamp"])
        for actor in frame_row.get("actors", []):
            actor_copy = dict(actor)
            actor_copy["_frame_index"] = frame_index
            actor_copy["_timestamp"] = timestamp
            tracks.setdefault(int(actor["actor_id"]), []).append(actor_copy)
    return tracks


def actor_center_xy(actor_row: JsonDict) -> np.ndarray:
    transform = actor_row["transform"]
    return np.array([float(transform["x"]), float(transform["y"])], dtype=np.float64)


def actor_yaw_deg(actor_row: JsonDict) -> float:
    return float(actor_row["transform"]["yaw"])


def actor_bbox_extent_xy(actor_row: JsonDict) -> Tuple[float, float]:
    bbox = actor_row.get("bbox_extent", {})
    return float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))


def polygons_from_rectangle(center_xy: Sequence[float], extent_xy: Sequence[float], yaw_deg: float) -> np.ndarray:
    return rectangle_corners(center_xy, extent_xy, yaw_deg)


def polygon_axes(poly: np.ndarray) -> List[np.ndarray]:
    axes: List[np.ndarray] = []
    for idx in range(poly.shape[0]):
        nxt = poly[(idx + 1) % poly.shape[0]]
        edge = nxt - poly[idx]
        normal = np.array([-edge[1], edge[0]], dtype=np.float64)
        norm = float(np.linalg.norm(normal))
        if norm <= 1e-9:
            continue
        axes.append(normal / norm)
    return axes


def polygons_intersect_sat(poly_a: np.ndarray, poly_b: np.ndarray) -> bool:
    for axis in polygon_axes(poly_a) + polygon_axes(poly_b):
        proj_a = poly_a @ axis
        proj_b = poly_b @ axis
        if np.max(proj_a) < np.min(proj_b) or np.max(proj_b) < np.min(proj_a):
            return False
    return True


def corridor_rectangle(tx_xy: Sequence[float], dst_xy: Sequence[float], width_m: float) -> Tuple[np.ndarray, np.ndarray, float]:
    tx = np.array(tx_xy, dtype=np.float64)
    dst = np.array(dst_xy, dtype=np.float64)
    delta = dst - tx
    length = float(np.linalg.norm(delta))
    yaw_deg = float(math.degrees(math.atan2(delta[1], delta[0])))
    center_xy = (tx + dst) / 2.0
    extent_xy = np.array([max(length / 2.0, 0.1), max(float(width_m) / 2.0, 0.1)], dtype=np.float64)
    return center_xy, extent_xy, yaw_deg


def dynamic_rss_summary(tx_dir: Path, static_rss_path: Optional[Path] = None, floor_dbm: float = -200.0) -> JsonDict:
    if static_rss_path is not None and static_rss_path.exists() and (tx_dir / "rss_maps.npz").exists():
        with np.load(tx_dir / "rss_maps.npz") as data:
            rss_stack = np.asarray(data["rss_dbm"], dtype=np.float64)
            building_mask = np.asarray(data["building_mask"], dtype=bool) if "building_mask" in data.files else None
        static = np.asarray(np.load(static_rss_path), dtype=np.float64)
        if building_mask is None:
            building_mask = np.zeros(static.shape, dtype=bool)
        static_valid = np.isfinite(static) & (static > float(floor_dbm))
        dynamic_finite_all = np.all(np.isfinite(rss_stack), axis=0)
        dynamic_above_floor_all = np.all(rss_stack > float(floor_dbm), axis=0)
        common = (~building_mask) & static_valid & dynamic_finite_all & dynamic_above_floor_all
        if not np.any(common):
            return {
                "mask_policy": "valid_non_building_common_mask",
                "finite_cell_count": 0,
                "p50_temporal_range_db": None,
                "p90_temporal_range_db": None,
                "p95_temporal_range_db": None,
                "p99_temporal_range_db": None,
                "max_temporal_range_db": None,
            }
        temporal_range = np.max(rss_stack[:, common], axis=0) - np.min(rss_stack[:, common], axis=0)
        delta = rss_stack[:, common] - static[common][None, :]
        return {
            "mask_policy": "valid_non_building_common_mask",
            "floor_dbm": float(floor_dbm),
            "finite_cell_count": int(temporal_range.size),
            "common_mask_cell_count": int(np.sum(common)),
            "non_building_cell_count": int(np.sum(~building_mask)),
            "static_valid_cell_count": int(np.sum((~building_mask) & static_valid)),
            "dynamic_all_frames_valid_cell_count": int(np.sum((~building_mask) & dynamic_finite_all & dynamic_above_floor_all)),
            "p50_temporal_range_db": float(np.percentile(temporal_range, 50)),
            "p90_temporal_range_db": float(np.percentile(temporal_range, 90)),
            "p95_temporal_range_db": float(np.percentile(temporal_range, 95)),
            "p99_temporal_range_db": float(np.percentile(temporal_range, 99)),
            "max_temporal_range_db": float(np.max(temporal_range)),
            "cells_over_1db": int(np.sum(temporal_range > 1.0)),
            "cells_over_3db": int(np.sum(temporal_range > 3.0)),
            "cells_over_6db": int(np.sum(temporal_range > 6.0)),
            "delta_from_static_db": {
                "p50": float(np.percentile(delta, 50)),
                "p95": float(np.percentile(delta, 95)),
                "min": float(np.min(delta)),
                "max": float(np.max(delta)),
            },
            "abs_delta_from_static_db": {
                "p50": float(np.percentile(np.abs(delta), 50)),
                "p95": float(np.percentile(np.abs(delta), 95)),
                "max": float(np.max(np.abs(delta))),
            },
        }
    meta_path = tx_dir / "rss_heatmap_meta.json"
    return load_json(meta_path).get("dynamic_rss_change_validation", {})


def load_frame_indices_from_rss(tx_dir: Path) -> List[int]:
    with np.load(tx_dir / "rss_maps.npz") as data:
        return [int(item) for item in data["frame_indices"].tolist()]


def lane_adherence_metrics(
    tracks: Dict[int, List[JsonDict]],
    routes_by_id: Dict[str, JsonDict],
    max_offset_m: float,
) -> Tuple[List[JsonDict], List[JsonDict]]:
    violations: List[JsonDict] = []
    details: List[JsonDict] = []
    for actor_id, rows in tracks.items():
        route_id = rows[0].get("route_id")
        route = routes_by_id.get(str(route_id)) if route_id is not None else None
        if route is None:
            continue
        polyline = np.array([[float(item["x"]), float(item["y"])] for item in route.get("polyline", [])], dtype=np.float64)
        if polyline.shape[0] < 2:
            continue
        offsets = [point_polyline_distance(actor_center_xy(row), polyline) for row in rows]
        max_offset = max(offsets) if offsets else 0.0
        details.append(
            {
                "actor_id": int(actor_id),
                "route_id": route_id,
                "max_route_offset_m": float(max_offset),
                "frames_over_threshold": int(sum(value > max_offset_m for value in offsets)),
            }
        )
        consecutive = 0
        longest = 0
        first_bad_frame = None
        for row, offset in zip(rows, offsets):
            if offset > max_offset_m:
                consecutive += 1
                longest = max(longest, consecutive)
                if first_bad_frame is None:
                    first_bad_frame = int(row["_frame_index"])
            else:
                consecutive = 0
        if longest > 0:
            violations.append(
                {
                    "actor_id": int(actor_id),
                    "route_id": route_id,
                    "max_route_offset_m": float(max_offset),
                    "longest_consecutive_violation_frames": int(longest),
                    "first_bad_frame": first_bad_frame,
                }
            )
    return violations, details


def building_collision_metrics(tracks: Dict[int, List[JsonDict]], manifest: JsonDict) -> List[JsonDict]:
    building_rects = building_rectangles_from_manifest(manifest)
    collisions: List[JsonDict] = []
    for actor_id, rows in tracks.items():
        for row in rows:
            vehicle_poly = polygons_from_rectangle(actor_center_xy(row), actor_bbox_extent_xy(row), actor_yaw_deg(row))
            for building in building_rects:
                building_poly = polygons_from_rectangle(building["center_xy"], building["extent_xy"], building["yaw_deg"])
                if polygons_intersect_sat(vehicle_poly, building_poly):
                    collisions.append(
                        {
                            "actor_id": int(actor_id),
                            "frame_index": int(row["_frame_index"]),
                            "building_id": building.get("id"),
                        }
                    )
                    break
            if collisions and collisions[-1]["actor_id"] == int(actor_id):
                break
    return collisions


def vehicle_vehicle_collision_metrics(tracks: Dict[int, List[JsonDict]], shrink_m: float) -> List[JsonDict]:
    rows_by_frame: Dict[int, List[Tuple[int, JsonDict]]] = {}
    for actor_id, rows in tracks.items():
        for row in rows:
            rows_by_frame.setdefault(int(row["_frame_index"]), []).append((int(actor_id), row))

    collisions: List[JsonDict] = []
    seen_pairs = set()
    for frame_index in sorted(rows_by_frame):
        rows = rows_by_frame[frame_index]
        for idx, (actor_a, row_a) in enumerate(rows):
            for actor_b, row_b in rows[idx + 1 :]:
                pair_key = tuple(sorted((actor_a, actor_b)))
                extent_a = actor_bbox_extent_xy(row_a)
                extent_b = actor_bbox_extent_xy(row_b)
                poly_a = polygons_from_rectangle(
                    actor_center_xy(row_a),
                    [max(extent_a[0] - shrink_m, 0.05), max(extent_a[1] - shrink_m, 0.05)],
                    actor_yaw_deg(row_a),
                )
                poly_b = polygons_from_rectangle(
                    actor_center_xy(row_b),
                    [max(extent_b[0] - shrink_m, 0.05), max(extent_b[1] - shrink_m, 0.05)],
                    actor_yaw_deg(row_b),
                )
                if not polygons_intersect_sat(poly_a, poly_b):
                    continue
                center_distance = float(np.linalg.norm(actor_center_xy(row_a) - actor_center_xy(row_b)))
                collisions.append(
                    {
                        "frame_index": int(frame_index),
                        "actor_a": int(actor_a),
                        "actor_b": int(actor_b),
                        "route_a": row_a.get("route_id"),
                        "route_b": row_b.get("route_id"),
                        "center_distance_m": center_distance,
                        "first_collision_for_pair": pair_key not in seen_pairs,
                    }
                )
                seen_pairs.add(pair_key)
    return collisions


def jump_metrics(tracks: Dict[int, List[JsonDict]], max_jump_per_frame_m: float) -> List[JsonDict]:
    violations: List[JsonDict] = []
    for actor_id, rows in tracks.items():
        for prev_row, row in zip(rows[:-1], rows[1:]):
            jump = float(np.linalg.norm(actor_center_xy(row) - actor_center_xy(prev_row)))
            if jump > max_jump_per_frame_m:
                violations.append(
                    {
                        "actor_id": int(actor_id),
                        "prev_frame_index": int(prev_row["_frame_index"]),
                        "frame_index": int(row["_frame_index"]),
                        "jump_m": jump,
                    }
                )
                break
    return violations


def idle_metrics(
    tracks: Dict[int, List[JsonDict]],
    idle_speed_threshold_mps: float,
    max_all_slow_duration_s: float,
    fps: float,
) -> JsonDict:
    actor_rows = []
    per_actor = []
    for actor_id, rows in tracks.items():
        enters_label = any(bool(row.get("in_valid_crop")) for row in rows)
        if not enters_label:
            continue
        speeds = [speed_magnitude(row) for row in rows]
        idle_fraction = float(sum(speed < idle_speed_threshold_mps for speed in speeds) / max(len(speeds), 1))
        per_actor.append(
            {
                "actor_id": int(actor_id),
                "idle_fraction": idle_fraction,
                "entered_label_region": enters_label,
                "max_speed_mps": float(max(speeds) if speeds else 0.0),
            }
        )
        actor_rows.append(rows)

    if not actor_rows:
        return {"per_actor": per_actor, "all_slow_longest_frames": 0, "all_slow_longest_s": 0.0}

    frame_count = min(len(rows) for rows in actor_rows)
    all_slow_flags = []
    for frame_idx in range(frame_count):
        all_slow = all(speed_magnitude(rows[frame_idx]) < idle_speed_threshold_mps for rows in actor_rows)
        all_slow_flags.append(all_slow)
    longest = 0
    current = 0
    for flag in all_slow_flags:
        if flag:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return {
        "per_actor": per_actor,
        "all_slow_longest_frames": int(longest),
        "all_slow_longest_s": float(longest / max(fps, 1e-6)),
        "all_slow_limit_s": float(max_all_slow_duration_s),
    }


def route_mix_metrics(tracks: Dict[int, List[JsonDict]], routes_by_id: Dict[str, JsonDict], min_displacement_m: float) -> JsonDict:
    actors_crossing_label = 0
    straight_count = 0
    turning_count = 0
    displacement_count = 0
    details = []
    for actor_id, rows in tracks.items():
        route_id = str(rows[0].get("route_id")) if rows and rows[0].get("route_id") is not None else None
        route = routes_by_id.get(route_id) if route_id is not None else None
        turn_type = str(route.get("turn_type")) if route is not None else None
        enters_label = any(bool(row.get("in_valid_crop")) for row in rows)
        first_xy = actor_center_xy(rows[0])
        last_xy = actor_center_xy(rows[-1])
        displacement = float(np.linalg.norm(last_xy - first_xy))
        if enters_label:
            actors_crossing_label += 1
        if turn_type == "straight" and enters_label:
            straight_count += 1
        if turn_type not in (None, "straight") and enters_label:
            turning_count += 1
        if displacement >= min_displacement_m:
            displacement_count += 1
        details.append(
            {
                "actor_id": int(actor_id),
                "route_id": route_id,
                "turn_type": turn_type,
                "entered_label_region": enters_label,
                "displacement_m": displacement,
            }
        )
    return {
        "details": details,
        "actors_crossing_label_region": int(actors_crossing_label),
        "straight_actor_count": int(straight_count),
        "turning_actor_count": int(turning_count),
        "actors_over_min_displacement": int(displacement_count),
    }


def vehicle_type_mix_metrics(
    tracks: Dict[int, List[JsonDict]],
    large_vehicle_token: str,
) -> JsonDict:
    token = str(large_vehicle_token).lower().strip()
    details = []
    large_count = 0
    for actor_id, rows in tracks.items():
        vehicle_type = str(rows[0].get("vehicle_type", "")) if rows else ""
        is_large = bool(token and token in vehicle_type.lower())
        if is_large:
            large_count += 1
        details.append(
            {
                "actor_id": int(actor_id),
                "vehicle_type": vehicle_type,
                "is_large_vehicle": is_large,
            }
        )
    return {
        "large_vehicle_count": int(large_count),
        "details": details,
    }


def evaluate_episode_scene_qa(
    actor_states_path: Path,
    routes_path: Path,
    manifest: JsonDict,
    fps: float,
    config: EpisodeQAConfig,
) -> JsonDict:
    tracks = actor_tracks_from_states(actor_states_path)
    routes_by_id = route_lookup(routes_path)
    lane_violations, lane_details = lane_adherence_metrics(tracks, routes_by_id, config.lane_adherence_max_offset_m)
    lane_blockers = [
        row
        for row in lane_violations
        if int(row["longest_consecutive_violation_frames"]) > int(config.lane_adherence_max_consecutive_frames)
    ]
    building_collisions = building_collision_metrics(tracks, manifest)
    vehicle_collisions = vehicle_vehicle_collision_metrics(tracks, config.vehicle_collision_shrink_m)
    jumps = jump_metrics(tracks, config.max_jump_per_frame_m)
    idle = idle_metrics(tracks, config.idle_speed_threshold_mps, config.max_all_slow_duration_s, fps)
    route_mix = route_mix_metrics(tracks, routes_by_id, config.min_displacement_m)
    vehicle_mix = vehicle_type_mix_metrics(tracks, config.large_vehicle_token)

    idle_actor_blockers = [
        row for row in idle.get("per_actor", []) if float(row["idle_fraction"]) > float(config.max_idle_fraction)
    ]
    all_slow_blocker = float(idle.get("all_slow_longest_s", 0.0)) > float(config.max_all_slow_duration_s)
    crossing_ok = int(route_mix["actors_crossing_label_region"]) >= int(config.min_vehicles_crossing_label)
    straight_ok = (not config.require_straight_vehicle) or int(route_mix["straight_actor_count"]) >= 1
    turning_ok = (not config.require_turning_vehicle) or int(route_mix["turning_actor_count"]) >= 1
    displacement_ok = int(route_mix["actors_over_min_displacement"]) >= int(config.min_vehicles_crossing_label)
    large_vehicle_ok = int(vehicle_mix["large_vehicle_count"]) <= int(config.max_large_vehicle_count)

    hard_blockers = []
    warnings = []
    traffic_visual_blockers = []
    propagation_blockers = []
    if lane_blockers:
        warnings.append("lane_adherence")
    if building_collisions:
        hard_blockers.append("building_collision")
        traffic_visual_blockers.append("building_collision")
    if vehicle_collisions:
        hard_blockers.append("vehicle_collision")
        traffic_visual_blockers.append("vehicle_collision")
    if jumps:
        hard_blockers.append("frame_jump")
        traffic_visual_blockers.append("frame_jump")
    if idle_actor_blockers:
        warnings.append("idle_fraction")
    if all_slow_blocker:
        hard_blockers.append("all_slow")
        traffic_visual_blockers.append("all_slow")
    if not large_vehicle_ok:
        hard_blockers.append("too_many_large_vehicles")

    return {
        "config": asdict(config),
        "scene_qc_pass": not hard_blockers,
        "traffic_visual_pass": not traffic_visual_blockers,
        "propagation_candidate_pass": not propagation_blockers,
        "blockers": hard_blockers,
        "warnings": warnings,
        "traffic_visual_blockers": traffic_visual_blockers,
        "propagation_blockers": propagation_blockers,
        "lane_adherence": {
            "details": lane_details,
            "violations": lane_blockers,
        },
        "building_collision": {
            "violations": building_collisions,
        },
        "vehicle_collision": {
            "shrink_m": float(config.vehicle_collision_shrink_m),
            "violations": vehicle_collisions,
        },
        "frame_jump": {
            "violations": jumps,
        },
        "idle_stop": {
            "details": idle,
            "actor_idle_fraction_violations": idle_actor_blockers,
            "all_slow_blocker": bool(all_slow_blocker),
        },
        "route_mix": {
            **route_mix,
            "crossing_ok": bool(crossing_ok),
            "straight_ok": bool(straight_ok),
            "turning_ok": bool(turning_ok),
            "displacement_ok": bool(displacement_ok),
            "decision_policy": "diagnostic_only_not_used_for_accept_reject_or_index",
        },
        "vehicle_mix": {
            **vehicle_mix,
            "large_vehicle_ok": bool(large_vehicle_ok),
        },
    }


def evaluate_episode_tx_qa(
    tx_entry: JsonDict,
    manifest: JsonDict,
    motion_rows_by_frame: Dict[int, JsonDict],
    frame_indices: Sequence[int],
    tx_rss_dir: Path,
    config: TxQAConfig,
    static_rss_path: Optional[Path] = None,
) -> JsonDict:
    tx_xy = np.array([float(tx_entry["position"]["x"]), float(tx_entry["position"]["y"])], dtype=np.float64)
    label_center = region_center(manifest["valid_crop"])
    corridor_center, corridor_extent, corridor_yaw_deg = corridor_rectangle(tx_xy, label_center, config.corridor_width_m)
    corridor_poly = polygons_from_rectangle(corridor_center, corridor_extent, corridor_yaw_deg)
    min_clearance = float("inf")
    corridor_hits: List[JsonDict] = []
    for frame_index in frame_indices:
        row = motion_rows_by_frame[int(frame_index)]
        for vehicle in row.get("vehicles", []):
            position = vehicle.get("position") or [0.0, 0.0, 0.0]
            center_xy = np.array([float(position[0]), float(position[1])], dtype=np.float64)
            min_clearance = min(min_clearance, float(np.linalg.norm(center_xy - tx_xy)))
            yaw = float(vehicle.get("mesh_reference_yaw_deg", 0.0)) + float(vehicle.get("yaw_delta_deg_sionna", 0.0))
            bbox = vehicle.get("bbox_extent") or {"x": 0.0, "y": 0.0}
            vehicle_poly = polygons_from_rectangle(center_xy, [float(bbox.get("x", 0.0)), float(bbox.get("y", 0.0))], yaw)
            if polygons_intersect_sat(vehicle_poly, corridor_poly):
                corridor_hits.append(
                    {
                        "frame_index": int(frame_index),
                        "actor_id": int(vehicle["actor_id"]),
                        "sionna_object": str(vehicle["sionna_object"]),
                    }
                )
    rss_summary = dynamic_rss_summary(tx_rss_dir, static_rss_path=static_rss_path)
    p95_range = rss_summary.get("p95_temporal_range_db")
    max_range = rss_summary.get("max_temporal_range_db")
    dynamic_ok = False
    if p95_range is not None and max_range is not None:
        dynamic_ok = (
            float(p95_range) >= float(config.min_p95_temporal_range_db)
            or float(max_range) >= float(config.min_max_temporal_range_db)
        )
    blockers = []
    if not dynamic_ok:
        blockers.append("dynamic_rss_change")
    return {
        "tx_id": str(tx_entry["tx_id"]),
        "config": asdict(config),
        "pair_qc_pass": not blockers,
        "blockers": blockers,
        "min_vehicle_center_clearance_m": None if math.isinf(min_clearance) else float(min_clearance),
        "diagnostic_clearance_reference_m": float(config.diagnostic_clearance_reference_m),
        "corridor_crossings": corridor_hits,
        "geometry_diagnostics_policy": "clearance_and_corridor_crossings_not_used_for_accept_reject_or_index",
        "dynamic_rss_change": rss_summary,
    }


def deterministic_split(episode_ids: Sequence[str], seed: int = 7) -> JsonDict:
    ids = sorted(episode_ids)
    rng = random.Random(seed)
    rng.shuffle(ids)
    n = len(ids)
    train_end = int(round(n * 0.70))
    val_end = int(round(n * 0.85))
    return {
        "seed": int(seed),
        "train": ids[:train_end],
        "val": ids[train_end:val_end],
        "test": ids[val_end:],
    }


def summarize_episode_for_index(
    episode_id: str,
    episode_meta: JsonDict,
    qa_report: JsonDict,
    tx_reports: Sequence[JsonDict],
    frame_count: int,
) -> List[JsonDict]:
    accepted_tx_ids = {str(report["tx_id"]) for report in tx_reports if bool(report.get("pair_qc_pass"))}
    rows = []
    for report in tx_reports:
        tx_id = str(report["tx_id"])
        rows.append(
            {
                "episode_id": episode_id,
                "tx_id": tx_id,
                "accepted": bool(report.get("pair_qc_pass")) and bool(qa_report.get("scene_qc_pass")),
                "scene_qc_pass": bool(qa_report.get("scene_qc_pass")),
                "tx_qc_pass": bool(report.get("pair_qc_pass")),
                "accepted_tx_ids": sorted(accepted_tx_ids),
                "frame_count": int(frame_count),
                "fps": float(episode_meta.get("fps", 0.0)),
                "town": episode_meta.get("town"),
                "scene_mode": episode_meta.get("scene_mode"),
                "actual_total_vehicle_count": int(episode_meta.get("actual_total_vehicle_count", 0)),
                "actual_background_count": int(episode_meta.get("actual_background_count", 0)),
                "actual_required_controlled_count": int(episode_meta.get("actual_required_controlled_count", 0)),
            }
        )
    return rows
