from __future__ import annotations

import math
import random
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from dynamic_radio_dataset.geometry.collision import building_rectangles_from_manifest, inside_any_building
from dynamic_radio_dataset.geometry.regions import min_distance_to_polylines, region_center, scene_signature
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.raster.traffic_grid import rasterize_building_mask
from dynamic_radio_dataset.tx.lane_exclusion import (
    LANE_EXCLUSION_METHOD,
    build_drivable_lane_exclusion,
    compile_drivable_exclusion,
    count_points_inside_drivable_exclusion,
    point_inside_drivable_exclusion,
)


def generate_tx_catalog(
    config: dict,
    *,
    output_dir: Path | None = None,
    placement_method: str | None = None,
    sidecar_label: str | None = None,
) -> dict[str, Any]:
    root = dataset_root(config)
    static_dir = output_dir or (root / "scene_static")
    reference_dir = root / "reference_scene"
    scene_meta = load_json(reference_dir / "scene_meta.json")
    routes = load_json(reference_dir / "routes.json").get("routes", [])
    manifest_path = reference_dir / "sionna_export" / "manifest.json"
    manifest = load_json(manifest_path) if manifest_path.exists() else {}
    placement = generate_tx_candidates(scene_meta, routes, manifest, config, placement_method=placement_method)
    static_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = static_dir / ("tx_catalog.json" if sidecar_label is None else f"tx_catalog_{sidecar_label}.json")
    summary_path = static_dir / (
        "tx_placement_summary.json" if sidecar_label is None else f"tx_placement_summary_{sidecar_label}.json"
    )
    save_json(catalog_path, placement["tx_catalog_bundle"])
    save_json(summary_path, placement["summary"])
    drivable_exclusion_path: Path | None = None
    if placement.get("drivable_exclusion"):
        drivable_exclusion_path = static_dir / "tx_drivable_exclusion.json"
        save_json(drivable_exclusion_path, placement["drivable_exclusion"])
    if sidecar_label is None:
        save_json(static_dir / "scene_signature.json", scene_signature(scene_meta))
    if sidecar_label is None and manifest:
        resolution = int(config.get("sionna", {}).get("resolution", 128)) if isinstance(config.get("sionna"), Mapping) else 128
        building_mask = rasterize_building_mask(manifest, manifest["valid_crop"], resolution)
        np.save(static_dir / "building_mask_uint8.npy", building_mask.astype(np.uint8))
        np.save(static_dir / "loss_mask_uint8.npy", (~building_mask).astype(np.uint8))
    if sidecar_label is None:
        save_json(
            static_dir / "scene_static_meta.json",
            {
                "schema": "multi_scene_static_bundle_v1",
                "scene_signature": scene_signature(scene_meta),
                "reference_dataset_dir": str(reference_dir),
                "reference_export_dir": str(reference_dir / "sionna_export"),
                "tx_catalog": str(static_dir / "tx_catalog.json"),
                "static_rss_cache_policy": "not_generated_here_run_prepare_rf_cache",
            },
        )
    result = {"tx_catalog": str(catalog_path), "summary": placement["summary"], "summary_path": str(summary_path)}
    if drivable_exclusion_path is not None:
        result["drivable_exclusion"] = str(drivable_exclusion_path)
    return result


