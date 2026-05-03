from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


PRIMARY_CONTROLLED_ROLES = {"primary", "primary_controlled", "main", "main_occluder"}
AUXILIARY_CONTROLLED_ROLES = {"context", "target", "auxiliary_controlled"}
BACKGROUND_TM_ROLES = {"background", "background_tm", "ambient"}


@dataclass
class VehiclePlan:
    role: str
    route_id: str
    vehicle_type: str
    speed_diff: float
    start_delay_s: float
    ignore_lights: bool = True
    required: bool = True
    control_mode: str = "traffic_manager_route"


@dataclass
class TrafficPlan:
    plan_id: str
    scene_id: str
    target_tx_id: str
    scenario_type: str
    duration_s: float
    fps: float
    vehicle_count: int
    vehicles: list[VehiclePlan]
    expected_core_visit_count: int
    expected_tx_corridor_hits: dict[str, int]
    expected_min_tx_clearance_m: float
    collision_risk: float
    expected_metrics: dict[str, Any] = field(default_factory=dict)
    background_tm: dict[str, Any] = field(default_factory=dict)
    bucket: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        row = asdict(self)
        row["schema"] = "traffic_plan_v2"
        return row

    def to_carla_plan(self) -> dict[str, Any]:
        return {
            "schema": "carla_offline_traffic_plan_v2",
            "plan_id": self.plan_id,
            "scene_id": self.scene_id,
            "tx_id": self.target_tx_id,
            "target_tx_id": self.target_tx_id,
            "route_ids": [vehicle.route_id for vehicle in self.vehicles],
            "vehicle_role_policy": {
                "controlled_vehicle_roles": ["primary_controlled", "auxiliary_controlled"],
                "background_role": "background_tm",
                "background_tm_route_matching": "disabled",
            },
            "vehicles": [
                {
                    "plan_index": index,
                    "role": canonical_vehicle_role(vehicle.role),
                    "route_id": vehicle.route_id,
                    "vehicle_type": vehicle.vehicle_type,
                    "start_delay_s": float(vehicle.start_delay_s),
                    "speed_diff": float(vehicle.speed_diff),
                    "ignore_lights": bool(vehicle.ignore_lights),
                    "required": bool(vehicle.required),
                    "control_mode": vehicle.control_mode,
                }
                for index, vehicle in enumerate(self.vehicles)
            ],
            "background_tm": dict(self.background_tm),
            "expected_metrics": self.expected_metrics,
            "bucket": dict(self.bucket),
        }


def traffic_plan_from_dict(row: dict[str, Any]) -> TrafficPlan:
    vehicles = [_vehicle_plan_from_dict(vehicle) for vehicle in row["vehicles"]]
    return TrafficPlan(
        plan_id=str(row["plan_id"]),
        scene_id=str(row["scene_id"]),
        target_tx_id=str(row["target_tx_id"]),
        scenario_type=str(row["scenario_type"]),
        duration_s=float(row["duration_s"]),
        fps=float(row["fps"]),
        vehicle_count=int(row["vehicle_count"]),
        vehicles=vehicles,
        expected_core_visit_count=int(row["expected_core_visit_count"]),
        expected_tx_corridor_hits=dict(row["expected_tx_corridor_hits"]),
        expected_min_tx_clearance_m=float(row["expected_min_tx_clearance_m"]),
        collision_risk=float(row["collision_risk"]),
        expected_metrics=dict(row.get("expected_metrics", {})),
        background_tm=dict(row.get("background_tm", {})),
        bucket=dict(row.get("bucket", {})),
    )


def _vehicle_plan_from_dict(row: dict[str, Any]) -> VehiclePlan:
    return VehiclePlan(
        role=canonical_vehicle_role(str(row["role"])),
        route_id=str(row["route_id"]),
        vehicle_type=str(row["vehicle_type"]),
        speed_diff=float(row["speed_diff"]),
        start_delay_s=float(row["start_delay_s"]),
        ignore_lights=bool(row.get("ignore_lights", True)),
        required=bool(row.get("required", role_is_required(row.get("role")))),
        control_mode=str(row.get("control_mode", "traffic_manager_route")),
    )


def canonical_vehicle_role(role: object) -> str:
    value = str(role or "").strip()
    if value in PRIMARY_CONTROLLED_ROLES:
        return "primary_controlled"
    if value in AUXILIARY_CONTROLLED_ROLES:
        return "auxiliary_controlled"
    if value in BACKGROUND_TM_ROLES:
        return "background_tm"
    return value or "auxiliary_controlled"


def role_is_required(role: object) -> bool:
    return canonical_vehicle_role(role) == "primary_controlled"
