from __future__ import annotations

import random
from collections import Counter
from typing import Any

from dynamic_radio_dataset.json_utils import load_json
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.plans.catalog import write_plan_catalog
from dynamic_radio_dataset.plans.schemas import TrafficPlan, VehiclePlan, canonical_vehicle_role
from dynamic_radio_dataset.plans.validator import validate_plan


SMALL_VEHICLES = [
    "vehicle.mini.cooper_s",
    "vehicle.nissan.micra",
    "vehicle.tesla.model3",
    "vehicle.chevrolet.impala",
    "vehicle.seat.leon",
]
LARGE_VEHICLE = "vehicle.mitsubishi.fusorosa"
SCENARIO_TYPES = [
    "single_primary_pass_tx",
    "two_vehicle_cross",
    "primary_near_tx_context_far",
    "primary_turning_near_tx",
    "dense_but_clean",
    "tx_occlusion_candidate",
    "mixed_vehicle_type",
]


def generate_plan_bank(config: dict, num_plans: int | None = None) -> dict:
    root = dataset_root(config)
    route_dir = root / "route_library"
    features = load_json(route_dir / "route_features.json")["features"]
    conflicts = load_json(route_dir / "route_conflicts.json")["conflicts"]
    tx_catalog = load_json(root / "scene_static" / "tx_catalog.json")["tx_catalog"]
    target_count = int(num_plans or config["plans"]["num_plans"])
    rng = random.Random(int(config["plans"].get("seed", 17)))
    tx_ids = [str(tx["tx_id"]) for tx in tx_catalog]
    vehicle_count_targets = _vehicle_count_targets(config["traffic"]["vehicle_count_mix"], target_count)
    accepted: list[TrafficPlan] = []
    rejected: list[dict[str, Any]] = []
    attempts = 0
    max_attempts = target_count * int(config["plans"].get("candidate_multiplier", 8))
    while len(accepted) < target_count and attempts < max_attempts:
        attempts += 1
        target_tx_id = tx_ids[len(accepted) % len(tx_ids)]
        vehicle_count = _choose_vehicle_count(vehicle_count_targets, accepted, config["traffic"]["vehicle_count_mix"], rng)
        large_vehicle_count = _choose_large_vehicle_count(
            config,
            vehicle_count=vehicle_count,
            bucket_target=max(1, vehicle_count_targets.get(vehicle_count, target_count)),
            accepted=accepted,
            rng=rng,
        )
        plan = _sample_plan(
            config,
            features,
            conflicts,
            target_tx_id,
            len(accepted),
            rng,
            vehicle_count=vehicle_count,
            target_large_vehicle_count=large_vehicle_count,
        )
        ok, reason, score = validate_plan(plan, config)
        if ok:
            plan.expected_metrics["preflight_score"] = score
            accepted.append(plan)
        else:
            row = plan.to_dict()
            row["failure_code"] = reason
            rejected.append(row)
    return write_plan_catalog(root / "plan_catalog", accepted, rejected)


