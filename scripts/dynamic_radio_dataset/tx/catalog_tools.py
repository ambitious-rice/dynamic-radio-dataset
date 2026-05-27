from __future__ import annotations

import html
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from dynamic_radio_dataset.configs import load_config
from dynamic_radio_dataset.geometry.regions import region_axes, region_center, region_size
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.multi_scene.config import load_multi_scene_config, make_single_scene_config, multi_scene_root, scene_dataset_root
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.rf.processing import trajectory_qc_pass
from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments
from dynamic_radio_dataset.tx.lane_exclusion import LANE_EXCLUSION_METHOD
from dynamic_radio_dataset.tx.placement import generate_tx_catalog


def regenerate_tx_catalogs(
    config_path: Path,
    *,
    placement_method: str | None = None,
    sidecar: bool = False,
    max_scenes: int | None = None,
    render_visualization: bool = True,
    allow_active_collection: bool = False,
) -> dict[str, Any]:
    """Regenerate TX catalogs for a single- or multi-scene config.

    In sidecar mode the formal `tx_catalog.json` and episode assignments are not
    touched. This is the intended review mode while CARLA trajectory collection is
    still active.
    """

    if not sidecar and not allow_active_collection:
        active = active_collection_processes()
        if active:
            raise RuntimeError(f"Refusing to overwrite formal TX catalogs while collection is active: {active}")
    if _looks_multi_scene_config(config_path):
        return _regenerate_multi_scene_tx_catalogs(
            config_path,
            placement_method=placement_method,
            sidecar=sidecar,
            max_scenes=max_scenes,
            render_visualization=render_visualization,
        )
    config = load_config(config_path)
    _apply_placement_method(config, placement_method)
    label = _sidecar_label(_configured_placement_method(config)) if sidecar else None
    result = generate_tx_catalog(config, placement_method=placement_method, sidecar_label=label)
    root = dataset_root(config)
    report = {
        "schema": "tx_catalog_regeneration_report_v1",
        "config": str(config_path),
        "dataset_root": str(root),
        "sidecar": bool(sidecar),
        "sidecar_label": label,
        "scene_count": 1,
        "scene_results": [{"scene_id": str(config.get("scene", {}).get("scene_id", "single_scene")), "status": "ok", "result": result}],
    }
    report_path = root / "indexes" / f"tx_catalog_regeneration_{label or 'formal'}_latest.json"
    save_json(report_path, report)
    report["report"] = str(report_path)
    return report


def promote_tx_catalogs(
    config_path: Path,
    *,
    source: str = "candidate_lane_exclusion",
    allow_active_collection: bool = False,
) -> dict[str, Any]:
    """Promote reviewed sidecar TX catalogs to formal catalogs and rebuild assignments."""

    active = active_collection_processes()
    if active and not allow_active_collection:
        raise RuntimeError(f"Refusing TX promotion while collection is active: {active}")
    if not _looks_multi_scene_config(config_path):
        raise ValueError("promote-tx-catalogs currently expects a multi-scene config")
    config = load_multi_scene_config(config_path)
    scenes = list(config.get("scenes", []))
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    for scene in scenes:
        scene_config = make_single_scene_config(config, scene)
        scene_root = scene_dataset_root(config, scene)
        static_dir = scene_root / "scene_static"
        source_catalog = static_dir / f"tx_catalog_{source}.json"
        source_summary = static_dir / f"tx_placement_summary_{source}.json"
        if not source_catalog.exists() or not source_summary.exists():
            raise FileNotFoundError(f"Missing source TX sidecar for {scene.get('scene_id')}: {source_catalog}, {source_summary}")
        backup_dir = static_dir / "tx_catalog_backups" / timestamp
        backup_dir.mkdir(parents=True, exist_ok=True)
        for name in ("tx_catalog.json", "tx_placement_summary.json", "tx_drivable_exclusion.json"):
            path = static_dir / name
            if path.exists():
                shutil.copy2(path, backup_dir / name)
        shutil.copy2(source_catalog, static_dir / "tx_catalog.json")
        shutil.copy2(source_summary, static_dir / "tx_placement_summary.json")
        deleted = _delete_accepted_tx_assignments(scene_root / "episodes")
        assignment = ensure_tx_assignments(scene_config)
        results.append(
            {
                "scene_id": str(scene.get("scene_id")),
                "status": "ok",
                "source_catalog": str(source_catalog),
                "source_summary": str(source_summary),
                "backup_dir": str(backup_dir),
                "deleted_tx_assignment_count": int(deleted),
                "assignment": assignment,
            }
        )
    root = multi_scene_root(config)
    report = {
        "schema": "tx_catalog_promotion_report_v1",
        "config": str(config_path),
        "dataset_root": str(root),
        "source": str(source),
        "promoted_scene_count": len(results),
        "scene_results": results,
    }
    report_path = root / "indexes" / f"tx_catalog_promotion_{timestamp}.json"
    save_json(report_path, report)
    save_json(root / "indexes" / "tx_catalog_promotion_latest.json", report)
    report["report"] = str(report_path)
    return report


