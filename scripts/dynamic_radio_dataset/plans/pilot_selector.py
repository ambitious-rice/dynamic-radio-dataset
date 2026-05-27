from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from dynamic_radio_dataset.json_utils import iter_jsonl, save_json
from dynamic_radio_dataset.paths import dataset_root


def select_pilot_plans(config: dict, output_path: Path | None = None) -> dict[str, Any]:
    root = dataset_root(config)
    catalog_dir = root / "plan_catalog"
    accepted = list(iter_jsonl(catalog_dir / "plans.jsonl"))
    rejected = list(iter_jsonl(catalog_dir / "rejected_preflight_plans.jsonl")) if (catalog_dir / "rejected_preflight_plans.jsonl").exists() else []
    required_counts = _required_vehicle_counts(config)

    selected: list[dict[str, Any]] = []
    selected_ids: set[str] = set()
    missing_counts: list[int] = []
    missing_reasons: dict[str, Any] = {}

    for count in required_counts:
        candidates = [row for row in accepted if int(row.get("vehicle_count", -1)) == count]
        if not candidates:
            missing_counts.append(count)
            missing_reasons[str(count)] = _missing_count_reason(count, rejected)
            continue
        row = _best_candidate(candidates)
        selected.append(_summarize_plan(row))
        selected_ids.add(str(row["plan_id"]))

    contains_fusorosa = any(bool(row["contains_fusorosa"]) for row in selected)
    if not contains_fusorosa:
        fuso_candidates = [row for row in accepted if _contains_fusorosa(row) and str(row["plan_id"]) not in selected_ids]
        if fuso_candidates:
            row = _best_candidate(fuso_candidates)
            selected.append(_summarize_plan(row, selection_reason="fusorosa_coverage"))
            selected_ids.add(str(row["plan_id"]))
            contains_fusorosa = True

    result = {
        "schema": "pilot_plan_selection_dry_run_v1",
        "dataset_root": str(root),
        "required_vehicle_counts": required_counts,
        "required_actual_target_counts": required_counts,
        "selection_policy": (
            "vehicle_count is the requested total actual-vehicle target; only controlled vehicles are planned routes, "
            "background_tm vehicles are requested from CARLA Traffic Manager and validated by actual recorded count after collect"
        ),
        "can_select_required_pilot_set": bool(not missing_counts and contains_fusorosa),
        "missing_vehicle_counts": missing_counts,
        "contains_fusorosa": bool(contains_fusorosa),
        "selected_plan_ids": [row["plan_id"] for row in selected],
        "selected_plans": selected,
        "failure_reasons": {
            "missing_vehicle_counts": missing_reasons,
            "fusorosa": None if contains_fusorosa else _missing_fusorosa_reason(rejected),
        },
        "accepted_vehicle_count_histogram": dict(Counter(int(row.get("vehicle_count", -1)) for row in accepted)),
        "accepted_requested_background_count_histogram": dict(Counter(_requested_background_count(row) for row in accepted)),
        "rejected_vehicle_count_histogram": dict(Counter(int(row.get("vehicle_count", -1)) for row in rejected)),
        "rejection_reason_histogram": dict(Counter(str(row.get("failure_code")) for row in rejected)),
    }
    target_path = output_path or (catalog_dir / "pilot_selection_dry_run.json")
    save_json(target_path, result)
    result["output_path"] = str(target_path)
    return result


def _best_candidate(rows: list[dict]) -> dict:
    return sorted(
        rows,
        key=lambda row: (
            float(row.get("collision_risk", 0.0)),
            -float(row.get("expected_metrics", {}).get("preflight_score", 0.0)),
            str(row.get("plan_id", "")),
        ),
    )[0]