def _sample_plan(
    config: dict,
    features: list[dict],
    conflicts: list[dict],
    target_tx_id: str,
    index: int,
    rng: random.Random,
    vehicle_count: int | None = None,
    target_large_vehicle_count: int | None = None,
) -> TrafficPlan:
    traffic = config["traffic"]
    plans_cfg = config["plans"]
    requested_vehicle_count = int(vehicle_count if vehicle_count is not None else _weighted_vehicle_count(traffic["vehicle_count_mix"], rng))
    requested_large_vehicle_count = int(
        target_large_vehicle_count
        if target_large_vehicle_count is not None
        else _weighted_large_vehicle_count(config, requested_vehicle_count, rng)
    )
    scenario_type = SCENARIO_TYPES[index % len(SCENARIO_TYPES)]
    primary_count = min(max(1, int(plans_cfg.get("primary_controlled_count", plans_cfg.get("primary_count", 2)))), requested_vehicle_count)
    requested_auxiliary_count = max(0, int(plans_cfg.get("auxiliary_controlled_count", 1)))
    auxiliary_count = min(requested_auxiliary_count, max(0, requested_vehicle_count - primary_count))
    requested_background_count = max(0, requested_vehicle_count - primary_count - auxiliary_count)
    primary_routes = _pick_primary_routes(features, target_tx_id, primary_count, rng, config)
    auxiliary_routes = _pick_auxiliary_routes(features, primary_routes, auxiliary_count, rng, config)
    route_rows = primary_routes + auxiliary_routes
    controlled_large_count = _controlled_large_vehicle_count(config, requested_large_vehicle_count, len(route_rows))
    large_controlled_indexes = _large_controlled_indexes(controlled_large_count, len(primary_routes), len(route_rows))
    vehicles = [
        _vehicle_for_route(
            config,
            route_row,
            idx,
            idx < len(primary_routes),
            rng,
            use_large=idx in large_controlled_indexes,
        )
        for idx, route_row in enumerate(route_rows)
    ]
    planned_controlled_large_count = sum(_is_large_vehicle_type(vehicle.vehicle_type) for vehicle in vehicles)
    planned_background_large_count = max(0, requested_large_vehicle_count - planned_controlled_large_count)
    metrics = _expected_metrics(config, route_rows, vehicles, target_tx_id, conflicts)
    background_tm = _background_tm_spec(config, requested_background_count, planned_background_large_count, rng)
    planned_large_count = planned_controlled_large_count + int(background_tm.get("planned_large_vehicle_count", 0))
    bucket = _plan_bucket(config, requested_vehicle_count, requested_large_vehicle_count)
    metrics["requested_vehicle_count"] = requested_vehicle_count
    metrics["requested_total_vehicle_count"] = requested_vehicle_count
    metrics["controlled_vehicle_count"] = len(vehicles)
    metrics["requested_background_count"] = requested_background_count
    metrics["vehicle_role_counts"] = _planned_role_counts(vehicles, requested_background_count)
    metrics["target_large_vehicle_count"] = requested_large_vehicle_count
    metrics["planned_large_vehicle_count"] = int(planned_large_count)
    metrics["planned_controlled_large_vehicle_count"] = int(planned_controlled_large_count)
    metrics["planned_background_large_vehicle_count"] = int(background_tm.get("planned_large_vehicle_count", 0))
    metrics["bucket"] = bucket
    return TrafficPlan(
        plan_id=f"plan_{index:06d}",
        scene_id=str(config["scene"].get("scene_id", "town10_junction")),
        target_tx_id=target_tx_id,
        scenario_type=scenario_type,
        duration_s=float(traffic["duration_s"]),
        fps=float(traffic["fps"]),
        vehicle_count=requested_vehicle_count,
        vehicles=vehicles,
        expected_core_visit_count=int(metrics["expected_core_visit_count"]),
        expected_tx_corridor_hits=dict(metrics["expected_tx_corridor_hits"]),
        expected_min_tx_clearance_m=float(metrics["expected_min_tx_clearance_m"]),
        collision_risk=float(metrics["collision_risk"]),
        expected_metrics=metrics,
        background_tm=background_tm,
        bucket=bucket,
    )


def _weighted_vehicle_count(mix: dict, rng: random.Random) -> int:
    items = [(int(k), float(v)) for k, v in mix.items()]
    total = sum(weight for _, weight in items)
    point = rng.random() * total
    running = 0.0
    for count, weight in items:
        running += weight
        if point <= running:
            return count
    return items[-1][0]


