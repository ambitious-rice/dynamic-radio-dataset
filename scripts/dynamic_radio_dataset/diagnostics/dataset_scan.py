from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from dynamic_radio_dataset.json_utils import load_json


@dataclass
class DatasetScan:
    root: Path
    attempts: list[dict[str, Any]]
    failed_attempts: list[dict[str, Any]]
    trajectory_reports: list[dict[str, Any]]
    rf_metas: list[dict[str, Any]]
    rf_failure_summaries: list[dict[str, Any]]
    qa_reports: list[dict[str, Any]]
    index_rows: list[dict[str, Any]]
    command_timings: list[dict[str, Any]]
    collection_summary: dict[str, Any] | None
    visualization_manifest: dict[str, Any] | None


def scan_dataset(root: Path) -> DatasetScan:
    root = Path(root)
    return DatasetScan(
        root=root,
        attempts=load_attempt_records(root),
        failed_attempts=load_failed_attempt_records(root),
        trajectory_reports=load_trajectory_records(root),
        rf_metas=load_rf_meta_records(root),
        rf_failure_summaries=load_rf_failure_summaries(root),
        qa_reports=load_qa_records(root),
        index_rows=read_jsonl(root / "episode_index.jsonl"),
        command_timings=read_jsonl(root / "command_timings.jsonl"),
        collection_summary=load_optional_json(root / "collection_summary.json"),
        visualization_manifest=load_optional_json(root / "renders" / "green_absolute_sample" / "sampled_visualizations.json"),
    )


def load_json_files(root: Path, pattern: str) -> list[dict[str, Any]]:
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob(pattern)):
        row = load_json_record(path)
        if row is None:
            continue
        row["_source_path"] = str(path)
        rows.append(row)
    return rows


def load_attempt_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "attempts").glob("attempt_*/attempt_meta.json")):
        row = load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("_source_path", str(path))
        row.setdefault("attempt_id", path.parent.name)
        attach_plan(row, path.parent / "plan.json")
        records.append(row)
    return records


def load_failed_attempt_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for failed_dir in sorted((root / "failed_attempts").glob("*")):
        if not failed_dir.is_dir():
            continue
        meta = load_json_record(failed_dir / "attempt_meta.json")
        failure = load_json_record(failed_dir / "failure.json")
        row = dict(meta or failure or {})
        if failure:
            row.setdefault("failure_code", failure.get("failure_code"))
            row.setdefault("status", failure.get("status"))
            row["failure_record"] = failure
        source_path = (failed_dir / "attempt_meta.json") if meta else failed_dir
        row.setdefault("source_path", str(source_path))
        row.setdefault("_source_path", str(source_path))
        row.setdefault("attempt_id", failed_dir.name)
        attach_plan(row, failed_dir / "plan.json")
        records.append(row)
    return records


def load_trajectory_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for base in (root / "episodes", root / "failed_attempts"):
        for path in sorted(base.glob("*/trajectory_qa.json")):
            row = load_json_record(path)
            if row is None:
                continue
            row.setdefault("source_path", str(path))
            row.setdefault("_source_path", str(path))
            row.setdefault("episode_id", path.parent.name)
            attach_plan(row, path.parent / "plan.json")
            records.append(row)
    return records


def load_rf_meta_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "episodes").glob("episode_*/rf_process_meta.json")):
        row = load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("_source_path", str(path))
        row.setdefault("episode_id", path.parent.name)
        records.append(row)
    return records


def load_rf_failure_summaries(root: Path, *, dedupe: bool = False) -> list[dict[str, Any]]:
    summaries: list[dict[str, Any]] = []
    candidates = [root / "rf_failure_summary.json"]
    candidates.extend(sorted(root.glob("rf_failure_summary_*.json")))
    seen: set[str] = set()
    for path in candidates:
        row = load_json_record(path)
        if row is None:
            continue
        if dedupe:
            signature = json.dumps(
                {
                    "schema": row.get("schema"),
                    "use_gpu": row.get("use_gpu"),
                    "gpu_ids": row.get("gpu_ids"),
                    "worker_count": row.get("worker_count"),
                    "episode_candidates": row.get("episode_candidates"),
                    "queued_episode_count": row.get("queued_episode_count"),
                    "processed_episode_count": row.get("processed_episode_count"),
                    "failed_episode_count": row.get("failed_episode_count"),
                    "failures": [
                        {
                            "episode_id": item.get("episode_id"),
                            "gpu_id": item.get("gpu_id"),
                            "worker_index": item.get("worker_index"),
                            "mitsuba_variant": item.get("mitsuba_variant"),
                            "status": item.get("status"),
                        }
                        for item in row.get("failures", [])
                        if isinstance(item, dict)
                    ],
                },
                sort_keys=True,
            )
            if signature in seen:
                continue
            seen.add(signature)
        row.setdefault("source_path", str(path))
        row.setdefault("_source_path", str(path))
        summaries.append(row)
    return summaries


def load_qa_records(root: Path) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for path in sorted((root / "episodes").glob("episode_*/qa_report.json")):
        row = load_json_record(path)
        if row is None:
            continue
        row.setdefault("source_path", str(path))
        row.setdefault("_source_path", str(path))
        row.setdefault("episode_id", path.parent.name)
        records.append(row)
    return records


def load_json_record(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = load_json(path)
    except Exception:  # noqa: BLE001
        return {"source_path": str(path), "_source_path": str(path), "failure_code": "json_read_failed"}
    return value if isinstance(value, dict) else None


def load_optional_json(path: Path) -> dict[str, Any] | None:
    value = load_json_record(path)
    return value if isinstance(value, dict) else None


def attach_plan(row: dict[str, Any], plan_path: Path) -> None:
    plan = load_json_record(plan_path)
    if plan is None:
        return
    row.setdefault("plan_id", plan.get("plan_id"))
    row.setdefault("plan_bucket", plan.get("bucket"))
    row["plan"] = plan


def read_jsonl(path: Path) -> list[dict[str, Any]]:
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