def generate_tx_candidates(
    scene_meta: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    manifest: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    placement_method: str | None = None,
) -> dict[str, Any]:
    tx_cfg = config.get("tx", {}) if isinstance(config.get("tx"), Mapping) else {}
    placement_cfg = tx_cfg.get("placement", {}) if isinstance(tx_cfg.get("placement"), Mapping) else {}
    requested_method = _requested_method(placement_cfg, placement_method)
    effective_method = requested_method
    count = int(tx_cfg.get("candidate_count", tx_cfg.get("tx_candidate_count", config.get("tx_candidates_per_scene", 20))))
    seed = int(tx_cfg.get("placement_seed", config.get("dataset", {}).get("seed", 1701) if isinstance(config.get("dataset"), Mapping) else 1701))
    rng = random.Random(seed)
    center = _scene_center(scene_meta)
    polylines = [_route_polyline(route) for route in routes]
    polylines = [poly for poly in polylines if poly.shape[0] >= 2]
    buildings = building_rectangles_from_manifest(dict(manifest)) if manifest else []
    warnings: list[str] = []
    drivable_exclusion: dict[str, Any] | None = None
    compiled_exclusion = None
    lane_cfg = placement_cfg.get("lane_exclusion", {}) if isinstance(placement_cfg.get("lane_exclusion"), Mapping) else {}
    lane_enabled = bool(lane_cfg.get("enabled", requested_method == LANE_EXCLUSION_METHOD))
    if requested_method == LANE_EXCLUSION_METHOD:
        if lane_enabled:
            try:
                drivable_exclusion = build_drivable_lane_exclusion(scene_meta, config)
                compiled_exclusion = compile_drivable_exclusion(drivable_exclusion)
                if compiled_exclusion is None:
                    raise RuntimeError("generated drivable lane exclusion has no usable rectangles")
            except Exception as exc:  # noqa: BLE001
                effective_method = "roadside_proxy"
                drivable_exclusion = None
                compiled_exclusion = None
                warnings.append(
                    "lane_exclusion_generation_failed_fallback_to_roadside_proxy:"
                    f"{type(exc).__name__}: {exc}"
                )
        else:
            effective_method = "roadside_proxy"
            warnings.append("lane_exclusion_disabled_fallback_to_roadside_proxy")
    params = {
        "candidate_count": count,
        "tx_height_m": float(tx_cfg.get("tx_height_m", tx_cfg.get("height_m", config.get("sionna", {}).get("tx_height_m", 1.5) if isinstance(config.get("sionna"), Mapping) else 1.5))),
        "min_distance_to_road_m": float(placement_cfg.get("min_distance_to_road_m", 2.0)),
        "max_distance_to_road_m": float(placement_cfg.get("max_distance_to_road_m", 8.0)),
        "min_pairwise_tx_distance_m": float(placement_cfg.get("min_pairwise_tx_distance_m", 8.0)),
        "max_distance_to_scene_center_m": float(placement_cfg.get("max_distance_to_scene_center_m", 42.0)),
        "grid_step_m": float(placement_cfg.get("grid_step_m", 1.5)),
        "placement_seed": seed,
    }
    rejected: Counter[str] = Counter()
    pool = _candidate_pool(center, params, rng)
    scored: list[dict[str, Any]] = []
    for point in pool:
        point_xy = np.array(point, dtype=np.float64)
        d_center = float(np.linalg.norm(point_xy - center))
        if d_center > params["max_distance_to_scene_center_m"]:
            rejected["too_far_from_center"] += 1
            continue
        if inside_any_building(point_xy, buildings):
            rejected["inside_building"] += 1
            continue
        d_road = min_distance_to_polylines(point_xy, polylines)
        if not math.isfinite(d_road):
            rejected["no_route_geometry"] += 1
            continue
        if d_road < params["min_distance_to_road_m"]:
            rejected["too_close_to_route_centerline"] += 1
            continue
        if d_road > params["max_distance_to_road_m"]:
            rejected["too_far_from_route"] += 1
            continue
        if compiled_exclusion is not None and point_inside_drivable_exclusion(point_xy, compiled_exclusion):
            rejected["inside_drivable_lane"] += 1
            continue
        scored.append(_candidate_row(point_xy, center, polylines, d_road, params))
    selected = _select_with_relaxation(scored, count, float(params["min_pairwise_tx_distance_m"]), warnings)
    if len(selected) < count:
        raise RuntimeError(f"Only found {len(selected)} TX candidates; need {count}. rejection_histogram={dict(rejected)}")
    tx_catalog = [_tx_entry(row, index, params, center, config, effective_method) for index, row in enumerate(selected[:count])]
    bundle = {
        "schema": "multi_scene_tx_catalog_v1",
        "placement_method": effective_method,
        "requested_placement_method": requested_method,
        "placement_seed": seed,
        "selected_count": len(tx_catalog),
        "tx_catalog": tx_catalog,
    }
    inside_accepted = (
        count_points_inside_drivable_exclusion([[float(row["x"]), float(row["y"])] for row in selected[:count]], compiled_exclusion)
        if compiled_exclusion is not None
        else None
    )
    summary = _summary(
        bundle,
        selected[:count],
        rejected,
        params,
        warnings,
        requested_method=requested_method,
        effective_method=effective_method,
        drivable_exclusion=drivable_exclusion,
        lane_enabled=lane_enabled,
        inside_drivable_lane_accepted=inside_accepted,
    )
    return {"tx_catalog_bundle": bundle, "summary": summary, "drivable_exclusion": drivable_exclusion}


