from __future__ import annotations

import math
from typing import Any, Dict, Sequence

import numpy as np


JsonDict = Dict[str, Any]


def region_center(region: JsonDict) -> np.ndarray:
    center = region["center"]
    return np.array([float(center["x"]), float(center["y"])], dtype=np.float64)


def region_size(region: JsonDict) -> tuple[float, float]:
    return float(region["width_m"]), float(region["height_m"])


def region_axes(region: JsonDict) -> tuple[np.ndarray, np.ndarray]:
    yaw = math.radians(float(region.get("yaw_deg", 0.0)))
    x_axis = np.array([math.cos(yaw), math.sin(yaw)], dtype=np.float64)
    y_axis = np.array([-math.sin(yaw), math.cos(yaw)], dtype=np.float64)
    return x_axis, y_axis


def region_extent_for_plot(region: JsonDict) -> tuple[float, float, float, float]:
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
    expected_info = expected.get("scene_info", {}) if isinstance(expected.get("scene_info"), dict) else {}
    actual_info = actual.get("scene_info", {}) if isinstance(actual.get("scene_info"), dict) else {}
    for key in ("junction_id",):
        if expected_info.get(key) != actual_info.get(key):
            return False
    for key in ("junction_center", "junction_bbox_extent"):
        expected_value = expected_info.get(key)
        actual_value = actual_info.get(key)
        if isinstance(expected_value, dict) or isinstance(actual_value, dict):
            if not isinstance(expected_value, dict) or not isinstance(actual_value, dict):
                return False
            for axis in ("x", "y", "z"):
                if abs(float(expected_value[axis]) - float(actual_value[axis])) > tol:
                    return False
        elif expected_value != actual_value:
            return False
    return True


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


def corridor_rectangle(tx_xy: Sequence[float], dst_xy: Sequence[float], width_m: float) -> tuple[np.ndarray, np.ndarray, float]:
    tx = np.array(tx_xy, dtype=np.float64)
    dst = np.array(dst_xy, dtype=np.float64)
    delta = dst - tx
    length = float(np.linalg.norm(delta))
    yaw_deg = float(math.degrees(math.atan2(delta[1], delta[0])))
    center_xy = (tx + dst) / 2.0
    extent_xy = np.array([max(length / 2.0, 0.1), max(float(width_m) / 2.0, 0.1)], dtype=np.float64)
    return center_xy, extent_xy, yaw_deg