def _summarize_plan(row: dict, selection_reason: str | None = None) -> dict[str, Any]:
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    risk_summary = metrics.get("collision_risk_summary", {}) if isinstance(metrics.get("collision_risk_summary"), dict) else {}
    spawn_summary = metrics.get("spawn_start_conflict_summary", {}) if isinstance(metrics.get("spawn_start_conflict_summary"), dict) else {}
    return {
        "plan_id": str(row["plan_id"]),
        "selection_reason": selection_reason or f"{int(row['vehicle_count'])}_vehicle_coverage",
        "vehicle_count": int(row["vehicle_count"]),
        "requested_total_vehicle_count": int(row["vehicle_count"]),
        "controlled_vehicle_count": int(len(row.get("vehicles", []))),
        "requested_background_count": _requested_background_count(row),
        "role_counts": metrics.get("vehicle_role_counts", {}),
        "target_tx_id": str(row.get("target_tx_id")),
        "scenario_type": str(row.get("scenario_type")),
        "contains_fusorosa": _contains_fusorosa(row),
        "target_large_vehicle_count": int(metrics.get("target_large_vehicle_count", 1 if _contains_fusorosa(row) else 0)),
        "vehicle_roles": [str(vehicle.get("role")) for vehicle in row.get("vehicles", [])],
        "vehicle_types": [str(vehicle.get("vehicle_type")) for vehicle in row.get("vehicles", [])],
        "background_vehicle_types": [
            str(vehicle_type)
            for vehicle_type in (
                row.get("background_tm", {}).get("vehicle_types", [])
                if isinstance(row.get("background_tm"), dict)
                else []
            )
        ],
        "route_ids": [str(vehicle.get("route_id")) for vehicle in row.get("vehicles", [])],
        "start_delay_s": [float(vehicle.get("start_delay_s", 0.0)) for vehicle in row.get("vehicles", [])],
        "speed_diff": [float(vehicle.get("speed_diff", 0.0)) for vehicle in row.get("vehicles", [])],
        "risk_summary": {
            "collision_risk": float(row.get("collision_risk", 0.0)),
            "policy": risk_summary.get("policy"),
            "active_conflict_pair_count": int(risk_summary.get("active_conflict_pair_count", 0)),
            "extreme_pair_risk": float(risk_summary.get("extreme_pair_risk", row.get("collision_risk", 0.0))),
            "sum_pair_risk_diagnostic": float(risk_summary.get("sum_pair_risk_diagnostic", 0.0)),
            "top_conflict_pairs": risk_summary.get("top_conflict_pairs", []),
        },
        "spawn_start_summary": {
            "policy": spawn_summary.get("policy"),
            "min_spawn_start_separation_m": float(spawn_summary.get("min_spawn_start_separation_m", 0.0)),
            "min_observed_start_distance_m": spawn_summary.get("min_observed_start_distance_m"),
            "conflict_count": int(spawn_summary.get("conflict_count", 0)),
            "conflicts": spawn_summary.get("conflicts", []),
        },
    }


def _contains_fusorosa(row: dict) -> bool:
    if any("fusorosa" in str(vehicle.get("vehicle_type", "")).lower() for vehicle in row.get("vehicles", [])):
        return True
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    return any("fusorosa" in str(vehicle_type).lower() for vehicle_type in background.get("vehicle_types", []))


def _required_vehicle_counts(config: dict) -> list[int]:
    collection = config.get("collection", {})
    if isinstance(collection.get("bucket_order"), list):
        return [int(item) for item in collection["bucket_order"]]
    mix = config.get("traffic", {}).get("vehicle_count_mix", {})
    return sorted(int(count) for count in mix)


def _requested_background_count(row: dict) -> int:
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return int(background.get("requested_count", metrics.get("requested_background_count", 0)))


def _missing_count_reason(count: int, rejected: list[dict]) -> dict[str, Any]:
    rows = [row for row in rejected if int(row.get("vehicle_count", -1)) == count]
    if not rows:
        return {
            "reason": "not_sampled_or_sampler_quota_missing",
            "suggested_fix": "increase num_plans/candidate_multiplier or inspect vehicle_count quota generation",
        }
    hist = Counter(str(row.get("failure_code")) for row in rows)
    top_reason = hist.most_common(1)[0][0]
    suggested = "inspect sampler/preflight for this vehicle_count"
    if "conflict_risk" in top_reason:
        suggested = "review extreme conflict risk threshold or route timing spacing for this vehicle_count"
    return {
        "reason": f"rejected_by_{top_reason}",
        "rejected_count": len(rows),
        "rejection_reason_histogram": dict(hist),
        "suggested_fix": suggested,
    }


def _missing_fusorosa_reason(rejected: list[dict]) -> dict[str, Any]:
    rows = [row for row in rejected if _contains_fusorosa(row)]
    if not rows:
        return {
            "reason": "not_sampled_or_sampler_quota_missing",
            "suggested_fix": "generate a larger plan bank or review configured large-vehicle quotas",
        }
    hist = Counter(str(row.get("failure_code")) for row in rows)
    return {
        "reason": f"rejected_by_{hist.most_common(1)[0][0]}",
        "rejected_count": len(rows),
        "rejection_reason_histogram": dict(hist),
        "suggested_fix": "inspect FusoRosa route combinations and extreme conflict risk diagnostics",
    }
