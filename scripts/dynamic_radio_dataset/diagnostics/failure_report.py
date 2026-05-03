from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from dynamic_radio_dataset.configs import load_config
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root


def write_failure_report(
    config: dict | None = None,
    *,
    dataset_root_path: Path | None = None,
    output_path: Path | None = None,
    max_examples: int = 5,
) -> dict[str, Any]:
    root = dataset_root(config) if dataset_root_path is None else Path(dataset_root_path)
    report = build_failure_report(root, max_examples=max_examples)
    destination = output_path or (root / "diagnostics" / "failure_report.json")
    save_json(destination, report)
    return {"dataset_root": str(root), "failure_report": str(destination), "summary": report["summary"]}


def build_failure_report(root: Path, *, max_examples: int = 5) -> dict[str, Any]:
    max_examples = max(1, int(max_examples))
    attempts = _load_attempt_records(root)
    failed_attempts = _load_failed_attempt_records(root)
    trajectory_reports = _load_trajectory_records(root)
    rf_metas = _load_rf_meta_records(root)
    rf_summaries = _load_rf_failure_summaries(root)
    qa_reports = _load_qa_records(root)
    index_rows = _read_jsonl(root / "episode_index.jsonl")

    accepted_attempts = [row for row in attempts if str(row.get("status")) == "TRAJECTORY_ACCEPTED"]
    failed_attempt_rows = [row for row in attempts if str(row.get("status")) != "TRAJECTORY_ACCEPTED"]
    trajectory_failures = [row for row in trajectory_reports if not bool(row.get("trajectory_qc_pass"))]
    rf_failures = _rf_failure_rows(rf_metas, rf_summaries)
    per_tx_failures = _per_tx_failure_rows(qa_reports)

    report = {
        "schema": "dynamic_radio_dataset_failure_report_v1",
        "dataset_root": str(root),
        "summary": {
            "attempt_count": int(len(attempts)),
            "accepted_attempt_count": int(len(accepted_attempts)),
            "failed_attempt_count": int(len(failed_attempt_rows)),
            "archived_failed_attempt_count": int(len(failed_attempts)),
            "trajectory_report_count": int(len(trajectory_reports)),
            "trajectory_qa_failed_count": int(len(trajectory_failures)),
            "rf_meta_count": int(len(rf_metas)),
            "rf_failed_episode_count": int(len(rf_failures)),
            "qa_report_count": int(len(qa_reports)),
            "per_tx_qa_failed_count": int(len(per_tx_failures)),
            "indexed_row_count": int(len(index_rows)),
        },
        "histograms": {
            "attempt_status": _histogram(attempts, "status"),
            "failure_code": _failure_code_histogram(attempts, failed_attempts, trajectory_failures, rf_failures, per_tx_failures),
            "route_id": _route_histogram(attempts, failed_attempts, trajectory_reports),
            "requested_vehicle_count": _plan_value_histogram(attempts, failed_attempts, "vehicle_count"),
            "actual_vehicle_count": _actual_vehicle_histogram(trajectory_reports, qa_reports),
            "large_vehicle_count": _large_vehicle_histogram(attempts, failed_attempts, trajectory_reports, qa_reports),
            "plan_bucket": _plan_bucket_histogram(attempts, failed_attempts),
            "selection_cell": _selection_cell_histogram(attempts, failed_attempts),
        },
        "carla_failures": _carla_failures(failed_attempt_rows, failed_attempts, max_examples=max_examples),
        "trajectory_qa_failures": _trajectory_failure_summary(trajectory_failures, max_examples=max_examples),
        "rf_failures": _rf_failure_summary(rf_failures, max_examples=max_examples),
        "per_tx_qa_failures": _per_tx_failure_summary(per_tx_failures, max_examples=max_examples),
        "accepted_vs_failed": _accepted_vs_failed(attempts, failed_attempts),
        "examples": {
            "attempt_failures": _examples(failed_attempt_rows + failed_attempts, max_examples=max_examples),
            "trajectory_qa_failures": _examples(trajectory_failures, max_examples=max_examples),
            "rf_failures": _examples(rf_failures, max_examples=max_examples),
            "per_tx_qa_failures": _examples(per_tx_failures, max_examples=max_examples),
        },
    }
    return report


def _load_attempt_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "attempts").glob("attempt_*/attempt_meta.json")):
        row = _load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("attempt_id", path.parent.name)
        _attach_plan(row, path.parent / "plan.json")
        records.append(row)
    return records


