from __future__ import annotations

import shutil
from collections import Counter
from pathlib import Path
from typing import Any

from dynamic_radio_dataset.diagnostics.dataset_scan import (
    load_json_files,
    load_optional_json,
    load_rf_failure_summaries,
    read_jsonl,
)
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root


FULL_RUN_TRAJECTORIES = 3000
FULL_RUN_TX_PAIRS = 9000


def write_timing_report(config: dict) -> dict:
    root = dataset_root(config)
    attempts = load_json_files(root / "attempts", "attempt_*/attempt_meta.json")
    rf_metas = load_json_files(root / "episodes", "episode_*/rf_process_meta.json")
    rf_failure_summaries = load_rf_failure_summaries(root, dedupe=True)
    trajectory_reports = load_json_files(root / "episodes", "episode_*/trajectory_qa.json")
    qa_reports = load_json_files(root / "episodes", "episode_*/qa_report.json")
    index_rows = read_jsonl(root / "episode_index.jsonl")
    command_timings = read_jsonl(root / "command_timings.jsonl")
    visualization_manifest = load_optional_json(root / "renders" / "green_absolute_sample" / "sampled_visualizations.json")
    collection_summary = load_optional_json(root / "collection_summary.json")
    selection_manifest = _load_selection_manifest(config, root, collection_summary)
    target_trajectories, target_tx_pairs = _target_full_run_counts(config, root)

    trajectory_accepted_count = sum(1 for report in trajectory_reports if bool(report.get("trajectory_qc_pass")))
    attempted_count = len(attempts)
    collection_elapsed_values = [float(item.get("elapsed_s", 0.0)) for item in attempts]
    collection_total_s = float(sum(collection_elapsed_values))
    collection_per_attempt_s = _safe_div(collection_total_s, attempted_count)
    collection_per_accepted_s = _safe_div(collection_total_s, trajectory_accepted_count)
    collection_by_bucket = _collection_by_bucket(attempts)
    collection_by_selection_cell = _collection_by_selection_cell(attempts)

    rf_elapsed_values = [float(item.get("elapsed_s", 0.0)) for item in rf_metas if str(item.get("status")) == "processed"]
    rf_total_s = float(sum(rf_elapsed_values)) if rf_elapsed_values else _latest_elapsed(command_timings, "process-rf")
    rf_processed_episode_count = len(rf_elapsed_values) if rf_elapsed_values else sum(1 for report in qa_reports if report.get("tx_reports"))
    rf_total_pair_count = sum(len(report.get("tx_reports") or []) for report in qa_reports)
    rf_accepted_pair_count = sum(1 for row in index_rows if bool(row.get("accepted")))
    rf_per_accepted_trajectory_s = _safe_div(rf_total_s, rf_processed_episode_count)
    rf_per_tx_pair_s = _safe_div(rf_total_s, rf_total_pair_count)
    rf_by_gpu = _rf_by_gpu(rf_metas, qa_reports)
    rf_failed_by_gpu = _rf_failed_by_gpu(rf_failure_summaries)

    route_plan_total_s = _sum_latest_elapsed(command_timings, ["build-route-library", "generate-plans"])
    plan_selection_total_s = _plan_selection_elapsed(selection_manifest, command_timings)
    finalize_total_s = _latest_elapsed(command_timings, "finalize")
    visualization_total_s = _visualization_elapsed(visualization_manifest, command_timings)
    visualization_count = _visualization_count(visualization_manifest)

    collection_estimate_s = _multiply(collection_per_accepted_s, target_trajectories)
    rf_estimate_s = _multiply(rf_per_tx_pair_s, target_tx_pairs)
    route_plan_estimate_s = route_plan_total_s
    plan_selection_estimate_s = plan_selection_total_s
    finalize_estimate_s = finalize_total_s
    estimated_total_s = _sum_present(
        [collection_estimate_s, rf_estimate_s, route_plan_estimate_s, plan_selection_estimate_s, finalize_estimate_s]
    )

    status_histogram = Counter(str(item.get("status") or "unknown") for item in attempts)
    failure_histogram = Counter(
        str(item.get("failure_code"))
        for item in attempts
        if str(item.get("status") or "") != "TRAJECTORY_ACCEPTED" and item.get("failure_code")
    )

    report = {
        "schema": "dynamic_radio_dataset_timing_report_v1",
        "dataset_root": str(root),
        "target_full_run": {
            "trajectory_accepted_count": int(target_trajectories),
            "episode_tx_pair_count": int(target_tx_pairs),
            "default_reference": {
                "trajectory_accepted_count": FULL_RUN_TRAJECTORIES,
                "episode_tx_pair_count": FULL_RUN_TX_PAIRS,
            },
        },
        "observed": {
            "accepted_trajectory_count": int(trajectory_accepted_count),
            "attempted_count": int(attempted_count),
            "trajectory_pass_rate": _safe_div(trajectory_accepted_count, attempted_count),
            "attempt_status_histogram": dict(status_histogram),
            "attempt_failure_code_histogram": dict(failure_histogram),
            "rf_processed_episode_count": int(rf_processed_episode_count),
            "rf_failed_episode_count": int(sum(int(item.get("failed_episode_count", 0)) for item in rf_failure_summaries)),
            "rf_total_pair_count": int(rf_total_pair_count),
            "rf_accepted_pair_count": int(rf_accepted_pair_count),
            "rf_pass_rate": _safe_div(rf_accepted_pair_count, rf_total_pair_count),
        },
        "wall_clock_s": {
            "route_library_and_plan_generation": route_plan_total_s,
            "plan_selection": plan_selection_total_s,
            "collection_total": collection_total_s,
            "collection_per_attempt": collection_per_attempt_s,
            "collection_per_accepted_trajectory": collection_per_accepted_s,
            "rf_processing_total": rf_total_s,
            "rf_per_accepted_trajectory": rf_per_accepted_trajectory_s,
            "rf_per_tx_pair": rf_per_tx_pair_s,
            "finalize_index": finalize_total_s,
            "visualization_total": visualization_total_s,
            "visualization_per_selected_episode_tx": _safe_div(visualization_total_s, visualization_count),
        },
        "collection_by_bucket": collection_by_bucket,
        "collection_by_selection_cell": collection_by_selection_cell,
        "selection_matrix_validation": _selection_matrix_validation(collection_summary, selection_manifest),
        "rf_by_gpu": rf_by_gpu,
        "rf_failed_by_gpu": rf_failed_by_gpu,
        "rf_failure_summaries": _rf_failure_overview(rf_failure_summaries),
        "estimated_full_run_s": {
            "collection": collection_estimate_s,
            "rf_processing": rf_estimate_s,
            "route_library_and_plan_generation": route_plan_estimate_s,
            "plan_selection": plan_selection_estimate_s,
            "finalize_index": finalize_estimate_s,
            "total_without_visualization": estimated_total_s,
        },
        "estimated_full_run_hours": {
            "collection": _safe_div(collection_estimate_s, 3600.0),
            "rf_processing": _safe_div(rf_estimate_s, 3600.0),
            "total_without_visualization": _safe_div(estimated_total_s, 3600.0),
        },
        "estimated_3000_trajectories_9000_tx_pairs": {
            "collection_s": _multiply(collection_per_accepted_s, FULL_RUN_TRAJECTORIES),
            "rf_processing_s": _multiply(rf_per_tx_pair_s, FULL_RUN_TX_PAIRS),
            "total_without_visualization_s": _sum_present(
                [
                    _multiply(collection_per_accepted_s, FULL_RUN_TRAJECTORIES),
                    _multiply(rf_per_tx_pair_s, FULL_RUN_TX_PAIRS),
                    route_plan_estimate_s,
                    plan_selection_estimate_s,
                    finalize_estimate_s,
                ]
            ),
        },
        "bottleneck_analysis": _bottleneck_analysis(
            collection_per_attempt_s=collection_per_attempt_s,
            collection_per_accepted_s=collection_per_accepted_s,
            trajectory_pass_rate=_safe_div(trajectory_accepted_count, attempted_count),
            rf_per_tx_pair_s=rf_per_tx_pair_s,
            visualization_per_selected_s=_safe_div(visualization_total_s, visualization_count),
            rf_by_gpu=rf_by_gpu,
            rf_failure_summaries=rf_failure_summaries,
        ),
        "extrapolation_formula": (
            "collection_per_accepted_trajectory_s * target_trajectories + "
            "rf_per_tx_pair_s * target_tx_pairs + route_library_and_plan_generation_s + plan_selection_s + finalize_index_s"
        ),
        "confidence_notes": _confidence_notes(
            trajectory_accepted_count=trajectory_accepted_count,
            attempted_count=attempted_count,
            rf_total_pair_count=rf_total_pair_count,
            has_rf_metas=bool(rf_elapsed_values),
            has_visualization=visualization_manifest is not None,
        ),
    }
    save_json(root / "timing_report.json", report)
    indexes_dir = root / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "timing_report.json", indexes_dir / "timing_report.json")
    return {"timing_report": str(root / "timing_report.json"), "index_copy": str(indexes_dir / "timing_report.json")}