def render_tx_catalog_visualizations(
    config_path: Path,
    *,
    source: str | None = None,
    output_dir: Path | None = None,
    max_scenes: int | None = None,
) -> dict[str, Any]:
    if not _looks_multi_scene_config(config_path):
        raise ValueError("TX catalog visualization currently expects a multi-scene config")
    config = load_multi_scene_config(config_path)
    root = multi_scene_root(config)
    source_label = None if source in (None, "", "formal") else str(source)
    if output_dir is None:
        dirname = "tx_visualization_lane_exclusion" if source_label == "candidate_lane_exclusion" else "tx_visualization"
        out_dir = root / "indexes" / dirname
    else:
        out_dir = output_dir if output_dir.is_absolute() else root / output_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    scene_svgs: list[str] = []
    summaries: list[dict[str, Any]] = []
    for scene in scenes:
        scene_id = str(scene.get("scene_id"))
        scene_root = scene_dataset_root(config, scene)
        static_dir = scene_root / "scene_static"
        catalog_path = static_dir / ("tx_catalog.json" if source_label is None else f"tx_catalog_{source_label}.json")
        summary_path = static_dir / (
            "tx_placement_summary.json" if source_label is None else f"tx_placement_summary_{source_label}.json"
        )
        scene_meta_path = scene_root / "reference_scene" / "scene_meta.json"
        routes_path = scene_root / "reference_scene" / "routes.json"
        if not catalog_path.exists() or not scene_meta_path.exists() or not routes_path.exists():
            summaries.append(
                {
                    "scene_id": scene_id,
                    "status": "missing_inputs",
                    "catalog_path": str(catalog_path),
                    "scene_meta_path": str(scene_meta_path),
                    "routes_path": str(routes_path),
                }
            )
            continue
        catalog = load_json(catalog_path)
        summary = load_json(summary_path) if summary_path.exists() else {}
        scene_meta = load_json(scene_meta_path)
        routes = load_json(routes_path).get("routes", [])
        exclusion_path = static_dir / "tx_drivable_exclusion.json"
        exclusion = load_json(exclusion_path) if exclusion_path.exists() else None
        svg_path = out_dir / f"{scene_id}_tx_routes.svg"
        _write_scene_svg(svg_path, scene_id, scene_meta, routes, catalog, summary, exclusion)
        scene_svgs.append(str(svg_path))
        rejection_hist = summary.get("rejection_reasons_histogram", {}) if isinstance(summary.get("rejection_reasons_histogram"), Mapping) else {}
        summaries.append(
            {
                "scene_id": scene_id,
                "status": "ok",
                "svg": str(svg_path),
                "catalog": str(catalog_path),
                "candidate_count": int(summary.get("candidate_count", len(catalog.get("tx_catalog", [])))),
                "placement_method": summary.get("placement_method", catalog.get("placement_method")),
                "inside_drivable_lane_accepted": summary.get("inside_drivable_lane_accepted"),
                "rejected_inside_drivable_lane": int(rejection_hist.get("inside_drivable_lane", 0) or 0),
                "warnings": summary.get("warnings", []),
            }
        )
    contact = out_dir / "tx_routes_contact_sheet.html"
    _write_contact_sheet(contact, scene_svgs, summaries)
    result = {
        "schema": "tx_visualization_summary_v1",
        "config": str(config_path),
        "source": source_label or "formal",
        "output_dir": str(out_dir),
        "contact_sheet": str(contact),
        "scene_svgs": scene_svgs,
        "summaries": summaries,
    }
    save_json(out_dir / "tx_visualization_summary.json", result)
    return result


