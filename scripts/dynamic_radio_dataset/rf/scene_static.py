from __future__ import annotations

import math
import sys
import time
from dataclasses import asdict, dataclass
from glob import glob
from pathlib import Path
from typing import Any, Dict

import numpy as np

from dynamic_radio_dataset.geometry.collision import (
    building_rectangles_from_manifest,
    inside_any_building,
    min_building_clearance,
)
from dynamic_radio_dataset.geometry.regions import (
    min_distance_to_polylines,
    normalize_angle_deg,
    rectangle_corners,
    region_center,
    region_extent_for_plot,
    region_size,
    scene_signature,
)


JsonDict = Dict[str, Any]


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


def route_polylines(routes: dict[str, JsonDict]) -> list[np.ndarray]:
    polylines = []
    for route in routes.values():
        polyline = route.get("polyline") or []
        if len(polyline) < 2:
            continue
        polylines.append(
            np.array([[float(item["x"]), float(item["y"])] for item in polyline], dtype=np.float64)
        )
    return polylines


def select_tx_candidates(
    scene_meta: JsonDict,
    routes: dict[str, JsonDict],
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
    candidates: list[JsonDict] = []
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

        def lane_probe(x: float, y: float) -> dict[str, object]:
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
    selected: list[JsonDict] = []
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
    routes: dict[str, JsonDict],
    manifest: JsonDict,
    tx_selection: JsonDict,
    output_path: Path,
    topdown_image_path: Path | None = None,
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
    from matplotlib import image as mpimg
    from matplotlib.patches import Polygon, Rectangle

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