def _vehicle_count_targets(mix: dict, target_count: int) -> dict[int, int]:
    items = [(int(k), float(v)) for k, v in mix.items()]
    total_weight = sum(weight for _, weight in items)
    if total_weight <= 0:
        return {count: 0 for count, _ in items}
    raw = {count: target_count * weight / total_weight for count, weight in items}
    targets = {count: int(value) for count, value in raw.items()}
    remainder = int(target_count - sum(targets.values()))
    for count, _ in sorted(items, key=lambda item: raw[item[0]] - targets[item[0]], reverse=True)[:remainder]:
        targets[count] += 1
    return targets


def _choose_vehicle_count(
    targets: dict[int, int],
    accepted: list[TrafficPlan],
    mix: dict,
    rng: random.Random,
) -> int:
    observed = Counter(int(plan.vehicle_count) for plan in accepted)
    return _choose_quota_value(targets, observed, mix, rng)


def _choose_large_vehicle_count(
    config: dict,
    *,
    vehicle_count: int,
    bucket_target: int,
    accepted: list[TrafficPlan],
    rng: random.Random,
) -> int:
    mix = _large_vehicle_count_mix(config, vehicle_count)
    targets = _vehicle_count_targets(mix, bucket_target)
    observed = Counter(
        int(plan.expected_metrics.get("target_large_vehicle_count", _planned_large_vehicle_count(plan)))
        for plan in accepted
        if int(plan.vehicle_count) == int(vehicle_count)
    )
    return _choose_quota_value(targets, observed, mix, rng)


def _choose_quota_value(
    targets: dict[int, int],
    observed: Counter[int],
    fallback_mix: dict,
    rng: random.Random,
) -> int:
    deficits = {count: target - observed[count] for count, target in targets.items() if target > observed[count]}
    if not deficits:
        return _weighted_vehicle_count(fallback_mix, rng)
    max_deficit = max(deficits.values())
    return int(rng.choice([count for count, deficit in sorted(deficits.items()) if deficit == max_deficit]))


def _weighted_large_vehicle_count(config: dict, vehicle_count: int, rng: random.Random) -> int:
    return _weighted_vehicle_count(_large_vehicle_count_mix(config, vehicle_count), rng)


def _large_vehicle_count_mix(config: dict, vehicle_count: int) -> dict[int, float]:
    plans_cfg = config.get("plans", {})
    nested = plans_cfg.get("large_vehicle_count_mix_by_vehicle_count", {})
    if isinstance(nested, dict):
        value = nested.get(vehicle_count, nested.get(str(vehicle_count)))
        if isinstance(value, dict) and value:
            return {int(count): float(weight) for count, weight in value.items()}
    max_large = min(int(config.get("traffic", {}).get("max_large_vehicle_count", 1)), int(vehicle_count))
    allowed_max = min(max_large, 2 if int(vehicle_count) < 6 else max_large)
    return {count: 1.0 for count in range(0, allowed_max + 1)}


def _planned_large_vehicle_count(plan: TrafficPlan) -> int:
    controlled = sum(_is_large_vehicle_type(vehicle.vehicle_type) for vehicle in plan.vehicles)
    background = 0
    if isinstance(plan.background_tm, dict):
        background = sum(_is_large_vehicle_type(str(item)) for item in plan.background_tm.get("vehicle_types", []))
    return int(controlled + background)


def _pick_primary_routes(features: list[dict], tx_id: str, count: int, rng: random.Random, config: dict) -> list[dict]:
    del tx_id
    candidates = [
        row
        for row in features
        if float(row.get("route_length_m", 0.0)) > 0.0
    ]
    candidates.sort(key=lambda row: str(row["route_id"]))
    return _sample_routes_with_spawn_spacing(candidates, count, rng, [], _min_spawn_start_separation_m(config))


def _pick_auxiliary_routes(features: list[dict], selected_routes: list[dict], count: int, rng: random.Random, config: dict) -> list[dict]:
    used_route_ids = {str(row["route_id"]) for row in selected_routes}
    candidates = [
        row
        for row in features
        if str(row["route_id"]) not in used_route_ids
        and float(row.get("route_length_m", 0.0)) > 0.0
    ]
    candidates.sort(key=lambda row: str(row["route_id"]))
    if count <= 0:
        return []
    return _sample_routes_with_spawn_spacing(candidates, count, rng, selected_routes, _min_spawn_start_separation_m(config))


