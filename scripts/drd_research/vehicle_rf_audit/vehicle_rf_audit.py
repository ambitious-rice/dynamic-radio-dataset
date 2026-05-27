#!/usr/bin/env python3
"""Standalone vehicle blueprint RF audit for 2D-occupancy radio-map datasets.

This tool is diagnostic-only. It does not modify the DRD pipeline, CARLA
collection, Sionna numeric code, or existing datasets.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import numpy as np

TOOL_VERSION = "0.1.0"
DEFAULT_SHADOW_THRESHOLD_DB = 3.0
DEFAULT_BRIGHT_THRESHOLD_DB = 3.0
DEFAULT_AFFECTED_THRESHOLD_DB = 1.0
DEFAULT_MIN_SHADOW_STRENGTH_DB = 4.0
DEFAULT_MAX_BRIGHT_SPOT_SCORE = 0.18
DEFAULT_MIN_CONSISTENCY_SCORE = 0.55
# Do not exclude large vehicles by name alone. High clearance/open underbody is
# decided by explicit visual review or RF pattern scores.
HIGH_CLEARANCE_PATTERNS: tuple[str, ...] = ()
NON_SOLID_2D_PATTERNS = (
    "crossbike",
    "diamondback",
    "gazelle",
    "harley",
    "kawasaki",
    "omafiets",
    "vespa",
    "yamaha",
)
LOW_BODY_PATTERNS = (
    "audi", "bmw", "chevrolet", "citroen", "coupe", "crown", "dodge",
    "ford.mustang", "jeep", "lincoln", "mercedes", "micro", "mini",
    "mkz", "nissan", "patrol", "prius", "seat", "tesla.model3",
    "toyota", "volkswagen",
)
RF_CASE_FILENAMES = {
    "baseline": ("baseline.npz", "no_vehicle.npz", "static.npz"),
    "with_vehicle": ("with_vehicle.npz", "vehicle.npz", "test_vehicle.npz", "rss_maps.npz"),
    "diff": ("difference.npz", "diff.npz", "delta.npz"),
    "mask": ("vehicle_mask.npz", "occupancy_mask.npz", "shadow_mask.npz", "vehicle_mask.npy", "occupancy_mask.npy", "shadow_mask.npy"),
}


@dataclass
class Candidate:
    blueprint_id: str
    family: str = "unknown"
    attributes: dict[str, Any] = field(default_factory=dict)
    source: str = "manual"


@dataclass
class AuditResult:
    blueprint_id: str
    family: str
    decision: str
    exclusion_reasons: list[str]
    geometry_prior: str
    visual_clearance: str
    rf_case_dir: str = ""
    shadow_strength: float | None = None
    underbody_bright_spot_score: float | None = None
    shadow_consistency_score: float | None = None
    affected_area_ratio: float | None = None
    bright_area_ratio: float | None = None
    preview_diff: str = ""
    preview_with_vehicle: str = ""


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize_id(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("_")


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"JSON file not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON {path}: {exc}") from None


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_candidates(path: Path | None) -> list[Candidate]:
    if path is None:
        return []
    suffix = path.suffix.lower()
    if suffix == ".jsonl":
        rows = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rows.append(json.loads(line))
        return [_candidate_from_obj(row, str(path)) for row in rows]
    if suffix == ".json":
        data = load_json(path)
        if isinstance(data, dict):
            rows = data.get("blueprints", data.get("vehicles", data.get("candidates", [])))
        else:
            rows = data
        if not isinstance(rows, list):
            raise RuntimeError(f"Candidate JSON must contain a list: {path}")
        return [_candidate_from_obj(row, str(path)) for row in rows]
    if suffix == ".csv":
        with path.open("r", encoding="utf-8", newline="") as f:
            reader = csv.DictReader(f)
            return [_candidate_from_obj(row, str(path)) for row in reader]
    raise RuntimeError(f"Unsupported candidate format: {path}")


def _candidate_from_obj(obj: Any, source: str) -> Candidate:
    if isinstance(obj, str):
        return Candidate(blueprint_id=obj, family=_family_from_id(obj), source=source)
    if not isinstance(obj, dict):
        raise RuntimeError(f"Invalid candidate row: {obj!r}")
    blueprint_id = str(obj.get("blueprint_id") or obj.get("id") or obj.get("type_id") or "")
    if not blueprint_id:
        raise RuntimeError(f"Candidate row missing blueprint_id/id/type_id: {obj!r}")
    attrs = {k: v for k, v in obj.items() if k not in {"blueprint_id", "id", "type_id", "family"}}
    return Candidate(blueprint_id=blueprint_id, family=str(obj.get("family") or _family_from_id(blueprint_id)), attributes=attrs, source=source)


def _family_from_id(blueprint_id: str) -> str:
    parts = blueprint_id.split(".")
    if len(parts) >= 2 and parts[0] == "vehicle":
        return parts[1]
    return "unknown"


def enumerate_carla_blueprints(host: str, port: int, timeout: float) -> list[Candidate]:
    try:
        import carla  # type: ignore
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(f"Failed to import carla Python API: {type(exc).__name__}: {exc}") from None
    client = carla.Client(host, int(port))
    client.set_timeout(float(timeout))
    world = client.get_world()
    library = world.get_blueprint_library().filter("vehicle.*")
    rows: list[Candidate] = []
    for bp in library:
        attrs = {}
        for attr in bp:
            attrs[str(attr.id)] = str(attr)
        rows.append(Candidate(blueprint_id=str(bp.id), family=_family_from_id(str(bp.id)), attributes=attrs, source=f"carla://{host}:{port}"))
    return sorted(rows, key=lambda item: item.blueprint_id)


def load_view_review(path: Path | None) -> dict[str, dict[str, Any]]:
    if path is None:
        return {}
    if path.suffix.lower() == ".json":
        data = load_json(path)
        if isinstance(data, dict):
            if "reviews" in data and isinstance(data["reviews"], list):
                return {str(row["blueprint_id"]): row for row in data["reviews"] if isinstance(row, dict) and row.get("blueprint_id")}
            return {str(k): v if isinstance(v, dict) else {"value": v} for k, v in data.items()}
        if isinstance(data, list):
            return {str(row["blueprint_id"]): row for row in data if isinstance(row, dict) and row.get("blueprint_id")}
    if path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8", newline="") as f:
            return {str(row["blueprint_id"]): row for row in csv.DictReader(f) if row.get("blueprint_id")}
    raise RuntimeError(f"Unsupported view review format: {path}")


def geometry_prior(blueprint_id: str, review: dict[str, Any]) -> tuple[str, list[str]]:
    manual = str(review.get("visual_clearance") or review.get("clearance") or "").lower()
    if boolish(review.get("exclude")):
        return "manual_exclude", [str(review.get("reason") or "manual visual exclusion")]
    if manual in {"high", "very_high", "open_underbody"}:
        return "high_clearance", ["manual/visual high-clearance or open-underbody review"]
    lowered = blueprint_id.lower()
    for pat in NON_SOLID_2D_PATTERNS:
        if pat in lowered:
            return "non_solid_2d_vehicle", [f"blueprint id matches non-solid/two-wheel prior: {pat}"]
    for pat in HIGH_CLEARANCE_PATTERNS:
        if pat in lowered:
            return "high_clearance", [f"blueprint id matches high-clearance prior: {pat}"]
    for pat in LOW_BODY_PATTERNS:
        if pat in lowered:
            return "low_body", []
    return "unknown", []


def boolish(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in {"1", "true", "yes", "y", "exclude"}


def find_rf_case_dir(rf_root: Path | None, blueprint_id: str) -> Path | None:
    if rf_root is None:
        return None
    names = [blueprint_id, sanitize_id(blueprint_id), blueprint_id.replace(".", "_"), blueprint_id.split(".")[-1]]
    for name in names:
        candidate = rf_root / name
        if candidate.is_dir():
            return candidate
    return None


def find_first(case_dir: Path, names: Iterable[str]) -> Path | None:
    for name in names:
        path = case_dir / name
        if path.exists():
            return path
    return None


def load_np_array(path: Path) -> np.ndarray:
    if path.suffix.lower() == ".npy":
        return np.asarray(np.load(path, allow_pickle=False), dtype=np.float32)
    with np.load(path, allow_pickle=False) as data:
        for key in ("rss_dbm", "dynamic_rss_dbm", "radio_map_dbm", "tx_static_radio_dbm", "delta_db", "difference_db", "mask", "vehicle_mask", "occupancy_mask", "shadow_mask"):
            if key in data.files:
                arr = data[key]
                break
        else:
            arr = data[data.files[0]]
    return np.asarray(arr, dtype=np.float32)


def canonical_map(arr: np.ndarray) -> np.ndarray:
    arr = np.asarray(arr, dtype=np.float32)
    if arr.ndim == 4:
        arr = arr[0]
    if arr.ndim == 3:
        arr = np.nanmean(arr, axis=0)
    if arr.ndim != 2:
        raise RuntimeError(f"RF map must reduce to 2D, got shape {arr.shape}")
    return arr


def rf_metrics_for_case(case_dir: Path, out_preview_dir: Path, blueprint_id: str, args: argparse.Namespace) -> dict[str, Any]:
    diff_path = find_first(case_dir, RF_CASE_FILENAMES["diff"])
    baseline_path = find_first(case_dir, RF_CASE_FILENAMES["baseline"])
    vehicle_path = find_first(case_dir, RF_CASE_FILENAMES["with_vehicle"])
    if diff_path is not None:
        diff = canonical_map(load_np_array(diff_path))
        with_vehicle = diff
    else:
        if baseline_path is None or vehicle_path is None:
            raise RuntimeError(f"RF case lacks diff or baseline/with_vehicle maps: {case_dir}")
        baseline = canonical_map(load_np_array(baseline_path))
        with_vehicle = canonical_map(load_np_array(vehicle_path))
        if baseline.shape != with_vehicle.shape:
            raise RuntimeError(f"RF case shape mismatch: {baseline.shape} vs {with_vehicle.shape}")
        diff = with_vehicle - baseline
    finite = np.isfinite(diff)
    affected = finite & (np.abs(diff) >= float(args.affected_threshold_db))
    attenuation = np.maximum(-diff, 0.0)
    shadow = finite & (attenuation >= float(args.shadow_threshold_db))
    mask_path = find_first(case_dir, RF_CASE_FILENAMES["mask"])
    if mask_path is not None:
        mask = canonical_map(load_np_array(mask_path)) > 0.5
        if mask.shape == diff.shape:
            shadow = shadow | mask
    bright = finite & (diff >= float(args.bright_threshold_db))
    affected_area_ratio = ratio(affected, finite)
    bright_area_ratio = ratio(bright & affected, finite)
    if np.any(shadow):
        shadow_values = attenuation[shadow]
        shadow_strength = float(np.nanmedian(shadow_values))
        local_bright = bright & dilate_mask(shadow, int(args.shadow_dilate_px))
        bright_in_shadow_ratio = ratio(local_bright, dilate_mask(shadow, int(args.shadow_dilate_px)))
        bright_intensity = float(np.nanpercentile(np.maximum(diff[local_bright], 0.0), 95)) if np.any(local_bright) else 0.0
        underbody_score = float(min(1.0, bright_in_shadow_ratio * 3.0 + max(0.0, bright_intensity) / 30.0))
        cv = float(np.nanstd(shadow_values) / max(1e-6, np.nanmean(shadow_values)))
        consistency = float(max(0.0, min(1.0, 1.0 - 0.35 * cv - underbody_score)))
    else:
        shadow_strength = 0.0
        underbody_score = 1.0 if np.any(bright) else 0.5
        consistency = 0.0
    preview_base = out_preview_dir / sanitize_id(blueprint_id)
    out_preview_dir.mkdir(parents=True, exist_ok=True)
    diff_ppm = preview_base.with_name(preview_base.name + "_diff.ppm")
    write_heatmap_ppm(diff_ppm, diff, vmin=-20.0, vmax=20.0, diverging=True)
    with_ppm = preview_base.with_name(preview_base.name + "_with_vehicle.ppm")
    write_heatmap_ppm(with_ppm, with_vehicle, vmin=float(args.preview_vmin_dbm), vmax=float(args.preview_vmax_dbm), diverging=False)
    return {
        "shadow_strength": shadow_strength,
        "underbody_bright_spot_score": underbody_score,
        "shadow_consistency_score": consistency,
        "affected_area_ratio": affected_area_ratio,
        "bright_area_ratio": bright_area_ratio,
        "preview_diff": str(diff_ppm),
        "preview_with_vehicle": str(with_ppm),
    }


def ratio(mask: np.ndarray, denom_mask: np.ndarray) -> float:
    denom = int(np.count_nonzero(denom_mask))
    if denom <= 0:
        return 0.0
    return float(np.count_nonzero(mask) / denom)


def dilate_mask(mask: np.ndarray, radius: int) -> np.ndarray:
    if radius <= 0:
        return mask
    out = mask.copy()
    for dy in range(-radius, radius + 1):
        for dx in range(-radius, radius + 1):
            if dx * dx + dy * dy > radius * radius:
                continue
            shifted = np.zeros_like(mask, dtype=bool)
            y0 = max(0, dy); y1 = mask.shape[0] + min(0, dy)
            x0 = max(0, dx); x1 = mask.shape[1] + min(0, dx)
            sy0 = max(0, -dy); sy1 = mask.shape[0] - max(0, dy)
            sx0 = max(0, -dx); sx1 = mask.shape[1] - max(0, dx)
            shifted[y0:y1, x0:x1] = mask[sy0:sy1, sx0:sx1]
            out |= shifted
    return out


def write_heatmap_ppm(path: Path, arr: np.ndarray, vmin: float, vmax: float, diverging: bool) -> None:
    arr = np.asarray(arr, dtype=np.float32)
    arr = np.nan_to_num(arr, nan=vmin, posinf=vmax, neginf=vmin)
    t = np.clip((arr - vmin) / max(1e-6, vmax - vmin), 0.0, 1.0)
    if diverging:
        # blue -> white -> red
        r = np.where(t < 0.5, 2.0 * t, 1.0)
        g = np.where(t < 0.5, 2.0 * t, 2.0 * (1.0 - t))
        b = np.where(t < 0.5, 1.0, 2.0 * (1.0 - t))
    else:
        # dark blue -> green -> yellow
        r = np.clip(2.0 * t - 0.4, 0.0, 1.0)
        g = np.clip(1.7 * t, 0.0, 1.0)
        b = np.clip(1.0 - 1.2 * t, 0.0, 1.0)
    img = np.stack([r, g, b], axis=-1)
    img8 = np.asarray(np.round(img * 255.0), dtype=np.uint8)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        f.write(f"P6\n{img8.shape[1]} {img8.shape[0]}\n255\n".encode("ascii"))
        f.write(img8.tobytes())


def decide(prior: str, prior_reasons: list[str], metrics: dict[str, Any] | None, args: argparse.Namespace) -> tuple[str, list[str]]:
    if prior in {"manual_exclude", "non_solid_2d_vehicle"}:
        return "exclude", list(prior_reasons)
    if metrics is None:
        if prior == "high_clearance":
            return "exclude", list(prior_reasons) or ["high-clearance prior without RF override"]
        if prior == "low_body":
            return "whitelist", []
        return "review", ["no RF metrics and no decisive visual/geometry prior"]
    reasons: list[str] = []
    if metrics["underbody_bright_spot_score"] > float(args.max_bright_spot_score):
        reasons.append(f"underbody_bright_spot_score={metrics['underbody_bright_spot_score']:.3f} exceeds {args.max_bright_spot_score}")
    if metrics["shadow_strength"] < float(args.min_shadow_strength_db):
        reasons.append(f"shadow_strength={metrics['shadow_strength']:.3f} below {args.min_shadow_strength_db} dB")
    if metrics["shadow_consistency_score"] < float(args.min_consistency_score):
        reasons.append(f"shadow_consistency_score={metrics['shadow_consistency_score']:.3f} below {args.min_consistency_score}")
    if prior == "high_clearance" and metrics["underbody_bright_spot_score"] > float(args.high_clearance_override_score):
        reasons = list(prior_reasons) + reasons + ["high-clearance visual prior not overridden by RF pattern"]
    return ("exclude", reasons) if reasons else ("whitelist", [])


def audit_candidates(candidates: list[Candidate], args: argparse.Namespace) -> list[AuditResult]:
    review = load_view_review(args.view_review)
    results: list[AuditResult] = []
    out_preview_dir = args.out_dir / "previews"
    for item in candidates:
        row_review = review.get(item.blueprint_id, {})
        prior, prior_reasons = geometry_prior(item.blueprint_id, row_review)
        visual_clearance = str(row_review.get("visual_clearance") or row_review.get("clearance") or prior)
        case_dir = find_rf_case_dir(args.rf_root, item.blueprint_id)
        metrics = None
        rf_case_dir = ""
        if case_dir is not None:
            try:
                metrics = rf_metrics_for_case(case_dir, out_preview_dir, item.blueprint_id, args)
                rf_case_dir = str(case_dir)
            except Exception as exc:  # noqa: BLE001
                metrics = None
                prior_reasons = prior_reasons + [f"rf_case_read_failed: {type(exc).__name__}: {exc}"]
        decision, reasons = decide(prior, prior_reasons, metrics, args)
        results.append(AuditResult(
            blueprint_id=item.blueprint_id,
            family=item.family,
            decision=decision,
            exclusion_reasons=reasons,
            geometry_prior=prior,
            visual_clearance=visual_clearance,
            rf_case_dir=rf_case_dir,
            shadow_strength=metric(metrics, "shadow_strength"),
            underbody_bright_spot_score=metric(metrics, "underbody_bright_spot_score"),
            shadow_consistency_score=metric(metrics, "shadow_consistency_score"),
            affected_area_ratio=metric(metrics, "affected_area_ratio"),
            bright_area_ratio=metric(metrics, "bright_area_ratio"),
            preview_diff=str(metrics.get("preview_diff", "")) if metrics else "",
            preview_with_vehicle=str(metrics.get("preview_with_vehicle", "")) if metrics else "",
        ))
    return results


def metric(metrics: dict[str, Any] | None, key: str) -> float | None:
    return None if metrics is None else float(metrics[key])


def write_outputs(results: list[AuditResult], candidates: list[Candidate], args: argparse.Namespace) -> None:
    args.out_dir.mkdir(parents=True, exist_ok=True)
    csv_path = args.out_dir / "vehicle_audit_report.csv"
    fields = [
        "blueprint_id", "family", "decision", "exclusion_reasons", "geometry_prior", "visual_clearance", "rf_case_dir",
        "shadow_strength", "underbody_bright_spot_score", "shadow_consistency_score", "affected_area_ratio", "bright_area_ratio",
        "preview_diff", "preview_with_vehicle",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        for row in results:
            data = row.__dict__.copy()
            data["exclusion_reasons"] = "; ".join(row.exclusion_reasons)
            writer.writerow(data)
    whitelist = [r.blueprint_id for r in results if r.decision == "whitelist"]
    exclusions = {r.blueprint_id: r.exclusion_reasons for r in results if r.decision == "exclude"}
    review = {r.blueprint_id: r.exclusion_reasons for r in results if r.decision == "review"}
    common = {
        "schema": "vehicle_rf_audit_v1",
        "tool_version": TOOL_VERSION,
        "created_at": now_iso(),
        "policy": "input-label consistency constraint: keep vehicles whose RF shadow can be approximated by 2D occupancy as a solid blocker",
        "thresholds": thresholds_dict(args),
        "candidate_count": len(candidates),
    }
    write_json(args.out_dir / "vehicle_whitelist.json", {**common, "whitelist_count": len(whitelist), "vehicle_blueprint_ids": whitelist})
    write_json(args.out_dir / "vehicle_exclusion_reasons.json", {**common, "excluded_count": len(exclusions), "review_count": len(review), "excluded": exclusions, "needs_review": review})
    write_json(args.out_dir / "audit_manifest.json", {**common, "report_csv": str(csv_path), "preview_dir": str(args.out_dir / "previews")})


def thresholds_dict(args: argparse.Namespace) -> dict[str, float]:
    return {
        "shadow_threshold_db": float(args.shadow_threshold_db),
        "bright_threshold_db": float(args.bright_threshold_db),
        "affected_threshold_db": float(args.affected_threshold_db),
        "min_shadow_strength_db": float(args.min_shadow_strength_db),
        "max_bright_spot_score": float(args.max_bright_spot_score),
        "min_consistency_score": float(args.min_consistency_score),
    }


def write_candidates(path: Path, candidates: list[Candidate]) -> None:
    write_json(path, {"schema": "carla_vehicle_blueprint_candidates_v1", "created_at": now_iso(), "blueprints": [c.__dict__ for c in candidates]})


def write_rf_test_plan(path: Path, candidates: list[Candidate], args: argparse.Namespace) -> None:
    plan = {
        "schema": "vehicle_rf_audit_test_plan_v1",
        "created_at": now_iso(),
        "purpose": "fixed-scene/fixed-TX/fixed-vehicle-pose RF tests for 2D-occupancy blocker consistency",
        "fixed_context": {
            "scene_id": args.scene_id,
            "reference_episode_id": args.reference_episode_id,
            "reference_tx_id": args.reference_tx_id,
            "reference_frame": int(args.reference_frame),
            "reference_vehicle_pose_world": {
                "source": "episode_000051 frame 55 car_40, the previously inspected fusorosa underbody/mesh bright-spot case",
                "vehicle_type": "vehicle.mitsubishi.fusorosa",
                "x": -48.38772964477539,
                "y": 6.536416053771973,
                "z": -0.00450210552662611,
                "pitch": 0.412468284368515,
                "yaw": 89.56693267822266,
                "roll": -1.725616455078125,
                "bbox_extent": {"x": 5.136342525482178, "y": 1.9720759391784668, "z": 2.1264240741729736},
            },
            "notes": "Use this scene/TX/frame/pose for fixed blueprint substitution tests unless overridden.",
        },
        "expected_case_layout": {
            "per_vehicle_dir": "<rf_root>/<sanitized_blueprint_id>/",
            "accepted_inputs": ["baseline.npz + with_vehicle.npz", "difference.npz"],
            "rss_key_examples": ["rss_dbm", "dynamic_rss_dbm", "difference_db", "delta_db"],
        },
        "vehicles": [{"blueprint_id": c.blueprint_id, "case_dir_name": sanitize_id(c.blueprint_id), "family": c.family} for c in candidates],
    }
    write_json(path, plan)


def run_rf_command_template(candidates: list[Candidate], args: argparse.Namespace) -> None:
    if not args.rf_command_template:
        return
    rf_root = args.rf_root or (args.out_dir / "rf_cases")
    rf_root.mkdir(parents=True, exist_ok=True)
    review = load_view_review(args.view_review)
    command_reports = []
    for item in candidates:
        prior, prior_reasons = geometry_prior(item.blueprint_id, review.get(item.blueprint_id, {}))
        if args.skip_prior_excluded_rf and prior in {"manual_exclude", "high_clearance", "non_solid_2d_vehicle"}:
            command_reports.append({
                "blueprint_id": item.blueprint_id,
                "status": "skipped_by_prior",
                "prior": prior,
                "reasons": prior_reasons,
            })
            continue
        case_dir = rf_root / sanitize_id(item.blueprint_id)
        case_dir.mkdir(parents=True, exist_ok=True)
        fields = {
            "blueprint_id": item.blueprint_id,
            "blueprint_id_shell": shell_quote(item.blueprint_id),
            "case_dir": str(case_dir),
            "case_dir_shell": shell_quote(str(case_dir)),
            "scene_id": args.scene_id,
            "reference_episode_id": args.reference_episode_id,
            "reference_tx_id": args.reference_tx_id,
            "reference_frame": str(int(args.reference_frame)),
        }
        command = args.rf_command_template.format(**fields)
        stdout_path = case_dir / "rf_command_stdout.log"
        stderr_path = case_dir / "rf_command_stderr.log"
        result = subprocess.run(
            command,
            shell=True,
            cwd=str(args.command_workdir) if args.command_workdir else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=float(args.rf_command_timeout_sec) if args.rf_command_timeout_sec else None,
        )
        stdout_path.write_text(result.stdout, encoding="utf-8")
        stderr_path.write_text(result.stderr, encoding="utf-8")
        command_reports.append({
            "blueprint_id": item.blueprint_id,
            "status": "ok" if result.returncode == 0 else "failed",
            "returncode": int(result.returncode),
            "command": command,
            "case_dir": str(case_dir),
            "stdout": str(stdout_path),
            "stderr": str(stderr_path),
        })
        if result.returncode != 0 and args.fail_on_rf_command_error:
            write_json(args.out_dir / "rf_command_report.json", {"schema": "vehicle_rf_command_report_v1", "commands": command_reports})
            raise RuntimeError(f"RF command failed for {item.blueprint_id} with return code {result.returncode}")
    write_json(args.out_dir / "rf_command_report.json", {"schema": "vehicle_rf_command_report_v1", "commands": command_reports})
    args.rf_root = rf_root


def shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Standalone CARLA vehicle blueprint RF audit for 2D occupancy radio-map datasets.")
    parser.add_argument("--blueprints", type=Path, help="Candidate vehicle blueprints as JSON/JSONL/CSV")
    parser.add_argument("--out-dir", type=Path, default=Path("tmp/vehicle_rf_audit"), help="Output directory")
    parser.add_argument("--rf-root", type=Path, help="Optional RF case root containing before/after or diff maps per blueprint")
    parser.add_argument("--view-review", type=Path, help="Optional manual/visual clearance review JSON/CSV")
    parser.add_argument("--enumerate-carla", action="store_true", help="Connect to CARLA and enumerate vehicle.* blueprints")
    parser.add_argument("--carla-host", default="127.0.0.1")
    parser.add_argument("--carla-port", type=int, default=2000)
    parser.add_argument("--carla-timeout", type=float, default=10.0)
    parser.add_argument("--write-rf-test-plan", action="store_true", help="Write fixed-scene RF test plan JSON for external Sionna test execution")
    parser.add_argument("--rf-command-template", help="Optional shell command template to run an external fixed-scene RF test per blueprint")
    parser.add_argument("--command-workdir", type=Path, help="Working directory for --rf-command-template")
    parser.add_argument("--rf-command-timeout-sec", type=float, default=0.0)
    parser.add_argument("--skip-prior-excluded-rf", action="store_true", help="Do not run RF command for manual/high-clearance/non-solid prior exclusions")
    parser.add_argument("--fail-on-rf-command-error", action="store_true")
    parser.add_argument("--scene-id", default="town10_junction_0189")
    parser.add_argument("--reference-episode-id", default="episode_000051")
    parser.add_argument("--reference-tx-id", default="tx_00")
    parser.add_argument("--reference-frame", type=int, default=55)
    parser.add_argument("--shadow-threshold-db", type=float, default=DEFAULT_SHADOW_THRESHOLD_DB)
    parser.add_argument("--bright-threshold-db", type=float, default=DEFAULT_BRIGHT_THRESHOLD_DB)
    parser.add_argument("--affected-threshold-db", type=float, default=DEFAULT_AFFECTED_THRESHOLD_DB)
    parser.add_argument("--min-shadow-strength-db", type=float, default=DEFAULT_MIN_SHADOW_STRENGTH_DB)
    parser.add_argument("--max-bright-spot-score", type=float, default=DEFAULT_MAX_BRIGHT_SPOT_SCORE)
    parser.add_argument("--min-consistency-score", type=float, default=DEFAULT_MIN_CONSISTENCY_SCORE)
    parser.add_argument("--high-clearance-override-score", type=float, default=0.05)
    parser.add_argument("--shadow-dilate-px", type=int, default=3)
    parser.add_argument("--preview-vmin-dbm", type=float, default=-115.0)
    parser.add_argument("--preview-vmax-dbm", type=float, default=-35.0)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        candidates = load_candidates(args.blueprints)
        if args.enumerate_carla:
            candidates.extend(enumerate_carla_blueprints(args.carla_host, args.carla_port, args.carla_timeout))
        unique: dict[str, Candidate] = {}
        for item in candidates:
            unique[item.blueprint_id] = item
        candidates = sorted(unique.values(), key=lambda item: item.blueprint_id)
        if not candidates:
            raise RuntimeError("No candidate blueprints. Pass --blueprints or --enumerate-carla.")
        args.out_dir.mkdir(parents=True, exist_ok=True)
        write_candidates(args.out_dir / "vehicle_blueprint_candidates.json", candidates)
        if args.write_rf_test_plan:
            write_rf_test_plan(args.out_dir / "rf_test_plan.json", candidates, args)
        run_rf_command_template(candidates, args)
        results = audit_candidates(candidates, args)
        write_outputs(results, candidates, args)
        counts = {key: sum(r.decision == key for r in results) for key in ("whitelist", "exclude", "review")}
        print(f"candidate_count: {len(candidates)}")
        print(f"whitelist: {counts['whitelist']}")
        print(f"exclude: {counts['exclude']}")
        print(f"review: {counts['review']}")
        print(f"out_dir: {args.out_dir}")
        return 0
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