def _requested_method(placement_cfg: Mapping[str, Any], override: str | None) -> str:
    method = str(override or placement_cfg.get("method", "roadside_proxy")).strip() or "roadside_proxy"
    if method not in {"roadside_proxy", LANE_EXCLUSION_METHOD}:
        raise ValueError(f"Unsupported TX placement method: {method}")
    return method


def _scene_center(scene_meta: Mapping[str, Any]) -> np.ndarray:
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), Mapping) else {}
    if isinstance(info.get("junction_center"), Mapping):
        c = info["junction_center"]
        return np.array([float(c["x"]), float(c["y"])], dtype=np.float64)
    return region_center(scene_meta["valid_crop"])


def _route_polyline(route: Mapping[str, Any]) -> np.ndarray:
    return np.array([[float(p["x"]), float(p["y"])] for p in route.get("polyline", []) if isinstance(p, Mapping)], dtype=np.float64)


def _candidate_pool(center: np.ndarray, params: Mapping[str, float], rng: random.Random) -> list[tuple[float, float]]:
    radius = float(params["max_distance_to_scene_center_m"])
    step = float(params["grid_step_m"])
    points: list[tuple[float, float]] = []
    x_values = np.arange(center[0] - radius, center[0] + radius + 1e-6, step)
    y_values = np.arange(center[1] - radius, center[1] + radius + 1e-6, step)
    for x in x_values:
        for y in y_values:
            if float(np.linalg.norm(np.array([x, y]) - center)) <= radius:
                points.append((float(x), float(y)))
    rng.shuffle(points)
    return points


def _candidate_row(point: np.ndarray, center: np.ndarray, polylines: Sequence[np.ndarray], d_road: float, params: Mapping[str, float]) -> dict[str, Any]:
    d_center = float(np.linalg.norm(point - center))
    target_road = 0.5 * (float(params["min_distance_to_road_m"]) + float(params["max_distance_to_road_m"]))
    score = -abs(d_road - target_road) - 0.02 * d_center
    nearest_route_index = int(np.argmin([min_distance_to_polylines(point, [poly]) for poly in polylines])) if polylines else -1
    return {
        "x": float(point[0]),
        "y": float(point[1]),
        "distance_to_center_m": d_center,
        "distance_to_nearest_route_m": float(d_road),
        "nearest_route_id": None if nearest_route_index < 0 else nearest_route_index,
        "azimuth_deg": float(math.degrees(math.atan2(point[1] - center[1], point[0] - center[0]))),
        "score": float(score),
    }


def _select_spaced(rows: Sequence[Mapping[str, Any]], count: int, min_distance: float) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda item: float(item["score"]), reverse=True):
        p = np.array([float(row["x"]), float(row["y"])], dtype=np.float64)
        if any(float(np.linalg.norm(p - np.array([float(other["x"]), float(other["y"])], dtype=np.float64))) < min_distance for other in selected):
            continue
        selected.append(dict(row))
        if len(selected) >= count:
            break
    return selected


def _select_with_relaxation(rows: Sequence[Mapping[str, Any]], count: int, configured_min_distance: float, warnings: list[str]) -> list[dict[str, Any]]:
    spacings: list[float] = [float(configured_min_distance), max(2.0, float(configured_min_distance) * 0.6), 3.0, 2.0]
    unique_spacings: list[float] = []
    for spacing in spacings:
        spacing = max(0.0, float(spacing))
        if unique_spacings and spacing >= unique_spacings[-1] - 1e-6:
            continue
        if all(abs(spacing - existing) > 1e-6 for existing in unique_spacings):
            unique_spacings.append(spacing)
    selected: list[dict[str, Any]] = []
    for idx, spacing in enumerate(unique_spacings):
        selected = _select_spaced(rows, count, spacing)
        if len(selected) >= count:
            if idx == 1:
                warnings.append("relaxed_pairwise_spacing_once")
            elif idx > 1:
                warnings.append(f"relaxed_pairwise_spacing_to_{spacing:.1f}m")
            return selected
    return selected


