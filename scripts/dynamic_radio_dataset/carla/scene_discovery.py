from __future__ import annotations

import argparse
import time
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from dynamic_radio_dataset.json_utils import save_json, write_jsonl
from dynamic_radio_dataset.multi_scene.config import candidate_catalog_path, discovery_output_dir, load_multi_scene_config


def discover_scenes(config_path: Path, *, max_candidates: int | None = None) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    discovery = config.get("discovery", {}) if isinstance(config.get("discovery"), dict) else {}
    carla_cfg = discovery.get("carla", config.get("carla", {})) if isinstance(discovery.get("carla", config.get("carla", {})), dict) else {}
    host = str(carla_cfg.get("host", "localhost"))
    port = int(carla_cfg.get("port", 2000))
    towns = [str(item) for item in discovery.get("towns", [discovery.get("town", "Town10HD_Opt")])]
    modes = [str(item) for item in discovery.get("scene_modes", ["junction", "corridor"])]
    limit = int(max_candidates if max_candidates is not None else discovery.get("max_candidates", 80))
    scene_args = _scene_args(discovery.get("scene", {}))
    rows: list[dict[str, Any]] = []
    started = time.time()
    for town in towns:
        world_map = _load_world_map(host, port, town, float(carla_cfg.get("timeout_s", 30.0)))
        if "junction" in modes:
            rows.extend(_junction_candidates(world_map, scene_args))
        if "corridor" in modes:
            rows.extend(_corridor_candidates(world_map, scene_args))
    rows.sort(key=lambda row: (-float(row.get("score", 0.0)), str(row.get("candidate_id", ""))))
    rows = rows[:limit]
    out_path = candidate_catalog_path(config)
    write_jsonl(out_path, rows)
    summary = {
        "schema": "scene_candidate_discovery_summary_v1",
        "candidate_catalog": str(out_path),
        "candidate_count": len(rows),
        "towns": towns,
        "scene_modes": modes,
        "elapsed_s": float(time.time() - started),
        "scene_type_hint_histogram": dict(Counter(str(row.get("scene_type_hint", "unknown")) for row in rows)),
        "mode_histogram": dict(Counter(str(row.get("mode", "unknown")) for row in rows)),
    }
    out_dir = discovery_output_dir(config)
    save_json(out_dir / "scene_discovery_summary.json", summary)
    return summary


def _load_world_map(host: str, port: int, town: str, timeout_s: float):
    from dynamic_radio_dataset.carla.collect import bootstrap_carla_api  # noqa: PLC0415

    bootstrap_carla_api()
    import carla  # type: ignore  # noqa: PLC0415

    client = carla.Client(host, int(port))
    client.set_timeout(float(timeout_s))
    world = client.get_world()
    current = Path(world.get_map().name).name
    wanted = Path(town).name
    if wanted and current != wanted:
        world = client.load_world(wanted)
        time.sleep(2.0)
    return world.get_map()


def _scene_args(raw: dict[str, Any]) -> argparse.Namespace:
    return argparse.Namespace(
        support_size=float(raw.get("support_size", 192.0)),
        valid_size=float(raw.get("valid_size", 96.0)),
        route_step=float(raw.get("route_step", 2.0)),
        route_approach=float(raw.get("route_approach", 25.0)),
        route_exit=float(raw.get("route_exit", 25.0)),
        min_approach=float(raw.get("min_approach", 18.0)),
        min_exit=float(raw.get("min_exit", 18.0)),
        junction_core_radius=float(raw.get("junction_core_radius", 12.0)),
    )


def _junction_candidates(world_map, args: argparse.Namespace) -> list[dict[str, Any]]:
    from dynamic_radio_dataset.carla import collect as cc  # noqa: PLC0415

    grp = _global_route_planner(world_map, args.route_step)
    rows: list[dict[str, Any]] = []
    for junction in cc.collect_junctions(world_map):
        center = junction.bounding_box.location
        support = cc.RectRegion("support_region", center, args.support_size, args.support_size, yaw_deg=0.0)
        valid = cc.RectRegion("valid_crop", center, args.valid_size, args.valid_size, yaw_deg=0.0)
        routes = []
        for idx, pair in enumerate(junction.get_waypoints(cc.carla.LaneType.Driving)):
            route = cc.trace_checked_route(
                grp=grp,
                junction_id=int(junction.id),
                center=center,
                support_region=support,
                entry_wp=pair[0],
                exit_wp=pair[1],
                route_id=f"junction_{int(junction.id):04d}_route_{idx:03d}",
                scene_type="junction",
                args=args,
            )
            if route is not None:
                routes.append(route)
        if not routes:
            continue
        turn_hist = Counter(route.turn_type for route in routes)
        direction_count = cc.route_direction_bins(routes)
        avg_len = sum(cc.polyline_length(route.locations) for route in routes) / len(routes)
        scene_id = f"{_town_slug(world_map.name)}_junction_{int(junction.id):04d}"
        rows.append(
            _descriptor(
                candidate_id=scene_id,
                scene_id=scene_id,
                town=Path(world_map.name).name,
                mode="junction",
                scene_type_hint=_junction_type_hint(turn_hist, direction_count),
                center=center,
                support=support,
                valid=valid,
                routes=routes,
                selector={"mode": "junction", "junction_id": int(junction.id)},
                score=100.0 * direction_count + 10.0 * len(routes) + avg_len,
                extra={"junction_id": int(junction.id), "junction_bbox_extent": cc.loc_to_dict(junction.bounding_box.extent)},
            )
        )
    return rows