def active_collection_processes() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    proc_root = Path("/proc")
    if not proc_root.exists():
        return rows
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            raw = (entry / "cmdline").read_bytes()
        except OSError:
            continue
        if not raw:
            continue
        cmd = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        if not cmd:
            continue
        if "promote-tx-catalogs" in cmd:
            continue
        if "collect-multi-scene" in cmd or "collect_multi_scene_2workers_autorestart" in cmd:
            rows.append({"pid": int(entry.name), "cmd": cmd[:500]})
    return rows


def _regenerate_multi_scene_tx_catalogs(
    config_path: Path,
    *,
    placement_method: str | None,
    sidecar: bool,
    max_scenes: int | None,
    render_visualization: bool,
) -> dict[str, Any]:
    config = load_multi_scene_config(config_path)
    scenes = list(config.get("scenes", []))
    if max_scenes is not None:
        scenes = scenes[: int(max_scenes)]
    root = multi_scene_root(config)
    label = _sidecar_label(placement_method or _configured_placement_method(config)) if sidecar else None
    results = []
    for scene in scenes:
        scene_config = make_single_scene_config(config, scene)
        _apply_placement_method(scene_config, placement_method)
        result = generate_tx_catalog(scene_config, placement_method=placement_method, sidecar_label=label)
        summary = result.get("summary", {}) if isinstance(result.get("summary"), Mapping) else {}
        results.append(
            {
                "scene_id": str(scene.get("scene_id")),
                "status": "ok",
                "candidate_count": int(summary.get("candidate_count", 0)),
                "placement_method": summary.get("placement_method"),
                "requested_placement_method": summary.get("requested_placement_method"),
                "inside_drivable_lane_accepted": summary.get("inside_drivable_lane_accepted"),
                "rejection_reasons_histogram": summary.get("rejection_reasons_histogram", {}),
                "warnings": summary.get("warnings", []),
                "result": result,
            }
        )
    visualization = None
    if render_visualization and sidecar:
        visualization = render_tx_catalog_visualizations(config_path, source=label, max_scenes=max_scenes)
    report = {
        "schema": "tx_catalog_regeneration_report_v1",
        "config": str(config_path),
        "dataset_root": str(root),
        "sidecar": bool(sidecar),
        "sidecar_label": label,
        "scene_count": len(results),
        "scene_results": results,
        "visualization": visualization,
    }
    suffix = label or "formal"
    latest_path = root / "indexes" / f"tx_catalog_regeneration_{suffix}_latest.json"
    timestamp_path = root / "indexes" / f"tx_catalog_regeneration_{suffix}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    save_json(latest_path, report)
    save_json(timestamp_path, report)
    report["report"] = str(latest_path)
    return report


def _apply_placement_method(config: dict[str, Any], placement_method: str | None) -> None:
    if placement_method is None:
        return
    placement = config.setdefault("tx", {}).setdefault("placement", {})
    placement["method"] = str(placement_method)
    if str(placement_method) == LANE_EXCLUSION_METHOD:
        lane = placement.setdefault("lane_exclusion", {})
        lane.setdefault("enabled", True)
        lane.setdefault("lane_sample_step_m", 2.0)
        lane.setdefault("lane_inflation_margin_m", 1.5)


def _configured_placement_method(config: Mapping[str, Any]) -> str:
    tx_cfg = config.get("tx", {}) if isinstance(config.get("tx"), Mapping) else {}
    placement = tx_cfg.get("placement", {}) if isinstance(tx_cfg.get("placement"), Mapping) else {}
    return str(placement.get("method", "roadside_proxy"))