def _latest_elapsed(command_timings: list[dict], command: str) -> float | None:
    matching = [row for row in command_timings if row.get("command") == command and row.get("status") == "ok"]
    if not matching:
        return None
    return float(matching[-1].get("elapsed_s", 0.0))


def _sum_latest_elapsed(command_timings: list[dict], commands: list[str]) -> float | None:
    values = [_latest_elapsed(command_timings, command) for command in commands]
    present = [value for value in values if value is not None]
    if not present:
        return None
    return float(sum(present))


def _visualization_elapsed(manifest: dict | None, command_timings: list[dict]) -> float | None:
    if manifest is not None:
        summary = manifest.get("summary") if isinstance(manifest.get("summary"), dict) else {}
        value = summary.get("total_elapsed_s")
        if value is not None:
            return float(value)
    return _latest_elapsed(command_timings, "render-sample")


def _visualization_count(manifest: dict | None) -> int:
    if manifest is None:
        return 0
    rows = manifest.get("rows")
    return len(rows) if isinstance(rows, list) else 0


def _load_selection_manifest(config: dict, root: Path, collection_summary: dict | None) -> dict | None:
    candidates: list[Path] = []
    if collection_summary is not None and collection_summary.get("selection_manifest"):
        candidates.append(Path(str(collection_summary["selection_manifest"])))
    collection = config.get("collection", {}) if isinstance(config.get("collection"), dict) else {}
    configured = collection.get("selection_manifest")
    if configured:
        path = Path(str(configured))
        if path.is_absolute():
            candidates.append(path)
        elif len(path.parts) == 1:
            candidates.append(root / "plan_catalog" / path)
        else:
            candidates.append(root / path)
    candidates.extend(sorted((root / "plan_catalog").glob("validation_selection_*.json")))
    for path in candidates:
        if not path.exists():
            continue
        value = load_optional_json(path)
        if value is not None:
            return value
    return None