def _tx_entry(row: Mapping[str, Any], index: int, params: Mapping[str, float], center: np.ndarray, config: Mapping[str, Any], placement_method: str) -> dict[str, Any]:
    tx_id = f"tx_{index:02d}"
    sionna = config.get("sionna", {}) if isinstance(config.get("sionna"), Mapping) else {}
    reason = (
        "near_route_centerline_and_outside_drivable_lane_proxy"
        if placement_method == LANE_EXCLUSION_METHOD
        else "near_route_centerline_not_on_route_proxy"
    )
    return {
        "tx_id": tx_id,
        "tx_candidate_id": tx_id,
        "position": {"x": float(row["x"]), "y": float(row["y"]), "z": float(params["tx_height_m"])},
        "tx_power_dbm": float(sionna.get("tx_power_dbm", 23.0)),
        "placement_method": placement_method,
        "placement_reason": reason,
        "distance_to_center_m": float(row["distance_to_center_m"]),
        "distance_to_road_m": float(row["distance_to_nearest_route_m"]),
        "nearest_route_id": row.get("nearest_route_id"),
        "azimuth_deg": float(row["azimuth_deg"]),
        "corridor_to_label_center": {"x0": float(row["x"]), "y0": float(row["y"]), "x1": float(center[0]), "y1": float(center[1]), "width_m": float(config.get("tx", {}).get("corridor_width_m", 10.0) if isinstance(config.get("tx"), Mapping) else 10.0)},
    }


def _summary(
    bundle: Mapping[str, Any],
    selected: Sequence[Mapping[str, Any]],
    rejected: Counter[str],
    params: Mapping[str, float],
    warnings: Sequence[str],
    *,
    requested_method: str,
    effective_method: str,
    drivable_exclusion: Mapping[str, Any] | None,
    lane_enabled: bool,
    inside_drivable_lane_accepted: int | None,
) -> dict[str, Any]:
    pairwise = []
    for idx, a in enumerate(selected):
        pa = np.array([float(a["x"]), float(a["y"])])
        for b in selected[idx + 1 :]:
            pairwise.append(float(np.linalg.norm(pa - np.array([float(b["x"]), float(b["y"])]))))
    return {
        "schema": "tx_placement_summary_v1",
        "candidate_count": int(bundle["selected_count"]),
        "placement_seed": int(params["placement_seed"]),
        "placement_method": effective_method,
        "requested_placement_method": requested_method,
        "distance_to_center_stats": _stats([float(row["distance_to_center_m"]) for row in selected]),
        "distance_to_nearest_route_stats": _stats([float(row["distance_to_nearest_route_m"]) for row in selected]),
        "pairwise_tx_distance_stats": _stats(pairwise),
        "rejected_candidate_count": int(sum(rejected.values())),
        "rejection_reasons_histogram": dict(rejected),
        "inside_drivable_lane_accepted": inside_drivable_lane_accepted,
        "lane_exclusion": {
            "enabled": bool(lane_enabled),
            "generated": drivable_exclusion is not None,
            "rectangle_count": int(drivable_exclusion.get("rectangle_count", 0)) if isinstance(drivable_exclusion, Mapping) else 0,
            "lane_sample_step_m": drivable_exclusion.get("lane_sample_step_m") if isinstance(drivable_exclusion, Mapping) else None,
            "lane_inflation_margin_m": drivable_exclusion.get("lane_inflation_margin_m") if isinstance(drivable_exclusion, Mapping) else None,
        },
        "warnings": list(warnings),
    }


def _stats(values: Sequence[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None}
    return {"count": len(values), "min": min(values), "max": max(values), "mean": sum(values) / len(values)}
