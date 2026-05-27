"""Small schema/constants module for the offline QA audit scripts.

This package is intentionally outside ``dynamic_radio_dataset``.  It only
describes read-only audit outputs and lightweight metric thresholds; it is not
part of the formal generation pipeline.
"""

from __future__ import annotations

from dataclasses import dataclass


AUDIT_SCHEMA = "dynamic_radio_dataset_qa_audit_v1"
UNAVAILABLE = "unavailable"

PRIMARY_CONTROLLED_ROLES = {"primary", "primary_controlled", "main", "main_occluder"}
AUXILIARY_CONTROLLED_ROLES = {"context", "target", "auxiliary_controlled"}
BACKGROUND_TM_ROLES = {"background", "background_tm", "ambient"}

LARGE_VEHICLE_HINTS = (
    "carlamotors",
    "carlacola",
    "firetruck",
    "ambulance",
    "truck",
    "bus",
    "sprinter",
)


@dataclass(frozen=True)
class AuditThresholds:
    """Heuristic thresholds for offline ranking only.

    These are not formal QA gates.  They are deliberately conservative and are
    written into ``summary.json`` so audit scores remain interpretable.
    """

    stationary_speed_mps: float = 0.2
    harsh_brake_mps2: float = -4.0
    harsh_accel_mps2: float = 3.0
    high_jerk_mps3: float = 10.0
    long_stationary_s: float = 3.0
    all_stop_warning_s: float = 2.0
    low_progress_m: float = 5.0
    close_gap_m: float = 1.0
    close_center_distance_m: float = 3.0


ACCEPTED_EPISODE_HEADERS = [
    "episode_id",
    "episode_dir",
    "scene_id",
    "plan_id",
    "target_tx_id",
    "route_ids",
    "vehicle_count",
    "target_large_vehicle_count",
    "actual_large_vehicle_count",
    "planned_required_controlled_count",
    "planned_optional_controlled_count",
    "requested_background_count",
    "actual_required_controlled_count",
    "actual_optional_controlled_count",
    "actual_background_count",
    "actual_total_vehicle_count",
    "actor_count",
    "primary_controlled_actor_count",
    "auxiliary_controlled_actor_count",
    "background_tm_actor_count",
    "large_actor_count",
    "trajectory_qc_pass",
    "trajectory_failure_code",
    "trajectory_warnings",
    "trajectory_blockers",
    "scene_qc_pass",
    "qa_warnings",
    "qa_blockers",
    "tx_count",
    "accepted_pair_count",
    "accepted_tx_ids",
    "validation_valid_clip",
    "validation_passed_target_count",
    "rf_status",
    "rf_failure_code",
    "rf_error",
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
    "suspicious_score",
    "suspicious_reasons",
    "unavailable_metrics",
    "missing_files",
    "partial_warnings",
]


ACTOR_BEHAVIOR_HEADERS = [
    "episode_id",
    "scene_id",
    "plan_id",
    "vehicle_count",
    "target_large_vehicle_count",
    "actor_id",
    "role",
    "role_group",
    "control_mode",
    "vehicle_type",
    "is_large_vehicle",
    "route_id",
    "frame_count",
    "duration_s",
    "speed_mean_mps",
    "speed_p95_mps",
    "speed_max_mps",
    "accel_signed_min_mps2",
    "accel_signed_max_mps2",
    "accel_abs_mean_mps2",
    "accel_abs_p95_mps2",
    "accel_abs_max_mps2",
    "jerk_abs_p95_mps3",
    "jerk_abs_max_mps3",
    "harsh_brake_count",
    "harsh_accel_count",
    "stationary_duration_s",
    "stationary_fraction",
    "displacement_m",
    "path_length_m",
    "route_progress_proxy_m",
    "low_progress_warning",
    "min_inter_vehicle_gap_m",
    "min_center_distance_m",
    "has_complete_track",
    "warnings",
]


FAILURE_CASE_HEADERS = [
    "record_id",
    "source_kind",
    "failure_stage",
    "failure_code",
    "status",
    "failure_category",
    "route_ids",
    "plan_id",
    "target_tx_id",
    "vehicle_count",
    "target_large_vehicle_count",
    "attempt_id",
    "episode_id",
    "log_paths",
    "source_path",
    "archived_source_path",
    "is_runtime_environment_failure",
    "is_plan_spawn_failure",
    "is_trajectory_qa_failure",
    "is_rf_failure",
    "warnings",
]


ROUTE_FAILURE_RATE_HEADERS = [
    "route_id",
    "accepted_count",
    "failed_count",
    "total_count",
    "failure_rate",
    "top_failure_stage",
    "top_failure_code",
    "failure_stage_counts",
    "failure_code_counts",
]


BUCKET_FAILURE_RATE_HEADERS = [
    "vehicle_count",
    "target_large_vehicle_count",
    "accepted_count",
    "failed_count",
    "total_count",
    "failure_rate",
    "top_failure_stage",
    "top_failure_code",
    "failure_stage_counts",
    "failure_code_counts",
]


def canonical_role(role: object) -> str:
    value = str(role or "").strip()
    if value in PRIMARY_CONTROLLED_ROLES:
        return "primary_controlled"
    if value in AUXILIARY_CONTROLLED_ROLES:
        return "auxiliary_controlled"
    if value in BACKGROUND_TM_ROLES:
        return "background_tm"
    return value or "unknown"


def is_large_vehicle_type(vehicle_type: object) -> bool:
    text = str(vehicle_type or "").lower()
    return any(token in text for token in LARGE_VEHICLE_HINTS)
