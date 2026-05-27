from __future__ import annotations

import glob
import math
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.regions import rectangle_corners, region_axes, region_center, region_size
from dynamic_radio_dataset.paths import repo_root, resolve_repo_path


LANE_EXCLUSION_METHOD = "roadside_proxy_lane_exclusion_v1"


@dataclass(frozen=True)
class CompiledDrivableExclusion:
    centers_xy: np.ndarray
    extent_x: np.ndarray
    extent_y: np.ndarray
    cos_neg_yaw: np.ndarray
    sin_neg_yaw: np.ndarray
    bbox_radius: np.ndarray


def build_drivable_lane_exclusion(scene_meta: Mapping[str, Any], config: Mapping[str, Any]) -> dict[str, Any]:
    """Build a local drivable-lane exclusion sidecar from CARLA/OpenDRIVE waypoints.

    The geometry is intentionally conservative and small in scope: each CARLA
    driving-lane waypoint near the scene is approximated as an inflated oriented
    rectangle. TX placement can then reject candidate points that fall inside any
    of these rectangles without changing route generation, traffic plans, or QA.
    """

    tx_cfg = config.get("tx", {}) if isinstance(config.get("tx"), Mapping) else {}
    placement_cfg = tx_cfg.get("placement", {}) if isinstance(tx_cfg.get("placement"), Mapping) else {}
    lane_cfg = placement_cfg.get("lane_exclusion", {}) if isinstance(placement_cfg.get("lane_exclusion"), Mapping) else {}
    sample_step_m = float(lane_cfg.get("lane_sample_step_m", lane_cfg.get("sample_step_m", 2.0)))
    if sample_step_m <= 0.0:
        raise ValueError("tx.placement.lane_exclusion.lane_sample_step_m must be positive")
    inflation_margin_m = float(lane_cfg.get("lane_inflation_margin_m", lane_cfg.get("inflation_margin_m", 1.5)))
    if inflation_margin_m < 0.0:
        raise ValueError("tx.placement.lane_exclusion.lane_inflation_margin_m must be non-negative")
    town = _town_name(scene_meta, config)
    center = _scene_center(scene_meta)
    support_region = scene_meta.get("support_region") if isinstance(scene_meta.get("support_region"), Mapping) else None
    radius_m = _exclusion_radius_m(scene_meta, placement_cfg, lane_cfg)
    crop_padding_m = max(sample_step_m, inflation_margin_m) + float(lane_cfg.get("crop_padding_m", 4.0))

    carla = _load_carla_module()
    carla_map, opendrive_path = _load_carla_map(carla, town, config)
    waypoints = carla_map.generate_waypoints(sample_step_m)
    rectangles: list[dict[str, Any]] = []
    sampled_count = 0
    driving_count = 0
    for waypoint in waypoints:
        sampled_count += 1
        if not _is_driving_waypoint(carla, waypoint):
            continue
        driving_count += 1
        loc = waypoint.transform.location
        point_xy = np.array([float(loc.x), float(loc.y)], dtype=np.float64)
        if float(np.linalg.norm(point_xy - center)) > radius_m + crop_padding_m:
            continue
        if support_region is not None and not _region_contains(point_xy, support_region, padding_m=crop_padding_m):
            continue
        lane_width_m = max(0.1, float(getattr(waypoint, "lane_width", 0.0) or 0.0))
        yaw_deg = float(waypoint.transform.rotation.yaw)
        extent_x = max(sample_step_m * 0.55, 0.5)
        extent_y = lane_width_m / 2.0 + inflation_margin_m
        corners = rectangle_corners(point_xy, [extent_x, extent_y], yaw_deg)
        rectangles.append(
            {
                "center_xy": [float(point_xy[0]), float(point_xy[1])],
                "extent_xy": [float(extent_x), float(extent_y)],
                "yaw_deg": yaw_deg,
                "lane_width_m": lane_width_m,
                "road_id": int(getattr(waypoint, "road_id", 0)),
                "section_id": int(getattr(waypoint, "section_id", 0)),
                "lane_id": int(getattr(waypoint, "lane_id", 0)),
                "s": float(getattr(waypoint, "s", 0.0)),
                "is_junction": bool(getattr(waypoint, "is_junction", False)),
                "corners_xy": [[float(x), float(y)] for x, y in corners.tolist()],
            }
        )
    if not rectangles:
        raise RuntimeError(
            f"No CARLA driving-lane waypoints found near scene center "
            f"({center[0]:.3f}, {center[1]:.3f}) in {town}"
        )
    return {
        "schema": "tx_drivable_exclusion_v1",
        "method": "carla_map_waypoint_rectangles_v1",
        "placement_method": LANE_EXCLUSION_METHOD,
        "town": town,
        "opendrive_path": str(opendrive_path) if opendrive_path is not None else None,
        "scene_center": {"x": float(center[0]), "y": float(center[1])},
        "radius_m": float(radius_m),
        "crop_padding_m": float(crop_padding_m),
        "lane_sample_step_m": float(sample_step_m),
        "lane_inflation_margin_m": float(inflation_margin_m),
        "sampled_waypoint_count": int(sampled_count),
        "driving_waypoint_count": int(driving_count),
        "rectangle_count": int(len(rectangles)),
        "rectangles": rectangles,
    }


