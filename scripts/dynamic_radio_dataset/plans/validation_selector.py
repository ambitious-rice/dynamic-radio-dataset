from __future__ import annotations

import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.json_utils import iter_jsonl, save_json
from dynamic_radio_dataset.paths import dataset_root, repo_root


def select_validation_plans(config: dict, output_path: Path | None = None) -> dict[str, Any]:
    """Build a deterministic per-cell plan queue from an existing plan catalog."""
    started = time.time()
    root = dataset_root(config)
    catalog_path = root / "plan_catalog" / "plans.jsonl"
    if not catalog_path.exists():
        raise FileNotFoundError(f"Plan catalog is missing: {catalog_path}")
    accepted = list(iter_jsonl(catalog_path))
    matrix = _selection_matrix(config)
    if not matrix:
        raise ValueError("collection.selection_matrix must be configured for select-validation-plans.")

    cells: list[dict[str, Any]] = []
    missing_cells: list[dict[str, Any]] = []
    all_candidate_ids: list[str] = []
    selected_candidate_ids: list[str] = []
    for order, spec in enumerate(matrix):
        vehicle_count = int(spec["vehicle_count"])
        target_large = int(spec["target_large_vehicle_count"])
        target_accepted = int(spec["target_accepted"])
        candidates = [
            row
            for row in accepted
            if _plan_vehicle_count(row) == vehicle_count and _target_large_vehicle_count(row) == target_large
        ]
        candidates = sorted(candidates, key=_candidate_sort_key)
        candidate_summaries = [_summarize_candidate(row, rank) for rank, row in enumerate(candidates)]
        candidate_ids = [str(row["plan_id"]) for row in candidates]
        all_candidate_ids.extend(candidate_ids)
        selected_candidate_ids.extend(candidate_ids[:target_accepted])
        cell = {
            "cell_id": _cell_id(vehicle_count, target_large),
            "order": int(order),
            "vehicle_count": int(vehicle_count),
            "target_large_vehicle_count": int(target_large),
            "target_accepted": int(target_accepted),
            "candidate_count": int(len(candidates)),
            "candidate_plan_ids": candidate_ids,
            "selected_candidate_plan_ids": candidate_ids[:target_accepted],
            "candidates": candidate_summaries,
        }
        cells.append(cell)
        if len(candidates) < target_accepted:
            missing_cells.append(
                {
                    "cell_id": cell["cell_id"],
                    "vehicle_count": int(vehicle_count),
                    "target_large_vehicle_count": int(target_large),
                    "target_accepted": int(target_accepted),
                    "candidate_count": int(len(candidates)),
                }
            )

    target_path = _output_manifest_path(config, output_path)
    elapsed_s = float(time.time() - started)
    manifest = {
        "schema": "validation_plan_selection_manifest_v1",
        "selection_id": target_path.stem,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root),
        "source_plan_catalog": str(catalog_path),
        "selection_policy": {
            "mode": "deterministic_matrix_queue",
            "cell_key": ["vehicle_count", "target_large_vehicle_count"],
            "candidate_sort": ["collision_risk_ascending", "preflight_score_descending", "plan_id_ascending"],
            "collection_behavior": (
                "collect each cell in order; retry failed plans up to the configured per-plan retry limit, "
                "then continue with the next candidate in the same cell until the cell target is reached"
            ),
        },
        "summary": {
            "selection_elapsed_s": elapsed_s,
            "matrix_cell_count": int(len(cells)),
            "target_accepted_total": int(sum(int(cell["target_accepted"]) for cell in cells)),
            "candidate_plan_count": int(len(set(all_candidate_ids))),
            "selected_candidate_plan_count": int(len(selected_candidate_ids)),
            "can_reach_targets": bool(not missing_cells),
            "missing_cells": missing_cells,
            "accepted_plan_catalog_count": int(len(accepted)),
            "accepted_vehicle_count_histogram": _histogram(_plan_vehicle_count(row) for row in accepted),
            "accepted_target_large_vehicle_count_histogram": _histogram(
                _target_large_vehicle_count(row) for row in accepted
            ),
            "accepted_matrix_cell_histogram": _histogram(
                _cell_id(_plan_vehicle_count(row), _target_large_vehicle_count(row)) for row in accepted
            ),
        },
        "matrix": [
            {
                "cell_id": str(cell["cell_id"]),
                "vehicle_count": int(cell["vehicle_count"]),
                "target_large_vehicle_count": int(cell["target_large_vehicle_count"]),
                "target_accepted": int(cell["target_accepted"]),
            }
            for cell in cells
        ],
        "cells": cells,
    }
    save_json(target_path, manifest)
    return {
        "selection_manifest": str(target_path),
        "target_accepted_total": int(manifest["summary"]["target_accepted_total"]),
        "matrix_cell_count": int(len(cells)),
        "can_reach_targets": bool(not missing_cells),
        "missing_cells": missing_cells,
    }


def configured_selection_manifest_path(config: dict) -> Path | None:
    collection = config.get("collection", {}) if isinstance(config.get("collection"), dict) else {}
    raw = collection.get("selection_manifest")
    if raw is None or not str(raw).strip():
        return None
    return _resolve_dataset_relative_path(config, Path(str(raw)))


def selection_matrix_target_total(config: dict) -> int:
    return int(sum(int(row["target_accepted"]) for row in _selection_matrix(config)))