def _sidecar_label(method: str) -> str:
    if str(method) == LANE_EXCLUSION_METHOD:
        return "candidate_lane_exclusion"
    return "candidate_roadside_proxy"


def _delete_accepted_tx_assignments(episodes_dir: Path) -> int:
    if not episodes_dir.exists():
        return 0
    deleted = 0
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        if not trajectory_qc_pass(episode_dir):
            continue
        assignment = episode_dir / "tx_assignment.json"
        if assignment.exists():
            assignment.unlink()
            deleted += 1
    return deleted


def _write_scene_svg(
    path: Path,
    scene_id: str,
    scene_meta: Mapping[str, Any],
    routes: Sequence[Mapping[str, Any]],
    catalog: Mapping[str, Any],
    summary: Mapping[str, Any],
    exclusion: Mapping[str, Any] | None,
) -> None:
    size = 900
    project = _projector(scene_meta, size)
    parts: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{size}" height="{size}" viewBox="0 0 {size} {size}">',
        '<rect width="100%" height="100%" fill="white"/>',
    ]
    for i in range(13):
        pos = i * size / 12.0
        parts.append(f'<line x1="{pos:.1f}" y1="0" x2="{pos:.1f}" y2="{size}" stroke="#e6e6e6" stroke-width="1"/>')
        parts.append(f'<line x1="0" y1="{pos:.1f}" x2="{size}" y2="{pos:.1f}" stroke="#e6e6e6" stroke-width="1"/>')
    if exclusion:
        for rect in exclusion.get("rectangles", []):
            if not isinstance(rect, Mapping):
                continue
            corners = rect.get("corners_xy", [])
            if not isinstance(corners, Sequence) or len(corners) < 3:
                continue
            points = [_format_point(project(float(corner[0]), float(corner[1]))) for corner in corners if isinstance(corner, Sequence) and len(corner) >= 2]
            if len(points) >= 3:
                parts.append(f'<polygon points="{" ".join(points)}" fill="#f39c12" opacity="0.13" stroke="#d35400" stroke-width="0.3"/>')
    colors = ["#1f77b4", "#2ca02c", "#9467bd", "#17becf", "#8c564b", "#bcbd22", "#7f7f7f"]
    for idx, route in enumerate(routes):
        polyline = route.get("polyline", [])
        if not isinstance(polyline, Sequence):
            continue
        pts = []
        for point in polyline:
            if isinstance(point, Mapping):
                pts.append(_format_point(project(float(point["x"]), float(point["y"]))))
        if len(pts) >= 2:
            color = colors[idx % len(colors)]
            parts.append(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{color}" stroke-width="3" opacity="0.72"/>')
    center = _scene_center(scene_meta)
    cx, cy = project(float(center[0]), float(center[1]))
    parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="7" fill="#0047ab" opacity="0.95"/>')
    parts.append(f'<line x1="{cx - 13:.1f}" y1="{cy:.1f}" x2="{cx + 13:.1f}" y2="{cy:.1f}" stroke="#0047ab" stroke-width="3"/>')
    parts.append(f'<line x1="{cx:.1f}" y1="{cy - 13:.1f}" x2="{cx:.1f}" y2="{cy + 13:.1f}" stroke="#0047ab" stroke-width="3"/>')
    for tx in catalog.get("tx_catalog", []):
        if not isinstance(tx, Mapping):
            continue
        pos = tx.get("position", {}) if isinstance(tx.get("position"), Mapping) else {}
        x, y = project(float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
        label = html.escape(str(tx.get("tx_id", "")))
        parts.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6.5" fill="#e74c3c" stroke="#111" stroke-width="1.2"/>')
        parts.append(f'<text x="{x + 8:.1f}" y="{y - 8:.1f}" font-size="15" fill="#111">{label}</text>')
    hist = summary.get("rejection_reasons_histogram", {}) if isinstance(summary.get("rejection_reasons_histogram"), Mapping) else {}
    text_rows = [
        scene_id,
        f"method: {summary.get('placement_method', catalog.get('placement_method', 'unknown'))}",
        f"TX: {summary.get('candidate_count', len(catalog.get('tx_catalog', [])))}",
        f"rejected inside lane: {hist.get('inside_drivable_lane', 0)}",
        f"accepted inside lane: {summary.get('inside_drivable_lane_accepted', 'n/a')}",
    ]
    warnings = summary.get("warnings", [])
    if warnings:
        text_rows.append("warnings: " + ", ".join(str(item) for item in warnings[:2]))
    parts.append('<rect x="12" y="12" width="570" height="132" rx="8" fill="white" opacity="0.86" stroke="#bbb"/>')
    for idx, row in enumerate(text_rows):
        parts.append(f'<text x="24" y="{38 + idx * 20}" font-size="16" fill="#111">{html.escape(str(row))}</text>')
    parts.append("</svg>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def _write_contact_sheet(path: Path, scene_svgs: Sequence[str], summaries: Sequence[Mapping[str, Any]]) -> None:
    items = []
    by_svg = {str(row.get("svg")): row for row in summaries if row.get("svg")}
    for svg in scene_svgs:
        row = by_svg.get(str(svg), {})
        rel = os.path.relpath(svg, path.parent)
        title = html.escape(str(row.get("scene_id", Path(svg).stem)))
        inside = html.escape(str(row.get("inside_drivable_lane_accepted", "n/a")))
        rejected = html.escape(str(row.get("rejected_inside_drivable_lane", "n/a")))
        items.append(
            "<section>"
            f"<h2>{title}</h2>"
            f"<p>accepted inside lane: {inside}; rejected inside lane: {rejected}</p>"
            f'<object data="{html.escape(rel)}" type="image/svg+xml" width="100%" height="520"></object>'
            "</section>"
        )
    html_text = (
        "<!doctype html><html><head><meta charset='utf-8'>"
        "<title>TX route visualization</title>"
        "<style>body{font-family:sans-serif;margin:20px;background:#f7f7f7}"
        "main{display:grid;grid-template-columns:repeat(auto-fit,minmax(520px,1fr));gap:18px}"
        "section{background:white;border:1px solid #ddd;padding:12px;border-radius:8px}"
        "h2{font-size:18px;margin:0 0 6px}p{margin:0 0 8px;color:#555}</style>"
        "</head><body><h1>TX route visualization</h1><main>"
        + "\n".join(items)
        + "</main></body></html>\n"
    )
    path.write_text(html_text, encoding="utf-8")


def _projector(scene_meta: Mapping[str, Any], size: int):
    region = scene_meta.get("support_region") if isinstance(scene_meta.get("support_region"), Mapping) else scene_meta.get("valid_crop")
    if not isinstance(region, Mapping):
        raise ValueError("scene_meta needs support_region or valid_crop for visualization")
    center = region_center(region)
    x_axis, y_axis = region_axes(region)
    width, height = region_size(region)

    def project(x: float, y: float) -> tuple[float, float]:
        delta = np.array([float(x), float(y)], dtype=np.float64) - center
        local_x = float(delta @ x_axis)
        local_y = float(delta @ y_axis)
        px = (local_x / width + 0.5) * size
        py = (0.5 - local_y / height) * size
        return px, py

    return project


def _scene_center(scene_meta: Mapping[str, Any]) -> np.ndarray:
    info = scene_meta.get("scene_info", {}) if isinstance(scene_meta.get("scene_info"), Mapping) else {}
    if isinstance(info.get("junction_center"), Mapping):
        c = info["junction_center"]
        return np.array([float(c["x"]), float(c["y"])], dtype=np.float64)
    return region_center(scene_meta["valid_crop"])


def _format_point(point: tuple[float, float]) -> str:
    return f"{point[0]:.1f},{point[1]:.1f}"


def _looks_multi_scene_config(path: Path) -> bool:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    markers = ("base_single_scene_config", "single_scene_template_config", "selected_tx_per_episode", "tx_candidates_per_scene")
    return "scenes:" in text and any(marker in text for marker in markers)