def compile_drivable_exclusion(exclusion: Mapping[str, Any] | None) -> CompiledDrivableExclusion | None:
    if not exclusion:
        return None
    rectangles = exclusion.get("rectangles", [])
    if not isinstance(rectangles, Sequence) or not rectangles:
        return None
    centers: list[list[float]] = []
    extent_x: list[float] = []
    extent_y: list[float] = []
    yaws: list[float] = []
    for rect in rectangles:
        if not isinstance(rect, Mapping):
            continue
        center = rect.get("center_xy", [])
        extent = rect.get("extent_xy", [])
        if not isinstance(center, Sequence) or not isinstance(extent, Sequence) or len(center) < 2 or len(extent) < 2:
            continue
        centers.append([float(center[0]), float(center[1])])
        extent_x.append(float(extent[0]))
        extent_y.append(float(extent[1]))
        yaws.append(float(rect.get("yaw_deg", 0.0)))
    if not centers:
        return None
    yaw = np.radians(np.array(yaws, dtype=np.float64))
    ex = np.array(extent_x, dtype=np.float64)
    ey = np.array(extent_y, dtype=np.float64)
    return CompiledDrivableExclusion(
        centers_xy=np.array(centers, dtype=np.float64),
        extent_x=ex,
        extent_y=ey,
        cos_neg_yaw=np.cos(-yaw),
        sin_neg_yaw=np.sin(-yaw),
        bbox_radius=np.sqrt(ex * ex + ey * ey),
    )


def point_inside_drivable_exclusion(point_xy: Sequence[float] | np.ndarray, compiled: CompiledDrivableExclusion | None) -> bool:
    if compiled is None or compiled.centers_xy.size == 0:
        return False
    point = np.array(point_xy, dtype=np.float64)
    delta = point[None, :] - compiled.centers_xy
    near = (np.abs(delta[:, 0]) <= compiled.bbox_radius) & (np.abs(delta[:, 1]) <= compiled.bbox_radius)
    if not np.any(near):
        return False
    dx = delta[near, 0]
    dy = delta[near, 1]
    cosv = compiled.cos_neg_yaw[near]
    sinv = compiled.sin_neg_yaw[near]
    local_x = dx * cosv - dy * sinv
    local_y = dx * sinv + dy * cosv
    return bool(np.any((np.abs(local_x) <= compiled.extent_x[near]) & (np.abs(local_y) <= compiled.extent_y[near])))


def count_points_inside_drivable_exclusion(
    points_xy: Sequence[Sequence[float]] | np.ndarray,
    compiled: CompiledDrivableExclusion | None,
) -> int:
    if compiled is None:
        return 0
    return sum(1 for point in points_xy if point_inside_drivable_exclusion(point, compiled))