def _plan_selection_elapsed(selection_manifest: dict | None, command_timings: list[dict]) -> float | None:
    if selection_manifest is not None:
        summary = selection_manifest.get("summary") if isinstance(selection_manifest.get("summary"), dict) else {}
        value = summary.get("selection_elapsed_s")
        if value is not None:
            return float(value)
    return _latest_elapsed(command_timings, "select-validation-plans")


def _target_full_run_counts(config: dict, root: Path) -> tuple[int, int]:
    trajectory_target = int(config.get("profile", {}).get("target_accepted_episodes", FULL_RUN_TRAJECTORIES))
    if trajectory_target <= 0:
        trajectory_target = FULL_RUN_TRAJECTORIES
    tx_count = 0
    tx_catalog_path = root / "scene_static" / "tx_catalog.json"
    if tx_catalog_path.exists():
        try:
            tx_catalog = load_json(tx_catalog_path).get("tx_catalog", [])
            tx_count = len(tx_catalog) if isinstance(tx_catalog, list) else 0
        except Exception:  # noqa: BLE001
            tx_count = 0
    if tx_count <= 0:
        tx_count = int(config.get("tx", {}).get("selected_tx_per_episode", config.get("tx", {}).get("count", 3)))
    selected_tx = int(config.get("tx", {}).get("selected_tx_per_episode", 0)) if isinstance(config.get("tx"), dict) else 0
    if selected_tx > 0:
        tx_count = selected_tx
    return int(trajectory_target), int(trajectory_target * max(tx_count, 1))


