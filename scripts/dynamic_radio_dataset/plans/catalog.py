from __future__ import annotations

from pathlib import Path
from typing import Iterable

from dynamic_radio_dataset.json_utils import iter_jsonl, save_json, write_jsonl
from dynamic_radio_dataset.plans.schemas import TrafficPlan, traffic_plan_from_dict


def write_plan_catalog(output_dir: Path, accepted: Iterable[TrafficPlan], rejected: Iterable[dict]) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    accepted_rows = [plan.to_dict() for plan in accepted]
    rejected_rows = list(rejected)
    write_jsonl(output_dir / "plans.jsonl", accepted_rows)
    write_jsonl(output_dir / "rejected_preflight_plans.jsonl", rejected_rows)
    summary = {
        "schema": "plan_bank_summary_v1",
        "accepted_plan_count": len(accepted_rows),
        "rejected_preflight_count": len(rejected_rows),
        "preflight_pass_rate": float(len(accepted_rows) / max(len(accepted_rows) + len(rejected_rows), 1)),
        "vehicle_count_histogram": _histogram(row["vehicle_count"] for row in accepted_rows),
        "rejected_vehicle_count_histogram": _histogram(row["vehicle_count"] for row in rejected_rows),
        "controlled_vehicle_count_histogram": _histogram(len(row.get("vehicles", [])) for row in accepted_rows),
        "requested_background_count_histogram": _histogram(_requested_background_count(row) for row in accepted_rows),
        "bucket_vehicle_count_histogram": _histogram(_bucket_vehicle_count(row) for row in accepted_rows),
        "bucket_order_histogram": _histogram(_bucket_order(row) for row in accepted_rows),
        "target_large_vehicle_count_histogram": _histogram(_target_large_vehicle_count(row) for row in accepted_rows),
        "target_large_by_vehicle_count_histogram": _histogram(
            f"{_bucket_vehicle_count(row)}:{_target_large_vehicle_count(row)}" for row in accepted_rows
        ),
        "planned_large_vehicle_count_histogram": _histogram(_planned_large_vehicle_count(row) for row in accepted_rows),
        "planned_background_large_vehicle_count_histogram": _histogram(
            _planned_background_large_vehicle_count(row) for row in accepted_rows
        ),
        "rejection_reason_histogram": _histogram(row.get("failure_code") for row in rejected_rows),
        "target_tx_histogram": _histogram(row["target_tx_id"] for row in accepted_rows),
        "scenario_type_histogram": _histogram(row["scenario_type"] for row in accepted_rows),
        "role_count_summary": _role_count_summary(accepted_rows),
        "accepted_fusorosa_plan_count": _fusorosa_plan_count(accepted_rows),
        "rejected_fusorosa_plan_count": _fusorosa_plan_count(rejected_rows),
        "accepted_three_large_plan_count": sum(1 for row in accepted_rows if _target_large_vehicle_count(row) == 3),
        "accepted_three_large_6plus_plan_count": sum(
            1 for row in accepted_rows if _target_large_vehicle_count(row) == 3 and int(row.get("vehicle_count", 0)) >= 6
        ),
        "accepted_6_vehicle_plan_count": sum(1 for row in accepted_rows if int(row.get("vehicle_count", 0)) == 6),
        "accepted_6plus_requested_total_plan_count": sum(1 for row in accepted_rows if int(row.get("vehicle_count", 0)) >= 6),
        "collision_risk_related_rejected_count": sum(
            1 for row in rejected_rows if "conflict_risk" in str(row.get("failure_code", ""))
        ),
    }
    save_json(output_dir / "plan_bank_summary.json", summary)
    return summary


def read_plan_catalog(path: Path) -> list[TrafficPlan]:
    return [traffic_plan_from_dict(row) for row in iter_jsonl(path)]


def _histogram(values) -> dict[str, int]:
    result: dict[str, int] = {}
    for value in values:
        key = str(value)
        result[key] = result.get(key, 0) + 1
    return result


def _fusorosa_plan_count(rows: list[dict]) -> int:
    return sum(1 for row in rows if _planned_large_vehicle_count(row) > 0)


def _requested_background_count(row: dict) -> int:
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return int(background.get("requested_count", metrics.get("requested_background_count", 0)))


def _bucket_vehicle_count(row: dict) -> int:
    bucket = row.get("bucket", {}) if isinstance(row.get("bucket"), dict) else {}
    return int(bucket.get("vehicle_count", row.get("vehicle_count", 0)))


def _bucket_order(row: dict) -> int:
    bucket = row.get("bucket", {}) if isinstance(row.get("bucket"), dict) else {}
    return int(bucket.get("bucket_order", 0))


def _target_large_vehicle_count(row: dict) -> int:
    bucket = row.get("bucket", {}) if isinstance(row.get("bucket"), dict) else {}
    metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
    return int(bucket.get("target_large_vehicle_count", metrics.get("target_large_vehicle_count", _planned_large_vehicle_count(row))))


def _planned_large_vehicle_count(row: dict) -> int:
    controlled = sum("fusorosa" in str(vehicle.get("vehicle_type", "")).lower() for vehicle in row.get("vehicles", []))
    background = _planned_background_large_vehicle_count(row)
    return int(controlled + background)


def _planned_background_large_vehicle_count(row: dict) -> int:
    background = row.get("background_tm", {}) if isinstance(row.get("background_tm"), dict) else {}
    return int(sum("fusorosa" in str(item).lower() for item in background.get("vehicle_types", [])))


def _role_count_summary(rows: list[dict]) -> dict[str, int]:
    result = {
        "planned_required_controlled_count": 0,
        "planned_optional_controlled_count": 0,
        "requested_background_count": 0,
        "requested_total_vehicle_count": 0,
    }
    for row in rows:
        metrics = row.get("expected_metrics", {}) if isinstance(row.get("expected_metrics"), dict) else {}
        role_counts = metrics.get("vehicle_role_counts", {}) if isinstance(metrics.get("vehicle_role_counts"), dict) else {}
        result["planned_required_controlled_count"] += int(role_counts.get("planned_required_controlled_count", 0))
        result["planned_optional_controlled_count"] += int(role_counts.get("planned_optional_controlled_count", 0))
        result["requested_background_count"] += _requested_background_count(row)
        result["requested_total_vehicle_count"] += int(row.get("vehicle_count", 0))
    return result