def _vehicle_for_route(
    config: dict,
    route: dict,
    index: int,
    primary: bool,
    rng: random.Random,
    *,
    use_large: bool = False,
) -> VehiclePlan:
    role = "primary_controlled" if primary else "auxiliary_controlled"
    vehicle_type = (
        LARGE_VEHICLE
        if bool(use_large)
        else SMALL_VEHICLES[(index + rng.randint(0, len(SMALL_VEHICLES) - 1)) % len(SMALL_VEHICLES)]
    )
    delays = config["plans"]["primary_start_delay_s"] if primary else config["plans"]["context_start_delay_s"]
    speed_options = config["plans"]["primary_speed_diff"] if primary else config["plans"]["context_speed_diff"]
    return VehiclePlan(
        role=role,
        route_id=str(route["route_id"]),
        vehicle_type=vehicle_type,
        speed_diff=float(rng.choice(speed_options)),
        start_delay_s=float(rng.choice(delays)),
        ignore_lights=bool(config["traffic"].get("primary_ignore_lights", True) if primary else config["traffic"].get("context_ignore_lights", True)),
        required=bool(primary or config["plans"].get("auxiliary_controlled_required", False)),
    )


def _expected_metrics(config: dict, route_rows: list[dict], vehicles: list[VehiclePlan], target_tx_id: str, conflicts: list[dict]) -> dict:
    by_id = {row["route_id"]: row for row in route_rows}
    primary_vehicles = [vehicle for vehicle in vehicles if canonical_vehicle_role(vehicle.role) == "primary_controlled"]
    target_window = list(config["plans"].get("target_event_window_s", [2.0, 6.0]))
    tx_ids = sorted(route_rows[0]["hits_tx_corridor"].keys()) if route_rows else [target_tx_id]
    target_hits = {
        tx_id: sum(1 for row in route_rows if row["hits_tx_corridor"].get(tx_id, False))
        for tx_id in tx_ids
    }
    core_visits = sum(1 for vehicle in primary_vehicles if by_id[vehicle.route_id].get("estimated_core_entry_s") is not None)
    min_clearance = min(
        float(distance)
        for row in route_rows
        for distance in row["min_distance_to_tx_m"].values()
    )
    primary_event_times = {
        f"{vehicle.route_id}:{idx}": _event_time(by_id[vehicle.route_id], target_tx_id, vehicle.start_delay_s)
        for idx, vehicle in enumerate(primary_vehicles)
        if _event_time(by_id[vehicle.route_id], target_tx_id, vehicle.start_delay_s) is not None
    }
    primary_motion = {
        f"{vehicle.route_id}:{idx}": _expected_motion_m(config, by_id[vehicle.route_id], vehicle)
        for idx, vehicle in enumerate(primary_vehicles)
    }
    risk_summary = _collision_risk_summary(config, vehicles, conflicts)
    spawn_start_summary = _spawn_start_conflict_summary(config, route_rows, vehicles)
    return {
        "expected_core_visit_count": core_visits,
        "expected_tx_corridor_hits": target_hits,
        "expected_min_tx_clearance_m": min_clearance,
        "collision_risk": float(risk_summary["extreme_pair_risk"]),
        "collision_risk_summary": risk_summary,
        "spawn_start_conflict_summary": spawn_start_summary,
        "primary_event_times_s": primary_event_times,
        "primary_expected_motion_m": primary_motion,
        "target_event_window_s": target_window,
        "geometry_heuristics_policy": (
            "core/corridor/clearance are diagnostics only; preflight decisions use vehicle count, "
            "required primary expected motion, controlled vehicle mix, and extreme timing-aware controlled-route conflict risk"
        ),
    }