def _collection_by_bucket(attempts: list[dict]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict]] = {}
    for attempt in attempts:
        bucket_meta = attempt.get("plan_bucket", {}) if isinstance(attempt.get("plan_bucket"), dict) else {}
        bucket = str(bucket_meta.get("vehicle_count", "unknown"))
        grouped.setdefault(bucket, []).append(attempt)
    result: dict[str, dict[str, Any]] = {}
    for bucket, rows in sorted(grouped.items()):
        elapsed = float(sum(float(row.get("elapsed_s", 0.0)) for row in rows))
        accepted = sum(1 for row in rows if str(row.get("status")) == "TRAJECTORY_ACCEPTED")
        failures = Counter(
            str(row.get("failure_code"))
            for row in rows
            if str(row.get("status")) != "TRAJECTORY_ACCEPTED" and row.get("failure_code")
        )
        statuses = Counter(str(row.get("status") or "unknown") for row in rows)
        result[bucket] = {
            "attempt_count": int(len(rows)),
            "accepted_count": int(accepted),
            "pass_rate": _safe_div(accepted, len(rows)),
            "elapsed_s": elapsed,
            "per_attempt_s": _safe_div(elapsed, len(rows)),
            "per_accepted_s": _safe_div(elapsed, accepted),
            "status_histogram": dict(statuses),
            "failure_code_histogram": dict(failures),
        }
    return result


def _collection_by_selection_cell(attempts: list[dict]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict]] = {}
    for attempt in attempts:
        bucket_meta = attempt.get("plan_bucket", {}) if isinstance(attempt.get("plan_bucket"), dict) else {}
        vehicle_count = int(bucket_meta.get("vehicle_count", 0))
        target_large = int(bucket_meta.get("target_large_vehicle_count", 0))
        cell = f"vehicles_{vehicle_count}_large_{target_large}"
        grouped.setdefault(cell, []).append(attempt)
    result: dict[str, dict[str, Any]] = {}
    for cell, rows in sorted(grouped.items()):
        elapsed = float(sum(float(row.get("elapsed_s", 0.0)) for row in rows))
        accepted = sum(1 for row in rows if str(row.get("status")) == "TRAJECTORY_ACCEPTED")
        failures = Counter(
            str(row.get("failure_code"))
            for row in rows
            if str(row.get("status")) != "TRAJECTORY_ACCEPTED" and row.get("failure_code")
        )
        statuses = Counter(str(row.get("status") or "unknown") for row in rows)
        result[cell] = {
            "attempt_count": int(len(rows)),
            "accepted_count": int(accepted),
            "pass_rate": _safe_div(accepted, len(rows)),
            "elapsed_s": elapsed,
            "per_attempt_s": _safe_div(elapsed, len(rows)),
            "per_accepted_s": _safe_div(elapsed, accepted),
            "status_histogram": dict(statuses),
            "failure_code_histogram": dict(failures),
        }
    return result


def _selection_matrix_validation(collection_summary: dict | None, selection_manifest: dict | None) -> dict[str, Any] | None:
    if collection_summary is None and selection_manifest is None:
        return None
    outcome = None
    if collection_summary is not None:
        outcome = collection_summary.get("accepted_selection_validation")
    manifest_summary = None
    if selection_manifest is not None and isinstance(selection_manifest.get("summary"), dict):
        manifest_summary = selection_manifest.get("summary")
    return {
        "manifest_summary": manifest_summary,
        "accepted_outcome": outcome,
        "selection_targets_reached": collection_summary.get("selection_targets_reached") if collection_summary else None,
        "selection_manifest": collection_summary.get("selection_manifest") if collection_summary else None,
    }


