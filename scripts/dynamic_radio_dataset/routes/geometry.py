from __future__ import annotations

import math
from typing import Callable, Sequence

import numpy as np


def region_center(region: dict) -> np.ndarray:
    center = region["center"]
    return np.array([float(center["x"]), float(center["y"])], dtype=np.float64)


def route_polyline(route: dict) -> np.ndarray:
    return np.array([[float(p["x"]), float(p["y"])] for p in route.get("polyline", [])], dtype=np.float64)


def sample_polyline(polyline: np.ndarray, step_m: float = 0.5) -> tuple[np.ndarray, np.ndarray]:
    if polyline.shape[0] < 2:
        return polyline.copy(), np.zeros((polyline.shape[0],), dtype=np.float64)
    points: list[np.ndarray] = []
    distances: list[float] = []
    total = 0.0
    for start, end in zip(polyline[:-1], polyline[1:]):
        seg = end - start
        length = float(np.linalg.norm(seg))
        if length <= 1e-9:
            continue
        count = max(1, int(math.ceil(length / max(step_m, 1e-3))))
        for idx in range(count):
            t = idx / count
            points.append(start * (1.0 - t) + end * t)
            distances.append(total + length * t)
        total += length
    points.append(polyline[-1].copy())
    distances.append(total)
    return np.stack(points, axis=0), np.array(distances, dtype=np.float64)


def first_distance_where(samples: np.ndarray, distances: np.ndarray, predicate: Callable[[np.ndarray], bool]) -> float:
    for point, distance in zip(samples, distances):
        if predicate(point):
            return float(distance)
    return float("inf")


def point_segment_distance(point: np.ndarray, start: np.ndarray, end: np.ndarray) -> float:
    delta = end - start
    denom = float(np.dot(delta, delta))
    if denom <= 1e-9:
        return float(np.linalg.norm(point - start))
    t = max(0.0, min(1.0, float(np.dot(point - start, delta) / denom)))
    return float(np.linalg.norm(point - (start + t * delta)))


def point_polyline_distance(point: np.ndarray, polyline: np.ndarray) -> float:
    if polyline.shape[0] < 2:
        return float(np.linalg.norm(point - polyline[0]))
    return min(point_segment_distance(point, polyline[i], polyline[i + 1]) for i in range(polyline.shape[0] - 1))


def corridor_rectangle(tx_xy: Sequence[float], dst_xy: Sequence[float], width_m: float) -> tuple[np.ndarray, np.ndarray, float]:
    tx = np.array(tx_xy, dtype=np.float64)
    dst = np.array(dst_xy, dtype=np.float64)
    delta = dst - tx
    length = float(np.linalg.norm(delta))
    yaw_deg = float(math.degrees(math.atan2(delta[1], delta[0])))
    return (tx + dst) / 2.0, np.array([max(length / 2.0, 0.1), max(width_m / 2.0, 0.1)]), yaw_deg


def point_in_rotated_rect(point: np.ndarray, center: np.ndarray, extent: np.ndarray, yaw_deg: float) -> bool:
    yaw = math.radians(float(yaw_deg))
    c = math.cos(-yaw)
    s = math.sin(-yaw)
    delta = point - center
    local_x = delta[0] * c - delta[1] * s
    local_y = delta[0] * s + delta[1] * c
    return abs(local_x) <= float(extent[0]) and abs(local_y) <= float(extent[1])