def _output_manifest_path(config: dict, output_path: Path | None) -> Path:
    if output_path is not None:
        return output_path if output_path.is_absolute() else repo_root() / output_path
    configured = configured_selection_manifest_path(config)
    if configured is not None:
        return configured
    total = selection_matrix_target_total(config)
    return dataset_root(config) / "plan_catalog" / f"validation_selection_{total}.json"


def _resolve_dataset_relative_path(config: dict, path: Path) -> Path:
    if path.is_absolute():
        return path
    if len(path.parts) == 1:
        return dataset_root(config) / "plan_catalog" / path
    return dataset_root(config) / path


def _selection_matrix(config: dict) -> list[dict[str, int]]:
    collection = config.get("collection", {}) if isinstance(config.get("collection"), dict) else {}
    raw = collection.get("selection_matrix")
    if isinstance(raw, Mapping):
        rows = []
        for key, value in raw.items():
            left, sep, right = str(key).partition(":")
            if not sep:
                raise ValueError(f"Invalid selection_matrix key {key!r}; expected VEHICLE_COUNT:TARGET_LARGE.")
            rows.append(
                {
                    "vehicle_count": int(left),
                    "target_large_vehicle_count": int(right),
                    "target_accepted": int(value),
                }
            )
        return rows
    if not isinstance(raw, list):
        return []
    rows: list[dict[str, int]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError(f"Invalid selection_matrix row {item!r}; expected mapping.")
        quota = item.get("target_accepted", item.get("accepted_episodes", item.get("quota")))
        if quota is None:
            raise ValueError(f"Invalid selection_matrix row {item!r}; missing target_accepted.")
        rows.append(
            {
                "vehicle_count": int(item["vehicle_count"]),
                "target_large_vehicle_count": int(item["target_large_vehicle_count"]),
                "target_accepted": int(quota),
            }
        )
    return rows


def _candidate_sort_key(row: dict[str, Any]) -> tuple[float, float, str]:
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return (
        float(row.get("collision_risk", metrics.get("collision_risk", 0.0))),
        -float(metrics.get("preflight_score", 0.0)),
        str(row.get("plan_id", "")),
    )


def _summarize_candidate(row: dict[str, Any], rank: int) -> dict[str, Any]:
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    controlled_vehicle_types = [str(vehicle.get("vehicle_type", "")) for vehicle in row.get("vehicles", [])]
    background_vehicle_types = [str(item) for item in background.get("vehicle_types", [])]
    planned_large = _planned_large_vehicle_count(row)
    target_large = _target_large_vehicle_count(row)
    return {
        "rank": int(rank),
        "plan_id": str(row["plan_id"]),
        "vehicle_count": int(_plan_vehicle_count(row)),
        "target_large_vehicle_count": int(target_large),
        "planned_large_vehicle_count": int(planned_large),
        "planned_controlled_large_vehicle_count": int(
            sum(_is_large_vehicle(vehicle_type) for vehicle_type in controlled_vehicle_types)
        ),
        "planned_background_large_vehicle_count": int(
            sum(_is_large_vehicle(vehicle_type) for vehicle_type in background_vehicle_types)
        ),
        "controlled_vehicle_count": int(len(row.get("vehicles", []))),
        "requested_background_count": _requested_background_count(row),
        "controlled_vehicle_types": controlled_vehicle_types,
        "background_vehicle_types": background_vehicle_types,
        "contains_fusorosa": bool(planned_large > 0),
        "target_tx_id": str(row.get("target_tx_id", "")),
        "scenario_type": str(row.get("scenario_type", "")),
        "route_ids": [str(vehicle.get("route_id", "")) for vehicle in row.get("vehicles", [])],
        "collision_risk": float(row.get("collision_risk", metrics.get("collision_risk", 0.0))),
        "preflight_score": float(metrics.get("preflight_score", 0.0)),
    }


def _plan_vehicle_count(row: dict[str, Any]) -> int:
    bucket = row.get("bucket", {}) if isinstance(row.get("bucket"), dict) else {}
    return int(bucket.get("vehicle_count", row.get("vehicle_count", 0)))


def _target_large_vehicle_count(row: dict[str, Any]) -> int:
    bucket = row.get("bucket", {}) if isinstance(row.get("bucket"), dict) else {}
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return int(bucket.get("target_large_vehicle_count", metrics.get("target_large_vehicle_count", _planned_large_vehicle_count(row))))


def _planned_large_vehicle_count(row: dict[str, Any]) -> int:
    controlled = sum(_is_large_vehicle(vehicle.get("vehicle_type", "")) for vehicle in row.get("vehicles", []))
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    return int(controlled + sum(_is_large_vehicle(item) for item in background.get("vehicle_types", [])))


def _requested_background_count(row: dict[str, Any]) -> int:
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return int(background.get("requested_count", metrics.get("requested_background_count", 0)))


def _is_large_vehicle(value: Any) -> bool:
    return "fusorosa" in str(value).lower()


def _cell_id(vehicle_count: int, target_large_vehicle_count: int) -> str:
    return f"vehicles_{int(vehicle_count)}_large_{int(target_large_vehicle_count)}"


def _histogram(values) -> dict[str, int]:
    counter = Counter(str(value) for value in values)
    return {key: int(counter[key]) for key in sorted(counter)}