def _rf_by_gpu(rf_metas: list[dict], qa_reports: list[dict]) -> dict[str, dict[str, Any]]:
    tx_count_by_episode = {
        str(report.get("episode_id") or Path(str(report.get("_source_path", ""))).parent.name): len(report.get("tx_reports") or [])
        for report in qa_reports
    }
    grouped: dict[str, list[dict]] = {}
    for meta in rf_metas:
        if str(meta.get("status")) != "processed":
            continue
        gpu = str(meta.get("gpu_id") if meta.get("gpu_id") is not None else ("cpu" if not bool(meta.get("use_gpu")) else "unknown"))
        grouped.setdefault(gpu, []).append(meta)
    result: dict[str, dict[str, Any]] = {}
    for gpu, rows in sorted(grouped.items()):
        elapsed = float(sum(float(row.get("elapsed_s", 0.0)) for row in rows))
        tx_pairs = sum(int(tx_count_by_episode.get(str(row.get("episode_id")), 0)) for row in rows)
        result[gpu] = {
            "episode_count": int(len(rows)),
            "tx_pair_count": int(tx_pairs),
            "elapsed_s_sum": elapsed,
            "per_episode_s": _safe_div(elapsed, len(rows)),
            "per_tx_pair_s": _safe_div(elapsed, tx_pairs),
            "use_gpu": any(bool(row.get("use_gpu")) for row in rows),
            "mitsuba_variants": sorted({str(row.get("mitsuba_variant", "")) for row in rows if row.get("mitsuba_variant")}),
        }
    return result


def _rf_failed_by_gpu(rf_failure_summaries: list[dict]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict]] = {}
    source_paths: dict[str, set[str]] = {}
    for summary in rf_failure_summaries:
        summary_use_gpu = bool(summary.get("use_gpu"))
        source_path = str(summary.get("_source_path", ""))
        failures = summary.get("failures") if isinstance(summary.get("failures"), list) else []
        for failure in failures:
            if not isinstance(failure, dict):
                continue
            gpu = failure.get("gpu_id")
            key = str(gpu) if gpu is not None else ("cpu" if not summary_use_gpu else "unknown")
            grouped.setdefault(key, []).append(failure)
            if source_path:
                source_paths.setdefault(key, set()).add(source_path)

    result: dict[str, dict[str, Any]] = {}
    for gpu, rows in sorted(grouped.items()):
        elapsed = float(sum(float(row.get("elapsed_s", 0.0)) for row in rows))
        result[gpu] = {
            "failed_episode_count": int(len(rows)),
            "elapsed_s_sum": elapsed,
            "per_failed_episode_s": _safe_div(elapsed, len(rows)),
            "use_gpu": gpu not in {"cpu"},
            "worker_indices": sorted({int(row.get("worker_index")) for row in rows if row.get("worker_index") is not None}),
            "mitsuba_variants": sorted({str(row.get("mitsuba_variant", "")) for row in rows if row.get("mitsuba_variant")}),
            "source_files": sorted(source_paths.get(gpu, set())),
        }
    return result


def _rf_failure_overview(rf_failure_summaries: list[dict]) -> list[dict[str, Any]]:
    overview: list[dict[str, Any]] = []
    for summary in rf_failure_summaries:
        failures = summary.get("failures") if isinstance(summary.get("failures"), list) else []
        variants = sorted(
            {
                str(item.get("mitsuba_variant", ""))
                for item in failures
                if isinstance(item, dict) and item.get("mitsuba_variant")
            }
        )
        overview.append(
            {
                "source_path": str(summary.get("_source_path", "")),
                "schema": summary.get("schema"),
                "use_gpu": bool(summary.get("use_gpu")),
                "gpu_ids": [str(item) for item in (summary.get("gpu_ids") or [])],
                "worker_count": summary.get("worker_count"),
                "episode_candidates": summary.get("episode_candidates"),
                "queued_episode_count": summary.get("queued_episode_count"),
                "processed_episode_count": summary.get("processed_episode_count"),
                "failed_episode_count": summary.get("failed_episode_count"),
                "mitsuba_variants": variants,
            }
        )
    return overview


