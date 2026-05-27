#!/usr/bin/env python3
"""
Collect a CARLA dynamic scene for future radio-map reconstruction work.

The script intentionally stops at CARLA-side scene/trajectory/state export:
it does not run Sionna RT or generate radio labels.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import queue
import random
import shutil
import socket
import struct
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple


def bootstrap_carla_api() -> Path:
    """Make the CARLA egg and PythonAPI agents importable from this repo."""
    repo_root = _find_repo_root(Path(__file__).resolve())
    egg_glob = str(repo_root / "PythonAPI" / "carla" / "dist" / "carla-*-py3.*-linux-x86_64.egg")
    eggs = sorted(glob.glob(egg_glob))
    if eggs:
        sys.path.insert(0, eggs[-1])
    sys.path.insert(0, str(repo_root / "PythonAPI" / "carla"))
    return repo_root


def _find_repo_root(path: Path) -> Path:
    for parent in [path.parent, *path.parents]:
        if (parent / "CarlaUE4").exists() and (parent / "PythonAPI").exists():
            return parent
    raise RuntimeError(f"Could not locate CARLA repo root from {path}")


REPO_ROOT = bootstrap_carla_api()

import carla  # noqa: E402
from dynamic_radio_dataset.plans.schemas import canonical_vehicle_role, role_is_required  # noqa: E402

try:  # noqa: E402
    from agents.navigation.global_route_planner import GlobalRoutePlanner  # type: ignore

    GLOBAL_ROUTE_PLANNER_IMPORT_ERROR = None
except Exception as exc:  # noqa: BLE001
    GlobalRoutePlanner = None  # type: ignore
    GLOBAL_ROUTE_PLANNER_IMPORT_ERROR = exc


class TrafficPlanSpawnIncomplete(RuntimeError):
    """Raised when CARLA cannot realize every required controlled vehicle in a TrafficPlan."""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Collect a dynamic CARLA scene with verified crossing routes and support/valid crops."
    )
    parser.add_argument("--host", type=str, default="localhost")
    parser.add_argument("--port", type=int, default=2000)
    parser.add_argument(
        "--traffic-manager-port",
        type=int,
        default=None,
        help="Optional Traffic Manager RPC port; use distinct ports when running multiple CARLA servers.",
    )
    parser.add_argument("--town", type=str, default="", help="e.g. Town03. Leave empty to keep current world.")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--frames", type=int, default=240)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument(
        "--traffic-preroll-s",
        type=float,
        default=0.0,
        help=(
            "Advance traffic for this many seconds before recorded frame 0. "
            "Useful for formal 8s label windows where vehicles should already be flowing."
        ),
    )
    parser.add_argument("--scene-mode", choices=["junction", "corridor"], default="junction")
    parser.add_argument("--scene-id", type=str, default="")
    parser.add_argument("--scene-type", type=str, default="")
    parser.add_argument("--scene-junction-id", type=int, default=None)
    parser.add_argument("--scene-corridor-index", type=int, default=None)
    parser.add_argument("--valid-size", type=float, default=64.0, help="Square valid crop size in meters.")
    parser.add_argument("--support-size", type=float, default=112.0, help="Square support region size in meters.")
    parser.add_argument("--route-step", type=float, default=2.0)
    parser.add_argument("--route-approach", type=float, default=34.0)
    parser.add_argument("--route-exit", type=float, default=34.0)
    parser.add_argument("--min-approach", type=float, default=18.0)
    parser.add_argument("--min-exit", type=float, default=18.0)
    parser.add_argument("--junction-core-radius", type=float, default=12.0)
    parser.add_argument("--num-target-vehicles", type=int, default=4)
    parser.add_argument("--num-background-vehicles", type=int, default=16)
    parser.add_argument(
        "--min-passed-targets",
        type=int,
        default=2,
        help="Legacy diagnostic threshold only; formal plan-first acceptance uses trajectory QA.",
    )
    parser.add_argument(
        "--min-frames-after-core",
        type=int,
        default=3,
        help="Legacy diagnostic threshold for validation_report only.",
    )
    parser.add_argument(
        "--core-exit-buffer-m",
        type=float,
        default=6.0,
        help="Legacy diagnostic threshold for validation_report only.",
    )
    parser.add_argument(
        "--min-target-displacement-m",
        type=float,
        default=12.0,
        help="Legacy diagnostic threshold for validation_report only.",
    )
    parser.add_argument("--target-speed-diff", type=float, default=-20.0)
    parser.add_argument("--background-speed-diff", type=float, default=0.0)
    parser.add_argument(
        "--target-route-ids",
        type=str,
        default="",
        help=(
            "Optional comma-separated explicit target route ids. "
            "When provided, these routes are used directly instead of randomly sampling scene routes."
        ),
    )
    parser.add_argument(
        "--traffic-plan-json",
        type=Path,
        default=None,
        help=(
            "Optional offline traffic plan JSON. When provided, target routes, vehicle types, "
            "per-vehicle speed differences, and start delays are read from this plan."
        ),
    )
    parser.add_argument("--vehicle-size-preset", choices=["mixed", "passenger"], default="mixed")
    parser.add_argument(
        "--vehicle-allowlist",
        type=str,
        default="",
        help=(
            "Optional comma-separated vehicle blueprint allowlist. "
            "Items containing a dot are matched as exact blueprint ids; other items are treated as lowercase substrings."
        ),
    )
    parser.add_argument("--vehicle-spawn-attempts", type=int, default=12)
    parser.add_argument("--vehicle-min-length", type=float, default=3.2)
    parser.add_argument("--vehicle-max-length", type=float, default=5.6)
    parser.add_argument("--vehicle-max-width", type=float, default=2.4)
    parser.add_argument("--vehicle-max-height", type=float, default=2.2)
    parser.add_argument("--target-ignore-lights", action="store_true", default=True)
    parser.add_argument("--target-obey-lights", dest="target_ignore_lights", action="store_false")
    parser.add_argument("--warmup-frames", type=int, default=20, help="Minimum synchronous ticks after spawning before recording.")
    parser.add_argument("--warmup-max-frames", type=int, default=80, help="Maximum ticks allowed for vehicle vertical settling.")
    parser.add_argument("--warmup-check-window", type=int, default=5, help="Recent-frame window used for settling checks.")
    parser.add_argument("--warmup-max-abs-vz", type=float, default=0.35, help="Settled if every tracked vehicle has abs(vertical velocity) below this.")
    parser.add_argument("--warmup-max-z-range", type=float, default=0.08, help="Settled if each tracked vehicle's z range over the check window is below this.")
    parser.add_argument("--disable-warmup-stability-check", action="store_true")
    parser.add_argument("--clear-existing-vehicles", action="store_true")
    parser.add_argument("--clear-existing-sensors", action="store_true")
    parser.add_argument("--image-width", type=int, default=720)
    parser.add_argument("--image-height", type=int, default=720)
    parser.add_argument("--camera-fov", type=float, default=70.0)
    parser.add_argument("--camera-height", type=float, default=0.0, help="0 means compute from support size and FOV.")
    parser.add_argument("--output-dir", type=str, default="./datasets/dynamic_radio_scene_demo")
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument("--no-video", action="store_true", help="Save PNG frames but skip video creation.")
    parser.add_argument(
        "--no-rendering-mode",
        action="store_true",
        help=(
            "Enable CARLA world no_rendering_mode for trajectory-only collection. "
            "Requires --no-video because RGB camera sensors need rendering."
        ),
    )
    parser.add_argument("--raw-avi-fallback", action="store_true", default=True)
    parser.add_argument("--no-raw-avi-fallback", dest="raw_avi_fallback", action="store_false")
    return parser.parse_args()


def dist2d(a: carla.Location, b: carla.Location) -> float:
    return math.hypot(a.x - b.x, a.y - b.y)


def normalize_angle_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def polyline_length(locations: Sequence[carla.Location]) -> float:
    if len(locations) < 2:
        return 0.0
    return sum(locations[i].distance(locations[i - 1]) for i in range(1, len(locations)))


def loc_to_dict(loc: carla.Location) -> Dict[str, float]:
    return {"x": float(loc.x), "y": float(loc.y), "z": float(loc.z)}


def rot_to_dict(rot: carla.Rotation) -> Dict[str, float]:
    return {"pitch": float(rot.pitch), "yaw": float(rot.yaw), "roll": float(rot.roll)}


def transform_to_dict(tf: carla.Transform) -> Dict[str, Dict[str, float]]:
    return {"location": loc_to_dict(tf.location), "rotation": rot_to_dict(tf.rotation)}


def ensure_empty_dir(path: Path, keep_existing: bool) -> None:
    if path.exists() and not keep_existing:
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def parse_route_id_list(text: str) -> List[str]:
    return [item.strip() for item in str(text).split(",") if item.strip()]


def waypoint_junction_id(wp: carla.Waypoint) -> Optional[int]:
    if not wp.is_junction:
        return None
    jid = getattr(wp, "junction_id", None)
    if jid is not None and int(jid) >= 0:
        return int(jid)
    junction = wp.get_junction()
    return None if junction is None else int(junction.id)


def road_option_name(option: object) -> str:
    return getattr(option, "name", str(option))


def classify_turn(start_yaw: float, end_yaw: float) -> str:
    delta = normalize_angle_deg(end_yaw - start_yaw)
    if abs(delta) <= 35.0:
        return "straight"
    if abs(delta) >= 135.0:
        return "uturn"
    return "left" if delta > 0.0 else "right"


@dataclass
class RectRegion:
    name: str
    center: carla.Location
    width: float
    height: float
    yaw_deg: float = 0.0

    def world_to_local(self, loc: carla.Location) -> Tuple[float, float]:
        dx = loc.x - self.center.x
        dy = loc.y - self.center.y
        yaw = math.radians(self.yaw_deg)
        c = math.cos(yaw)
        s = math.sin(yaw)
        return dx * c + dy * s, -dx * s + dy * c

    def local_to_world(self, x: float, y: float, z: Optional[float] = None) -> carla.Location:
        yaw = math.radians(self.yaw_deg)
        c = math.cos(yaw)
        s = math.sin(yaw)
        return carla.Location(
            x=self.center.x + x * c - y * s,
            y=self.center.y + x * s + y * c,
            z=self.center.z if z is None else z,
        )

    def contains(self, loc: carla.Location) -> bool:
        x, y = self.world_to_local(loc)
        return abs(x) <= self.width / 2.0 and abs(y) <= self.height / 2.0

    def corners(self, z_offset: float = 0.5) -> List[carla.Location]:
        z = self.center.z + z_offset
        return [
            self.local_to_world(self.width / 2.0, self.height / 2.0, z),
            self.local_to_world(-self.width / 2.0, self.height / 2.0, z),
            self.local_to_world(-self.width / 2.0, -self.height / 2.0, z),
            self.local_to_world(self.width / 2.0, -self.height / 2.0, z),
        ]

    def to_dict(self) -> Dict[str, object]:
        return {
            "name": self.name,
            "center": loc_to_dict(self.center),
            "width_m": float(self.width),
            "height_m": float(self.height),
            "yaw_deg": float(self.yaw_deg),
            "x_axis_world": {
                "x": math.cos(math.radians(self.yaw_deg)),
                "y": math.sin(math.radians(self.yaw_deg)),
            },
            "y_axis_world": {
                "x": -math.sin(math.radians(self.yaw_deg)),
                "y": math.cos(math.radians(self.yaw_deg)),
            },
        }


@dataclass
class RouteSpec:
    route_id: str
    scene_type: str
    start_wp: carla.Waypoint
    end_wp: carla.Waypoint
    route_wps: List[carla.Waypoint]
    road_options: List[str]
    locations: List[carla.Location]
    turn_type: str
    approach_length: float
    crossing_length: float
    exit_length: float
    min_center_distance: float
    entry_road_id: int
    entry_lane_id: int
    exit_road_id: int
    exit_lane_id: int

    def to_dict(self, compact: bool = False) -> Dict[str, object]:
        data: Dict[str, object] = {
            "route_id": self.route_id,
            "scene_type": self.scene_type,
            "turn_type": self.turn_type,
            "start_transform": transform_to_dict(self.start_wp.transform),
            "end_transform": transform_to_dict(self.end_wp.transform),
            "entry": {"road_id": self.entry_road_id, "lane_id": self.entry_lane_id},
            "exit": {"road_id": self.exit_road_id, "lane_id": self.exit_lane_id},
            "num_waypoints": len(self.route_wps),
            "route_length_m": polyline_length(self.locations),
            "approach_length_m": float(self.approach_length),
            "crossing_length_m": float(self.crossing_length),
            "exit_length_m": float(self.exit_length),
            "min_center_distance_m": float(self.min_center_distance),
        }
        if not compact:
            data["polyline"] = [loc_to_dict(loc) for loc in self.locations]
            data["road_options"] = self.road_options
        return data


@dataclass
class SceneSpec:
    mode: str
    center: carla.Location
    yaw_deg: float
    support_region: RectRegion
    valid_crop: RectRegion
    routes: List[RouteSpec]
    info: Dict[str, object]


class CarlaSyncContext:
    def __init__(
        self,
        world: carla.World,
        traffic_manager: carla.TrafficManager,
        fps: float,
        *,
        no_rendering_mode: bool = False,
    ):
        self.world = world
        self.tm = traffic_manager
        self.fps = fps
        self.no_rendering_mode = bool(no_rendering_mode)
        self._old_settings = None

    def __enter__(self) -> "CarlaSyncContext":
        self._old_settings = self.world.get_settings()
        new_settings = self.world.get_settings()
        new_settings.synchronous_mode = True
        new_settings.fixed_delta_seconds = 1.0 / self.fps
        new_settings.no_rendering_mode = self.no_rendering_mode
        self.world.apply_settings(new_settings)
        self.tm.set_synchronous_mode(True)
        return self

    def tick(self) -> int:
        return self.world.tick()

    def __exit__(self, exc_type, exc, tb) -> None:
        if self.no_rendering_mode:
            # Do not toggle CARLA world/TM settings at trajectory-only attempt
            # boundaries. On this packaged CARLA build, even restoring to
            # async no_rendering settings can make a just-finished server exit
            # before actor cleanup on some adapters. The next attempt
            # re-applies the same synchronous no_rendering settings on entry;
            # the supervisor terminates the server when collection ends.
            return
        self.tm.set_synchronous_mode(False)
        if self._old_settings is not None:
            self.world.apply_settings(self._old_settings)


class RawAviWriter:
    """Tiny uncompressed AVI writer used when ffmpeg is not installed."""

    def __init__(self, path: Path, width: int, height: int, fps: float):
        self.path = path
        self.width = width
        self.height = height
        self.fps = fps
        self.row_size = ((width * 3 + 3) // 4) * 4
        self.frame_size = self.row_size * height
        self.file = path.open("wb")
        self.index_entries: List[Tuple[bytes, int, int, int]] = []
        self._avih_frames_pos = 0
        self._strh_length_pos = 0
        self._riff_size_pos = 0
        self._movi_size_pos = 0
        self._movi_data_start = 0
        self._write_header()

    def _write_chunk_header(self, fourcc: bytes, size: int) -> int:
        pos = self.file.tell()
        self.file.write(fourcc)
        self.file.write(struct.pack("<I", size))
        return pos

    def _write_header(self) -> None:
        self.file.write(b"RIFF")
        self._riff_size_pos = self.file.tell()
        self.file.write(struct.pack("<I", 0))
        self.file.write(b"AVI ")

        hdrl_start = self.file.tell()
        self.file.write(b"LIST")
        hdrl_size_pos = self.file.tell()
        self.file.write(struct.pack("<I", 0))
        self.file.write(b"hdrl")

        microsec_per_frame = int(1_000_000.0 / self.fps)
        max_bytes_per_sec = int(self.frame_size * self.fps)
        self._write_chunk_header(b"avih", 56)
        avih_data_start = self.file.tell()
        self._avih_frames_pos = avih_data_start + 16
        self.file.write(
            struct.pack(
                "<IIIIIIIIII4I",
                microsec_per_frame,
                max_bytes_per_sec,
                0,
                0x10,
                0,
                0,
                1,
                self.frame_size,
                self.width,
                self.height,
                0,
                0,
                0,
                0,
            )
        )

        strl_start = self.file.tell()
        self.file.write(b"LIST")
        strl_size_pos = self.file.tell()
        self.file.write(struct.pack("<I", 0))
        self.file.write(b"strl")

        self._write_chunk_header(b"strh", 56)
        strh_data_start = self.file.tell()
        self._strh_length_pos = strh_data_start + 32
        rate = int(round(self.fps * 1000.0))
        scale = 1000
        self.file.write(
            struct.pack(
                "<4s4sIHHIIIIIIIIhhhh",
                b"vids",
                b"DIB ",
                0,
                0,
                0,
                0,
                scale,
                rate,
                0,
                0,
                self.frame_size,
                0xFFFFFFFF,
                0,
                0,
                0,
                self.width,
                self.height,
            )
        )

        self._write_chunk_header(b"strf", 40)
        self.file.write(
            struct.pack(
                "<IiiHHIIiiII",
                40,
                self.width,
                self.height,
                1,
                24,
                0,
                self.frame_size,
                0,
                0,
                0,
                0,
            )
        )

        strl_end = self.file.tell()
        self.file.seek(strl_size_pos)
        self.file.write(struct.pack("<I", strl_end - strl_start - 8))
        self.file.seek(strl_end)

        hdrl_end = self.file.tell()
        self.file.seek(hdrl_size_pos)
        self.file.write(struct.pack("<I", hdrl_end - hdrl_start - 8))
        self.file.seek(hdrl_end)

        self.file.write(b"LIST")
        self._movi_size_pos = self.file.tell()
        self.file.write(struct.pack("<I", 0))
        self.file.write(b"movi")
        self._movi_data_start = self.file.tell()

    def write_carla_image(self, image: carla.Image) -> None:
        if image.width != self.width or image.height != self.height:
            raise ValueError(f"AVI size mismatch: got {image.width}x{image.height}, expected {self.width}x{self.height}")
        chunk_start = self.file.tell()
        self._write_chunk_header(b"00db", self.frame_size)
        raw = image.raw_data
        padding = b"\x00" * (self.row_size - self.width * 3)
        for row_idx in range(self.height - 1, -1, -1):
            row = raw[row_idx * self.width * 4 : (row_idx + 1) * self.width * 4]
            bgr = bytearray(row)
            del bgr[3::4]
            self.file.write(bgr)
            if padding:
                self.file.write(padding)
        self.index_entries.append((b"00db", 0x10, chunk_start - self._movi_data_start, self.frame_size))

    def close(self) -> None:
        if self.file.closed:
            return
        movi_end = self.file.tell()
        self.file.seek(self._movi_size_pos)
        self.file.write(struct.pack("<I", movi_end - self._movi_size_pos - 4))
        self.file.seek(movi_end)

        self._write_chunk_header(b"idx1", 16 * len(self.index_entries))
        for entry in self.index_entries:
            self.file.write(struct.pack("<4sIII", *entry))

        file_end = self.file.tell()
        self.file.seek(self._avih_frames_pos)
        self.file.write(struct.pack("<I", len(self.index_entries)))
        self.file.seek(self._strh_length_pos)
        self.file.write(struct.pack("<I", len(self.index_entries)))
        self.file.seek(self._riff_size_pos)
        self.file.write(struct.pack("<I", file_end - 8))
        self.file.seek(file_end)
        self.file.close()


def choose_waypoint(current: carla.Waypoint, candidates: Sequence[carla.Waypoint]) -> Optional[carla.Waypoint]:
    if not candidates:
        return None
    same_lane = [wp for wp in candidates if wp.road_id == current.road_id and wp.lane_id == current.lane_id]
    non_junction = [wp for wp in candidates if not wp.is_junction]
    pool = same_lane or non_junction or list(candidates)
    return min(pool, key=lambda wp: abs(normalize_angle_deg(wp.transform.rotation.yaw - current.transform.rotation.yaw)))


def extend_waypoint(wp: carla.Waypoint, direction: int, distance: float, step: float) -> Tuple[carla.Waypoint, float]:
    current = wp
    traveled = 0.0
    while traveled < distance:
        candidates = current.next(step) if direction > 0 else current.previous(step)
        nxt = choose_waypoint(current, candidates)
        if nxt is None:
            break
        seg_len = nxt.transform.location.distance(current.transform.location)
        if seg_len < 0.05:
            break
        traveled += seg_len
        current = nxt
    return current, traveled


def trace_towards_waypoint(
    start_wp: carla.Waypoint,
    end_wp: carla.Waypoint,
    step: float,
    max_steps: int = 80,
) -> List[carla.Waypoint]:
    """Follow waypoint.next() greedily toward a known end waypoint."""
    route = [start_wp]
    current = start_wp
    best_distance = current.transform.location.distance(end_wp.transform.location)
    for _ in range(max_steps):
        if best_distance <= step * 0.75:
            break
        candidates = current.next(step)
        if not candidates:
            break
        nxt = min(candidates, key=lambda wp: wp.transform.location.distance(end_wp.transform.location))
        new_distance = nxt.transform.location.distance(end_wp.transform.location)
        if new_distance > best_distance + step * 0.75:
            break
        route.append(nxt)
        current = nxt
        best_distance = new_distance
    if route[-1].transform.location.distance(end_wp.transform.location) > 0.1:
        route.append(end_wp)
    return route


def dedupe_waypoints(route: Sequence[carla.Waypoint]) -> List[carla.Waypoint]:
    deduped: List[carla.Waypoint] = []
    last_loc: Optional[carla.Location] = None
    for wp in route:
        loc = wp.transform.location
        if last_loc is None or loc.distance(last_loc) > 0.2:
            deduped.append(wp)
            last_loc = loc
    return deduped


def trace_route_between(
    grp: Optional[object],
    start_wp: carla.Waypoint,
    end_wp: carla.Waypoint,
    step: float,
    fallback_max_steps: int,
) -> Tuple[List[carla.Waypoint], List[str]]:
    if grp is not None:
        try:
            trace = grp.trace_route(start_wp.transform.location, end_wp.transform.location)
            route_wps = [wp for wp, _ in trace]
            road_options = [road_option_name(opt) for _, opt in trace]
            if route_wps:
                return route_wps, road_options
        except Exception:
            pass
    route_wps = dedupe_waypoints(trace_towards_waypoint(start_wp, end_wp, step, max_steps=fallback_max_steps))
    return route_wps, ["LANEFOLLOW"] * len(route_wps)


def trace_checked_route(
    grp: Optional[object],
    junction_id: int,
    center: carla.Location,
    support_region: RectRegion,
    entry_wp: carla.Waypoint,
    exit_wp: carla.Waypoint,
    route_id: str,
    scene_type: str,
    args: argparse.Namespace,
) -> Optional[RouteSpec]:
    start_wp, raw_approach = extend_waypoint(entry_wp, -1, args.route_approach, args.route_step)
    end_wp, raw_exit = extend_waypoint(exit_wp, 1, args.route_exit, args.route_step)
    if raw_approach < args.min_approach or raw_exit < args.min_exit:
        return None

    route_wps, road_options = trace_route_between(grp, start_wp, end_wp, args.route_step, fallback_max_steps=200)
    locations = [wp.transform.location for wp in route_wps]
    if len(locations) < 3:
        return None
    if not all(support_region.contains(loc) for loc in locations):
        return None

    inside = [i for i, wp in enumerate(route_wps) if waypoint_junction_id(wp) == junction_id]
    if not inside:
        return None
    first_inside = min(inside)
    last_inside = max(inside)
    if first_inside == 0 or last_inside >= len(route_wps) - 1:
        return None

    approach_length = polyline_length(locations[: first_inside + 1])
    crossing_length = polyline_length(locations[first_inside : last_inside + 1])
    exit_length = polyline_length(locations[last_inside:])
    min_center_distance = min(dist2d(loc, center) for loc in locations)
    if approach_length < args.min_approach or exit_length < args.min_exit:
        return None
    if min_center_distance > args.junction_core_radius:
        return None

    turn_type = classify_turn(entry_wp.transform.rotation.yaw, exit_wp.transform.rotation.yaw)
    if turn_type == "uturn":
        return None

    return RouteSpec(
        route_id=route_id,
        scene_type=scene_type,
        start_wp=start_wp,
        end_wp=end_wp,
        route_wps=route_wps,
        road_options=road_options,
        locations=locations,
        turn_type=turn_type,
        approach_length=approach_length,
        crossing_length=crossing_length,
        exit_length=exit_length,
        min_center_distance=min_center_distance,
        entry_road_id=int(entry_wp.road_id),
        entry_lane_id=int(entry_wp.lane_id),
        exit_road_id=int(exit_wp.road_id),
        exit_lane_id=int(exit_wp.lane_id),
    )


def collect_junctions(world_map: carla.Map) -> List[carla.Junction]:
    seen: Dict[int, carla.Junction] = {}
    for wp in world_map.generate_waypoints(4.0):
        if wp.is_junction:
            junction = wp.get_junction()
            if junction is not None:
                seen[int(junction.id)] = junction
    return list(seen.values())


def route_direction_bins(routes: Sequence[RouteSpec]) -> int:
    bins = set()
    for route in routes:
        yaw = route.start_wp.transform.rotation.yaw
        bins.add(int((yaw % 360.0) // 45.0))
    return len(bins)


def build_junction_scene(world_map: carla.Map, grp: Optional[object], args: argparse.Namespace) -> SceneSpec:
    candidates = []
    for junction in collect_junctions(world_map):
        center = junction.bounding_box.location
        support_region = RectRegion("support_region", center, args.support_size, args.support_size, yaw_deg=0.0)
        valid_crop = RectRegion("valid_crop", center, args.valid_size, args.valid_size, yaw_deg=0.0)
        route_specs: List[RouteSpec] = []
        pairs = junction.get_waypoints(carla.LaneType.Driving)
        for idx, pair in enumerate(pairs):
            entry_wp, exit_wp = pair
            route = trace_checked_route(
                grp=grp,
                junction_id=int(junction.id),
                center=center,
                support_region=support_region,
                entry_wp=entry_wp,
                exit_wp=exit_wp,
                route_id=f"junction_{junction.id:04d}_route_{idx:03d}",
                scene_type="junction",
                args=args,
            )
            if route is not None:
                route_specs.append(route)

        if not route_specs:
            continue
        roads = sorted({route.entry_road_id for route in route_specs} | {route.exit_road_id for route in route_specs})
        lanes = sorted({(route.entry_road_id, route.entry_lane_id) for route in route_specs})
        turn_types = sorted({route.turn_type for route in route_specs})
        direction_count = route_direction_bins(route_specs)
        avg_route_len = sum(polyline_length(route.locations) for route in route_specs) / len(route_specs)
        score = 100.0 * direction_count + 10.0 * len(route_specs) + avg_route_len
        candidates.append(
            (
                score,
                SceneSpec(
                    mode="junction",
                    center=center,
                    yaw_deg=0.0,
                    support_region=support_region,
                    valid_crop=valid_crop,
                    routes=route_specs,
                    info={
                        "junction_id": int(junction.id),
                        "junction_center": loc_to_dict(center),
                        "junction_bbox_extent": loc_to_dict(junction.bounding_box.extent),
                        "num_valid_routes": len(route_specs),
                        "num_direction_bins": direction_count,
                        "roads": roads,
                        "lanes": [{"road_id": r, "lane_id": l} for r, l in lanes],
                        "turn_types": turn_types,
                    },
                ),
            )
        )

    if not candidates:
        raise RuntimeError(
            "No valid junction scene found. Try increasing --support-size, lowering --route-approach/--route-exit, "
            "or using --scene-mode corridor."
        )
    if args.scene_junction_id is not None:
        for _, candidate in candidates:
            if int(candidate.info.get("junction_id", -1)) == int(args.scene_junction_id):
                return candidate
        raise RuntimeError(f"Requested scene junction id is not available: {args.scene_junction_id}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def build_corridor_scene(world_map: carla.Map, grp: Optional[object], args: argparse.Namespace) -> SceneSpec:
    candidates = []
    for idx, (wp0, wp1) in enumerate(world_map.get_topology()):
        if wp0.is_junction or wp1.is_junction:
            continue
        length = wp0.transform.location.distance(wp1.transform.location)
        if length < args.valid_size * 0.8:
            continue
        center = carla.Location(
            x=(wp0.transform.location.x + wp1.transform.location.x) / 2.0,
            y=(wp0.transform.location.y + wp1.transform.location.y) / 2.0,
            z=(wp0.transform.location.z + wp1.transform.location.z) / 2.0,
        )
        yaw = wp0.transform.rotation.yaw
        support_region = RectRegion("support_region", center, args.support_size, args.support_size, yaw_deg=yaw)
        valid_crop = RectRegion("valid_crop", center, args.valid_size, args.valid_size, yaw_deg=yaw)
        start_wp, _ = extend_waypoint(wp0, -1, args.route_approach, args.route_step)
        end_wp, _ = extend_waypoint(wp1, 1, args.route_exit, args.route_step)
        route_wps, road_options = trace_route_between(grp, start_wp, end_wp, args.route_step, fallback_max_steps=120)
        locations = [wp.transform.location for wp in route_wps]
        if len(locations) < 4 or not all(support_region.contains(loc) for loc in locations):
            continue
        if min(dist2d(loc, center) for loc in locations) > args.junction_core_radius:
            continue
        route = RouteSpec(
            route_id=f"corridor_route_{idx:04d}",
            scene_type="corridor",
            start_wp=start_wp,
            end_wp=end_wp,
            route_wps=route_wps,
            road_options=road_options,
            locations=locations,
            turn_type="corridor",
            approach_length=args.route_approach,
            crossing_length=args.valid_size,
            exit_length=args.route_exit,
            min_center_distance=min(dist2d(loc, center) for loc in locations),
            entry_road_id=int(wp0.road_id),
            entry_lane_id=int(wp0.lane_id),
            exit_road_id=int(wp1.road_id),
            exit_lane_id=int(wp1.lane_id),
        )
        candidates.append(
            (
                polyline_length(locations),
                SceneSpec(
                    mode="corridor",
                    center=center,
                    yaw_deg=yaw,
                    support_region=support_region,
                    valid_crop=valid_crop,
                    routes=[route],
                    info={
                        "candidate_index": int(idx),
                        "road_id": int(wp0.road_id),
                        "lane_id": int(wp0.lane_id),
                        "center": loc_to_dict(center),
                        "yaw_deg": float(yaw),
                    },
                ),
            )
        )

    if not candidates:
        raise RuntimeError("No valid corridor scene found. Try increasing --support-size or changing towns.")
    if args.scene_corridor_index is not None:
        for _, candidate in candidates:
            if int(candidate.info.get("candidate_index", -1)) == int(args.scene_corridor_index):
                return candidate
        raise RuntimeError(f"Requested scene corridor index is not available: {args.scene_corridor_index}")
    candidates.sort(key=lambda item: item[0], reverse=True)
    return candidates[0][1]


def select_scene(world_map: carla.Map, args: argparse.Namespace) -> SceneSpec:
    if args.support_size <= args.valid_size:
        raise ValueError("--support-size must be larger than --valid-size to preserve an edge buffer.")
    grp = None
    if GlobalRoutePlanner is not None:
        try:
            grp = GlobalRoutePlanner(world_map, args.route_step)
            print("[INFO] Using CARLA GlobalRoutePlanner for route tracing")
        except Exception as exc:  # noqa: BLE001
            print(f"[WARN] GlobalRoutePlanner initialization failed; using waypoint fallback: {exc!r}")
    elif GLOBAL_ROUTE_PLANNER_IMPORT_ERROR is not None:
        print(f"[WARN] GlobalRoutePlanner unavailable; using waypoint fallback: {GLOBAL_ROUTE_PLANNER_IMPORT_ERROR!r}")
    if args.scene_mode == "junction":
        return build_junction_scene(world_map, grp, args)
    return build_corridor_scene(world_map, grp, args)


PASSENGER_EXCLUDED_BLUEPRINT_TOKENS = (
    "ambulance",
    "carlacola",
    "cybertruck",
    "firetruck",
    "fusorosa",
    "microlino",
    "sprinter",
    "t2",
)


def parse_vehicle_allowlist(text: str) -> List[str]:
    return [item.strip().lower() for item in text.split(",") if item.strip()]


def blueprint_matches_allowlist(blueprint_id: str, allowlist: Sequence[str]) -> bool:
    if not allowlist:
        return True
    blueprint_id = blueprint_id.lower()
    for item in allowlist:
        if "." in item:
            if blueprint_id == item:
                return True
            continue
        if item in blueprint_id:
            return True
    return False


def candidate_vehicle_blueprints(world: carla.World, args: argparse.Namespace) -> List[carla.ActorBlueprint]:
    allowlist = parse_vehicle_allowlist(args.vehicle_allowlist)
    blueprints = [
        bp
        for bp in world.get_blueprint_library().filter("vehicle.*")
        if bp.has_attribute("number_of_wheels") and int(bp.get_attribute("number_of_wheels")) == 4
    ]
    if not blueprints:
        blueprints = list(world.get_blueprint_library().filter("vehicle.*"))
    if allowlist:
        filtered = [bp for bp in blueprints if blueprint_matches_allowlist(bp.id, allowlist)]
        if not filtered:
            raise RuntimeError(
                "Vehicle allowlist matched no CARLA blueprints. "
                f"allowlist={allowlist}"
            )
        blueprints = filtered
    if args.vehicle_size_preset == "passenger":
        filtered = [
            bp
            for bp in blueprints
            if not any(token in bp.id.lower() for token in PASSENGER_EXCLUDED_BLUEPRINT_TOKENS)
        ]
        if filtered:
            blueprints = filtered
    return blueprints


def vehicle_size_ok(actor: carla.Actor, args: argparse.Namespace) -> bool:
    if args.vehicle_size_preset == "mixed":
        return True
    extent = actor.bounding_box.extent
    length = float(extent.x) * 2.0
    width = float(extent.y) * 2.0
    height = float(extent.z) * 2.0
    return (
        args.vehicle_min_length <= length <= args.vehicle_max_length
        and width <= args.vehicle_max_width
        and height <= args.vehicle_max_height
    )


def make_vehicle_blueprint(
    blueprints: Sequence[carla.ActorBlueprint],
    rng: random.Random,
    blueprint_id: Optional[str] = None,
) -> carla.ActorBlueprint:
    if blueprint_id:
        matches = [bp for bp in blueprints if bp.id == blueprint_id]
        if not matches:
            raise RuntimeError(f"Requested vehicle blueprint is not available under the current policy: {blueprint_id}")
        bp = matches[0]
    else:
        bp = rng.choice(blueprints)
    if bp.has_attribute("color"):
        bp.set_attribute("color", rng.choice(bp.get_attribute("color").recommended_values))
    if bp.has_attribute("driver_id"):
        bp.set_attribute("driver_id", rng.choice(bp.get_attribute("driver_id").recommended_values))
    if bp.has_attribute("role_name"):
        bp.set_attribute("role_name", "dynamic_radio_vehicle")
    return bp


def try_spawn_vehicle(
    world: carla.World,
    client: carla.Client,
    blueprints: Sequence[carla.ActorBlueprint],
    transform: carla.Transform,
    args: argparse.Namespace,
    rng: random.Random,
    blueprint_id: Optional[str] = None,
) -> Optional[carla.Actor]:
    attempts = max(1, int(args.vehicle_spawn_attempts))
    for _ in range(attempts):
        actor = world.try_spawn_actor(make_vehicle_blueprint(blueprints, rng, blueprint_id=blueprint_id), transform)
        if actor is None:
            continue
        if vehicle_size_ok(actor, args):
            return actor
        destroy_actors(client, [actor])
    return None


def load_traffic_plan(path: Optional[Path]) -> Optional[Dict[str, object]]:
    if path is None:
        return None
    with path.open("r", encoding="utf-8") as f:
        plan = json.load(f)
    vehicles = plan.get("vehicles", [])
    if not isinstance(vehicles, list) or not vehicles:
        raise RuntimeError(f"Traffic plan has no vehicles: {path}")
    return plan


def normalize_plan_vehicle_row(row: Dict[str, object], fallback_index: int) -> Dict[str, object]:
    result = dict(row)
    result["plan_index"] = int(result.get("plan_index", fallback_index))
    result["role"] = canonical_vehicle_role(result.get("role"))
    result["required"] = bool(result.get("required", role_is_required(result.get("role"))))
    result["control_mode"] = str(result.get("control_mode", "traffic_manager_route"))
    return result


def plan_row_required(row: Dict[str, object]) -> bool:
    return bool(row.get("required", role_is_required(row.get("role"))))


def traffic_plan_background_policy(traffic_plan: Optional[Dict[str, object]], args: argparse.Namespace) -> Dict[str, object]:
    background = {}
    if isinstance(traffic_plan, dict) and isinstance(traffic_plan.get("background_tm"), dict):
        background = dict(traffic_plan["background_tm"])  # type: ignore[index]
    jitter = background.get("speed_diff_jitter", [-10.0, 15.0])
    if not isinstance(jitter, list) or len(jitter) != 2:
        jitter = [-10.0, 15.0]
    return {
        "role": "background_tm",
        "requested_count": int(background.get("requested_count", args.num_background_vehicles)),
        "vehicle_types": [
            str(item)
            for item in background.get("vehicle_types", [])
            if str(item).strip()
        ],
        "vehicle_type_policy": str(background.get("vehicle_type_policy", "allowlist")),
        "planned_large_vehicle_count": int(background.get("planned_large_vehicle_count", 0)),
        "spawn_region": str(background.get("spawn_region", "support_region")),
        "min_distance_to_controlled_spawn_m": float(background.get("min_distance_to_controlled_spawn_m", 6.0)),
        "max_distance_to_controlled_route_m": float(background.get("max_distance_to_controlled_route_m", 45.0)),
        "obey_traffic_lights": bool(background.get("obey_traffic_lights", True)),
        "speed_diff": float(background.get("speed_diff", args.background_speed_diff)),
        "speed_diff_jitter": [float(jitter[0]), float(jitter[1])],
    }


def planned_role_counts(plan_vehicles: Sequence[Dict[str, object]], requested_background_count: int) -> Dict[str, int]:
    required = sum(1 for row in plan_vehicles if plan_row_required(row))
    optional = len(plan_vehicles) - required
    primary = sum(1 for row in plan_vehicles if canonical_vehicle_role(row.get("role")) == "primary_controlled")
    auxiliary = sum(1 for row in plan_vehicles if canonical_vehicle_role(row.get("role")) == "auxiliary_controlled")
    return {
        "planned_required_controlled_count": int(required),
        "planned_optional_controlled_count": int(optional),
        "planned_primary_controlled_count": int(primary),
        "planned_auxiliary_controlled_count": int(auxiliary),
        "requested_background_count": int(requested_background_count),
        "requested_total_vehicle_count": int(required + optional + requested_background_count),
    }


def configure_autopilot_for_route(
    actor: carla.Actor,
    traffic_manager: carla.TrafficManager,
    route: RouteSpec,
    speed_diff: float,
    ignore_lights: bool,
) -> None:
    actor.set_autopilot(True, traffic_manager.get_port())
    traffic_manager.set_path(actor, route.locations)
    traffic_manager.auto_lane_change(actor, False)
    traffic_manager.vehicle_percentage_speed_difference(actor, speed_diff)
    traffic_manager.distance_to_leading_vehicle(actor, 3.0)
    if ignore_lights:
        traffic_manager.ignore_lights_percentage(actor, 100.0)


def hold_vehicle_until_release(actor: carla.Actor, traffic_manager: carla.TrafficManager) -> None:
    actor.set_autopilot(False, traffic_manager.get_port())
    actor.apply_control(carla.VehicleControl(throttle=0.0, brake=1.0, hand_brake=True))


def release_due_target_vehicles(
    frame_index: int,
    traffic_manager: carla.TrafficManager,
    target_routes: Dict[int, RouteSpec],
    target_plan_rows: Dict[int, Dict[str, object]],
    args: argparse.Namespace,
) -> None:
    for actor_id, plan_row in target_plan_rows.items():
        if bool(plan_row.get("released", False)):
            continue
        release_frame = int(plan_row.get("release_frame", 0))
        if frame_index < release_frame:
            continue
        actor = plan_row.get("_actor")
        if actor is None:
            continue
        configure_autopilot_for_route(
            actor=actor,  # type: ignore[arg-type]
            traffic_manager=traffic_manager,
            route=target_routes[int(actor_id)],
            speed_diff=float(plan_row.get("speed_diff", args.target_speed_diff)),
            ignore_lights=bool(plan_row.get("ignore_lights", args.target_ignore_lights)),
        )
        try:
            actor.apply_control(carla.VehicleControl(throttle=0.0, brake=0.0, hand_brake=False))  # type: ignore[union-attr]
        except RuntimeError:
            pass
        plan_row["released"] = True


def spawn_target_vehicles(
    client: carla.Client,
    world: carla.World,
    traffic_manager: carla.TrafficManager,
    scene: SceneSpec,
    args: argparse.Namespace,
    rng: random.Random,
    blueprints: Sequence[carla.ActorBlueprint],
) -> Tuple[List[carla.Actor], Dict[int, RouteSpec], Dict[int, Dict[str, object]], Dict[str, object]]:
    traffic_plan = load_traffic_plan(args.traffic_plan_json)
    explicit_route_ids = parse_route_id_list(args.target_route_ids)
    plan_vehicles: List[Dict[str, object]] = []
    if traffic_plan is not None:
        raw_vehicles = traffic_plan.get("vehicles", [])
        plan_vehicles = [
            normalize_plan_vehicle_row(dict(row), route_index)
            for route_index, row in enumerate(raw_vehicles)  # type: ignore[arg-type]
        ]
        explicit_route_ids = [str(row["route_id"]) for row in plan_vehicles]
    if explicit_route_ids:
        routes_by_id = {route.route_id: route for route in scene.routes}
        missing_route_ids = [route_id for route_id in explicit_route_ids if route_id not in routes_by_id]
        if missing_route_ids:
            raise RuntimeError(
                "Requested target route ids are not available in the selected scene: "
                f"{missing_route_ids}"
            )
        target_routes = [routes_by_id[route_id] for route_id in explicit_route_ids]
    else:
        shuffled_routes = list(scene.routes)
        rng.shuffle(shuffled_routes)
        target_routes = shuffled_routes[: max(1, min(args.num_target_vehicles, len(shuffled_routes)))]
    actors: List[carla.Actor] = []
    actor_routes: Dict[int, RouteSpec] = {}
    actor_plan_rows: Dict[int, Dict[str, object]] = {}
    missing_required_rows: List[Dict[str, object]] = []
    missing_optional_rows: List[Dict[str, object]] = []
    for route_index, route in enumerate(target_routes):
        plan_row = plan_vehicles[route_index] if route_index < len(plan_vehicles) else normalize_plan_vehicle_row({}, route_index)
        requested_blueprint = str(plan_row.get("vehicle_type", "")).strip() or None
        spawn_tf = carla.Transform(
            carla.Location(
                x=route.start_wp.transform.location.x,
                y=route.start_wp.transform.location.y,
                z=route.start_wp.transform.location.z + 0.25,
            ),
            route.start_wp.transform.rotation,
        )
        actor = try_spawn_vehicle(world, client, blueprints, spawn_tf, args, rng, blueprint_id=requested_blueprint)
        if actor is None:
            missing_row = {
                "plan_index": int(plan_row.get("plan_index", route_index)),
                "route_id": route.route_id,
                "role": canonical_vehicle_role(plan_row.get("role", "auxiliary_controlled")),
                "required": bool(plan_row_required(plan_row)),
                "requested_vehicle_type": requested_blueprint,
                "spawn_location": loc_to_dict(spawn_tf.location),
            }
            if plan_row_required(plan_row):
                missing_required_rows.append(missing_row)
            else:
                missing_optional_rows.append(missing_row)
            continue
        start_delay_s = max(0.0, float(plan_row.get("start_delay_s", 0.0)))
        speed_diff = float(plan_row.get("speed_diff", args.target_speed_diff))
        release_frame = int(round(start_delay_s * float(args.fps)))
        if traffic_plan is not None:
            hold_vehicle_until_release(actor, traffic_manager)
        else:
            configure_autopilot_for_route(actor, traffic_manager, route, speed_diff, bool(args.target_ignore_lights))
        actors.append(actor)
        actor_routes[int(actor.id)] = route
        actor_plan_rows[int(actor.id)] = {
            "plan_index": int(plan_row.get("plan_index", route_index)),
            "role": canonical_vehicle_role(plan_row.get("role", "auxiliary_controlled")),
            "required": bool(plan_row_required(plan_row)),
            "control_mode": str(plan_row.get("control_mode", "traffic_manager_route")),
            "route_id": route.route_id,
            "requested_vehicle_type": requested_blueprint,
            "actual_vehicle_type": actor.type_id,
            "start_delay_s": start_delay_s,
            "release_frame": release_frame,
            "speed_diff": speed_diff,
            "ignore_lights": bool(plan_row.get("ignore_lights", args.target_ignore_lights)),
            "released": False if traffic_plan is not None else release_frame <= 0,
            "_actor": actor,
        }
    required_actor_count = sum(1 for row in actor_plan_rows.values() if bool(row.get("required", False)))
    optional_actor_count = sum(1 for row in actor_plan_rows.values() if not bool(row.get("required", False)))
    spawned_rows = [
        {
            "actor_id": int(actor.id),
            "plan_index": int(actor_plan_rows[int(actor.id)].get("plan_index", -1)),
            "route_id": str(actor_plan_rows[int(actor.id)].get("route_id", "")),
            "role": str(actor_plan_rows[int(actor.id)].get("role", "")),
            "required": bool(actor_plan_rows[int(actor.id)].get("required", False)),
            "actual_vehicle_type": str(actor_plan_rows[int(actor.id)].get("actual_vehicle_type", "")),
        }
        for actor in actors
    ]
    spawn_report: Dict[str, object] = {
        "planned_required_controlled_count": int(sum(1 for row in plan_vehicles if plan_row_required(row))),
        "planned_optional_controlled_count": int(sum(1 for row in plan_vehicles if not plan_row_required(row))),
        "actual_required_controlled_count": int(required_actor_count),
        "actual_optional_controlled_count": int(optional_actor_count),
        "missing_required_controlled": missing_required_rows,
        "missing_optional_controlled": missing_optional_rows,
        "spawned_controlled": spawned_rows,
    }
    if traffic_plan is not None and missing_required_rows:
        write_stage(
            Path(args.output_dir),
            "spawn_actors_failed",
            failure_code="traffic_plan_spawn_incomplete",
            requested_required_controlled_count=int(spawn_report["planned_required_controlled_count"]),
            spawned_required_controlled_count=int(spawn_report["actual_required_controlled_count"]),
            requested_optional_controlled_count=int(spawn_report["planned_optional_controlled_count"]),
            spawned_optional_controlled_count=int(spawn_report["actual_optional_controlled_count"]),
            missing_required_controlled=missing_required_rows,
            missing_optional_controlled=missing_optional_rows,
            spawned_plan_rows=spawned_rows,
        )
        destroy_actors(client, actors)
        raise TrafficPlanSpawnIncomplete(
            "traffic_plan_spawn_incomplete: CARLA could not spawn every required TrafficPlan controlled vehicle. "
            f"required={spawn_report['planned_required_controlled_count']} "
            f"spawned_required={spawn_report['actual_required_controlled_count']} "
            f"missing_required={missing_required_rows}"
        )
    if not actors:
        raise RuntimeError("No target vehicles spawned successfully.")
    return actors, actor_routes, actor_plan_rows, spawn_report


def spawn_background_vehicles(
    client: carla.Client,
    world: carla.World,
    traffic_manager: carla.TrafficManager,
    scene: SceneSpec,
    args: argparse.Namespace,
    rng: random.Random,
    blueprints: Sequence[carla.ActorBlueprint],
    traffic_plan: Optional[Dict[str, object]],
    controlled_routes: Dict[int, RouteSpec],
) -> Tuple[List[carla.Actor], Dict[str, object]]:
    policy = traffic_plan_background_policy(traffic_plan, args)
    requested_count = max(0, int(policy["requested_count"]))
    spawn_points = _background_spawn_points(world, scene, controlled_routes, policy)
    rng.shuffle(spawn_points)
    actors: List[carla.Actor] = []
    failures: List[Dict[str, object]] = []
    requested_types = [str(item) for item in policy.get("vehicle_types", []) if str(item).strip()]
    exact_type_list = str(policy.get("vehicle_type_policy", "")) in {"exact_ordered_list", "exact_list"}
    for sp in spawn_points:
        if len(actors) >= requested_count:
            break
        if exact_type_list and len(actors) < len(requested_types):
            blueprint_id = requested_types[len(actors)]
        else:
            blueprint_id = rng.choice(requested_types) if requested_types else None
        actor = try_spawn_vehicle(world, client, blueprints, sp, args, rng, blueprint_id=blueprint_id)
        if actor is None:
            failures.append(
                {
                    "requested_vehicle_type": blueprint_id,
                    "spawn_location": loc_to_dict(sp.location),
                }
            )
            continue
        actor.set_autopilot(True, traffic_manager.get_port())
        jitter = policy["speed_diff_jitter"]
        speed_diff = float(policy["speed_diff"]) + rng.uniform(float(jitter[0]), float(jitter[1]))  # type: ignore[index]
        traffic_manager.vehicle_percentage_speed_difference(actor, speed_diff)
        traffic_manager.auto_lane_change(actor, True)
        traffic_manager.distance_to_leading_vehicle(actor, 3.0)
        if not bool(policy["obey_traffic_lights"]):
            traffic_manager.ignore_lights_percentage(actor, 100.0)
        actors.append(actor)
    report = {
        "policy": policy,
        "requested_background_count": int(requested_count),
        "requested_vehicle_types": requested_types,
        "candidate_spawn_point_count": int(len(spawn_points)),
        "actual_background_count": int(len(actors)),
        "background_actor_ids": [int(actor.id) for actor in actors],
        "actual_vehicle_types": [str(actor.type_id) for actor in actors],
        "spawn_failures": failures,
    }
    return actors, report


def _background_spawn_points(
    world: carla.World,
    scene: SceneSpec,
    controlled_routes: Dict[int, RouteSpec],
    policy: Dict[str, object],
) -> List[carla.Transform]:
    spawn_points = [sp for sp in world.get_map().get_spawn_points() if scene.support_region.contains(sp.location)]
    if str(policy.get("spawn_region", "support_region")) != "support_region":
        raise RuntimeError(f"Unsupported background_tm spawn_region: {policy.get('spawn_region')}")
    min_start_distance_m = float(policy.get("min_distance_to_controlled_spawn_m", 0.0))
    max_route_distance_m = float(policy.get("max_distance_to_controlled_route_m", 0.0))
    route_specs = list(controlled_routes.values())
    result = []
    for spawn_point in spawn_points:
        if min_start_distance_m > 0.0 and any(
            dist2d(spawn_point.location, route.start_wp.transform.location) < min_start_distance_m
            for route in route_specs
        ):
            continue
        if max_route_distance_m > 0.0 and route_specs:
            nearest = min(_distance_to_route_locations(spawn_point.location, route.locations) for route in route_specs)
            if nearest > max_route_distance_m:
                continue
        result.append(spawn_point)
    return result


def _distance_to_route_locations(location: carla.Location, route_locations: Sequence[carla.Location]) -> float:
    if not route_locations:
        return float("inf")
    return min(dist2d(location, item) for item in route_locations)


def get_sensor_frame(sensor_queue: "queue.Queue[carla.Image]", frame_id: int, timeout: float = 10.0) -> carla.Image:
    while True:
        image = sensor_queue.get(timeout=timeout)
        if image.frame == frame_id:
            return image
        if image.frame > frame_id:
            return image


def camera_height_for_region(region_size: float, fov_deg: float) -> float:
    half_angle = math.radians(fov_deg / 2.0)
    return region_size / (2.0 * math.tan(half_angle)) * 1.15


def draw_rect(world: carla.World, region: RectRegion, color: carla.Color, life_time: float = 0.15) -> None:
    corners = region.corners(z_offset=0.5)
    for idx in range(4):
        world.debug.draw_line(
            corners[idx],
            corners[(idx + 1) % 4],
            thickness=0.12,
            color=color,
            life_time=life_time,
            persistent_lines=False,
        )


def draw_scene_debug(world: carla.World, scene: SceneSpec, args: argparse.Namespace) -> None:
    draw_rect(world, scene.support_region, carla.Color(255, 230, 0), life_time=0.2)
    draw_rect(world, scene.valid_crop, carla.Color(0, 255, 0), life_time=0.2)
    center = scene.center + carla.Location(z=0.7)
    world.debug.draw_point(center, size=0.15, color=carla.Color(255, 0, 0), life_time=0.2, persistent_lines=False)
    for route in scene.routes[: args.num_target_vehicles]:
        for p0, p1 in zip(route.locations[:-1], route.locations[1:]):
            world.debug.draw_line(
                p0 + carla.Location(z=0.35),
                p1 + carla.Location(z=0.35),
                thickness=0.08,
                color=carla.Color(0, 180, 255),
                life_time=0.2,
                persistent_lines=False,
            )


def actor_state_dict(
    actor: carla.Actor,
    role: str,
    frame_index: int,
    scene: SceneSpec,
    route: Optional[RouteSpec],
) -> Dict[str, object]:
    tf = actor.get_transform()
    vel = actor.get_velocity()
    bbox = actor.bounding_box.extent
    local_x, local_y = scene.support_region.world_to_local(tf.location)
    in_support = scene.support_region.contains(tf.location)
    in_valid = scene.valid_crop.contains(tf.location)
    row: Dict[str, object] = {
        "actor_id": int(actor.id),
        "role": role,
        "vehicle_type": actor.type_id,
        "transform": {
            "x": float(tf.location.x),
            "y": float(tf.location.y),
            "z": float(tf.location.z),
            "pitch": float(tf.rotation.pitch),
            "yaw": float(tf.rotation.yaw),
            "roll": float(tf.rotation.roll),
        },
        "local_xy_support": {"x": float(local_x), "y": float(local_y)},
        "velocity": {"x": float(vel.x), "y": float(vel.y), "z": float(vel.z)},
        "bbox_extent": {"x": float(bbox.x), "y": float(bbox.y), "z": float(bbox.z)},
        "in_support_region": bool(in_support),
        "in_valid_crop": bool(in_valid),
    }
    if route is not None:
        row["route_id"] = route.route_id
        row["distance_to_scene_center_m"] = float(dist2d(tf.location, scene.center))
        row["inside_core_region"] = bool(dist2d(tf.location, scene.center) <= max(1.0, route.min_center_distance + 3.0))
    return row


def update_target_validation(
    validation: Dict[int, Dict[str, object]],
    actor: carla.Actor,
    scene: SceneSpec,
    frame_index: int,
    core_radius: float,
    core_exit_buffer_m: float,
) -> None:
    loc = actor.get_location()
    state = validation[int(actor.id)]
    d = dist2d(loc, scene.center)
    state["min_center_distance_m"] = min(float(state["min_center_distance_m"]), d)
    state["ever_in_support"] = bool(state["ever_in_support"] or scene.support_region.contains(loc))
    state["ever_in_valid"] = bool(state["ever_in_valid"] or scene.valid_crop.contains(loc))
    if d <= core_radius:
        state["visited_core"] = True
        if state.get("first_core_frame") is None:
            state["first_core_frame"] = frame_index
        state["last_core_frame"] = frame_index
    if state["visited_core"] and d > core_radius + core_exit_buffer_m:
        state["frames_after_core"] = int(state["frames_after_core"]) + 1
    start_loc = state["start_location"]
    if isinstance(start_loc, dict):
        disp = math.hypot(loc.x - float(start_loc["x"]), loc.y - float(start_loc["y"]))
        state["max_displacement_m"] = max(float(state["max_displacement_m"]), disp)


def is_target_passed(
    state: Dict[str, object],
    min_frames_after_core: int = 3,
    min_target_displacement_m: float = 12.0,
) -> bool:
    return bool(
        state.get("visited_core")
        and state.get("ever_in_valid")
        and int(state.get("frames_after_core", 0)) >= int(min_frames_after_core)
        and float(state.get("max_displacement_m", 0.0)) >= float(min_target_displacement_m)
    )


def save_json(path: Path, data: Dict[str, object]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def write_stage(out_dir: Path, stage: str, **details: object) -> None:
    save_json(
        out_dir / "collection_stage.json",
        {
            "schema": "carla_collection_stage_v1",
            "stage": stage,
            "updated_unix_s": time.time(),
            "details": details,
        },
    )


def _configure_line_buffering() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(line_buffering=True)
        except AttributeError:
            pass


def find_ffmpeg_exe() -> Optional[str]:
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is not None:
        return ffmpeg
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return None


def maybe_make_video(frame_dir: Path, video_path: Path, fps: float) -> bool:
    ffmpeg = find_ffmpeg_exe()
    if ffmpeg is None:
        return False
    cmd = [
        ffmpeg,
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frame_dir / "%06d.png"),
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(video_path),
    ]
    subprocess.run(cmd, check=True)
    return True


def destroy_actors(client: carla.Client, actors: Iterable[carla.Actor]) -> None:
    actor_ids = []
    for actor in actors:
        actor_id = getattr(actor, "id", None)
        if actor_id is not None:
            actor_ids.append(int(actor_id))
    if actor_ids:
        commands = [carla.command.DestroyActor(actor_id) for actor_id in sorted(set(actor_ids))]
        try:
            # Do not request an implicit world tick here. In multi-worker
            # no-rendering collection, several post-clip CARLA crashes were
            # isolated to the cleanup stage after complete actor_states had
            # already been written. A synchronous destroy response is useful,
            # but an extra tick during teardown is unnecessary and can trip
            # RenderThread/UE4 shutdown races.
            client.apply_batch_sync(commands, False)
        except RuntimeError as exc:
            print(f"[WARN] Failed to destroy {len(commands)} CARLA actors during cleanup: {exc}")


def carla_port_accepts(host: str, port: int, timeout_s: float = 0.25) -> bool:
    try:
        with socket.create_connection((str(host), int(port)), timeout=float(timeout_s)):
            return True
    except OSError:
        return False


def build_validation_report(
    args: argparse.Namespace,
    scene_meta: Dict[str, object],
    initial_role_counts: Dict[str, int],
    target_validation: Dict[int, Dict[str, object]],
) -> Dict[str, object]:
    validation_report: Dict[str, object] = {
        "min_passed_targets_required": args.min_passed_targets,
        "min_frames_after_core_required": int(args.min_frames_after_core),
        "core_exit_buffer_m": float(args.core_exit_buffer_m),
        "min_target_displacement_m": float(args.min_target_displacement_m),
        "passed_target_count": 0,
        "valid_clip": False,
        "targets": {},
        "vehicle_role_counts": scene_meta.get("vehicle_role_counts", initial_role_counts),
        "background_tm": scene_meta.get("background_spawn_report", {}),
        "support_valid_check": {
            "support_size_m": args.support_size,
            "valid_size_m": args.valid_size,
            "edge_buffer_m_each_side": (args.support_size - args.valid_size) / 2.0,
            "support_larger_than_valid": args.support_size > args.valid_size,
        },
    }
    targets = validation_report["targets"]
    assert isinstance(targets, dict)
    for actor_id, state in target_validation.items():
        state["passed"] = is_target_passed(
            state,
            min_frames_after_core=args.min_frames_after_core,
            min_target_displacement_m=args.min_target_displacement_m,
        )
        if state["passed"]:
            validation_report["passed_target_count"] = int(validation_report["passed_target_count"]) + 1
        targets[str(actor_id)] = state
    validation_report["valid_clip"] = int(validation_report["passed_target_count"]) >= args.min_passed_targets
    return validation_report


def vertical_settling_stats(history: Dict[int, List[Tuple[float, float]]], window: int) -> Dict[str, object]:
    max_abs_vz = 0.0
    max_z_range = 0.0
    actor_count = 0
    for values in history.values():
        recent = values[-window:]
        if not recent:
            continue
        actor_count += 1
        zs = [item[0] for item in recent]
        vzs = [item[1] for item in recent]
        max_abs_vz = max(max_abs_vz, max(abs(vz) for vz in vzs))
        max_z_range = max(max_z_range, max(zs) - min(zs))
    return {
        "actor_count": actor_count,
        "max_abs_vz_mps": float(max_abs_vz),
        "max_z_range_m": float(max_z_range),
    }


def run_vehicle_warmup(
    sync: CarlaSyncContext,
    actors: Sequence[carla.Actor],
    args: argparse.Namespace,
) -> Dict[str, object]:
    min_frames = max(0, int(args.warmup_frames))
    max_frames = min_frames if args.disable_warmup_stability_check else max(min_frames, int(args.warmup_max_frames))
    window = max(1, int(args.warmup_check_window))
    history: Dict[int, List[Tuple[float, float]]] = {int(actor.id): [] for actor in actors}
    final_stats: Dict[str, object] = {"actor_count": len(actors), "max_abs_vz_mps": None, "max_z_range_m": None}
    last_frame_id: Optional[int] = None

    for tick_index in range(max_frames):
        last_frame_id = int(sync.tick())
        for actor in actors:
            try:
                actor_id = int(actor.id)
                loc = actor.get_location()
                vel = actor.get_velocity()
                history.setdefault(actor_id, []).append((float(loc.z), float(vel.z)))
            except RuntimeError:
                continue
        final_stats = vertical_settling_stats(history, window)
        enough_history = tick_index + 1 >= min_frames and all(len(values) >= window for values in history.values() if values)
        if (
            enough_history
            and not args.disable_warmup_stability_check
            and float(final_stats["max_abs_vz_mps"]) <= float(args.warmup_max_abs_vz)
            and float(final_stats["max_z_range_m"]) <= float(args.warmup_max_z_range)
        ):
            settled = True
            return {
                "enabled": True,
                "settled": True,
                "ticks": tick_index + 1,
                "last_world_frame": last_frame_id,
                "check_window": window,
                "thresholds": {
                    "max_abs_vz_mps": float(args.warmup_max_abs_vz),
                    "max_z_range_m": float(args.warmup_max_z_range),
                },
                "final_stats": final_stats,
            }

    if args.disable_warmup_stability_check:
        settled = True
    else:
        settled = max_frames == 0
    return {
        "enabled": True,
        "settled": settled,
        "ticks": max_frames,
        "last_world_frame": last_frame_id,
        "check_window": window,
        "thresholds": {
            "max_abs_vz_mps": float(args.warmup_max_abs_vz),
            "max_z_range_m": float(args.warmup_max_z_range),
        },
        "final_stats": final_stats,
    }


def clear_existing_vehicles(client: carla.Client, world: carla.World) -> int:
    vehicles = list(world.get_actors().filter("vehicle.*"))
    destroy_actors(client, vehicles)
    return len(vehicles)


def clear_existing_sensors(client: carla.Client, world: carla.World) -> int:
    sensors = list(world.get_actors().filter("sensor.*"))
    destroy_actors(client, sensors)
    return len(sensors)


def main() -> int:
    _configure_line_buffering()
    args = parse_args()
    if args.no_rendering_mode and not args.no_video:
        raise ValueError("--no-rendering-mode requires --no-video; RGB camera output needs CARLA rendering.")
    rng = random.Random(args.seed)

    out_dir = Path(args.output_dir)
    frames_dir = out_dir / "frames" / "topdown_rgb"
    ensure_empty_dir(out_dir, args.keep_existing)
    ensure_empty_dir(frames_dir, args.keep_existing)
    write_stage(
        out_dir,
        "initialized",
        no_video=bool(args.no_video),
        no_rendering_mode=bool(args.no_rendering_mode),
        output_dir=str(out_dir),
    )

    client = carla.Client(args.host, args.port)
    client.set_timeout(30.0)
    write_stage(out_dir, "connecting", host=str(args.host), port=int(args.port))

    world = client.get_world()
    if args.town:
        requested_town = Path(args.town).name
        current_town = Path(world.get_map().name).name
        if current_town == requested_town:
            print(f"[INFO] Reusing loaded town: {current_town}")
            write_stage(out_dir, "world_ready", town=current_town, loaded=False)
        else:
            print(f"[INFO] Loading town: {requested_town} (current={current_town}, reason=town_changed)")
            write_stage(out_dir, "load_world", requested_town=requested_town, current_town=current_town, reason="town_changed")
            world = client.load_world(requested_town)
            time.sleep(3.0)
            write_stage(out_dir, "world_ready", town=requested_town, loaded=True)
    else:
        write_stage(out_dir, "world_ready", town=Path(world.get_map().name).name, loaded=False)

    world_map = world.get_map()
    if args.clear_existing_vehicles:
        count = clear_existing_vehicles(client, world)
        print(f"[INFO] Cleared {count} existing vehicles")
    if args.clear_existing_sensors:
        count = clear_existing_sensors(client, world)
        print(f"[INFO] Cleared {count} existing sensors")

    scene = select_scene(world_map, args)
    print(
        f"[INFO] Selected {scene.mode} scene center=({scene.center.x:.2f}, {scene.center.y:.2f}) "
        f"valid={args.valid_size:.1f}m support={args.support_size:.1f}m routes={len(scene.routes)}"
    )

    traffic_manager = (
        client.get_trafficmanager(int(args.traffic_manager_port))
        if args.traffic_manager_port is not None
        else client.get_trafficmanager()
    )
    traffic_manager.set_random_device_seed(args.seed)
    traffic_manager.set_global_distance_to_leading_vehicle(2.5)
    traffic_manager.global_percentage_speed_difference(0.0)
    vehicle_blueprints = candidate_vehicle_blueprints(world, args)
    print(
        f"[INFO] Vehicle blueprint policy={args.vehicle_size_preset} "
        f"candidates={len(vehicle_blueprints)}"
    )

    actors_to_destroy: List[carla.Actor] = []
    camera: Optional[carla.Actor] = None
    camera_queue: "queue.Queue[carla.Image]" = queue.Queue()
    avi_writer: Optional[RawAviWriter] = None
    topdown_video_path: Optional[Path] = None

    traffic_plan = load_traffic_plan(args.traffic_plan_json)
    traffic_plan_vehicle_rows = [
        normalize_plan_vehicle_row(dict(row), route_index)
        for route_index, row in enumerate(traffic_plan.get("vehicles", []))  # type: ignore[union-attr]
    ] if traffic_plan is not None else []
    background_policy = traffic_plan_background_policy(traffic_plan, args)
    initial_role_counts = planned_role_counts(
        traffic_plan_vehicle_rows,
        int(background_policy["requested_count"]),
    )
    scene_meta = {
        "schema": "dynamic_radio_scene_carla_v1",
        "host": args.host,
        "port": args.port,
        "traffic_manager_port": args.traffic_manager_port,
        "town": world_map.name,
        "seed": args.seed,
        "frames_requested": args.frames,
        "fps": args.fps,
        "fixed_delta_seconds": 1.0 / args.fps,
        "traffic_preroll_s": float(args.traffic_preroll_s),
        "traffic_preroll_frames": int(round(max(0.0, float(args.traffic_preroll_s)) * float(args.fps))),
        "scene_id": str(args.scene_id) if str(args.scene_id).strip() else None,
        "scene_type": str(args.scene_type) if str(args.scene_type).strip() else None,
        "scene_mode": scene.mode,
        "scene_info": scene.info,
        "support_region": scene.support_region.to_dict(),
        "valid_crop": scene.valid_crop.to_dict(),
        "edge_buffer_m_each_side": (args.support_size - args.valid_size) / 2.0,
        "junction_core_radius_m": args.junction_core_radius,
        "target_validation_policy": {
            "min_passed_targets": int(args.min_passed_targets),
            "min_frames_after_core": int(args.min_frames_after_core),
            "core_exit_buffer_m": float(args.core_exit_buffer_m),
            "min_target_displacement_m": float(args.min_target_displacement_m),
        },
        "warmup_policy": {
            "warmup_frames": int(args.warmup_frames),
            "warmup_max_frames": int(args.warmup_max_frames),
            "warmup_check_window": int(args.warmup_check_window),
            "warmup_max_abs_vz_mps": float(args.warmup_max_abs_vz),
            "warmup_max_z_range_m": float(args.warmup_max_z_range),
            "stability_check_disabled": bool(args.disable_warmup_stability_check),
        },
        "vehicle_policy": {
            "size_preset": args.vehicle_size_preset,
            "explicit_allowlist": parse_vehicle_allowlist(args.vehicle_allowlist),
            "explicit_target_route_ids": parse_route_id_list(args.target_route_ids),
            "traffic_plan_json": str(args.traffic_plan_json) if args.traffic_plan_json is not None else None,
            "traffic_plan_vehicle_count": len(traffic_plan_vehicle_rows),
            "planned_required_controlled_count": int(initial_role_counts["planned_required_controlled_count"]),
            "planned_optional_controlled_count": int(initial_role_counts["planned_optional_controlled_count"]),
            "requested_background_count": int(initial_role_counts["requested_background_count"]),
            "candidate_blueprint_count": len(vehicle_blueprints),
            "spawn_attempts_per_vehicle": int(args.vehicle_spawn_attempts),
            "passenger_size_limits_m": {
                "min_length": float(args.vehicle_min_length),
                "max_length": float(args.vehicle_max_length),
                "max_width": float(args.vehicle_max_width),
                "max_height": float(args.vehicle_max_height),
            }
            if args.vehicle_size_preset == "passenger"
            else None,
            "excluded_blueprint_tokens": list(PASSENGER_EXCLUDED_BLUEPRINT_TOKENS)
            if args.vehicle_size_preset == "passenger"
            else [],
        },
        "routes": [route.to_dict(compact=True) for route in scene.routes],
        "vehicle_role_policy": {
            "primary_controlled": "required route-controlled vehicles used for key RF dynamics",
            "auxiliary_controlled": "optional or configured-required route-controlled helpers",
            "background_tm": "CARLA Traffic Manager ambient traffic; not route-matched by plan consistency",
            **initial_role_counts,
        },
        "background_tm_policy": background_policy,
        "notes": {
            "sionna_rt_labels": "not generated by this script",
            "actor_states_scope": "all spawned controlled/background_tm vehicles, with support/valid membership per frame",
            "target_route_selection": "traffic plan"
            if args.traffic_plan_json is not None
            else ("explicit route ids" if parse_route_id_list(args.target_route_ids) else "random sample"),
        },
    }
    save_json(out_dir / "scene_meta.json", scene_meta)
    save_json(out_dir / "routes.json", {"routes": [route.to_dict(compact=False) for route in scene.routes]})
    write_stage(out_dir, "scene_selected", route_count=len(scene.routes))

    target_validation: Dict[int, Dict[str, object]] = {}
    validation_report: Optional[Dict[str, object]] = None
    recording_completed = False

    try:
        with CarlaSyncContext(
            world,
            traffic_manager,
            args.fps,
            no_rendering_mode=bool(args.no_rendering_mode),
        ) as sync:
            write_stage(out_dir, "spawn_actors_started", vehicle_count=len(traffic_plan_vehicle_rows))
            targets, target_routes, target_plan_rows, controlled_spawn_report = spawn_target_vehicles(
                client, world, traffic_manager, scene, args, rng, vehicle_blueprints
            )
            backgrounds, background_spawn_report = spawn_background_vehicles(
                client, world, traffic_manager, scene, args, rng, vehicle_blueprints, traffic_plan, target_routes
            )
            actors_to_destroy.extend(targets)
            actors_to_destroy.extend(backgrounds)
            role_counts = {
                "planned_required_controlled_count": int(controlled_spawn_report["planned_required_controlled_count"]),
                "planned_optional_controlled_count": int(controlled_spawn_report["planned_optional_controlled_count"]),
                "requested_background_count": int(background_spawn_report["requested_background_count"]),
                "actual_required_controlled_count": int(controlled_spawn_report["actual_required_controlled_count"]),
                "actual_optional_controlled_count": int(controlled_spawn_report["actual_optional_controlled_count"]),
                "actual_background_count": int(background_spawn_report["actual_background_count"]),
                "actual_total_vehicle_count": int(len(targets) + len(backgrounds)),
            }
            scene_meta["vehicle_role_counts"] = role_counts
            scene_meta["controlled_spawn_report"] = controlled_spawn_report
            scene_meta["background_spawn_report"] = background_spawn_report
            scene_meta["spawned_actor_ids"] = {
                "controlled": [int(actor.id) for actor in targets],
                "background_tm": [int(actor.id) for actor in backgrounds],
                "all": [int(actor.id) for actor in targets + backgrounds],
            }
            save_json(out_dir / "scene_meta.json", scene_meta)
            print(
                "[INFO] Spawned "
                f"required_controlled={role_counts['actual_required_controlled_count']}/"
                f"{role_counts['planned_required_controlled_count']} "
                f"optional_controlled={role_counts['actual_optional_controlled_count']}/"
                f"{role_counts['planned_optional_controlled_count']} "
                f"background_tm={role_counts['actual_background_count']}/"
                f"{role_counts['requested_background_count']}"
            )
            write_stage(out_dir, "spawn_actors_completed", **role_counts)

            write_stage(out_dir, "warmup_started", actor_count=len(targets) + len(backgrounds))
            warmup_report = run_vehicle_warmup(sync, targets + backgrounds, args)
            scene_meta["warmup_result"] = warmup_report
            save_json(out_dir / "scene_meta.json", scene_meta)
            print(
                "[INFO] Warm-up "
                f"ticks={warmup_report['ticks']} settled={warmup_report['settled']} "
                f"stats={warmup_report['final_stats']}"
            )
            if not warmup_report["settled"]:
                print("[WARN] Vehicle vertical settling thresholds were not met before recording.")
            write_stage(out_dir, "warmup_completed", settled=bool(warmup_report["settled"]), ticks=int(warmup_report["ticks"]))

            traffic_preroll_frames = int(round(max(0.0, float(args.traffic_preroll_s)) * float(args.fps)))
            if traffic_preroll_frames > 0:
                print(
                    f"[INFO] Traffic pre-roll frames={traffic_preroll_frames} "
                    f"seconds={traffic_preroll_frames / max(float(args.fps), 1e-6):.2f}"
                )
                write_stage(out_dir, "preroll_started", frames=traffic_preroll_frames)
                for preroll_frame in range(traffic_preroll_frames):
                    release_due_target_vehicles(preroll_frame, traffic_manager, target_routes, target_plan_rows, args)
                    sync.tick()
                write_stage(out_dir, "preroll_completed", frames=traffic_preroll_frames)

            cam_height = args.camera_height if args.camera_height > 0.0 else camera_height_for_region(args.support_size, args.camera_fov)
            if not args.no_video:
                write_stage(out_dir, "topdown_sensor_spawn_started")
                spectator = world.get_spectator()
                spectator.set_transform(
                    carla.Transform(
                        carla.Location(x=scene.center.x, y=scene.center.y, z=scene.center.z + cam_height),
                        carla.Rotation(pitch=-90.0, yaw=scene.yaw_deg, roll=0.0),
                    )
                )
                cam_bp = world.get_blueprint_library().find("sensor.camera.rgb")
                cam_bp.set_attribute("image_size_x", str(args.image_width))
                cam_bp.set_attribute("image_size_y", str(args.image_height))
                cam_bp.set_attribute("fov", str(args.camera_fov))
                cam_bp.set_attribute("sensor_tick", "0.0")
                camera = world.spawn_actor(
                    cam_bp,
                    carla.Transform(
                        carla.Location(x=scene.center.x, y=scene.center.y, z=scene.center.z + cam_height),
                        carla.Rotation(pitch=-90.0, yaw=scene.yaw_deg, roll=0.0),
                    ),
                )
                actors_to_destroy.append(camera)
                camera.listen(camera_queue.put)
                write_stage(out_dir, "topdown_sensor_spawn_completed")

            if not args.no_video and args.raw_avi_fallback and find_ffmpeg_exe() is None:
                topdown_video_path = out_dir / "topdown_raw.avi"
                avi_writer = RawAviWriter(topdown_video_path, args.image_width, args.image_height, args.fps)

            for actor in targets:
                route = target_routes[int(actor.id)]
                target_validation[int(actor.id)] = {
                    "actor_id": int(actor.id),
                    "route_id": route.route_id,
                    "turn_type": route.turn_type,
                    "start_location": loc_to_dict(actor.get_location()),
                    "visited_core": False,
                    "first_core_frame": None,
                    "last_core_frame": None,
                    "frames_after_core": 0,
                    "ever_in_support": False,
                    "ever_in_valid": False,
                    "min_center_distance_m": float("inf"),
                    "max_displacement_m": 0.0,
                    "traffic_plan": {
                        key: value
                        for key, value in target_plan_rows.get(int(actor.id), {}).items()
                        if key != "_actor"
                    },
                }

            tracks_path = out_dir / "frames" / "actor_states.jsonl"
            write_stage(out_dir, "recording_started", frames=int(args.frames), camera_enabled=bool(camera is not None))
            with tracks_path.open("w", encoding="utf-8") as tracks_f:
                for frame_index in range(args.frames):
                    if not args.no_rendering_mode:
                        draw_scene_debug(world, scene, args)
                    release_due_target_vehicles(
                        traffic_preroll_frames + frame_index,
                        traffic_manager,
                        target_routes,
                        target_plan_rows,
                        args,
                    )
                    frame_id = sync.tick()
                    snapshot = world.get_snapshot()
                    if camera is not None:
                        image = get_sensor_frame(camera_queue, frame_id)
                        image.save_to_disk(str(frames_dir / f"{frame_index:06d}.png"))
                        if avi_writer is not None:
                            avi_writer.write_carla_image(image)

                    actors = []
                    for actor in targets:
                        try:
                            actor_id = int(actor.id)
                            update_target_validation(
                                target_validation,
                                actor,
                                scene,
                                frame_index,
                                args.junction_core_radius,
                                args.core_exit_buffer_m,
                            )
                            actor_role = str(target_plan_rows.get(actor_id, {}).get("role", "target"))
                            actor_row = actor_state_dict(actor, actor_role, frame_index, scene, target_routes[actor_id])
                            if actor_id in target_plan_rows:
                                actor_row["traffic_plan"] = {
                                    key: value
                                    for key, value in target_plan_rows[actor_id].items()
                                    if key != "_actor"
                                }
                            actors.append(actor_row)
                        except RuntimeError:
                            continue
                    for actor in backgrounds:
                        try:
                            actors.append(actor_state_dict(actor, "background_tm", frame_index, scene, None))
                        except RuntimeError:
                            continue

                    frame_row = {
                        "frame_index": frame_index,
                        "world_frame": int(frame_id),
                        "timestamp": float(snapshot.timestamp.elapsed_seconds),
                        "actors": actors,
                    }
                    tracks_f.write(json.dumps(frame_row, ensure_ascii=False) + "\n")
                    if frame_index % 20 == 0 or frame_index == args.frames - 1:
                        passed_so_far = sum(
                            1
                            for state in target_validation.values()
                            if is_target_passed(
                                state,
                                min_frames_after_core=args.min_frames_after_core,
                                min_target_displacement_m=args.min_target_displacement_m,
                            )
                        )
                        print(
                            f"[INFO] Frame {frame_index + 1}/{args.frames} "
                            f"actors={len(actors)} targets_passed={passed_so_far}"
                        )
            write_stage(out_dir, "recording_completed", frames=int(args.frames))
            recording_completed = True

    finally:
        if recording_completed and target_validation and validation_report is None:
            validation_report = build_validation_report(args, scene_meta, initial_role_counts, target_validation)
            save_json(out_dir / "validation_report.json", validation_report)
            write_stage(
                out_dir,
                "validation_written",
                valid_clip=bool(validation_report["valid_clip"]),
                passed_target_count=int(validation_report["passed_target_count"]),
                cleanup_pending=True,
            )
        write_stage(out_dir, "cleanup_started", actor_count=len(actors_to_destroy), camera_present=bool(camera is not None))
        if avi_writer is not None:
            avi_writer.close()
        if camera is not None:
            try:
                camera.stop()
            except RuntimeError:
                pass
        if actors_to_destroy and not carla_port_accepts(args.host, args.port):
            print(
                "[WARN] CARLA server is unavailable during cleanup; "
                "skipping actor destruction and letting the supervisor restart the server."
            )
            write_stage(out_dir, "cleanup_skipped_server_unavailable", actor_count=len(actors_to_destroy))
        else:
            destroy_actors(client, actors_to_destroy)
            write_stage(out_dir, "cleanup_completed", actor_count=len(actors_to_destroy))

    if validation_report is None:
        validation_report = build_validation_report(args, scene_meta, initial_role_counts, target_validation)
        save_json(out_dir / "validation_report.json", validation_report)
        write_stage(
            out_dir,
            "validation_written",
            valid_clip=bool(validation_report["valid_clip"]),
            passed_target_count=int(validation_report["passed_target_count"]),
        )

    if not args.no_video and find_ffmpeg_exe() is not None:
        mp4_path = out_dir / "topdown.mp4"
        maybe_make_video(frames_dir, mp4_path, args.fps)
        topdown_video_path = mp4_path

    print(f"[OK] Scene metadata: {out_dir / 'scene_meta.json'}")
    print(f"[OK] Actor states: {out_dir / 'frames' / 'actor_states.jsonl'}")
    print(f"[OK] Validation: {out_dir / 'validation_report.json'}")
    if topdown_video_path is not None:
        print(f"[OK] Top-down video: {topdown_video_path}")
    elif not args.no_video:
        print("[WARN] ffmpeg was not found and raw AVI fallback was disabled; PNG frames were saved instead.")
    if not validation_report["valid_clip"]:
        msg = (
            f"Legacy passed-target diagnostic did not meet threshold: "
            f"passed {validation_report['passed_target_count']} / {args.min_passed_targets}. "
            "Plan-first formal acceptance is decided by trajectory_qa.json, not by passed_target_count."
        )
        print(f"[WARN] {msg}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted by user.")
        sys.exit(130)