def _background_tm_spec(config: dict, requested_count: int, large_vehicle_count: int, rng: random.Random) -> dict[str, Any]:
    traffic = config.get("traffic", {})
    plans_cfg = config.get("plans", {})
    vehicle_types = _background_vehicle_type_list(config, requested_count, large_vehicle_count, rng)
    return {
        "role": "background_tm",
        "control_mode": "carla_traffic_manager",
        "requested_count": int(requested_count),
        "count_range": [0, int(plans_cfg.get("background_tm_max_count", 4))],
        "vehicle_types": vehicle_types,
        "vehicle_type_policy": "exact_ordered_list",
        "planned_large_vehicle_count": int(sum(_is_large_vehicle_type(item) for item in vehicle_types)),
        "spawn_region": str(plans_cfg.get("background_spawn_region", "support_region")),
        "min_distance_to_controlled_spawn_m": float(plans_cfg.get("background_min_distance_to_controlled_spawn_m", 6.0)),
        "max_distance_to_controlled_route_m": float(plans_cfg.get("background_max_distance_to_controlled_route_m", 45.0)),
        "obey_traffic_lights": bool(traffic.get("background_obey_lights", True)),
        "speed_diff": float(traffic.get("background_speed_diff", 0.0)),
        "speed_diff_jitter": list(plans_cfg.get("background_speed_diff_jitter", [-10.0, 15.0])),
    }


def _background_vehicle_type_list(config: dict, requested_count: int, large_vehicle_count: int, rng: random.Random) -> list[str]:
    plans_cfg = config.get("plans", {})
    requested = max(0, int(requested_count))
    large_count = min(max(0, int(large_vehicle_count)), requested)
    small_types = [
        str(item)
        for item in plans_cfg.get("background_vehicle_types", SMALL_VEHICLES)
        if str(item).strip() and not _is_large_vehicle_type(str(item))
    ] or list(SMALL_VEHICLES)
    large_type = str(plans_cfg.get("large_vehicle_type", LARGE_VEHICLE))
    vehicle_types = [large_type] * large_count
    while len(vehicle_types) < requested:
        vehicle_types.append(str(rng.choice(small_types)))
    rng.shuffle(vehicle_types)
    return vehicle_types


def _controlled_large_vehicle_count(config: dict, target_large_count: int, controlled_count: int) -> int:
    if target_large_count <= 0 or controlled_count <= 0:
        return 0
    max_controlled = int(config.get("plans", {}).get("max_controlled_large_vehicle_count", 1))
    return int(min(target_large_count, max_controlled, controlled_count))


def _large_controlled_indexes(large_count: int, primary_count: int, controlled_count: int) -> set[int]:
    if large_count <= 0:
        return set()
    preferred = [0]
    if primary_count > 1:
        preferred.extend(range(1, primary_count))
    preferred.extend(range(primary_count, controlled_count))
    return set(preferred[:large_count])


def _plan_bucket(config: dict, vehicle_count: int, target_large_vehicle_count: int) -> dict[str, Any]:
    order = [
        int(item)
        for item in config.get("collection", {}).get(
            "bucket_order",
            sorted(int(key) for key in config.get("traffic", {}).get("vehicle_count_mix", {}).keys()),
        )
    ]
    bucket_order = order.index(int(vehicle_count)) if int(vehicle_count) in order else len(order)
    return {
        "vehicle_count": int(vehicle_count),
        "target_large_vehicle_count": int(target_large_vehicle_count),
        "bucket_order": int(bucket_order),
        "bucket_key": f"vehicle_count_{int(vehicle_count)}",
    }


def _is_large_vehicle_type(vehicle_type: str) -> bool:
    return "fusorosa" in str(vehicle_type).lower()


