from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from dynamic_radio_dataset.json_utils import iter_jsonl, save_json
from dynamic_radio_dataset.multi_scene.config import candidate_catalog_path, load_multi_scene_config, preview_dir
from dynamic_radio_dataset.multi_scene.png_draw import (
    blank,
    draw_disc,
    draw_polygon,
    draw_polyline,
    draw_text,
    paste,
    rectangle_corners,
    write_png,
)


def render_scene_previews(config_path: Path, *, limit: int | None = None) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    catalog_path = candidate_catalog_path(config)
    if not catalog_path.exists():
        raise FileNotFoundError(f"scene candidate catalog not found: {catalog_path}")
    candidates = list(iter_jsonl(catalog_path))
    if limit is not None:
        candidates = candidates[: int(limit)]
    out_dir = preview_dir(config)
    out_dir.mkdir(parents=True, exist_ok=True)
    preview_cfg = config.get("discovery", {}).get("preview", {}) if isinstance(config.get("discovery"), dict) else {}
    size = int(preview_cfg.get("image_size", 512))
    written: list[dict[str, Any]] = []
    for candidate in candidates:
        path = out_dir / f"{candidate['candidate_id']}.png"
        write_png(path, render_candidate_preview(candidate, size=size, tx_cfg=config.get("tx", {})))
        sidecar = dict(candidate)
        sidecar["preview_png"] = str(path)
        (out_dir / f"{candidate['candidate_id']}.json").write_text(json.dumps(sidecar, indent=2, ensure_ascii=False), encoding="utf-8")
        written.append({"candidate_id": candidate["candidate_id"], "preview_png": str(path)})
    sheet_path = out_dir / "contact_sheet.png"
    render_contact_sheet(candidates, sheet_path, cell_size=size, columns=int(preview_cfg.get("contact_sheet_columns", 4)))
    manifest = {"schema": "scene_preview_manifest_v1", "candidate_catalog": str(catalog_path), "preview_count": len(written), "previews": written, "contact_sheet": str(sheet_path)}
    save_json(out_dir / "preview_manifest.json", manifest)
    return manifest


def render_candidate_preview(candidate: Mapping[str, Any], *, size: int = 512, tx_cfg: Mapping[str, Any] | None = None) -> np.ndarray:
    tx_cfg = tx_cfg or {}
    margin_top = 70
    margin = 24
    img = blank(size, size, (248, 248, 244))
    draw_text(img, (8, 8), str(candidate.get("candidate_id", "candidate"))[:36], scale=2)
    center = candidate.get("center", {})
    draw_text(img, (8, 26), f"SCENE {candidate.get('scene_id','')} {candidate.get('mode','')}", scale=1)
    draw_text(img, (8, 38), f"CENTER {float(center.get('x',0)):.1f},{float(center.get('y',0)):.1f}", scale=1)
    draw_text(img, (8, 50), f"ROUTES {candidate.get('route_count',0)} TURNS {candidate.get('turn_type_histogram',{})}", scale=1)
    bounds = _world_bounds(candidate)

    def project(point: Sequence[float]) -> tuple[float, float]:
        x0, x1, y0, y1 = bounds
        px = margin + (float(point[0]) - x0) / max(x1 - x0, 1e-6) * (size - 2 * margin)
        py = margin_top + (y1 - float(point[1])) / max(y1 - y0, 1e-6) * (size - margin_top - margin)
        return px, py

    support = candidate.get("support_region", {}) if isinstance(candidate.get("support_region"), Mapping) else {}
    valid = candidate.get("valid_crop", {}) if isinstance(candidate.get("valid_crop"), Mapping) else {}
    for region, color, width in [(support, (120, 120, 120), 1), (valid, (0, 150, 90), 2)]:
        if region:
            rc = region.get("center", {})
            corners = rectangle_corners((float(rc.get("x", 0)), float(rc.get("y", 0))), float(region.get("width_m", 1)), float(region.get("height_m", 1)), float(region.get("yaw_deg", 0)))
            draw_polygon(img, [project(p) for p in corners], color, width=width)

    for route in candidate.get("routes", []) if isinstance(candidate.get("routes"), list) else []:
        poly = _route_polyline(route)
        if len(poly) >= 2:
            color = _turn_color(str(route.get("turn_type", "unknown")))
            draw_polyline(img, [project(p) for p in poly], color, width=2)
            draw_disc(img, project(poly[0]), 3, (30, 30, 30))

    cx = float(center.get("x", 0.0))
    cy = float(center.get("y", 0.0))
    draw_disc(img, project((cx, cy)), 5, (220, 40, 40))
    _draw_tx_region(img, project, (cx, cy), tx_cfg)
    return img


def render_contact_sheet(candidates: Sequence[Mapping[str, Any]], path: Path, *, cell_size: int = 512, columns: int = 4) -> None:
    if not candidates:
        write_png(path, blank(cell_size, cell_size, (248, 248, 244)))
        return
    columns = max(1, int(columns))
    rows = int(math.ceil(len(candidates) / columns))
    sheet = blank(columns * cell_size, rows * cell_size, (230, 230, 226))
    for index, candidate in enumerate(candidates):
        img = render_candidate_preview(candidate, size=cell_size)
        paste(sheet, img, ((index % columns) * cell_size, (index // columns) * cell_size))
    write_png(path, sheet)


def _world_bounds(candidate: Mapping[str, Any]) -> tuple[float, float, float, float]:
    support = candidate.get("support_region", {}) if isinstance(candidate.get("support_region"), Mapping) else {}
    center = support.get("center", candidate.get("center", {})) if isinstance(support, Mapping) else candidate.get("center", {})
    cx, cy = float(center.get("x", 0.0)), float(center.get("y", 0.0))
    width = float(support.get("width_m", 160.0)) if isinstance(support, Mapping) else 160.0
    height = float(support.get("height_m", 160.0)) if isinstance(support, Mapping) else 160.0
    return cx - width / 2, cx + width / 2, cy - height / 2, cy + height / 2


def _route_polyline(route: Mapping[str, Any]) -> list[tuple[float, float]]:
    result = []
    for point in route.get("polyline", []) if isinstance(route.get("polyline"), list) else []:
        if isinstance(point, Mapping):
            result.append((float(point.get("x", 0.0)), float(point.get("y", 0.0))))
        elif isinstance(point, (list, tuple)) and len(point) >= 2:
            result.append((float(point[0]), float(point[1])))
    return result


def _turn_color(turn_type: str) -> tuple[int, int, int]:
    value = turn_type.lower()
    if "left" in value:
        return (48, 105, 220)
    if "right" in value:
        return (220, 130, 30)
    if "straight" in value or "corridor" in value:
        return (40, 150, 70)
    return (100, 80, 180)


def _draw_tx_region(img: np.ndarray, project, center: tuple[float, float], tx_cfg: Mapping[str, Any]) -> None:
    placement = tx_cfg.get("placement", {}) if isinstance(tx_cfg.get("placement"), Mapping) else tx_cfg
    max_center = float(placement.get("max_distance_to_scene_center_m", placement.get("tx_scene_radius_max", 35.0)))
    min_center = float(placement.get("min_distance_to_scene_center_m", placement.get("tx_scene_radius_min", 8.0)))
    for radius, color in [(max_center, (170, 120, 210)), (min_center, (210, 170, 230))]:
        points = []
        for step in range(72):
            angle = 2 * math.pi * step / 72
            points.append(project((center[0] + radius * math.cos(angle), center[1] + radius * math.sin(angle))))
        draw_polygon(img, points, color, width=1)