def _load_carla_module():
    try:
        import carla  # type: ignore

        return carla
    except ImportError:
        pass
    for egg in sorted(glob.glob(str(repo_root() / "PythonAPI" / "carla" / "dist" / "carla-*-linux-x86_64.egg"))):
        if egg not in sys.path:
            sys.path.insert(0, egg)
    python_api = repo_root() / "PythonAPI" / "carla"
    if str(python_api) not in sys.path:
        sys.path.insert(0, str(python_api))
    import carla  # type: ignore

    return carla


def _load_carla_map(carla: Any, town: str, config: Mapping[str, Any]):
    carla_cfg = config.get("carla", {}) if isinstance(config.get("carla"), Mapping) else {}
    raw_path = carla_cfg.get("opendrive_path")
    candidates: list[Path] = []
    if raw_path:
        candidates.append(resolve_repo_path(str(raw_path)))
    candidates.extend(
        [
            repo_root() / "CarlaUE4" / "Content" / "Carla" / "Maps" / "OpenDrive" / f"{town}.xodr",
            repo_root() / "CarlaUE4" / "Content" / "Carla" / "Maps" / "OpenDrive" / f"{town.replace('_Opt', '')}.xodr",
        ]
    )
    for path in candidates:
        if path.exists():
            return carla.Map(town, path.read_text(encoding="utf-8", errors="replace")), path
    raise FileNotFoundError(f"Could not find OpenDRIVE file for town {town!r}; tried {[str(path) for path in candidates]}")


def _town_name(scene_meta: Mapping[str, Any], config: Mapping[str, Any]) -> str:
    town = scene_meta.get("town")
    if town:
        return Path(str(town)).name
    scene_cfg = config.get("scene", {}) if isinstance(config.get("scene"), Mapping) else {}
    town = scene_cfg.get("town")
    if town:
        return Path(str(town)).name
    raise ValueError("Cannot build lane exclusion without scene_meta.town or config.scene.town")


def _scene_center(scene_meta: Mapping[str, Any]) -> np.ndarray:
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), Mapping) else {}
    if isinstance(info.get("junction_center"), Mapping):
        c = info["junction_center"]
        return np.array([float(c["x"]), float(c["y"])], dtype=np.float64)
    crop = scene_meta.get("valid_crop") or scene_meta.get("support_region")
    if not isinstance(crop, Mapping):
        raise ValueError("scene_meta needs valid_crop or support_region")
    return region_center(crop)


def _exclusion_radius_m(
    scene_meta: Mapping[str, Any],
    placement_cfg: Mapping[str, Any],
    lane_cfg: Mapping[str, Any],
) -> float:
    if lane_cfg.get("radius_m") is not None:
        return float(lane_cfg["radius_m"])
    placement_radius = float(placement_cfg.get("max_distance_to_scene_center_m", 42.0))
    route_band = float(placement_cfg.get("max_distance_to_road_m", 8.0))
    support = scene_meta.get("support_region") if isinstance(scene_meta.get("support_region"), Mapping) else None
    if support is not None:
        width, height = region_size(support)
        support_radius = 0.5 * math.hypot(width, height)
        return max(placement_radius + route_band + 8.0, support_radius)
    return placement_radius + route_band + 20.0


def _region_contains(point_xy: np.ndarray, region: Mapping[str, Any], *, padding_m: float) -> bool:
    center = region_center(region)
    x_axis, y_axis = region_axes(region)
    width, height = region_size(region)
    delta = point_xy - center
    local_x = float(delta @ x_axis)
    local_y = float(delta @ y_axis)
    return abs(local_x) <= width / 2.0 + padding_m and abs(local_y) <= height / 2.0 + padding_m


def _is_driving_waypoint(carla: Any, waypoint: Any) -> bool:
    lane_type = getattr(waypoint, "lane_type", None)
    driving = getattr(getattr(carla, "LaneType", None), "Driving", None)
    if driving is None or lane_type is None:
        return True
    try:
        return bool(lane_type == driving or lane_type & driving)
    except Exception:  # noqa: BLE001
        return "Driving" in str(lane_type)
