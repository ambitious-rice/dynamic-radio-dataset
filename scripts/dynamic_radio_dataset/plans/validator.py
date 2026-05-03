from __future__ import annotations

from dynamic_radio_dataset.plans.schemas import TrafficPlan, canonical_vehicle_role


def validate_plan(plan: TrafficPlan, config: dict) -> tuple[bool, str | None, float]:
    plans_cfg = config["plans"]
    min_count, max_count = config["traffic"]["vehicle_count_range"]
    if not (int(min_count) <= plan.vehicle_count <= int(max_count)):
        return False, "vehicle_count_out_of_range", 0.0
    large_count = _planned_large_vehicle_count(plan)
    if large_count > int(config["traffic"].get("max_large_vehicle_count", 1)):
        return False, "too_many_large_vehicles", 0.0
    primary = [vehicle for vehicle in plan.vehicles if canonical_vehicle_role(vehicle.role) == "primary_controlled"]
    if not primary:
        return False, "missing_primary_vehicle", 0.0
    expected_primary_count = int(plans_cfg.get("primary_controlled_count", plans_cfg.get("primary_count", len(primary))))
    if len(primary) < min(expected_primary_count, plan.vehicle_count):
        return False, "insufficient_primary_controlled_routes", 0.0
    requested_vehicle_count = int(plan.expected_metrics.get("requested_vehicle_count", plan.vehicle_count))
    requested_background_count = int(plan.expected_metrics.get("requested_background_count", 0))
    if len(plan.vehicles) + requested_background_count != requested_vehicle_count:
        return False, "vehicle_role_count_mismatch", 0.0
    background_range = plan.background_tm.get("count_range", [0, plans_cfg.get("background_tm_max_count", 4)])
    if requested_background_count < int(background_range[0]) or requested_background_count > int(background_range[1]):
        return False, "background_count_out_of_range", 0.0
    if _background_vehicle_type_count(plan) not in {0, requested_background_count}:
        return False, "background_vehicle_type_list_length_mismatch", 0.0
    spawn_conflicts = plan.expected_metrics.get("spawn_start_conflict_summary", {})
    if isinstance(spawn_conflicts, dict):
        if spawn_conflicts.get("missing_geometry"):
            return False, "missing_route_spawn_geometry", 0.0
        if int(spawn_conflicts.get("conflict_count", 0)) > 0:
            return False, "spawn_start_conflict", 0.0
    if not _has_primary_expected_motion(plan, config):
        return False, "primary_expected_motion_too_short", 0.0
    if plan.collision_risk > float(plans_cfg.get("max_extreme_collision_risk", 8.0)):
        return False, "extreme_conflict_risk_too_high", 0.0
    return True, None, _score(plan)


def _planned_large_vehicle_count(plan: TrafficPlan) -> int:
    controlled = sum("fusorosa" in vehicle.vehicle_type.lower() for vehicle in plan.vehicles)
    background_types = plan.background_tm.get("vehicle_types", []) if isinstance(plan.background_tm, dict) else []
    background = sum("fusorosa" in str(item).lower() for item in background_types)
    return int(controlled + background)


def _background_vehicle_type_count(plan: TrafficPlan) -> int:
    if not isinstance(plan.background_tm, dict):
        return 0
    vehicle_types = plan.background_tm.get("vehicle_types", [])
    return len(vehicle_types) if isinstance(vehicle_types, list) else 0


def _has_primary_expected_motion(plan: TrafficPlan, config: dict) -> bool:
    threshold = float(config.get("qa", {}).get("min_main_displacement_m", 12.0))
    primary_motion = plan.expected_metrics.get("primary_expected_motion_m", {})
    return any(float(value) >= threshold for value in primary_motion.values())


def _score(plan: TrafficPlan) -> float:
    return (
        100.0
        + 1.5 * plan.vehicle_count
        + 0.5 * int(plan.expected_metrics.get("requested_background_count", 0))
        - 10.0 * plan.collision_risk
    )
