from __future__ import annotations

from typing import Any, Dict, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.collision import building_rectangles_from_manifest
from dynamic_radio_dataset.geometry.regions import cell_centers_from_region, rectangle_mask


JsonDict = Dict[str, Any]


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
    motion_rows_by_frame: dict[int, JsonDict],
    frame_indices: Sequence[int],
    region: JsonDict,
    resolution: int,
) -> tuple[np.ndarray, np.ndarray]:
    building_mask = rasterize_building_mask(manifest, region, resolution)
    traffic = np.zeros((len(frame_indices), resolution, resolution), dtype=np.uint8)
    for idx, frame_index in enumerate(frame_indices):
        row = motion_rows_by_frame[int(frame_index)]
        vehicle_mask = rasterize_vehicle_mask_from_motion_row(row, region, resolution)
        traffic[idx, building_mask] = 1
        traffic[idx, vehicle_mask] = 2
    return traffic, building_mask