def _load_failed_attempt_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for failed_dir in sorted((root / "failed_attempts").glob("*")):
        if not failed_dir.is_dir():
            continue
        meta = _load_json_record(failed_dir / "attempt_meta.json")
        failure = _load_json_record(failed_dir / "failure.json")
        row = dict(meta or failure or {})
        if failure:
            row.setdefault("failure_code", failure.get("failure_code"))
            row.setdefault("status", failure.get("status"))
            row["failure_record"] = failure
        row.setdefault("source_path", str((failed_dir / "attempt_meta.json") if meta else failed_dir))
        row.setdefault("attempt_id", failed_dir.name)
        _attach_plan(row, failed_dir / "plan.json")
        records.append(row)
    return records


def _load_trajectory_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for base in (root / "episodes", root / "failed_attempts"):
        for path in sorted(base.glob("*/trajectory_qa.json")):
            row = _load_json_record(path)
            if row is None:
                continue
            row.setdefault("source_path", str(path))
            row.setdefault("episode_id", path.parent.name)
            _attach_plan(row, path.parent / "plan.json")
            records.append(row)
    return records


def _load_rf_meta_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "episodes").glob("episode_*/rf_process_meta.json")):
        row = _load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("episode_id", path.parent.name)
        records.append(row)
    return records


def _load_rf_failure_summaries(root: Path) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    candidates = [root / "rf_failure_summary.json"]
    candidates.extend(sorted(root.glob("rf_failure_summary_*.json")))
    for path in candidates:
        row = _load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        summaries.append(row)
    return summaries


def _load_qa_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "episodes").glob("episode_*/qa_report.json")):
        row = _load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("episode_id", path.parent.name)
        records.append(row)
    return records


def _load_json_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = load_json(path)
    except Exception:  # noqa: BLE001
        return {"source_path": str(path), "failure_code": "json_read_failed"}
    return value if isinstance(value, dict) else None


def _attach_plan(row: dict[str, Any], plan_path: Path) -> None:
    plan = _load_json_record(plan_path)
    if plan is None:
        return
    row.setdefault("plan_id", plan.get("plan_id"))
    row.setdefault("plan_bucket", plan.get("bucket"))
    row["plan"] = plan


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            rows.append(value)
    return rows


