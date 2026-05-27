#!/usr/bin/env python3
"""Offline QA/failcase audit for an existing DynamicRadioMap dataset.

This script is intentionally read-only with respect to formal dataset artifacts:
it reads existing collection/QA/RF metadata and writes a diagnostics report
under ``<dataset-root>/diagnostics/qa_audit_<timestamp>/``.
"""

from __future__ import annotations

import argparse
import math
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

from schemas import (
    ACCEPTED_EPISODE_HEADERS,
    ACTOR_BEHAVIOR_HEADERS,
    AUDIT_SCHEMA,
    BUCKET_FAILURE_RATE_HEADERS,
    FAILURE_CASE_HEADERS,
    ROUTE_FAILURE_RATE_HEADERS,
    UNAVAILABLE,
    AuditThresholds,
    canonical_role,
    is_large_vehicle_type,
)
from utils import (
    JsonDict,
    as_semicolon_list,
    compact_json,
    first_present,
    histogram,
    iter_jsonl,
    load_json,
    make_unique_output_dir,
    max_value,
    mean,
    min_value,
    nested_get,
    percentile,
    safe_float,
    safe_int,
    sorted_counter,
    summarize_numeric,
    top_dict_items,
    write_csv,
    write_json,
)


CONTROLLED_GROUPS = {"primary_controlled", "auxiliary_controlled"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline QA/failcase audit for a DynamicRadioMap dataset.")
    parser.add_argument(
        "--dataset-root",
        type=Path,
        default=Path("datasets/DynamicRadioMap/Town10"),
        help="Dataset root to audit. Default: datasets/DynamicRadioMap/Town10",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=None,
        help="Optional explicit output directory. Default: <dataset-root>/diagnostics/qa_audit_<timestamp>",
    )
    parser.add_argument("--top-k", type=int, default=20, help="Number of sample cases to write.")
    parser.add_argument(
        "--timestamp",
        default=None,
        help="Optional timestamp suffix for deterministic output dir names, e.g. 20260503_143000.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    dataset_root = args.dataset_root.resolve()
    if not dataset_root.exists():
        raise SystemExit(f"Dataset root does not exist: {dataset_root}")

    timestamp = args.timestamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    output_dir = args.output_dir or (dataset_root / "diagnostics" / f"qa_audit_{timestamp}")
    output_dir = make_unique_output_dir(output_dir)
    output_dir.mkdir(parents=True, exist_ok=False)
    (output_dir / "tables").mkdir(parents=True, exist_ok=True)
    (output_dir / "samples").mkdir(parents=True, exist_ok=True)

    thresholds = AuditThresholds()
    report = build_audit(dataset_root, output_dir, thresholds=thresholds, top_k=max(1, int(args.top_k)))
    write_outputs(report, output_dir)
    print(f"qa_audit output: {output_dir}")
    return 0


def build_audit(dataset_root: Path, output_dir: Path, *, thresholds: AuditThresholds, top_k: int) -> JsonDict:
    missing_or_partial: list[JsonDict] = []
    collection_summary = load_json(dataset_root / "collection_summary.json", missing_or_partial)
    supervisor_status = load_json(dataset_root / "supervisor_status.json", missing_or_partial)

    rf_summary_files = [dataset_root / "rf_failure_summary.json"]
    rf_summary_files.extend(sorted(dataset_root.glob("rf_failure_summary_*.json")))
    rf_summaries = [row for path in rf_summary_files if (row := load_json(path, missing_or_partial)) is not None]
    rf_failed_by_episode = rf_failures_by_episode(rf_summaries)

    accepted_rows, actor_rows, accepted_context = scan_accepted_episodes(
        dataset_root,
        thresholds=thresholds,
        missing_or_partial=missing_or_partial,
        rf_failed_by_episode=rf_failed_by_episode,
    )
    failure_rows = scan_failure_cases(
        dataset_root,
        rf_summaries=rf_summaries,
        accepted_context=accepted_context,
        missing_or_partial=missing_or_partial,
    )

    route_rows, bucket_rows, cross_tabs = build_failure_rate_tables(accepted_rows, failure_rows)
    suspicious = sorted(
        (suspicious_case(row) for row in accepted_rows),
        key=lambda row: (-safe_float(row.get("suspicious_score"), 0.0), str(row.get("episode_id"))),
    )[:top_k]
    top_routes = sorted(
        route_rows,
        key=lambda row: (-safe_int(row.get("failed_count"), 0), -safe_float(row.get("failure_rate"), 0.0), str(row.get("route_id"))),
    )[:top_k]

    actor_group_metrics = build_actor_group_metrics(actor_rows)
    vehicle_size_metrics = build_actor_group_metrics(actor_rows, group_key="vehicle_size_group")
    vehicle_count_behavior = build_actor_group_metrics(actor_rows, group_key="vehicle_count_bucket")
    target_large_behavior = build_actor_group_metrics(actor_rows, group_key="target_large_bucket")

    summary = build_summary(
        dataset_root=dataset_root,
        output_dir=output_dir,
        thresholds=thresholds,
        collection_summary=collection_summary,
        supervisor_status=supervisor_status,
        rf_summaries=rf_summaries,
        accepted_rows=accepted_rows,
        actor_rows=actor_rows,
        failure_rows=failure_rows,
        route_rows=route_rows,
        bucket_rows=bucket_rows,
        cross_tabs=cross_tabs,
        suspicious=suspicious,
        actor_group_metrics=actor_group_metrics,
        vehicle_size_metrics=vehicle_size_metrics,
        vehicle_count_behavior=vehicle_count_behavior,
        target_large_behavior=target_large_behavior,
        missing_or_partial=missing_or_partial,
    )

    return {
        "summary": summary,
        "summary_md": render_summary_md(summary),
        "accepted_rows": accepted_rows,
        "actor_rows": actor_rows,
        "failure_rows": failure_rows,
        "route_rows": route_rows,
        "bucket_rows": bucket_rows,
        "suspicious": suspicious,
        "top_routes": top_routes,
        "missing_or_partial": missing_or_partial,
    }


def write_outputs(report: JsonDict, output_dir: Path) -> None:
    tables_dir = output_dir / "tables"
    samples_dir = output_dir / "samples"
    write_json(output_dir / "summary.json", report["summary"])
    (output_dir / "summary.md").write_text(report["summary_md"], encoding="utf-8")
    write_csv(tables_dir / "failure_cases.csv", report["failure_rows"], FAILURE_CASE_HEADERS)
    write_csv(tables_dir / "accepted_episode_metrics.csv", report["accepted_rows"], ACCEPTED_EPISODE_HEADERS)
    write_csv(tables_dir / "actor_behavior_metrics.csv", report["actor_rows"], ACTOR_BEHAVIOR_HEADERS)
    write_csv(tables_dir / "route_failure_rates.csv", report["route_rows"], ROUTE_FAILURE_RATE_HEADERS)
    write_csv(tables_dir / "bucket_failure_rates.csv", report["bucket_rows"], BUCKET_FAILURE_RATE_HEADERS)
    write_json(samples_dir / "suspicious_accepted_cases.json", report["suspicious"])
    write_json(samples_dir / "top_failure_routes.json", report["top_routes"])
    write_json(samples_dir / "missing_or_partial_records.json", report["missing_or_partial"])


def rf_failures_by_episode(rf_summaries: list[JsonDict]) -> dict[str, JsonDict]:
    by_episode: dict[str, JsonDict] = {}
    for summary in rf_summaries:
        for item in summary.get("failures", []) if isinstance(summary.get("failures"), list) else []:
            if not isinstance(item, dict):
                continue
            episode_id = item.get("episode_id")
            if episode_id is not None:
                by_episode[str(episode_id)] = item
    return by_episode


def scan_accepted_episodes(
    dataset_root: Path,
    *,
    thresholds: AuditThresholds,
    missing_or_partial: list[JsonDict],
    rf_failed_by_episode: dict[str, JsonDict],
) -> tuple[list[JsonDict], list[JsonDict], dict[str, JsonDict]]:
    episode_root = dataset_root / "episodes"
    rows: list[JsonDict] = []
    actor_rows: list[JsonDict] = []
    context: dict[str, JsonDict] = {}
    for episode_dir in sorted(episode_root.glob("episode_*")):
        if not episode_dir.is_dir():
            continue
        episode_id = episode_dir.name
        local_warnings: list[JsonDict] = []
        episode_meta = load_json(episode_dir / "episode_meta.json", local_warnings, required=True)
        plan = load_json(episode_dir / "plan.json", local_warnings, required=True)
        trajectory_qa = load_json(episode_dir / "trajectory_qa.json", local_warnings, required=True)
        qa_report = load_json(episode_dir / "qa_report.json", local_warnings, required=True)
        scene_meta = load_json(episode_dir / "scene_meta.json", local_warnings, required=True)
        validation_report = load_json(episode_dir / "validation_report.json", local_warnings, required=True)
        rf_meta = load_json(episode_dir / "rf_process_meta.json", local_warnings, required=False)

        missing_files = [warning["path"] for warning in local_warnings if warning.get("issue") == "missing_file"]
        partial_warnings: list[str] = []
        if rf_meta is None:
            if episode_id in rf_failed_by_episode:
                partial_warnings.append("rf_failed_summary_record_without_episode_rf_process_meta")
            else:
                partial_warnings.append("missing_rf_process_meta")
        if not (episode_dir / "rss_dynamic_dbm.npz").exists():
            partial_warnings.append("missing_rss_dynamic_dbm_npz")

        scene_id = first_present(
            nested_get(plan, ("scene_id",)),
            nested_get(scene_meta, ("scene_id",)),
            nested_get(qa_report, ("scene_signature", "scene_id")),
            nested_get(episode_meta, ("scene_id",)),
            nested_get(plan, ("expected_metrics", "scene_id")),
        )
        plan_id = first_present(nested_get(plan, ("plan_id",)), nested_get(trajectory_qa, ("plan_id",)))
        target_tx_id = first_present(nested_get(plan, ("target_tx_id",)), nested_get(trajectory_qa, ("target_tx_id",)))
        route_ids = route_ids_from_plan(plan)
        vehicle_count = plan_value(plan, None, "vehicle_count")
        target_large = plan_value(plan, None, "target_large_vehicle_count")
        role_counts = vehicle_role_counts(episode_meta, trajectory_qa, qa_report, validation_report)

        fps = safe_float(
            first_present(
                nested_get(episode_meta, ("fps",)),
                nested_get(scene_meta, ("fps",)),
                nested_get(plan, ("fps",)),
                nested_get(trajectory_qa, ("fps",)),
            ),
            10.0,
        )
        expected_frames = safe_int(
            first_present(
                nested_get(trajectory_qa, ("expected_frames",)),
                nested_get(episode_meta, ("frame_count",)),
                nested_get(scene_meta, ("frames_requested",)),
                nested_get(qa_report, ("summary", "frame_count")),
            )
        )

        behavior_summary, per_actor_rows = actor_behavior_from_episode(
            episode_dir=episode_dir,
            episode_id=episode_id,
            scene_id=str(scene_id) if scene_id is not None else "",
            plan_id=str(plan_id) if plan_id is not None else "",
            vehicle_count=vehicle_count,
            target_large=target_large,
            fps=fps,
            expected_frames=expected_frames,
            thresholds=thresholds,
            warnings=local_warnings,
        )
        actor_rows.extend(per_actor_rows)

        actor_count = safe_int(behavior_summary.get("actor_count"))
        role_actor_counts = Counter(str(row.get("role_group") or "unknown") for row in per_actor_rows)
        large_actor_count = sum(1 for row in per_actor_rows if bool(row.get("is_large_vehicle")))
        actual_large_vehicle_count = first_present(
            nested_get(trajectory_qa, ("vehicle_mix", "large_vehicle_count")),
            nested_get(qa_report, ("scene_qa", "vehicle_mix", "large_vehicle_count")),
            large_actor_count if per_actor_rows else None,
        )

        qa_warnings = []
        qa_warnings.extend(as_list(nested_get(qa_report, ("scene_qa", "warnings"))))
        qa_warnings.extend(as_list(nested_get(qa_report, ("warnings",))))
        qa_blockers = []
        qa_blockers.extend(as_list(nested_get(qa_report, ("scene_qa", "blockers"))))
        qa_blockers.extend(as_list(nested_get(qa_report, ("blockers",))))
        trajectory_warnings = as_list(nested_get(trajectory_qa, ("warnings",)))
        trajectory_blockers = as_list(nested_get(trajectory_qa, ("blockers",)))

        rf_failure = rf_failed_by_episode.get(episode_id)
        rf_status = first_present(nested_get(rf_meta, ("status",)), nested_get(rf_failure, ("status",)))
        rf_failure_code = first_present(nested_get(rf_meta, ("failure_code",)), nested_get(rf_failure, ("failure_code",)))
        rf_error = first_present(nested_get(rf_meta, ("error",)), nested_get(rf_failure, ("error",)))

        row = {
            "episode_id": episode_id,
            "episode_dir": str(episode_dir),
            "scene_id": scene_id,
            "plan_id": plan_id,
            "target_tx_id": target_tx_id,
            "route_ids": route_ids,
            "vehicle_count": vehicle_count,
            "target_large_vehicle_count": target_large,
            "actual_large_vehicle_count": actual_large_vehicle_count,
            "planned_required_controlled_count": role_counts.get("planned_required_controlled_count"),
            "planned_optional_controlled_count": role_counts.get("planned_optional_controlled_count"),
            "requested_background_count": role_counts.get("requested_background_count"),
            "actual_required_controlled_count": role_counts.get("actual_required_controlled_count"),
            "actual_optional_controlled_count": role_counts.get("actual_optional_controlled_count"),
            "actual_background_count": role_counts.get("actual_background_count"),
            "actual_total_vehicle_count": role_counts.get("actual_total_vehicle_count"),
            "actor_count": actor_count,
            "primary_controlled_actor_count": int(role_actor_counts.get("primary_controlled", 0)),
            "auxiliary_controlled_actor_count": int(role_actor_counts.get("auxiliary_controlled", 0)),
            "background_tm_actor_count": int(role_actor_counts.get("background_tm", 0)),
            "large_actor_count": int(large_actor_count),
            "trajectory_qc_pass": nested_get(trajectory_qa, ("trajectory_qc_pass",)),
            "trajectory_failure_code": nested_get(trajectory_qa, ("failure_code",)),
            "trajectory_warnings": trajectory_warnings,
            "trajectory_blockers": trajectory_blockers,
            "scene_qc_pass": nested_get(qa_report, ("scene_qc_pass",)),
            "qa_warnings": qa_warnings,
            "qa_blockers": qa_blockers,
            "tx_count": first_present(nested_get(qa_report, ("summary", "tx_count")), len(as_list(nested_get(qa_report, ("tx_reports",))))),
            "accepted_pair_count": nested_get(qa_report, ("summary", "accepted_pair_count")),
            "accepted_tx_ids": nested_get(qa_report, ("accepted_tx_ids",)),
            "validation_valid_clip": nested_get(validation_report, ("valid_clip",)),
            "validation_passed_target_count": nested_get(validation_report, ("passed_target_count",)),
            "rf_status": rf_status or "",
            "rf_failure_code": rf_failure_code or "",
            "rf_error": truncate_text(rf_error, 300),
            "missing_files": missing_files,
            "partial_warnings": partial_warnings,
        }
        row.update(behavior_summary)
        score, reasons = score_suspicious(row, thresholds)
        row["suspicious_score"] = score
        row["suspicious_reasons"] = reasons
        rows.append(row)

        context[episode_id] = {
            "episode_id": episode_id,
            "scene_id": scene_id,
            "plan_id": plan_id,
            "target_tx_id": target_tx_id,
            "route_ids": route_ids,
            "vehicle_count": vehicle_count,
            "target_large_vehicle_count": target_large,
            "episode_dir": str(episode_dir),
        }

        if missing_files or partial_warnings or any(w.get("issue") != "missing_file" for w in local_warnings):
            missing_or_partial.append(
                {
                    "scope": "accepted_episode",
                    "episode_id": episode_id,
                    "path": str(episode_dir),
                    "missing_files": missing_files,
                    "partial_warnings": partial_warnings,
                    "read_warnings": local_warnings,
                }
            )
    return rows, actor_rows, context


def actor_behavior_from_episode(
    *,
    episode_dir: Path,
    episode_id: str,
    scene_id: str,
    plan_id: str,
    vehicle_count: Any,
    target_large: Any,
    fps: float | None,
    expected_frames: int | None,
    thresholds: AuditThresholds,
    warnings: list[JsonDict],
) -> tuple[JsonDict, list[JsonDict]]:
    actor_states_path = episode_dir / "frames" / "actor_states.jsonl"
    if not actor_states_path.exists():
        unavailable = behavior_unavailable_fields()
        unavailable["actor_count"] = UNAVAILABLE
        unavailable["unavailable_metrics"] = sorted(unavailable_metric_names())
        return unavailable, []

    tracks: dict[str, list[JsonDict]] = defaultdict(list)
    rows_by_frame: dict[int, list[JsonDict]] = defaultdict(list)
    frame_indices: set[int] = set()
    for frame in iter_jsonl(actor_states_path, warnings):
        frame_index = safe_int(frame.get("frame_index"))
        timestamp = safe_float(frame.get("timestamp"))
        if frame_index is None:
            continue
        frame_indices.add(frame_index)
        actors = frame.get("actors", [])
        if not isinstance(actors, list):
            warnings.append({"path": str(actor_states_path), "frame_index": frame_index, "issue": "actors_not_list"})
            continue
        for actor in actors:
            if not isinstance(actor, dict):
                continue
            normalized = normalize_actor_state(actor, frame_index=frame_index, timestamp=timestamp)
            actor_id = str(normalized.get("actor_id"))
            tracks[actor_id].append(normalized)
            rows_by_frame[frame_index].append(normalized)

    if not tracks:
        unavailable = behavior_unavailable_fields()
        unavailable["actor_count"] = 0
        unavailable["frame_count"] = len(frame_indices)
        unavailable["unavailable_metrics"] = sorted(unavailable_metric_names())
        return unavailable, []

    frame_dt = frame_delta_s(fps)
    pair_metrics = pair_distance_metrics(rows_by_frame)
    all_stop_s = longest_all_stop_duration(rows_by_frame, thresholds.stationary_speed_mps, frame_dt)
    per_actor: list[JsonDict] = []
    all_speeds: list[float] = []
    all_abs_accels: list[float] = []
    all_abs_jerks: list[float] = []
    harsh_brake_count = 0
    harsh_accel_count = 0
    low_progress_count = 0
    stationary_max = 0.0
    displacements: list[float] = []
    unavailable_metrics: set[str] = set()

    for actor_id, track in sorted(tracks.items(), key=lambda item: str(item[0])):
        metrics = track_metrics(track, frame_dt=frame_dt, expected_frames=expected_frames, thresholds=thresholds)
        metrics["min_inter_vehicle_gap_m"] = pair_metrics["per_actor_gap"].get(actor_id, UNAVAILABLE)
        metrics["min_center_distance_m"] = pair_metrics["per_actor_center"].get(actor_id, UNAVAILABLE)
        metrics["episode_id"] = episode_id
        metrics["scene_id"] = scene_id
        metrics["plan_id"] = plan_id
        metrics["vehicle_count"] = vehicle_count
        metrics["target_large_vehicle_count"] = target_large
        metrics["vehicle_count_bucket"] = bucket_value(vehicle_count, prefix="vehicles")
        metrics["target_large_bucket"] = bucket_value(target_large, prefix="large")
        metrics["vehicle_size_group"] = "large_vehicle" if bool(metrics.get("is_large_vehicle")) else "normal_vehicle"
        per_actor.append(metrics)

        all_speeds.extend(metrics.pop("_speeds", []))
        all_abs_accels.extend(metrics.pop("_abs_accels", []))
        all_abs_jerks.extend(metrics.pop("_abs_jerks", []))
        harsh_brake_count += safe_int(metrics.get("harsh_brake_count"), 0) or 0
        harsh_accel_count += safe_int(metrics.get("harsh_accel_count"), 0) or 0
        if bool(metrics.get("low_progress_warning")):
            low_progress_count += 1
        stationary = safe_float(metrics.get("stationary_duration_s"), 0.0) or 0.0
        stationary_max = max(stationary_max, stationary)
        displacement = safe_float(metrics.get("displacement_m"))
        if displacement is not None:
            displacements.append(displacement)

    for name, value in {
        "speed_mean_mps": mean(all_speeds),
        "speed_p95_mps": percentile(all_speeds, 95),
        "speed_max_mps": max_value(all_speeds),
        "accel_abs_mean_mps2": mean(all_abs_accels),
        "accel_abs_p95_mps2": percentile(all_abs_accels, 95),
        "accel_abs_max_mps2": max_value(all_abs_accels),
        "jerk_abs_p95_mps3": percentile(all_abs_jerks, 95),
        "jerk_abs_max_mps3": max_value(all_abs_jerks),
        "min_inter_vehicle_gap_m": pair_metrics["global_gap"],
        "min_center_distance_m": pair_metrics["global_center"],
    }.items():
        if value == UNAVAILABLE:
            unavailable_metrics.add(name)

    summary = {
        "actor_count": int(len(tracks)),
        "frame_count": int(len(frame_indices)),
        "duration_s": float(len(frame_indices) * frame_dt) if frame_indices and frame_dt > 0 else UNAVAILABLE,
        "speed_mean_mps": mean(all_speeds),
        "speed_p95_mps": percentile(all_speeds, 95),
        "speed_max_mps": max_value(all_speeds),
        "accel_abs_mean_mps2": mean(all_abs_accels),
        "accel_abs_p95_mps2": percentile(all_abs_accels, 95),
        "accel_abs_max_mps2": max_value(all_abs_accels),
        "jerk_abs_p95_mps3": percentile(all_abs_jerks, 95),
        "jerk_abs_max_mps3": max_value(all_abs_jerks),
        "stationary_duration_max_s": float(stationary_max),
        "all_stop_duration_s": all_stop_s,
        "harsh_brake_count": int(harsh_brake_count),
        "harsh_accel_count": int(harsh_accel_count),
        "low_progress_actor_count": int(low_progress_count),
        "min_inter_vehicle_gap_m": pair_metrics["global_gap"],
        "min_center_distance_m": pair_metrics["global_center"],
        "route_progress_proxy_mean_m": mean(displacements),
        "route_progress_proxy_min_m": min_value(displacements),
        "unavailable_metrics": sorted(unavailable_metrics),
    }
    return summary, per_actor


def normalize_actor_state(actor: JsonDict, *, frame_index: int, timestamp: float | None) -> JsonDict:
    traffic_plan = actor.get("traffic_plan") if isinstance(actor.get("traffic_plan"), dict) else {}
    transform = actor.get("transform") if isinstance(actor.get("transform"), dict) else {}
    velocity = actor.get("velocity") if isinstance(actor.get("velocity"), dict) else {}
    bbox = actor.get("bbox_extent") if isinstance(actor.get("bbox_extent"), dict) else {}
    role = canonical_role(first_present(actor.get("role"), traffic_plan.get("role")))
    vehicle_type = str(first_present(actor.get("vehicle_type"), traffic_plan.get("actual_vehicle_type"), traffic_plan.get("requested_vehicle_type"), ""))
    x = safe_float(transform.get("x"))
    y = safe_float(transform.get("y"))
    speed = vector_norm(velocity.get("x"), velocity.get("y"), velocity.get("z"))
    extent_x = safe_float(bbox.get("x"), 0.0) or 0.0
    extent_y = safe_float(bbox.get("y"), 0.0) or 0.0
    return {
        "actor_id": actor.get("actor_id"),
        "frame_index": frame_index,
        "timestamp": timestamp,
        "role": role,
        "role_group": role,
        "control_mode": first_present(traffic_plan.get("control_mode"), actor.get("control_mode"), ""),
        "vehicle_type": vehicle_type,
        "is_large_vehicle": is_large_vehicle_type(vehicle_type),
        "route_id": first_present(actor.get("route_id"), traffic_plan.get("route_id")),
        "x": x,
        "y": y,
        "speed": speed,
        "radius": math.sqrt(extent_x * extent_x + extent_y * extent_y),
    }


def track_metrics(
    track: list[JsonDict],
    *,
    frame_dt: float,
    expected_frames: int | None,
    thresholds: AuditThresholds,
) -> JsonDict:
    rows = sorted(track, key=lambda row: (safe_int(row.get("frame_index"), 0) or 0, safe_float(row.get("timestamp"), 0.0) or 0.0))
    first = rows[0]
    speeds = [value for value in (safe_float(row.get("speed")) for row in rows) if value is not None]
    xy_rows = [(safe_float(row.get("x")), safe_float(row.get("y"))) for row in rows]
    valid_xy = [(x, y) for x, y in xy_rows if x is not None and y is not None]
    dts = transition_dts(rows, frame_dt)

    signed_accels: list[float] = []
    for idx in range(1, len(rows)):
        prev_speed = safe_float(rows[idx - 1].get("speed"))
        speed = safe_float(rows[idx].get("speed"))
        if prev_speed is None or speed is None:
            continue
        dt = dts[idx - 1] if idx - 1 < len(dts) else frame_dt
        if dt > 0:
            signed_accels.append((speed - prev_speed) / dt)
    abs_accels = [abs(value) for value in signed_accels]

    signed_jerks: list[float] = []
    for idx in range(1, len(signed_accels)):
        dt = dts[min(idx, len(dts) - 1)] if dts else frame_dt
        if dt > 0:
            signed_jerks.append((signed_accels[idx] - signed_accels[idx - 1]) / dt)
    abs_jerks = [abs(value) for value in signed_jerks]

    displacement = UNAVAILABLE
    path_length = UNAVAILABLE
    if len(valid_xy) >= 2:
        displacement = distance_xy(valid_xy[0], valid_xy[-1])
        path_length_value = 0.0
        prev: tuple[float, float] | None = None
        for xy in xy_rows:
            if xy[0] is None or xy[1] is None:
                continue
            current = (xy[0], xy[1])
            if prev is not None:
                path_length_value += distance_xy(prev, current)
            prev = current
        path_length = path_length_value

    stationary_duration = longest_stationary_duration(rows, thresholds.stationary_speed_mps, frame_dt)
    duration_s = float(len(rows) * frame_dt) if frame_dt > 0 else UNAVAILABLE
    stationary_fraction = (
        float(stationary_duration / duration_s)
        if isinstance(duration_s, float) and duration_s > 0 and stationary_duration != UNAVAILABLE
        else UNAVAILABLE
    )
    harsh_brake_count = sum(1 for value in signed_accels if value <= thresholds.harsh_brake_mps2)
    harsh_accel_count = sum(1 for value in signed_accels if value >= thresholds.harsh_accel_mps2)
    low_progress_warning = bool(safe_float(displacement, 0.0) is not None and (safe_float(displacement, 0.0) or 0.0) < thresholds.low_progress_m)

    warnings: list[str] = []
    if expected_frames is not None and len(rows) < expected_frames:
        warnings.append(f"short_track:{len(rows)}/{expected_frames}")
    if low_progress_warning:
        warnings.append("low_progress_proxy")
    if stationary_duration != UNAVAILABLE and safe_float(stationary_duration, 0.0) >= thresholds.long_stationary_s:
        warnings.append("long_stationary")

    return {
        "actor_id": first.get("actor_id"),
        "role": first.get("role"),
        "role_group": first.get("role_group"),
        "control_mode": first.get("control_mode"),
        "vehicle_type": first.get("vehicle_type"),
        "is_large_vehicle": bool(first.get("is_large_vehicle")),
        "route_id": first.get("route_id"),
        "frame_count": int(len(rows)),
        "duration_s": duration_s,
        "speed_mean_mps": mean(speeds),
        "speed_p95_mps": percentile(speeds, 95),
        "speed_max_mps": max_value(speeds),
        "accel_signed_min_mps2": min_value(signed_accels),
        "accel_signed_max_mps2": max_value(signed_accels),
        "accel_abs_mean_mps2": mean(abs_accels),
        "accel_abs_p95_mps2": percentile(abs_accels, 95),
        "accel_abs_max_mps2": max_value(abs_accels),
        "jerk_abs_p95_mps3": percentile(abs_jerks, 95),
        "jerk_abs_max_mps3": max_value(abs_jerks),
        "harsh_brake_count": int(harsh_brake_count),
        "harsh_accel_count": int(harsh_accel_count),
        "stationary_duration_s": stationary_duration,
        "stationary_fraction": stationary_fraction,
        "displacement_m": displacement,
        "path_length_m": path_length,
        "route_progress_proxy_m": displacement,
        "low_progress_warning": low_progress_warning,
        "has_complete_track": bool(expected_frames is None or len(rows) >= expected_frames),
        "warnings": warnings,
        "_speeds": speeds,
        "_abs_accels": abs_accels,
        "_abs_jerks": abs_jerks,
    }


def pair_distance_metrics(rows_by_frame: dict[int, list[JsonDict]]) -> JsonDict:
    global_gap: float | None = None
    global_center: float | None = None
    per_actor_gap: dict[str, float] = {}
    per_actor_center: dict[str, float] = {}
    for frame_index in sorted(rows_by_frame):
        actors = rows_by_frame[frame_index]
        for idx, actor_a in enumerate(actors):
            ax = safe_float(actor_a.get("x"))
            ay = safe_float(actor_a.get("y"))
            if ax is None or ay is None:
                continue
            for actor_b in actors[idx + 1 :]:
                bx = safe_float(actor_b.get("x"))
                by = safe_float(actor_b.get("y"))
                if bx is None or by is None:
                    continue
                center = math.hypot(ax - bx, ay - by)
                gap = center - (safe_float(actor_a.get("radius"), 0.0) or 0.0) - (safe_float(actor_b.get("radius"), 0.0) or 0.0)
                global_center = center if global_center is None else min(global_center, center)
                global_gap = gap if global_gap is None else min(global_gap, gap)
                for actor in (actor_a, actor_b):
                    actor_id = str(actor.get("actor_id"))
                    per_actor_center[actor_id] = center if actor_id not in per_actor_center else min(per_actor_center[actor_id], center)
                    per_actor_gap[actor_id] = gap if actor_id not in per_actor_gap else min(per_actor_gap[actor_id], gap)
    return {
        "global_gap": float(global_gap) if global_gap is not None else UNAVAILABLE,
        "global_center": float(global_center) if global_center is not None else UNAVAILABLE,
        "per_actor_gap": per_actor_gap,
        "per_actor_center": per_actor_center,
    }


def longest_all_stop_duration(rows_by_frame: dict[int, list[JsonDict]], speed_threshold: float, frame_dt: float) -> float | str:
    if not rows_by_frame:
        return UNAVAILABLE
    longest = 0.0
    current = 0.0
    for frame_index in sorted(rows_by_frame):
        actors = rows_by_frame[frame_index]
        speeds = [safe_float(actor.get("speed")) for actor in actors]
        all_stop = bool(speeds) and all(speed is not None and speed < speed_threshold for speed in speeds)
        if all_stop:
            current += frame_dt
            longest = max(longest, current)
        else:
            current = 0.0
    return float(longest)


def longest_stationary_duration(rows: list[JsonDict], speed_threshold: float, frame_dt: float) -> float | str:
    if not rows:
        return UNAVAILABLE
    dts = transition_dts(rows, frame_dt)
    longest = 0.0
    current = 0.0
    for idx, row in enumerate(rows):
        speed = safe_float(row.get("speed"))
        dt = dts[idx] if idx < len(dts) else frame_dt
        if speed is not None and speed < speed_threshold:
            current += dt
            longest = max(longest, current)
        else:
            current = 0.0
    return float(longest)


def transition_dts(rows: list[JsonDict], frame_dt: float) -> list[float]:
    dts: list[float] = []
    for prev, row in zip(rows[:-1], rows[1:]):
        prev_ts = safe_float(prev.get("timestamp"))
        ts = safe_float(row.get("timestamp"))
        if prev_ts is not None and ts is not None and ts > prev_ts:
            dts.append(float(ts - prev_ts))
        else:
            dts.append(frame_dt)
    return dts


def vector_norm(x: Any, y: Any, z: Any = 0.0) -> float | None:
    xx = safe_float(x)
    yy = safe_float(y)
    zz = safe_float(z, 0.0)
    if xx is None or yy is None or zz is None:
        return None
    return math.sqrt(xx * xx + yy * yy + zz * zz)


def distance_xy(a: tuple[float, float], b: tuple[float, float]) -> float:
    return float(math.hypot(a[0] - b[0], a[1] - b[1]))


def frame_delta_s(fps: float | None) -> float:
    if fps is None or fps <= 0:
        return 0.1
    return 1.0 / float(fps)


def behavior_unavailable_fields() -> JsonDict:
    return {name: UNAVAILABLE for name in unavailable_metric_names()}


def unavailable_metric_names() -> set[str]:
    return {
        "frame_count",
        "duration_s",
        "speed_mean_mps",
        "speed_p95_mps",
        "speed_max_mps",
        "accel_abs_mean_mps2",
        "accel_abs_p95_mps2",
        "accel_abs_max_mps2",
        "jerk_abs_p95_mps3",
        "jerk_abs_max_mps3",
        "stationary_duration_max_s",
        "all_stop_duration_s",
        "harsh_brake_count",
        "harsh_accel_count",
        "low_progress_actor_count",
        "min_inter_vehicle_gap_m",
        "min_center_distance_m",
        "route_progress_proxy_mean_m",
        "route_progress_proxy_min_m",
    }


def scan_failure_cases(
    dataset_root: Path,
    *,
    rf_summaries: list[JsonDict],
    accepted_context: dict[str, JsonDict],
    missing_or_partial: list[JsonDict],
) -> list[JsonDict]:
    rows: list[JsonDict] = []
    seen_attempt_ids: set[str] = set()
    archived_dirs = {path.name: path for path in sorted((dataset_root / "failed_attempts").glob("attempt_*")) if path.is_dir()}

    for attempt_dir in sorted((dataset_root / "attempts").glob("attempt_*")):
        if not attempt_dir.is_dir():
            continue
        local_warnings: list[JsonDict] = []
        attempt_meta = load_json(attempt_dir / "attempt_meta.json", local_warnings)
        trajectory_qa = load_json(attempt_dir / "trajectory_qa.json", local_warnings)
        failure = load_json(attempt_dir / "failure.json", local_warnings)
        status = first_present(nested_get(attempt_meta, ("status",)), nested_get(failure, ("status",)))
        trajectory_pass = nested_get(trajectory_qa, ("trajectory_qc_pass",))
        if str(status) == "TRAJECTORY_ACCEPTED" and trajectory_pass is not False:
            continue
        plan = load_json(attempt_dir / "plan.json", local_warnings)
        archived_dir = archived_dirs.get(attempt_dir.name)
        archived_failure = load_json(archived_dir / "failure.json", local_warnings) if archived_dir is not None else None
        row = build_attempt_failure_row(
            attempt_dir=attempt_dir,
            attempt_meta=attempt_meta,
            trajectory_qa=trajectory_qa,
            failure=first_present(failure, archived_failure),
            plan=plan,
            archived_dir=archived_dir,
            source_kind="attempt_failure",
            warnings=local_warnings,
        )
        rows.append(row)
        seen_attempt_ids.add(attempt_dir.name)
        if local_warnings:
            missing_or_partial.append(
                {"scope": "failure_attempt", "attempt_id": attempt_dir.name, "path": str(attempt_dir), "read_warnings": local_warnings}
            )

    for attempt_id, failed_dir in archived_dirs.items():
        if attempt_id in seen_attempt_ids:
            continue
        local_warnings = []
        attempt_meta = load_json(failed_dir / "attempt_meta.json", local_warnings)
        trajectory_qa = load_json(failed_dir / "trajectory_qa.json", local_warnings)
        failure = load_json(failed_dir / "failure.json", local_warnings)
        plan = load_json(failed_dir / "plan.json", local_warnings)
        rows.append(
            build_attempt_failure_row(
                attempt_dir=failed_dir,
                attempt_meta=attempt_meta,
                trajectory_qa=trajectory_qa,
                failure=failure,
                plan=plan,
                archived_dir=failed_dir,
                source_kind="archived_failure_only",
                warnings=local_warnings,
            )
        )
        if local_warnings:
            missing_or_partial.append(
                {"scope": "archived_failure_attempt", "attempt_id": attempt_id, "path": str(failed_dir), "read_warnings": local_warnings}
            )

    rows.extend(scan_rf_failure_rows(dataset_root, rf_summaries, accepted_context, missing_or_partial))
    rows.extend(scan_per_tx_failure_rows(dataset_root, accepted_context, missing_or_partial))

    for index, row in enumerate(rows):
        row.setdefault("record_id", f"failure_{index:06d}")
    return rows


def build_attempt_failure_row(
    *,
    attempt_dir: Path,
    attempt_meta: JsonDict | None,
    trajectory_qa: JsonDict | None,
    failure: JsonDict | None,
    plan: JsonDict | None,
    archived_dir: Path | None,
    source_kind: str,
    warnings: list[JsonDict],
) -> JsonDict:
    attempt_id = attempt_dir.name
    status = first_present(nested_get(attempt_meta, ("status",)), nested_get(failure, ("status",)))
    failure_code = first_present(
        nested_get(attempt_meta, ("failure_code",)),
        nested_get(trajectory_qa, ("failure_code",)),
        nested_get(failure, ("failure_code",)),
        status,
    )
    failure_stage = classify_failure_stage(status=status, failure_code=failure_code, trajectory_qa=trajectory_qa)
    category = classify_failure_category(failure_stage, failure_code)
    return {
        "record_id": attempt_id,
        "source_kind": source_kind,
        "failure_stage": failure_stage,
        "failure_code": failure_code,
        "status": status,
        "failure_category": category,
        "route_ids": route_ids_from_plan(plan),
        "plan_id": first_present(nested_get(attempt_meta, ("plan_id",)), nested_get(plan, ("plan_id",)), nested_get(trajectory_qa, ("plan_id",))),
        "target_tx_id": first_present(
            nested_get(attempt_meta, ("target_tx_id",)), nested_get(plan, ("target_tx_id",)), nested_get(trajectory_qa, ("target_tx_id",))
        ),
        "vehicle_count": plan_value(plan, attempt_meta, "vehicle_count"),
        "target_large_vehicle_count": plan_value(plan, attempt_meta, "target_large_vehicle_count"),
        "attempt_id": attempt_id,
        "episode_id": "",
        "log_paths": existing_log_paths(attempt_dir, archived_dir),
        "source_path": str(attempt_dir),
        "archived_source_path": str(archived_dir) if archived_dir is not None else "",
        "is_runtime_environment_failure": category == "runtime_environment",
        "is_plan_spawn_failure": category == "plan_spawn",
        "is_trajectory_qa_failure": category == "trajectory_qa",
        "is_rf_failure": False,
        "warnings": as_list(nested_get(trajectory_qa, ("warnings",))) + [compact_json(w) for w in warnings],
    }


def scan_rf_failure_rows(
    dataset_root: Path,
    rf_summaries: list[JsonDict],
    accepted_context: dict[str, JsonDict],
    missing_or_partial: list[JsonDict],
) -> list[JsonDict]:
    rows: list[JsonDict] = []
    seen: set[str] = set()
    for summary in rf_summaries:
        source_path = summary.get("source_path") or ""
        for item in summary.get("failures", []) if isinstance(summary.get("failures"), list) else []:
            if not isinstance(item, dict):
                continue
            episode_id = str(item.get("episode_id") or "")
            key = f"summary:{episode_id}:{item.get('worker_index')}:{item.get('gpu_id')}:{item.get('elapsed_s')}"
            if key in seen:
                continue
            seen.add(key)
            rows.append(build_rf_failure_row(dataset_root, item, accepted_context, source_path=source_path or str(dataset_root / "rf_failure_summary.json")))

    for path in sorted((dataset_root / "episodes").glob("episode_*/rf_process_meta*.json")):
        local_warnings: list[JsonDict] = []
        meta = load_json(path, local_warnings)
        if not meta or str(meta.get("status")) != "failed":
            continue
        episode_id = str(first_present(meta.get("episode_id"), path.parent.name))
        if any(row.get("episode_id") == episode_id for row in rows):
            continue
        rows.append(build_rf_failure_row(dataset_root, meta, accepted_context, source_path=str(path)))
        if local_warnings:
            missing_or_partial.append({"scope": "rf_process_meta", "episode_id": episode_id, "path": str(path), "read_warnings": local_warnings})
    return rows


def build_rf_failure_row(dataset_root: Path, item: JsonDict, accepted_context: dict[str, JsonDict], *, source_path: str) -> JsonDict:
    episode_id = str(item.get("episode_id") or "")
    context = accepted_context.get(episode_id, {})
    episode_dir = dataset_root / "episodes" / episode_id if episode_id else None
    failure_code = first_present(item.get("failure_code"), "rf_worker_failed")
    return {
        "record_id": f"rf:{episode_id}",
        "source_kind": "rf_failure",
        "failure_stage": "rf_processing",
        "failure_code": failure_code,
        "status": first_present(item.get("status"), "failed"),
        "failure_category": classify_failure_category("rf_processing", failure_code),
        "route_ids": context.get("route_ids", []),
        "plan_id": context.get("plan_id", ""),
        "target_tx_id": context.get("target_tx_id", ""),
        "vehicle_count": context.get("vehicle_count"),
        "target_large_vehicle_count": context.get("target_large_vehicle_count"),
        "attempt_id": "",
        "episode_id": episode_id,
        "log_paths": existing_log_paths(episode_dir) if episode_dir is not None else [],
        "source_path": source_path,
        "archived_source_path": "",
        "is_runtime_environment_failure": True,
        "is_plan_spawn_failure": False,
        "is_trajectory_qa_failure": False,
        "is_rf_failure": True,
        "warnings": [truncate_text(item.get("error"), 300)] if item.get("error") else [],
    }


def scan_per_tx_failure_rows(dataset_root: Path, accepted_context: dict[str, JsonDict], missing_or_partial: list[JsonDict]) -> list[JsonDict]:
    rows: list[JsonDict] = []
    for qa_path in sorted((dataset_root / "episodes").glob("episode_*/qa_report.json")):
        local_warnings: list[JsonDict] = []
        qa_report = load_json(qa_path, local_warnings)
        if not qa_report:
            continue
        episode_id = str(first_present(qa_report.get("episode_id"), qa_path.parent.name))
        scene_pass = bool(qa_report.get("scene_qc_pass"))
        tx_reports = qa_report.get("tx_reports", [])
        if not isinstance(tx_reports, list):
            continue
        for tx_report in tx_reports:
            if not isinstance(tx_report, dict):
                continue
            if scene_pass and bool(tx_report.get("pair_qc_pass")):
                continue
            blockers = as_list(tx_report.get("blockers"))
            failure_code = blockers[0] if blockers else ("scene_qc_failed" if not scene_pass else "per_tx_qa_failed")
            context = accepted_context.get(episode_id, {})
            rows.append(
                {
                    "record_id": f"per_tx:{episode_id}:{tx_report.get('tx_id')}",
                    "source_kind": "per_tx_qa_failure",
                    "failure_stage": "per_tx_qa",
                    "failure_code": failure_code,
                    "status": "failed",
                    "failure_category": "rf_or_per_tx_qa",
                    "route_ids": context.get("route_ids", []),
                    "plan_id": context.get("plan_id", ""),
                    "target_tx_id": context.get("target_tx_id", ""),
                    "vehicle_count": context.get("vehicle_count"),
                    "target_large_vehicle_count": context.get("target_large_vehicle_count"),
                    "attempt_id": "",
                    "episode_id": episode_id,
                    "log_paths": [],
                    "source_path": str(qa_path),
                    "archived_source_path": "",
                    "is_runtime_environment_failure": False,
                    "is_plan_spawn_failure": False,
                    "is_trajectory_qa_failure": False,
                    "is_rf_failure": False,
                    "warnings": blockers,
                }
            )
        if local_warnings:
            missing_or_partial.append({"scope": "per_tx_qa", "episode_id": episode_id, "path": str(qa_path), "read_warnings": local_warnings})
    return rows


def classify_failure_stage(*, status: Any, failure_code: Any, trajectory_qa: JsonDict | None) -> str:
    code = str(failure_code or "").lower()
    status_text = str(status or "").lower()
    if "rf" in code:
        return "rf_processing"
    if "carla" in code or status_text == "carla_failed" or "subprocess" in code:
        return "carla_collection"
    if "spawn" in code:
        return "spawn"
    if "preflight" in code or "plan" in code and "mismatch" in code:
        return "plan_preflight"
    if trajectory_qa is not None or status_text == "trajectory_rejected" or "collision" in code or "primary_not_moving" in code:
        return "trajectory_qa"
    return status_text or "unknown"


def classify_failure_category(failure_stage: Any, failure_code: Any) -> str:
    stage = str(failure_stage or "").lower()
    code = str(failure_code or "").lower()
    if stage == "rf_processing" or code.startswith("rf_") or "mitsuba" in code or "sionna" in code:
        return "runtime_environment"
    if "carla" in code or "subprocess" in code or "runtime" in code or "environment" in code:
        return "runtime_environment"
    if "spawn" in stage or "spawn" in code:
        return "plan_spawn"
    if "preflight" in stage or "plan" in code:
        return "plan_or_route"
    if "trajectory" in stage or "collision" in code or "not_moving" in code:
        return "trajectory_qa"
    if "qa" in stage:
        return "rf_or_per_tx_qa"
    return "unknown"


def existing_log_paths(primary_dir: Path | None, secondary_dir: Path | None = None) -> list[str]:
    paths: list[str] = []
    for base in (primary_dir, secondary_dir):
        if base is None:
            continue
        for name in ("carla_stdout.log", "carla_stderr.log", "stdout.log", "stderr.log"):
            path = base / name
            if path.exists():
                paths.append(str(path))
    return sorted(set(paths))


def build_failure_rate_tables(accepted_rows: list[JsonDict], failure_rows: list[JsonDict]) -> tuple[list[JsonDict], list[JsonDict], JsonDict]:
    accepted_by_route: Counter[str] = Counter()
    failed_by_route: Counter[str] = Counter()
    stage_by_route: dict[str, Counter[str]] = defaultdict(Counter)
    code_by_route: dict[str, Counter[str]] = defaultdict(Counter)

    for row in accepted_rows:
        for route_id in as_list(row.get("route_ids")):
            accepted_by_route[str(route_id)] += 1
    for row in failure_rows:
        routes = as_list(row.get("route_ids")) or ["unknown"]
        for route_id in routes:
            failed_by_route[str(route_id)] += 1
            stage_by_route[str(route_id)][str(row.get("failure_stage") or "unknown")] += 1
            code_by_route[str(route_id)][str(row.get("failure_code") or "unknown")] += 1

    route_rows: list[JsonDict] = []
    for route_id in sorted(set(accepted_by_route) | set(failed_by_route)):
        accepted = int(accepted_by_route.get(route_id, 0))
        failed = int(failed_by_route.get(route_id, 0))
        total = accepted + failed
        stage_counts = sorted_counter(stage_by_route.get(route_id, Counter()))
        code_counts = sorted_counter(code_by_route.get(route_id, Counter()))
        route_rows.append(
            {
                "route_id": route_id,
                "accepted_count": accepted,
                "failed_count": failed,
                "total_count": total,
                "failure_rate": float(failed / total) if total else UNAVAILABLE,
                "top_failure_stage": next(iter(stage_counts), ""),
                "top_failure_code": next(iter(code_counts), ""),
                "failure_stage_counts": stage_counts,
                "failure_code_counts": code_counts,
            }
        )

    accepted_by_bucket: Counter[tuple[str, str]] = Counter()
    failed_by_bucket: Counter[tuple[str, str]] = Counter()
    stage_by_bucket: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    code_by_bucket: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    for row in accepted_rows:
        key = bucket_key(row)
        accepted_by_bucket[key] += 1
    for row in failure_rows:
        key = bucket_key(row)
        failed_by_bucket[key] += 1
        stage_by_bucket[key][str(row.get("failure_stage") or "unknown")] += 1
        code_by_bucket[key][str(row.get("failure_code") or "unknown")] += 1

    bucket_rows: list[JsonDict] = []
    for key in sorted(set(accepted_by_bucket) | set(failed_by_bucket)):
        accepted = int(accepted_by_bucket.get(key, 0))
        failed = int(failed_by_bucket.get(key, 0))
        total = accepted + failed
        stage_counts = sorted_counter(stage_by_bucket.get(key, Counter()))
        code_counts = sorted_counter(code_by_bucket.get(key, Counter()))
        bucket_rows.append(
            {
                "vehicle_count": key[0],
                "target_large_vehicle_count": key[1],
                "accepted_count": accepted,
                "failed_count": failed,
                "total_count": total,
                "failure_rate": float(failed / total) if total else UNAVAILABLE,
                "top_failure_stage": next(iter(stage_counts), ""),
                "top_failure_code": next(iter(code_counts), ""),
                "failure_stage_counts": stage_counts,
                "failure_code_counts": code_counts,
            }
        )

    failure_code_by_route: dict[str, dict[str, int]] = {
        route_id: sorted_counter(counter) for route_id, counter in sorted(code_by_route.items())
    }
    failure_code_by_vehicle_bucket = {
        f"vehicles_{key[0]}_large_{key[1]}": sorted_counter(counter) for key, counter in sorted(code_by_bucket.items())
    }
    return route_rows, bucket_rows, {
        "failure_code_by_route": failure_code_by_route,
        "failure_code_by_vehicle_bucket": failure_code_by_vehicle_bucket,
    }


def build_actor_group_metrics(actor_rows: list[JsonDict], *, group_key: str = "role_group") -> dict[str, JsonDict]:
    groups: dict[str, list[JsonDict]] = defaultdict(list)
    for row in actor_rows:
        key = str(row.get(group_key) or "unknown")
        groups[key].append(row)
        if group_key == "role_group" and key in CONTROLLED_GROUPS:
            groups["controlled_combined"].append(row)
    result: dict[str, JsonDict] = {}
    for key, rows in sorted(groups.items()):
        result[key] = {
            "actor_count": int(len(rows)),
            "large_actor_count": int(sum(1 for row in rows if bool(row.get("is_large_vehicle")))),
            "speed_mean_mps": mean(row.get("speed_mean_mps") for row in rows),
            "speed_p95_mps": percentile((row.get("speed_p95_mps") for row in rows), 95),
            "speed_max_mps": max_value(row.get("speed_max_mps") for row in rows),
            "accel_abs_p95_mps2": percentile((row.get("accel_abs_p95_mps2") for row in rows), 95),
            "jerk_abs_p95_mps3": percentile((row.get("jerk_abs_p95_mps3") for row in rows), 95),
            "stationary_fraction_mean": mean(row.get("stationary_fraction") for row in rows),
            "stationary_duration_p95_s": percentile((row.get("stationary_duration_s") for row in rows), 95),
            "displacement_mean_m": mean(row.get("displacement_m") for row in rows),
            "low_progress_actor_count": int(sum(1 for row in rows if bool(row.get("low_progress_warning")))),
            "harsh_brake_count": int(sum(safe_int(row.get("harsh_brake_count"), 0) or 0 for row in rows)),
            "harsh_accel_count": int(sum(safe_int(row.get("harsh_accel_count"), 0) or 0 for row in rows)),
            "min_inter_vehicle_gap_m": min_value(row.get("min_inter_vehicle_gap_m") for row in rows),
            "min_center_distance_m": min_value(row.get("min_center_distance_m") for row in rows),
        }
    return result


def build_summary(
    *,
    dataset_root: Path,
    output_dir: Path,
    thresholds: AuditThresholds,
    collection_summary: JsonDict | None,
    supervisor_status: JsonDict | None,
    rf_summaries: list[JsonDict],
    accepted_rows: list[JsonDict],
    actor_rows: list[JsonDict],
    failure_rows: list[JsonDict],
    route_rows: list[JsonDict],
    bucket_rows: list[JsonDict],
    cross_tabs: JsonDict,
    suspicious: list[JsonDict],
    actor_group_metrics: dict[str, JsonDict],
    vehicle_size_metrics: dict[str, JsonDict],
    vehicle_count_behavior: dict[str, JsonDict],
    target_large_behavior: dict[str, JsonDict],
    missing_or_partial: list[JsonDict],
) -> JsonDict:
    failure_stage_hist = histogram(failure_rows, "failure_stage")
    failure_code_hist = histogram(failure_rows, "failure_code")
    failure_category_hist = histogram(failure_rows, "failure_category")
    qa_warning_counter: Counter[str] = Counter()
    for row in accepted_rows:
        for warning in as_list(row.get("trajectory_warnings")) + as_list(row.get("qa_warnings")):
            qa_warning_counter[str(warning)] += 1

    route_concentration = [
        {
            "route_id": row.get("route_id"),
            "failed_count": row.get("failed_count"),
            "accepted_count": row.get("accepted_count"),
            "failure_rate": row.get("failure_rate"),
            "top_failure_code": row.get("top_failure_code"),
        }
        for row in sorted(route_rows, key=lambda r: (-safe_int(r.get("failed_count"), 0), -safe_float(r.get("failure_rate"), 0.0)))[:10]
    ]
    bucket_concentration = [
        {
            "vehicle_count": row.get("vehicle_count"),
            "target_large_vehicle_count": row.get("target_large_vehicle_count"),
            "failed_count": row.get("failed_count"),
            "accepted_count": row.get("accepted_count"),
            "failure_rate": row.get("failure_rate"),
            "top_failure_code": row.get("top_failure_code"),
        }
        for row in sorted(bucket_rows, key=lambda r: (-safe_int(r.get("failed_count"), 0), -safe_float(r.get("failure_rate"), 0.0)))[:10]
    ]

    collection_extract = collection_summary_extract(collection_summary)
    rf_extract = rf_summary_extract(rf_summaries)
    behavior_anomaly_summary = {
        "suspicious_episode_count": int(sum(1 for row in accepted_rows if (safe_float(row.get("suspicious_score"), 0.0) or 0.0) > 0)),
        "top_suspicious_score": max_value(row.get("suspicious_score") for row in accepted_rows),
        "episodes_with_qa_warnings": int(sum(1 for row in accepted_rows if as_list(row.get("trajectory_warnings")) or as_list(row.get("qa_warnings")))),
        "episodes_with_long_stationary": int(
            sum((safe_float(row.get("stationary_duration_max_s"), 0.0) or 0.0) >= thresholds.long_stationary_s for row in accepted_rows)
        ),
        "episodes_with_all_stop_warning": int(
            sum((safe_float(row.get("all_stop_duration_s"), 0.0) or 0.0) >= thresholds.all_stop_warning_s for row in accepted_rows)
        ),
        "episodes_with_close_gap": int(
            sum(
                (safe_float(row.get("min_inter_vehicle_gap_m")) is not None)
                and ((safe_float(row.get("min_inter_vehicle_gap_m")) or 0.0) <= thresholds.close_gap_m)
                for row in accepted_rows
            )
        ),
        "total_harsh_brake_events": int(sum(safe_int(row.get("harsh_brake_count"), 0) or 0 for row in accepted_rows)),
        "total_harsh_accel_events": int(sum(safe_int(row.get("harsh_accel_count"), 0) or 0 for row in accepted_rows)),
    }
    unavailable_metrics = {
        "lane_deviation": UNAVAILABLE,
        "offroad_ratio": UNAVAILABLE,
        "route_progress_true_distance_along_route": UNAVAILABLE,
        "route_progress_proxy": "displacement_m from actor_states; not map-aware route progress",
        "min_inter_vehicle_distance": "available from actor center and bbox extent when actor_states contain positions",
    }
    qa_blocked_failures = {
        "trajectory_qa_failure_code_histogram": sorted_counter(
            Counter(str(row.get("failure_code")) for row in failure_rows if row.get("failure_stage") == "trajectory_qa")
        ),
        "trajectory_qa_failure_count": int(sum(1 for row in failure_rows if row.get("failure_stage") == "trajectory_qa")),
        "qa_warning_histogram_in_accepted": sorted_counter(qa_warning_counter),
    }
    problem_classification = infer_problem_classification(failure_rows, accepted_rows)
    tm_hypothesis = infer_tm_hypothesis(actor_group_metrics)

    return {
        "schema": AUDIT_SCHEMA,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "dataset_root": str(dataset_root),
        "output_dir": str(output_dir),
        "thresholds": asdict(thresholds),
        "counts": {
            "accepted_episode_dirs": int(len(accepted_rows)),
            "actor_metric_rows": int(len(actor_rows)),
            "failure_case_rows": int(len(failure_rows)),
            "failure_attempt_rows": int(sum(row.get("source_kind") in {"attempt_failure", "archived_failure_only"} for row in failure_rows)),
            "rf_failure_rows": int(sum(row.get("source_kind") == "rf_failure" for row in failure_rows)),
            "per_tx_qa_failure_rows": int(sum(row.get("source_kind") == "per_tx_qa_failure" for row in failure_rows)),
            "missing_or_partial_record_count": int(len(missing_or_partial)),
        },
        "collection_summary": collection_extract,
        "supervisor_status": supervisor_status or {},
        "rf_summary": rf_extract,
        "histograms": {
            "failure_stage": failure_stage_hist,
            "failure_code": failure_code_hist,
            "failure_category": failure_category_hist,
            "vehicle_count_failures": histogram(failure_rows, "vehicle_count"),
            "target_large_vehicle_count_failures": histogram(failure_rows, "target_large_vehicle_count"),
            "accepted_vehicle_count": histogram(accepted_rows, "vehicle_count"),
            "accepted_target_large_vehicle_count": histogram(accepted_rows, "target_large_vehicle_count"),
            "qa_warnings_in_accepted": sorted_counter(qa_warning_counter),
        },
        "cross_tabs": cross_tabs,
        "route_concentration_top": route_concentration,
        "bucket_concentration_top": bucket_concentration,
        "accepted_behavior_audit": behavior_anomaly_summary,
        "actor_group_metrics": actor_group_metrics,
        "vehicle_size_metrics": vehicle_size_metrics,
        "vehicle_count_behavior": vehicle_count_behavior,
        "target_large_vehicle_behavior": target_large_behavior,
        "qa_blocked_failures": qa_blocked_failures,
        "problem_classification": problem_classification,
        "tm_control_hypothesis": tm_hypothesis,
        "metrics_availability": unavailable_metrics,
        "top_suspicious_cases": suspicious[:10],
        "outputs": {
            "summary_md": str(output_dir / "summary.md"),
            "summary_json": str(output_dir / "summary.json"),
            "tables": str(output_dir / "tables"),
            "samples": str(output_dir / "samples"),
        },
    }


def render_summary_md(summary: JsonDict) -> str:
    counts = summary["counts"]
    hist = summary["histograms"]
    actor_groups = summary.get("actor_group_metrics", {})
    behavior = summary.get("accepted_behavior_audit", {})
    problem = summary.get("problem_classification", {})
    tm = summary.get("tm_control_hypothesis", {})
    lines = [
        "# Offline QA / Failcase Audit Summary",
        "",
        f"- Dataset root: `{summary['dataset_root']}`",
        f"- Generated at: `{summary['generated_at']}`",
        f"- Output dir: `{summary['output_dir']}`",
        "",
        "## Dataset Counts",
        "",
        f"- Accepted episode dirs scanned: **{counts['accepted_episode_dirs']}**",
        f"- Failure case rows emitted: **{counts['failure_case_rows']}**",
        f"  - Attempt/trajectory/CARLA failures: **{counts['failure_attempt_rows']}**",
        f"  - RF failures: **{counts['rf_failure_rows']}**",
        f"  - Per-TX QA failures: **{counts['per_tx_qa_failure_rows']}**",
        f"- Actor behavior rows: **{counts['actor_metric_rows']}**",
        f"- Missing/partial records logged: **{counts['missing_or_partial_record_count']}**",
        "",
        "## Most Common Failure Stages",
        "",
    ]
    lines.extend(markdown_counter(hist.get("failure_stage", {})))
    lines.extend(["", "## Most Common Failure Codes", ""])
    lines.extend(markdown_counter(hist.get("failure_code", {})))
    lines.extend(["", "## Failure Concentration by Route", ""])
    lines.extend(markdown_route_summary(summary.get("route_concentration_top", [])))
    lines.extend(["", "## Failure Concentration by Vehicle / Large-Vehicle Bucket", ""])
    lines.extend(markdown_bucket_summary(summary.get("bucket_concentration_top", [])))
    lines.extend(
        [
            "",
            "## Accepted Episode Behavior Audit",
            "",
            f"- Episodes with non-zero audit suspicious score: **{behavior.get('suspicious_episode_count')}**",
            f"- Top suspicious score: **{behavior.get('top_suspicious_score')}**",
            f"- Episodes with QA warnings: **{behavior.get('episodes_with_qa_warnings')}**",
            f"- Episodes with long stationary actor duration: **{behavior.get('episodes_with_long_stationary')}**",
            f"- Episodes with all-stop warning duration: **{behavior.get('episodes_with_all_stop_warning')}**",
            f"- Episodes with close inter-vehicle gap: **{behavior.get('episodes_with_close_gap')}**",
            f"- Total harsh brake events: **{behavior.get('total_harsh_brake_events')}**",
            f"- Total harsh acceleration events: **{behavior.get('total_harsh_accel_events')}**",
            "",
            "The suspicious score is an offline review ranking only. It is not a formal QA gate.",
            "",
            "## Controlled vs Background TM Behavior",
            "",
        ]
    )
    for group in ("primary_controlled", "auxiliary_controlled", "background_tm", "controlled_combined"):
        if group not in actor_groups:
            continue
        metrics = actor_groups[group]
        lines.append(
            "- "
            f"`{group}`: actors={metrics.get('actor_count')}, "
            f"speed_mean={fmt_metric(metrics.get('speed_mean_mps'))} m/s, "
            f"stationary_fraction_mean={fmt_metric(metrics.get('stationary_fraction_mean'))}, "
            f"displacement_mean={fmt_metric(metrics.get('displacement_mean_m'))} m, "
            f"harsh_brake={metrics.get('harsh_brake_count')}, "
            f"harsh_accel={metrics.get('harsh_accel_count')}, "
            f"low_progress={metrics.get('low_progress_actor_count')}"
        )
    lines.extend(
        [
            "",
            f"TM-control hypothesis status: **{tm.get('status', UNAVAILABLE)}**",
            f"- Evidence: {tm.get('evidence', UNAVAILABLE)}",
            f"- Limitation: {tm.get('limitation', 'offline actor-state metrics only; no new CARLA runs')}",
            "",
            "## What Existing QA Blocked",
            "",
        ]
    )
    lines.extend(markdown_counter(summary.get("qa_blocked_failures", {}).get("trajectory_qa_failure_code_histogram", {})))
    lines.extend(
        [
            "",
            "## Likely QA-Rule Issues vs Collection/Route/Runtime Issues",
            "",
            f"- QA-rule candidates: {problem.get('qa_rule_candidates', UNAVAILABLE)}",
            f"- Collection/route/runtime candidates: {problem.get('collection_route_runtime_candidates', UNAVAILABLE)}",
            f"- Notes: {problem.get('notes', UNAVAILABLE)}",
            "",
            "## Metrics Marked Unavailable / Proxy",
            "",
            "- Lane deviation: unavailable unless a reliable map/waypoint path is reused.",
            "- Offroad ratio: unavailable unless map/waypoint semantics are loaded reliably.",
            "- Route progress: proxy uses actor displacement from `actor_states.jsonl`; it is not true along-route progress.",
            "",
            "## Next-Step Recommendations",
            "",
            "1. Manually inspect the top cases in `samples/suspicious_accepted_cases.json` before changing QA rules.",
            "2. Inspect route/bucket concentration tables before changing route sampling or vehicle quotas.",
            "3. Treat CARLA subprocess and RF worker failures as runtime/collection issues, not trajectory QA issues.",
            "4. Use the controlled-vs-background metrics as preliminary evidence only; validating TM-vs-manual control needs a separate controlled experiment.",
            "5. Do not change the formal pipeline based solely on this audit ranking.",
        ]
    )
    return "\n".join(lines) + "\n"


def markdown_counter(counter: dict[str, int], *, limit: int = 10) -> list[str]:
    if not counter:
        return ["- None"]
    return [f"- `{item['key']}`: **{item['count']}**" for item in top_dict_items(counter, limit)]


def markdown_route_summary(rows: list[JsonDict]) -> list[str]:
    if not rows:
        return ["- No route-level failures found."]
    lines = []
    for row in rows[:10]:
        lines.append(
            "- "
            f"`{row.get('route_id')}`: failed={row.get('failed_count')}, "
            f"accepted={row.get('accepted_count')}, "
            f"failure_rate={fmt_metric(row.get('failure_rate'))}, "
            f"top_code=`{row.get('top_failure_code')}`"
        )
    return lines


def markdown_bucket_summary(rows: list[JsonDict]) -> list[str]:
    if not rows:
        return ["- No bucket-level failures found."]
    lines = []
    for row in rows[:10]:
        lines.append(
            "- "
            f"vehicles={row.get('vehicle_count')}, large={row.get('target_large_vehicle_count')}: "
            f"failed={row.get('failed_count')}, accepted={row.get('accepted_count')}, "
            f"failure_rate={fmt_metric(row.get('failure_rate'))}, top_code=`{row.get('top_failure_code')}`"
        )
    return lines


def fmt_metric(value: Any) -> str:
    numeric = safe_float(value)
    if numeric is None:
        return str(value)
    return f"{numeric:.3f}"


def collection_summary_extract(summary: JsonDict | None) -> JsonDict:
    if not summary:
        return {}
    return {
        "schema": summary.get("schema"),
        "attempted": summary.get("attempted"),
        "trajectory_accepted": summary.get("trajectory_accepted"),
        "initial_trajectory_accepted": summary.get("initial_trajectory_accepted"),
        "total_trajectory_accepted": summary.get("total_trajectory_accepted"),
        "target_accepted": summary.get("target_accepted"),
        "target_reached": summary.get("target_reached"),
        "stopped_reason": summary.get("stopped_reason"),
        "status_histogram": summary.get("status_histogram", {}),
        "failure_code_histogram": summary.get("failure_code_histogram", {}),
        "bucket_attempt_status_histogram": summary.get("bucket_attempt_status_histogram", {}),
        "bucket_attempt_failure_code_histogram": summary.get("bucket_attempt_failure_code_histogram", {}),
        "selection_cell_attempt_status_histogram": summary.get("selection_cell_attempt_status_histogram", {}),
        "selection_cell_attempt_failure_code_histogram": summary.get("selection_cell_attempt_failure_code_histogram", {}),
    }


def rf_summary_extract(summaries: list[JsonDict]) -> JsonDict:
    if not summaries:
        return {}
    processed = sum(safe_int(summary.get("processed_episode_count"), 0) or 0 for summary in summaries)
    failed = sum(safe_int(summary.get("failed_episode_count"), 0) or 0 for summary in summaries)
    queued = sum(safe_int(summary.get("queued_episode_count"), 0) or 0 for summary in summaries)
    return {
        "summary_file_count": int(len(summaries)),
        "queued_episode_count_sum": int(queued),
        "processed_episode_count_sum": int(processed),
        "failed_episode_count_sum": int(failed),
        "gpu_ids": [summary.get("gpu_ids") for summary in summaries if summary.get("gpu_ids") is not None],
        "worker_counts": [summary.get("worker_count") for summary in summaries if summary.get("worker_count") is not None],
    }


def infer_problem_classification(failure_rows: list[JsonDict], accepted_rows: list[JsonDict]) -> JsonDict:
    stage_counts = Counter(str(row.get("failure_stage") or "unknown") for row in failure_rows)
    category_counts = Counter(str(row.get("failure_category") or "unknown") for row in failure_rows)
    accepted_warning_count = sum(1 for row in accepted_rows if as_list(row.get("trajectory_warnings")) or as_list(row.get("qa_warnings")))
    qa_candidates = []
    if accepted_warning_count:
        qa_candidates.append(f"{accepted_warning_count} accepted episodes carry QA warnings; inspect whether warning-only behavior is acceptable.")
    suspicious_count = sum(1 for row in accepted_rows if (safe_float(row.get("suspicious_score"), 0.0) or 0.0) > 0)
    if suspicious_count:
        qa_candidates.append(f"{suspicious_count} accepted episodes have non-zero offline suspicious ranking.")
    if not qa_candidates:
        qa_candidates.append("No obvious QA-rule issue from lightweight metrics; manual review still required.")

    runtime_count = category_counts.get("runtime_environment", 0)
    traj_count = stage_counts.get("trajectory_qa", 0)
    collection_route_runtime = []
    if runtime_count:
        collection_route_runtime.append(f"{runtime_count} runtime/environment-class failures, including CARLA/RF failures.")
    if traj_count:
        collection_route_runtime.append(f"{traj_count} trajectory-QA failures likely reflect route interaction, collision, or motion-realizability problems.")
    if not collection_route_runtime:
        collection_route_runtime.append("No dominant collection/route/runtime class detected.")
    return {
        "stage_counts": sorted_counter(stage_counts),
        "category_counts": sorted_counter(category_counts),
        "qa_rule_candidates": " ".join(qa_candidates),
        "collection_route_runtime_candidates": " ".join(collection_route_runtime),
        "notes": "Classifications are heuristic labels for audit triage, not root-cause proof.",
    }


def infer_tm_hypothesis(actor_group_metrics: dict[str, JsonDict]) -> JsonDict:
    controlled = actor_group_metrics.get("controlled_combined")
    background = actor_group_metrics.get("background_tm")
    if not controlled or not background:
        return {
            "status": UNAVAILABLE,
            "evidence": "Need both controlled and background_tm actor tracks.",
            "limitation": "offline actor-state metrics only; no new CARLA runs",
        }
    controlled_jerk = safe_float(controlled.get("jerk_abs_p95_mps3"))
    background_jerk = safe_float(background.get("jerk_abs_p95_mps3"))
    controlled_stationary = safe_float(controlled.get("stationary_fraction_mean"))
    background_stationary = safe_float(background.get("stationary_fraction_mean"))
    controlled_harsh = (safe_int(controlled.get("harsh_brake_count"), 0) or 0) + (safe_int(controlled.get("harsh_accel_count"), 0) or 0)
    background_harsh = (safe_int(background.get("harsh_brake_count"), 0) or 0) + (safe_int(background.get("harsh_accel_count"), 0) or 0)
    controlled_count = max(1, safe_int(controlled.get("actor_count"), 1) or 1)
    background_count = max(1, safe_int(background.get("actor_count"), 1) or 1)
    controlled_harsh_per_actor = controlled_harsh / controlled_count
    background_harsh_per_actor = background_harsh / background_count
    controlled_low_progress = safe_int(controlled.get("low_progress_actor_count"), 0) or 0
    background_low_progress = safe_int(background.get("low_progress_actor_count"), 0) or 0

    evidence = (
        f"controlled jerk_p95={fmt_metric(controlled_jerk)}, background jerk_p95={fmt_metric(background_jerk)}; "
        f"controlled stationary_fraction={fmt_metric(controlled_stationary)}, background stationary_fraction={fmt_metric(background_stationary)}; "
        f"controlled harsh_events_per_actor={controlled_harsh_per_actor:.3f}, background harsh_events_per_actor={background_harsh_per_actor:.3f}; "
        f"controlled low_progress={controlled_low_progress}, background low_progress={background_low_progress}."
    )
    support_signals = 0
    contradiction_signals = 0
    if controlled_jerk is not None and background_jerk is not None and controlled_jerk > background_jerk:
        support_signals += 1
    if controlled_harsh_per_actor > background_harsh_per_actor:
        support_signals += 1
    if controlled_stationary is not None and background_stationary is not None:
        if controlled_stationary > background_stationary:
            support_signals += 1
        elif background_stationary > controlled_stationary + 0.05:
            contradiction_signals += 1
    if background_low_progress > controlled_low_progress:
        contradiction_signals += 1

    if support_signals >= 2 and contradiction_signals == 0:
        status = "preliminarily_supports_more_tm_control"
    elif support_signals > 0 or contradiction_signals > 0:
        status = "mixed_or_inconclusive"
    else:
        status = "not_supported_by_current_lightweight_metrics"
    return {
        "status": status,
        "evidence": evidence,
        "limitation": "This compares existing accepted tracks only; it does not isolate control policy causality.",
    }


def score_suspicious(row: JsonDict, thresholds: AuditThresholds) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    jerk_p95 = safe_float(row.get("jerk_abs_p95_mps3"))
    jerk_max = safe_float(row.get("jerk_abs_max_mps3"))
    if jerk_p95 is not None and jerk_p95 >= thresholds.high_jerk_mps3:
        score += 3.0
        reasons.append(f"high_jerk_p95:{jerk_p95:.2f}")
    if jerk_max is not None and jerk_max >= thresholds.high_jerk_mps3 * 2.0:
        score += 1.0
        reasons.append(f"high_jerk_max:{jerk_max:.2f}")
    harsh_brakes = safe_int(row.get("harsh_brake_count"), 0) or 0
    harsh_accels = safe_int(row.get("harsh_accel_count"), 0) or 0
    if harsh_brakes:
        score += min(5.0, harsh_brakes * 0.5)
        reasons.append(f"harsh_brake:{harsh_brakes}")
    if harsh_accels:
        score += min(5.0, harsh_accels * 0.5)
        reasons.append(f"harsh_accel:{harsh_accels}")
    stationary = safe_float(row.get("stationary_duration_max_s"))
    if stationary is not None and stationary >= thresholds.long_stationary_s:
        score += 2.0
        reasons.append(f"long_stationary:{stationary:.2f}s")
    all_stop = safe_float(row.get("all_stop_duration_s"))
    if all_stop is not None and all_stop >= thresholds.all_stop_warning_s:
        score += 4.0
        reasons.append(f"all_stop:{all_stop:.2f}s")
    low_progress = safe_int(row.get("low_progress_actor_count"), 0) or 0
    if low_progress:
        score += 2.0 * low_progress
        reasons.append(f"low_progress_actors:{low_progress}")
    min_gap = safe_float(row.get("min_inter_vehicle_gap_m"))
    if min_gap is not None and min_gap <= thresholds.close_gap_m:
        score += 5.0
        reasons.append(f"close_gap:{min_gap:.2f}m")
    min_center = safe_float(row.get("min_center_distance_m"))
    if min_center is not None and min_center <= thresholds.close_center_distance_m:
        score += 2.0
        reasons.append(f"close_center:{min_center:.2f}m")
    qa_warning_count = len(as_list(row.get("trajectory_warnings"))) + len(as_list(row.get("qa_warnings")))
    if qa_warning_count:
        score += float(qa_warning_count)
        reasons.append(f"qa_warnings:{qa_warning_count}")
    missing_count = len(as_list(row.get("missing_files")))
    if missing_count:
        score += 2.0 * missing_count
        reasons.append(f"missing_files:{missing_count}")
    return float(score), reasons


def suspicious_case(row: JsonDict) -> JsonDict:
    return {
        "episode_id": row.get("episode_id"),
        "scene_id": row.get("scene_id"),
        "plan_id": row.get("plan_id"),
        "route_ids": row.get("route_ids"),
        "vehicle_count": row.get("vehicle_count"),
        "target_large_vehicle_count": row.get("target_large_vehicle_count"),
        "suspicious_score": row.get("suspicious_score"),
        "suspicious_reasons": row.get("suspicious_reasons"),
        "speed_max_mps": row.get("speed_max_mps"),
        "jerk_abs_p95_mps3": row.get("jerk_abs_p95_mps3"),
        "jerk_abs_max_mps3": row.get("jerk_abs_max_mps3"),
        "harsh_brake_count": row.get("harsh_brake_count"),
        "harsh_accel_count": row.get("harsh_accel_count"),
        "stationary_duration_max_s": row.get("stationary_duration_max_s"),
        "all_stop_duration_s": row.get("all_stop_duration_s"),
        "low_progress_actor_count": row.get("low_progress_actor_count"),
        "min_inter_vehicle_gap_m": row.get("min_inter_vehicle_gap_m"),
        "trajectory_warnings": row.get("trajectory_warnings"),
        "qa_warnings": row.get("qa_warnings"),
        "missing_files": row.get("missing_files"),
        "episode_dir": row.get("episode_dir"),
    }


def route_ids_from_plan(plan: JsonDict | None) -> list[str]:
    if not isinstance(plan, dict):
        return []
    route_ids: list[str] = []
    for vehicle in plan.get("vehicles", []) if isinstance(plan.get("vehicles"), list) else []:
        if isinstance(vehicle, dict) and vehicle.get("route_id") is not None:
            route_ids.append(str(vehicle["route_id"]))
    for route_id in plan.get("route_ids", []) if isinstance(plan.get("route_ids"), list) else []:
        if route_id is not None:
            route_ids.append(str(route_id))
    return sorted(set(route_ids))


def plan_value(plan: JsonDict | None, meta: JsonDict | None, key: str) -> Any:
    for source in (plan, meta):
        if not isinstance(source, dict):
            continue
        bucket = source.get("bucket") if isinstance(source.get("bucket"), dict) else {}
        plan_bucket = source.get("plan_bucket") if isinstance(source.get("plan_bucket"), dict) else {}
        expected = source.get("expected_metrics") if isinstance(source.get("expected_metrics"), dict) else {}
        for container in (bucket, plan_bucket, source, expected):
            if isinstance(container, dict) and key in container:
                return container.get(key)
    if key == "target_large_vehicle_count":
        for source in (plan, meta):
            expected = source.get("expected_metrics") if isinstance(source, dict) and isinstance(source.get("expected_metrics"), dict) else {}
            if "target_large_vehicle_count" in expected:
                return expected["target_large_vehicle_count"]
    return None


def vehicle_role_counts(*records: JsonDict | None) -> JsonDict:
    keys = [
        "planned_required_controlled_count",
        "planned_optional_controlled_count",
        "requested_background_count",
        "actual_required_controlled_count",
        "actual_optional_controlled_count",
        "actual_background_count",
        "actual_total_vehicle_count",
    ]
    sources: list[JsonDict] = []
    for record in records:
        if not isinstance(record, dict):
            continue
        for candidate in (
            record.get("vehicle_role_counts"),
            nested_get(record, ("plan_consistency", "vehicle_role_counts")),
            nested_get(record, ("summary", "vehicle_role_counts")),
            nested_get(record, ("scene_qa", "vehicle_role_counts")),
        ):
            if isinstance(candidate, dict):
                sources.append(candidate)
    result: JsonDict = {}
    for key in keys:
        result[key] = first_present(*(source.get(key) for source in sources))
    return result


def as_list(value: Any) -> list[Any]:
    if value is None or value == "":
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return sorted(value)
    return [value]


def bucket_key(row: JsonDict) -> tuple[str, str]:
    vehicle = row.get("vehicle_count")
    large = row.get("target_large_vehicle_count")
    return (str(vehicle) if vehicle not in (None, "") else "unknown", str(large) if large not in (None, "") else "unknown")


def bucket_value(value: Any, *, prefix: str) -> str:
    if value in (None, ""):
        return f"{prefix}_unknown"
    return f"{prefix}_{value}"


def truncate_text(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


if __name__ == "__main__":
    raise SystemExit(main())
