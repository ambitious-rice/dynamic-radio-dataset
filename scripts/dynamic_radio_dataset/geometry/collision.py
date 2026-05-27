from __future__ import annotations

import math
from typing import Any, Dict, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.regions import rectangle_corners


JsonDict = Dict[str, Any]


def building_rectangles_from_manifest(manifest: JsonDict) -> list[JsonDict]:
    result: list[JsonDict] = []
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


def polygons_from_rectangle(center_xy: Sequence[float], extent_xy: Sequence[float], yaw_deg: float) -> np.ndarray:
    return rectangle_corners(center_xy, extent_xy, yaw_deg)


def polygon_axes(poly: np.ndarray) -> list[np.ndarray]:
    axes: list[np.ndarray] = []
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