def _rf_failure_rows(rf_metas: list[dict[str, Any]], rf_summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [dict(row) for row in rf_metas if str(row.get("status")) == "failed"]
    seen_episode_ids = {str(row.get("episode_id")) for row in rows if row.get("episode_id") is not None}
    for summary in rf_summaries:
        for item in summary.get("failures", []) if isinstance(summary.get("failures"), list) else []:
            if not isinstance(item, dict):
                continue
            episode_id = str(item.get("episode_id")) if item.get("episode_id") is not None else None
            if episode_id is not None and episode_id in seen_episode_ids:
                continue
            row = dict(item)
            row.setdefault("source_path", summary.get("source_path"))
            row.setdefault("failure_code", item.get("failure_code") or "rf_worker_failed")
            rows.append(row)
            if episode_id is not None:
                seen_episode_ids.add(episode_id)
    return rows


def _per_tx_failure_rows(qa_reports: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for report in qa_reports:
        scene_pass = bool(report.get("scene_qc_pass"))
        for tx_report in report.get("tx_reports", []) if isinstance(report.get("tx_reports"), list) else []:
            if bool(tx_report.get("pair_qc_pass")) and scene_pass:
                continue
            row = dict(tx_report)
            row["episode_id"] = report.get("episode_id")
            row["source_path"] = report.get("source_path")
            row["scene_qc_pass"] = scene_pass
            blockers = tx_report.get("blockers") if isinstance(tx_report.get("blockers"), list) else []
            row["failure_code"] = blockers[0] if blockers else ("scene_qc_failed" if not scene_pass else "per_tx_qa_failed")
            rows.append(row)
    return rows


def _histogram(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if value is not None:
            counter[str(value)] += 1
    return _sorted_counter(counter)


def _failure_code_histogram(*groups: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for group in groups:
        for row in group:
            code = row.get("failure_code")
            if code:
                counter[str(code)] += 1
    return _sorted_counter(counter)


def _route_histogram(*groups: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for group in groups:
        for row in group:
            for route_id in _route_ids(row):
                counter[str(route_id)] += 1
    return _sorted_counter(counter)


def _plan_value_histogram(*groups_and_key: Any) -> dict[str, int]:
    *groups, key = groups_and_key
    counter: Counter[str] = Counter()
    for group in groups:
        for row in group:
            value = _plan_value(row, str(key))
            if value is not None:
                counter[str(value)] += 1
    return _sorted_counter(counter)


def _actual_vehicle_histogram(trajectory_reports: list[dict[str, Any]], qa_reports: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in trajectory_reports:
        counts = row.get("vehicle_role_counts") if isinstance(row.get("vehicle_role_counts"), dict) else {}
        value = counts.get("actual_total_vehicle_count")
        if value is not None:
            counter[str(int(value))] += 1
    for row in qa_reports:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        counts = summary.get("vehicle_role_counts") if isinstance(summary.get("vehicle_role_counts"), dict) else {}
        value = counts.get("actual_total_vehicle_count")
        if value is not None:
            counter[str(int(value))] += 1
    return _sorted_counter(counter)


def _large_vehicle_histogram(
    attempts: list[dict[str, Any]],
    failed_attempts: list[dict[str, Any]],
    trajectory_reports: list[dict[str, Any]],
    qa_reports: list[dict[str, Any]],
) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in attempts + failed_attempts:
        value = _plan_value(row, "target_large_vehicle_count")
        if value is not None:
            counter[f"target:{int(value)}"] += 1
    for row in trajectory_reports:
        mix = row.get("vehicle_mix") if isinstance(row.get("vehicle_mix"), dict) else {}
        value = mix.get("large_vehicle_count")
        if value is not None:
            counter[f"actual:{int(value)}"] += 1
    for row in qa_reports:
        summary = row.get("summary") if isinstance(row.get("summary"), dict) else {}
        value = summary.get("large_vehicle_count")
        if value is not None:
            counter[f"actual:{int(value)}"] += 1
    return _sorted_counter(counter)


def _plan_bucket_histogram(attempts: list[dict[str, Any]], failed_attempts: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in attempts + failed_attempts:
        bucket = row.get("plan_bucket") if isinstance(row.get("plan_bucket"), dict) else {}
        vehicle_count = bucket.get("vehicle_count", _plan_value(row, "vehicle_count"))
        target_large = bucket.get("target_large_vehicle_count", _plan_value(row, "target_large_vehicle_count"))
        if vehicle_count is not None:
            counter[f"vehicle_count_{vehicle_count}_large_{target_large if target_large is not None else 'unknown'}"] += 1
    return _sorted_counter(counter)


def _selection_cell_histogram(attempts: list[dict[str, Any]], failed_attempts: list[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in attempts + failed_attempts:
        vehicle_count = _plan_value(row, "vehicle_count")
        target_large = _plan_value(row, "target_large_vehicle_count")
        if vehicle_count is not None and target_large is not None:
            counter[f"v{int(vehicle_count)}_large{int(target_large)}"] += 1
    return _sorted_counter(counter)


def _carla_failures(rows: list[dict[str, Any]], failed_attempts: list[dict[str, Any]], *, max_examples: int) -> dict[str, Any]:
    combined = rows + failed_attempts
    subprocess_rows = [row for row in combined if str(row.get("status")) == "CARLA_FAILED" or "carla" in str(row.get("failure_code", ""))]
    spawn_rows = [row for row in combined if str(row.get("failure_code")) == "traffic_plan_spawn_incomplete"]
    return {
        "subprocess_failure_count": int(len(subprocess_rows)),
        "spawn_failure_count": int(len(spawn_rows)),
        "failure_code_histogram": _failure_code_histogram(combined),
        "examples": _examples(subprocess_rows + spawn_rows, max_examples=max_examples),
    }


def _trajectory_failure_summary(rows: list[dict[str, Any]], *, max_examples: int) -> dict[str, Any]:
    blocker_counter: Counter[str] = Counter()
    for row in rows:
        for blocker in row.get("blockers", []) if isinstance(row.get("blockers"), list) else []:
            blocker_counter[str(blocker)] += 1
    return {
        "failure_count": int(len(rows)),
        "failure_code_histogram": _histogram(rows, "failure_code"),
        "blocker_histogram": _sorted_counter(blocker_counter),
        "examples": _examples(rows, max_examples=max_examples),
    }


def _rf_failure_summary(rows: list[dict[str, Any]], *, max_examples: int) -> dict[str, Any]:
    gpu_counter = Counter(str(row.get("gpu_id")) for row in rows if row.get("gpu_id") is not None)
    worker_counter = Counter(str(row.get("worker_index")) for row in rows if row.get("worker_index") is not None)
    return {
        "failure_count": int(len(rows)),
        "failure_code_histogram": _histogram(rows, "failure_code"),
        "gpu_histogram": _sorted_counter(gpu_counter),
        "worker_histogram": _sorted_counter(worker_counter),
        "examples": _examples(rows, max_examples=max_examples),
    }


def _per_tx_failure_summary(rows: list[dict[str, Any]], *, max_examples: int) -> dict[str, Any]:
    tx_counter = Counter(str(row.get("tx_id")) for row in rows if row.get("tx_id") is not None)
    metric_rows = []
    for row in rows:
        metrics = row.get("dynamic_rss_summary") or row.get("dynamic_rss_metrics") or row.get("rss_metrics")
        if isinstance(metrics, dict):
            metric_rows.append(metrics)
    return {
        "failure_count": int(len(rows)),
        "failure_code_histogram": _histogram(rows, "failure_code"),
        "tx_id_histogram": _sorted_counter(tx_counter),
        "metric_example_count": int(len(metric_rows)),
        "examples": _examples(rows, max_examples=max_examples),
    }


def _accepted_vs_failed(attempts: list[dict[str, Any]], failed_attempts: list[dict[str, Any]]) -> dict[str, Any]:
    accepted = [row for row in attempts if str(row.get("status")) == "TRAJECTORY_ACCEPTED"]
    failed = [row for row in attempts + failed_attempts if str(row.get("status")) != "TRAJECTORY_ACCEPTED"]
    return {
        "accepted": {
            "count": int(len(accepted)),
            "route_id": _route_histogram(accepted),
            "vehicle_count": _plan_value_histogram(accepted, "vehicle_count"),
            "large_vehicle_count": _plan_value_histogram(accepted, "target_large_vehicle_count"),
            "selection_cell": _selection_cell_histogram(accepted, []),
        },
        "failed": {
            "count": int(len(failed)),
            "route_id": _route_histogram(failed),
            "vehicle_count": _plan_value_histogram(failed, "vehicle_count"),
            "large_vehicle_count": _plan_value_histogram(failed, "target_large_vehicle_count"),
            "selection_cell": _selection_cell_histogram(failed, []),
            "failure_code": _failure_code_histogram(failed),
        },
    }


def _examples(rows: list[dict[str, Any]], *, max_examples: int) -> list[dict[str, Any]]:
    examples: list[dict[str, Any]] = []
    for row in rows[:max_examples]:
        examples.append(
            {
                "source_path": row.get("source_path"),
                "attempt_id": row.get("attempt_id"),
                "episode_id": row.get("episode_id"),
                "plan_id": row.get("plan_id"),
                "status": row.get("status"),
                "failure_code": row.get("failure_code"),
                "route_ids": _route_ids(row),
                "vehicle_count": _plan_value(row, "vehicle_count"),
                "target_large_vehicle_count": _plan_value(row, "target_large_vehicle_count"),
            }
        )
    return examples


def _route_ids(row: dict[str, Any]) -> list[str]:
    route_ids: list[str] = []
    plan = row.get("plan") if isinstance(row.get("plan"), dict) else {}
    vehicles = plan.get("vehicles", []) if isinstance(plan.get("vehicles"), list) else []
    for vehicle in vehicles:
        if isinstance(vehicle, dict) and vehicle.get("route_id") is not None:
            route_ids.append(str(vehicle["route_id"]))
    if row.get("route_id") is not None:
        route_ids.append(str(row["route_id"]))
    return sorted(set(route_ids))


def _plan_value(row: dict[str, Any], key: str) -> Any:
    bucket = row.get("plan_bucket") if isinstance(row.get("plan_bucket"), dict) else {}
    if key in bucket:
        return bucket[key]
    plan = row.get("plan") if isinstance(row.get("plan"), dict) else {}
    if key in plan:
        return plan[key]
    expected = plan.get("expected_metrics") if isinstance(plan.get("expected_metrics"), dict) else {}
    if key in expected:
        return expected[key]
    return None


def _sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a read-only failure report for a dynamic radio dataset.")
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--config", type=Path, default=None)
    source.add_argument("--dataset-root", type=Path, default=None)
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--output-json", type=Path, default=None)
    parser.add_argument("--max-examples", type=int, default=5)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config, profile_override=args.profile) if args.config is not None else None
    result = write_failure_report(
        config,
        dataset_root_path=args.dataset_root,
        output_path=args.output_json,
        max_examples=int(args.max_examples),
    )
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