def _bottleneck_analysis(
    *,
    collection_per_attempt_s: float | None,
    collection_per_accepted_s: float | None,
    trajectory_pass_rate: float | None,
    rf_per_tx_pair_s: float | None,
    visualization_per_selected_s: float | None,
    rf_by_gpu: dict[str, dict[str, Any]],
    rf_failure_summaries: list[dict],
) -> dict[str, Any]:
    candidates = {
        "collection_per_accepted_trajectory": collection_per_accepted_s,
        "rf_per_tx_pair": rf_per_tx_pair_s,
        "visualization_per_selected_episode_tx": visualization_per_selected_s,
    }
    present = {key: value for key, value in candidates.items() if value is not None}
    dominant = max(present, key=lambda key: float(present[key])) if present else None
    rf_gpu_enabled = any(bool(row.get("use_gpu")) for row in rf_by_gpu.values())
    gpu_rf_failed = any(bool(row.get("use_gpu")) and int(row.get("failed_episode_count", 0)) > 0 for row in rf_failure_summaries)
    multi_attempt_note = None
    if collection_per_attempt_s is not None and trajectory_pass_rate is not None:
        if trajectory_pass_rate >= 0.7:
            multi_attempt_note = "CARLA retries appear acceptable at the observed pass rate."
        elif trajectory_pass_rate >= 0.4:
            multi_attempt_note = "CARLA retry overhead is material; keep attempt budgets visible in collection_summary.json."
        else:
            multi_attempt_note = "CARLA retry overhead is high; inspect failure histograms before scaling collection."
    return {
        "dominant_observed_unit_cost": dominant,
        "collection_attempt_cost_s": collection_per_attempt_s,
        "accepted_pass_rate": trajectory_pass_rate,
        "rf_gpu_enabled": bool(rf_gpu_enabled),
        "gpu_rf_failed": bool(gpu_rf_failed),
        "gpu_rf_failure_note": (
            "GPU RF was attempted but failed before accepted GPU RF outputs were produced; RF timing is from the successful fallback path."
            if gpu_rf_failed and not rf_gpu_enabled
            else None
        ),
        "rf_per_tx_pair_s": rf_per_tx_pair_s,
        "visualization_per_selected_episode_tx_s": visualization_per_selected_s,
        "multi_attempt_collection_assessment": multi_attempt_note,
    }


def _safe_div(numerator: Any, denominator: Any) -> float | None:
    if numerator is None or denominator in {None, 0}:
        return None
    return float(numerator) / float(denominator)


def _multiply(value: float | None, factor: int) -> float | None:
    if value is None:
        return None
    return float(value) * float(factor)


def _sum_present(values: list[float | None]) -> float | None:
    present = [float(value) for value in values if value is not None]
    if not present:
        return None
    return float(sum(present))


def _confidence_notes(
    *,
    trajectory_accepted_count: int,
    attempted_count: int,
    rf_total_pair_count: int,
    has_rf_metas: bool,
    has_visualization: bool,
) -> list[str]:
    notes = [
        "Collection estimate includes failed trajectory-QA attempts as overhead through elapsed attempt_meta timings.",
        "RF estimate uses only processed accepted trajectories and their TX reports; trajectory-QA-failed attempts are excluded from RF timing.",
    ]
    if trajectory_accepted_count < 50:
        notes.append("Accepted trajectory count is below the requested medium sample size, so extrapolation confidence is low.")
    if attempted_count == 0:
        notes.append("No CARLA attempt timings were found.")
    if rf_total_pair_count == 0:
        notes.append("No RF TX-pair reports were found.")
    if not has_rf_metas:
        notes.append("Per-episode RF timing metadata was unavailable; process-rf command timing was used when present.")
    if not has_visualization:
        notes.append("Visualization sample manifest was not found, so visualization timing is absent.")
    if trajectory_accepted_count >= 50 and rf_total_pair_count >= 150:
        notes.append(f"Medium sample reached {trajectory_accepted_count} accepted trajectories and {rf_total_pair_count} processed TX pairs.")
    return notes