def _corridor_candidates(world_map, args: argparse.Namespace) -> list[dict[str, Any]]:
    from dynamic_radio_dataset.carla import collect as cc  # noqa: PLC0415

    grp = _global_route_planner(world_map, args.route_step)
    rows: list[dict[str, Any]] = []
    for idx, (wp0, wp1) in enumerate(world_map.get_topology()):
        if wp0.is_junction or wp1.is_junction:
            continue
        length = wp0.transform.location.distance(wp1.transform.location)
        if length < args.valid_size * 0.6:
            continue
        center = cc.carla.Location(
            x=(wp0.transform.location.x + wp1.transform.location.x) / 2.0,
            y=(wp0.transform.location.y + wp1.transform.location.y) / 2.0,
            z=(wp0.transform.location.z + wp1.transform.location.z) / 2.0,
        )
        yaw = wp0.transform.rotation.yaw
        support = cc.RectRegion("support_region", center, args.support_size, args.support_size, yaw_deg=yaw)
        valid = cc.RectRegion("valid_crop", center, args.valid_size, args.valid_size, yaw_deg=yaw)
        start_wp, _ = cc.extend_waypoint(wp0, -1, args.route_approach, args.route_step)
        end_wp, _ = cc.extend_waypoint(wp1, 1, args.route_exit, args.route_step)
        route_wps, road_options = cc.trace_route_between(grp, start_wp, end_wp, args.route_step, fallback_max_steps=120)
        locations = [wp.transform.location for wp in route_wps]
        if len(locations) < 4 or not all(support.contains(loc) for loc in locations):
            continue
        route = cc.RouteSpec(
            route_id=f"corridor_{idx:04d}_route_000",
            scene_type="corridor",
            start_wp=start_wp,
            end_wp=end_wp,
            route_wps=route_wps,
            road_options=road_options,
            locations=locations,
            turn_type="corridor",
            approach_length=args.route_approach,
            crossing_length=args.valid_size,
            exit_length=args.route_exit,
            min_center_distance=min(cc.dist2d(loc, center) for loc in locations),
            entry_road_id=int(wp0.road_id),
            entry_lane_id=int(wp0.lane_id),
            exit_road_id=int(wp1.road_id),
            exit_lane_id=int(wp1.lane_id),
        )
        scene_id = f"{_town_slug(world_map.name)}_corridor_{idx:04d}"
        rows.append(
            _descriptor(
                candidate_id=scene_id,
                scene_id=scene_id,
                town=Path(world_map.name).name,
                mode="corridor",
                scene_type_hint="corridor_or_mild_bend",
                center=center,
                support=support,
                valid=valid,
                routes=[route],
                selector={"mode": "corridor", "corridor_index": int(idx)},
                score=float(cc.polyline_length(locations)),
                extra={"corridor_index": int(idx), "road_id": int(wp0.road_id), "lane_id": int(wp0.lane_id), "yaw_deg": float(yaw)},
            )
        )
    return rows


def _descriptor(**kwargs) -> dict[str, Any]:
    routes = kwargs["routes"]
    turn_hist = Counter(route.turn_type for route in routes)
    lengths = [float(_route_length(route)) for route in routes]
    return {
        "schema": "scene_candidate_descriptor_v1",
        "candidate_id": kwargs["candidate_id"],
        "scene_id": kwargs["scene_id"],
        "town": kwargs["town"],
        "mode": kwargs["mode"],
        "scene_type_hint": kwargs["scene_type_hint"],
        "center": _loc(kwargs["center"]),
        "support_region": kwargs["support"].to_dict(),
        "valid_crop": kwargs["valid"].to_dict(),
        "selector": kwargs["selector"],
        "route_count": len(routes),
        "turn_type_histogram": {key: int(turn_hist[key]) for key in sorted(turn_hist)},
        "route_length_summary": _summary(lengths),
        "score": float(kwargs["score"]),
        "routes": [route.to_dict(compact=False) for route in routes],
        **kwargs.get("extra", {}),
    }


def _global_route_planner(world_map, route_step: float):
    from dynamic_radio_dataset.carla import collect as cc  # noqa: PLC0415

    if cc.GlobalRoutePlanner is None:
        return None
    try:
        return cc.GlobalRoutePlanner(world_map, float(route_step))
    except Exception:  # noqa: BLE001
        return None


def _junction_type_hint(turn_hist: Counter[str], direction_count: int) -> str:
    turns = set(turn_hist)
    if direction_count >= 4 and ("left" in turns or "right" in turns):
        return "multi_lane_junction"
    if "left" in turns or "right" in turns:
        return "junction_like"
    return "straight_through_junction"


def _route_length(route) -> float:
    from dynamic_radio_dataset.carla import collect as cc  # noqa: PLC0415

    return cc.polyline_length(route.locations)


def _summary(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {"count": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}


def _loc(loc) -> dict[str, float]:
    return {"x": float(loc.x), "y": float(loc.y), "z": float(loc.z)}


def _town_slug(town: str) -> str:
    return Path(str(town)).name.lower().replace("hd_opt", "").strip("_")