def _planned_role_counts(vehicles: list[VehiclePlan], requested_background_count: int) -> dict[str, int]:
    required_controlled = 0
    optional_controlled = 0
    primary_controlled = 0
    auxiliary_controlled = 0
    for vehicle in vehicles:
        role = canonical_vehicle_role(vehicle.role)
        if role == "primary_controlled":
            primary_controlled += 1
        if role == "auxiliary_controlled":
            auxiliary_controlled += 1
        if bool(vehicle.required):
            required_controlled += 1
        else:
            optional_controlled += 1
    return {
        "planned_required_controlled_count": int(required_controlled),
        "planned_optional_controlled_count": int(optional_controlled),
        "planned_primary_controlled_count": int(primary_controlled),
        "planned_auxiliary_controlled_count": int(auxiliary_controlled),
        "requested_background_count": int(requested_background_count),
        "requested_total_vehicle_count": int(required_controlled + optional_controlled + requested_background_count),
    }


def _sample_routes_with_spawn_spacing(
    candidates: list[dict],
    count: int,
    rng: random.Random,
    existing: list[dict],
    min_separation_m: float,
) -> list[dict]:
    shuffled = list(candidates)
    rng.shuffle(shuffled)
    selected: list[dict] = []
    for row in shuffled:
        if _route_start_is_clear(row, existing + selected, min_separation_m):
            selected.append(row)
        if len(selected) >= count:
            break
    return selected


def _route_start_is_clear(candidate: dict, selected: list[dict], min_separation_m: float) -> bool:
    candidate_xy = _route_start_xy(candidate)
    if candidate_xy is None:
        return False
    for row in selected:
        other_xy = _route_start_xy(row)
        if other_xy is None:
            return False
        dx = candidate_xy[0] - other_xy[0]
        dy = candidate_xy[1] - other_xy[1]
        if (dx * dx + dy * dy) ** 0.5 < min_separation_m:
            return False
    return True


def _spawn_start_conflict_summary(config: dict, route_rows: list[dict], vehicles: list[VehiclePlan]) -> dict[str, Any]:
    min_separation_m = _min_spawn_start_separation_m(config)
    rows: list[dict[str, Any]] = []
    missing_geometry: list[dict[str, Any]] = []
    min_distance_m: float | None = None
    for idx, (route_row, vehicle) in enumerate(zip(route_rows, vehicles)):
        xy = _route_start_xy(route_row)
        if xy is None:
            missing_geometry.append({"plan_index": idx, "route_id": vehicle.route_id})
        rows.append(
            {
                "plan_index": idx,
                "role": vehicle.role,
                "route_id": vehicle.route_id,
                "vehicle_type": vehicle.vehicle_type,
                "start_xy": None if xy is None else [float(xy[0]), float(xy[1])],
            }
        )
    conflicts: list[dict[str, Any]] = []
    for idx_a, row_a in enumerate(rows):
        xy_a = row_a["start_xy"]
        if xy_a is None:
            continue
        for idx_b in range(idx_a + 1, len(rows)):
            row_b = rows[idx_b]
            xy_b = row_b["start_xy"]
            if xy_b is None:
                continue
            distance_m = float(((xy_a[0] - xy_b[0]) ** 2 + (xy_a[1] - xy_b[1]) ** 2) ** 0.5)
            min_distance_m = distance_m if min_distance_m is None else min(min_distance_m, distance_m)
            if distance_m < min_separation_m:
                conflicts.append(
                    {
                        "plan_index_a": row_a["plan_index"],
                        "route_a": row_a["route_id"],
                        "role_a": row_a["role"],
                        "plan_index_b": row_b["plan_index"],
                        "route_b": row_b["route_id"],
                        "role_b": row_b["role"],
                        "start_distance_m": distance_m,
                    }
                )
    return {
        "policy": "controlled TrafficPlan actors are spawned before recording; controlled route starts must not overlap",
        "min_spawn_start_separation_m": float(min_separation_m),
        "min_observed_start_distance_m": min_distance_m,
        "conflict_count": len(conflicts),
        "conflicts": conflicts,
        "missing_geometry": missing_geometry,
    }


def _route_start_xy(route_row: dict) -> tuple[float, float] | None:
    value = route_row.get("spawn_start_xy")
    if isinstance(value, list) and len(value) >= 2:
        return float(value[0]), float(value[1])
    polyline = route_row.get("polyline", [])
    if isinstance(polyline, list) and polyline:
        first = polyline[0]
        if isinstance(first, dict) and "x" in first and "y" in first:
            return float(first["x"]), float(first["y"])
        if isinstance(first, (list, tuple)) and len(first) >= 2:
            return float(first[0]), float(first[1])
    return None


def _min_spawn_start_separation_m(config: dict) -> float:
    return float(config.get("plans", {}).get("min_spawn_start_separation_m", 1.0))


def _event_time(row: dict, tx_id: str, start_delay_s: float = 0.0) -> float | None:
    values = [row.get("estimated_core_entry_s"), row["estimated_tx_corridor_entry_s"].get(tx_id)]
    finite = [float(value) for value in values if value is not None]
    return min(finite) + float(start_delay_s) if finite else None


def _expected_motion_m(config: dict, route_row: dict, vehicle: VehiclePlan) -> float:
    duration_s = float(config["traffic"]["duration_s"])
    preroll_s = float(config["traffic"].get("traffic_preroll_s", 2.0))
    speed_mps = _vehicle_speed_mps(config, vehicle)
    available_s = max(0.0, duration_s + preroll_s - float(vehicle.start_delay_s))
    return float(min(float(route_row.get("route_length_m", 0.0)), speed_mps * available_s))


def _vehicle_speed_mps(config: dict, vehicle: VehiclePlan) -> float:
    base = float(config["traffic"].get("estimated_speed_mps", 6.8))
    # CARLA TrafficManager percentage_speed_difference: negative means faster.
    scale = max(0.1, 1.0 - float(vehicle.speed_diff) / 100.0)
    return float(base * scale)


def _collision_risk_summary(config: dict, vehicles: list[VehiclePlan], conflicts: list[dict]) -> dict[str, Any]:
    selected = {vehicle.route_id: vehicle for vehicle in vehicles}
    time_window_s = float(config["plans"].get("conflict_time_window_s", 1.5))
    pair_rows: list[dict[str, Any]] = []
    for row in conflicts:
        route_a = str(row["route_a"])
        route_b = str(row["route_b"])
        if route_a not in selected or route_b not in selected:
            continue
        vehicle_a = selected[route_a]
        vehicle_b = selected[route_b]
        time_a = float(row.get("distance_a_m", 0.0)) / max(_vehicle_speed_mps(config, vehicle_a), 1e-3) + float(vehicle_a.start_delay_s)
        time_b = float(row.get("distance_b_m", 0.0)) / max(_vehicle_speed_mps(config, vehicle_b), 1e-3) + float(vehicle_b.start_delay_s)
        time_gap = abs(time_a - time_b)
        if time_gap >= time_window_s:
            continue
        pair_risk = float(row.get("conflict_risk", 0.0)) * (1.0 - time_gap / max(time_window_s, 1e-3))
        pair_rows.append(
            {
                "route_a": route_a,
                "route_b": route_b,
                "time_gap_s": float(time_gap),
                "pair_risk": float(pair_risk),
                "min_distance_m": float(row.get("min_distance_m", 0.0)),
            }
        )
    pair_rows.sort(key=lambda item: float(item["pair_risk"]), reverse=True)
    top_rows = pair_rows[:3]
    return {
        "policy": "max_timing_aware_pair_risk_not_pair_count_sum",
        "active_conflict_pair_count": len(pair_rows),
        "extreme_pair_risk": float(top_rows[0]["pair_risk"]) if top_rows else 0.0,
        "sum_pair_risk_diagnostic": float(sum(float(item["pair_risk"]) for item in pair_rows)),
        "top_conflict_pairs": top_rows,
    }
