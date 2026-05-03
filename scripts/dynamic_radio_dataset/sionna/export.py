#!/usr/bin/env python3
"""
Export a CARLA dynamic clip to a Sionna RT/Mitsuba scene package.

Compared with the original bbox-only exporter, this version can:
- keep the support-region ground plane;
- keep low-risk building bbox proxies;
- upgrade vehicles to cached Unreal static meshes when available;
- keep explicit bbox fallback when a vehicle asset cannot be extracted;
- record geometry provenance and verification-friendly metadata in manifest.json.
"""

from __future__ import annotations

import argparse
import glob
import json
import math
import shutil
import sys
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

from dynamic_radio_dataset.assets.unreal_mesh import (
    compute_aabb,
    convert_umodel_vertices_to_carla_local,
    export_uasset_gltf,
    load_simple_gltf_mesh,
    load_vehicle_asset_catalog,
    normalize_mesh_to_aabb_center,
    resolve_umodel_executable,
    transform_local_mesh_to_world,
    write_ascii_ply,
)


Vec3 = Tuple[float, float, float]
Face = Tuple[int, int, int]

VEHICLE_SIZE_MISMATCH_RATIO = 0.35


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Export CARLA actor states to a Sionna RT scene package.")
    parser.add_argument("--dataset-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo"))
    parser.add_argument("--output-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo/sionna_export"))
    parser.add_argument("--snapshot-frame", type=int, default=70)
    parser.add_argument("--include-outside-support", action="store_true")
    parser.add_argument("--include-buildings", action="store_true", help="Query CARLA and export building geometry.")
    parser.add_argument("--vehicle-geometry", choices=["bbox", "catalog_mesh", "mixed"], default="catalog_mesh")
    parser.add_argument("--building-geometry", choices=["bbox", "environment_mesh", "mixed"], default="mixed")
    parser.add_argument(
        "--reuse-building-proxy-from-export",
        type=Path,
        default=None,
        help="Reuse static_buildings_proxy.ply and building manifest from an existing export when CARLA is unavailable.",
    )
    parser.add_argument(
        "--vehicle-asset-catalog",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "assets" / "vehicle_asset_catalog.json",
    )
    parser.add_argument("--asset-tool-dir", type=Path, default=Path("/share1/fzj/tools/src/UEViewer"))
    parser.add_argument("--asset-cache-dir", type=Path, default=Path("/share1/fzj/tools/umodel_cache"))
    parser.add_argument("--content-dir", type=Path, default=None, help="Defaults to <repo>/CarlaUE4/Content.")
    parser.add_argument("--require-vehicle-meshes", action="store_true")
    parser.add_argument("--carla-host", type=str, default="localhost")
    parser.add_argument("--carla-port", type=int, default=2000)
    parser.add_argument("--carla-timeout", type=float, default=30.0)
    parser.add_argument("--static-region-margin", type=float, default=8.0)
    parser.add_argument("--min-building-height", type=float, default=1.5)
    parser.add_argument("--max-buildings", type=int, default=0, help="0 means no limit.")
    parser.add_argument("--vehicle-material", type=str, default="mat-itu_metal")
    parser.add_argument("--ground-material", type=str, default="mat-itu_concrete")
    parser.add_argument("--building-material", type=str, default="mat-itu_concrete")
    parser.add_argument("--ground-z", type=float, default=0.0)
    parser.add_argument("--vehicle-bottom-clearance", type=float, default=0.02)
    parser.add_argument("--no-world-emitter", action="store_true", help="Skip Mitsuba visual emitter in scene.xml.")
    parser.add_argument("--keep-existing", action="store_true")
    return parser.parse_args()


def bootstrap_carla_api() -> Path:
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


def ensure_empty_dir(path: Path, keep_existing: bool) -> None:
    if path.exists() and not keep_existing:
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def load_json(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_frames(path: Path) -> Iterable[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def load_frame(path: Path, frame_index: int) -> Dict[str, object]:
    last_frame = None
    for row in iter_frames(path):
        last_frame = row
        if int(row["frame_index"]) == frame_index:
            return row
    if last_frame is None:
        raise RuntimeError(f"No frames found in {path}")
    raise RuntimeError(f"Frame {frame_index} not found in {path}; last frame is {last_frame['frame_index']}")


def collect_unique_actors(
    path: Path,
    snapshot: Dict[str, object],
    include_outside_support: bool,
) -> List[Dict[str, object]]:
    actors_by_id: Dict[int, Dict[str, object]] = {}
    for frame in iter_frames(path):
        for actor in frame["actors"]:
            if include_outside_support or actor["in_support_region"]:
                actors_by_id.setdefault(int(actor["actor_id"]), actor)

    for actor in snapshot["actors"]:
        if include_outside_support or actor["in_support_region"]:
            actors_by_id[int(actor["actor_id"])] = actor

    return [actors_by_id[actor_id] for actor_id in sorted(actors_by_id)]


def region_corners(region: Dict[str, object], z: float) -> List[Vec3]:
    center = region["center"]
    cx = float(center["x"])
    cy = float(center["y"])
    width = float(region["width_m"])
    height = float(region["height_m"])
    yaw = math.radians(float(region.get("yaw_deg", 0.0)))
    c = math.cos(yaw)
    s = math.sin(yaw)
    corners = []
    for lx, ly in [
        (-width / 2.0, -height / 2.0),
        (width / 2.0, -height / 2.0),
        (width / 2.0, height / 2.0),
        (-width / 2.0, height / 2.0),
    ]:
        corners.append((cx + lx * c - ly * s, cy + lx * s + ly * c, z))
    return corners


def make_ground_mesh(region: Dict[str, object], z: float) -> Tuple[List[Vec3], List[Face]]:
    vertices = region_corners(region, z)
    return vertices, [(0, 1, 2), (0, 2, 3)]


def rotate_xy(x: float, y: float, yaw_deg: float) -> Tuple[float, float]:
    yaw = math.radians(yaw_deg)
    c = math.cos(yaw)
    s = math.sin(yaw)
    return x * c - y * s, x * s + y * c


def normalize_angle_deg(angle: float) -> float:
    return (angle + 180.0) % 360.0 - 180.0


def rotate_xyz(x: float, y: float, z: float, rotation: object) -> Vec3:
    roll = math.radians(float(getattr(rotation, "roll", 0.0)))
    pitch = math.radians(float(getattr(rotation, "pitch", 0.0)))
    yaw = math.radians(float(getattr(rotation, "yaw", 0.0)))

    cr, sr = math.cos(roll), math.sin(roll)
    cp, sp = math.cos(pitch), math.sin(pitch)
    cy, sy = math.cos(yaw), math.sin(yaw)

    x1 = x
    y1 = y * cr - z * sr
    z1 = y * sr + z * cr
    x2 = x1 * cp + z1 * sp
    y2 = y1
    z2 = -x1 * sp + z1 * cp
    return x2 * cy - y2 * sy, x2 * sy + y2 * cy, z2


def box_mesh(
    center: Vec3,
    extent: Vec3,
    yaw_deg: float = 0.0,
    rotation: Optional[object] = None,
) -> Tuple[List[Vec3], List[Face]]:
    cx, cy, cz = center
    ex, ey, ez = extent
    vertices: List[Vec3] = []
    for z in (-ez, ez):
        for x, y in [(-ex, -ey), (ex, -ey), (ex, ey), (-ex, ey)]:
            if rotation is None:
                rx, ry = rotate_xy(x, y, yaw_deg)
                rz = z
            else:
                rx, ry, rz = rotate_xyz(x, y, z, rotation)
            vertices.append((cx + rx, cy + ry, cz + rz))

    faces: List[Face] = [
        (0, 2, 1),
        (0, 3, 2),
        (4, 5, 6),
        (4, 6, 7),
        (0, 1, 5),
        (0, 5, 4),
        (1, 2, 6),
        (1, 6, 5),
        (2, 3, 7),
        (2, 7, 6),
        (3, 0, 4),
        (3, 4, 7),
    ]
    return vertices, faces


def region_contains_xy(region: Dict[str, object], x: float, y: float, margin: float = 0.0) -> bool:
    center = region["center"]
    cx = float(center["x"])
    cy = float(center["y"])
    width = float(region["width_m"]) + 2.0 * margin
    height = float(region["height_m"]) + 2.0 * margin
    yaw = math.radians(float(region.get("yaw_deg", 0.0)))
    dx = x - cx
    dy = y - cy
    c = math.cos(-yaw)
    s = math.sin(-yaw)
    lx = dx * c - dy * s
    ly = dx * s + dy * c
    return abs(lx) <= width / 2.0 and abs(ly) <= height / 2.0


def append_mesh(
    dst_vertices: List[Vec3],
    dst_faces: List[Face],
    vertices: Sequence[Vec3],
    faces: Sequence[Face],
) -> None:
    offset = len(dst_vertices)
    dst_vertices.extend(vertices)
    dst_faces.extend((a + offset, b + offset, c + offset) for a, b, c in faces)


def carla_town_name(map_name: str) -> str:
    return Path(map_name).name


def load_carla_world(args: argparse.Namespace, town: str):
    bootstrap_carla_api()
    import carla  # type: ignore  # noqa: PLC0415

    client = carla.Client(args.carla_host, args.carla_port)
    client.set_timeout(args.carla_timeout)
    world = client.get_world()
    current_town = carla_town_name(world.get_map().name)
    target_town = carla_town_name(town)
    if target_town and current_town != target_town:
        world = client.load_world(target_town)
    return carla, world


def collect_building_bbox_mesh(
    args: argparse.Namespace,
    meta: Dict[str, object],
) -> Tuple[List[Vec3], List[Face], List[Dict[str, object]], Dict[str, object]]:
    carla, world = load_carla_world(args, str(meta.get("town", "")))
    vertices: List[Vec3] = []
    faces: List[Face] = []
    manifest: List[Dict[str, object]] = []
    env_building_count = 0
    env_name_examples: List[str] = []

    if args.building_geometry in {"environment_mesh", "mixed"}:
        try:
            env_objects = world.get_environment_objects(carla.CityObjectLabel.Buildings)
            env_building_count = len(env_objects)
            env_name_examples = sorted({str(obj.name) for obj in env_objects if getattr(obj, "name", None)})[:8]
        except Exception as exc:  # noqa: BLE001
            env_name_examples = [f"environment_mesh_query_failed:{exc.__class__.__name__}"]

    for index, bb in enumerate(world.get_level_bbs(carla.CityObjectLabel.Buildings)):
        loc = bb.location
        ext = bb.extent
        if float(ext.z) * 2.0 < args.min_building_height:
            continue
        if not region_contains_xy(meta["support_region"], float(loc.x), float(loc.y), args.static_region_margin):
            continue

        box_vertices, box_faces = box_mesh(
            center=(float(loc.x), float(loc.y), float(loc.z)),
            extent=(float(ext.x), float(ext.y), float(ext.z)),
            rotation=getattr(bb, "rotation", None),
        )
        append_mesh(vertices, faces, box_vertices, box_faces)
        manifest.append(
            {
                "source": "carla.get_level_bbs",
                "geometry_mode": "bbox",
                "label": "Buildings",
                "index": int(index),
                "center": {"x": float(loc.x), "y": float(loc.y), "z": float(loc.z)},
                "extent": {"x": float(ext.x), "y": float(ext.y), "z": float(ext.z)},
                "rotation": {
                    "pitch": float(getattr(getattr(bb, "rotation", None), "pitch", 0.0)),
                    "yaw": float(getattr(getattr(bb, "rotation", None), "yaw", 0.0)),
                    "roll": float(getattr(getattr(bb, "rotation", None), "roll", 0.0)),
                },
                "radio_material": args.building_material,
                "fallback_reason": "environment_mesh_export_not_available",
            }
        )
        if args.max_buildings and len(manifest) >= args.max_buildings:
            break

    stats = {
        "building_geometry_mode": args.building_geometry,
        "matched_building_mesh_count": 0,
        "unmatched_building_proxy_count": len(manifest),
        "environment_building_object_count": env_building_count,
        "environment_building_name_examples": env_name_examples,
    }
    return vertices, faces, manifest, stats


def load_building_proxy_from_export(reference_export_dir: Path) -> Tuple[Path, List[Dict[str, object]], Dict[str, object]]:
    manifest_path = reference_export_dir / "manifest.json"
    manifest = load_json(manifest_path)
    static_geometry = manifest.get("static_geometry", {})
    mesh_rel = static_geometry.get("mesh")
    if not mesh_rel:
        raise RuntimeError(f"No static building mesh recorded in {manifest_path}")
    mesh_src = reference_export_dir / str(mesh_rel)
    if not mesh_src.exists():
        raise FileNotFoundError(mesh_src)
    stats = {
        "building_geometry_mode": static_geometry.get("building_geometry_mode", "bbox"),
        "matched_building_mesh_count": int(static_geometry.get("matched_building_mesh_count", 0)),
        "unmatched_building_proxy_count": int(static_geometry.get("unmatched_building_proxy_count", 0)),
        "environment_building_object_count": int(static_geometry.get("environment_building_object_count", 0)),
        "environment_building_name_examples": static_geometry.get("environment_building_name_examples", []),
    }
    return mesh_src, manifest.get("buildings", []), stats


def material_xml() -> str:
    return """\t<bsdf type=\"diffuse\" id=\"mat-itu_metal\" name=\"mat-itu_metal\">
\t\t<rgb value=\"0.000000 0.001646 0.290089\" name=\"reflectance\"/>
\t</bsdf>
\t<bsdf type=\"twosided\" id=\"mat-itu_concrete\" name=\"mat-itu_concrete\">
\t\t<bsdf type=\"principled\" name=\"bsdf\">
\t\t\t<rgb value=\"0.800000 0.800000 0.800000\" name=\"base_color\"/>
\t\t\t<float name=\"metallic\" value=\"0.000000\"/>
\t\t\t<float name=\"roughness\" value=\"0.250000\"/>
\t\t\t<float name=\"specular\" value=\"0.500000\"/>
\t\t</bsdf>
\t</bsdf>"""


def emitter_xml() -> str:
    return """\t<emitter type=\"constant\" id=\"World\" name=\"World\">
\t\t<rgb value=\"1.000000 1.000000 1.000000\" name=\"radiance\"/>
\t</emitter>"""


def shape_xml(shape_id: str, mesh_name: str, material_id: str) -> str:
    return f"""\t<shape type=\"ply\" id=\"{shape_id}\" name=\"{shape_id}\">
\t\t<string name=\"filename\" value=\"meshes/{mesh_name}\"/>
\t\t<boolean name=\"face_normals\" value=\"true\"/>
\t\t<ref id=\"{material_id}\" name=\"bsdf\"/>
\t</shape>"""


def write_scene_xml(path: Path, shapes: Sequence[Tuple[str, str, str]], add_world_emitter: bool) -> None:
    lines = [
        '<scene version="2.1.0">',
        "",
        "<!-- Materials -->",
        material_xml(),
        "",
    ]
    if add_world_emitter:
        lines.extend(["<!-- Emitters -->", emitter_xml(), ""])
    lines.extend(["<!-- Shapes -->"])
    for shape_id, mesh_name, material_id in shapes:
        lines.append(shape_xml(shape_id, mesh_name, material_id))
    lines.append("</scene>")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def actor_bbox_dimensions(actor: Dict[str, object]) -> List[float]:
    bbox = actor["bbox_extent"]
    return [2.0 * float(bbox["x"]), 2.0 * float(bbox["y"]), 2.0 * float(bbox["z"])]


def actor_world_center(actor: Dict[str, object], center_z_offset_m: float) -> List[float]:
    tf = actor["transform"]
    return [float(tf["x"]), float(tf["y"]), float(tf["z"]) + float(center_z_offset_m)]


def size_error_stats(mesh_size: Sequence[float], bbox_size: Sequence[float]) -> Dict[str, List[float]]:
    abs_error = [abs(float(mesh_size[i]) - float(bbox_size[i])) for i in range(3)]
    rel_error = [
        abs_error[i] / max(float(bbox_size[i]), 1.0e-6)
        for i in range(3)
    ]
    return {"abs_m": abs_error, "rel": rel_error}


def make_vehicle_box_geometry(
    actor: Dict[str, object],
    bottom_clearance: float,
    material: str,
) -> Dict[str, object]:
    bbox = actor["bbox_extent"]
    center_z_offset = float(bbox["z"]) + bottom_clearance
    center = actor_world_center(actor, center_z_offset)
    extent = (float(bbox["x"]), float(bbox["y"]), float(bbox["z"]))
    vertices, faces = box_mesh(center=tuple(center), extent=extent, yaw_deg=float(actor["transform"]["yaw"]))
    bbox_size = actor_bbox_dimensions(actor)
    return {
        "geometry_mode": "bbox",
        "asset_source": "bbox_proxy",
        "fallback_reason": None,
        "mesh_vertices": vertices,
        "mesh_faces": faces,
        "mesh_file": None,
        "center_z_offset_m": center_z_offset,
        "mesh_reference_yaw_deg": float(actor["transform"]["yaw"]),
        "asset_yaw_offset_deg": 0.0,
        "mesh_aabb_local": {
            "center": [0.0, 0.0, 0.0],
            "size": bbox_size,
            "min": [-float(bbox["x"]), -float(bbox["y"]), -float(bbox["z"])],
            "max": [float(bbox["x"]), float(bbox["y"]), float(bbox["z"])],
        },
        "bbox_size_m": bbox_size,
        "mesh_size_m": bbox_size,
        "size_error": {"abs_m": [0.0, 0.0, 0.0], "rel": [0.0, 0.0, 0.0]},
        "geometry_proxy": "oriented_bbox_box",
        "approximate_match": False,
    }


def make_vehicle_catalog_geometry(
    actor: Dict[str, object],
    catalog_entry: Dict[str, object],
    content_dir: Path,
    asset_cache_dir: Path,
    umodel_exe: Path,
    bottom_clearance: float,
) -> Dict[str, object]:
    asset_relpath = str(catalog_entry["asset_relpath"])
    game_tag = str(catalog_entry.get("game_tag", "ue4.26"))
    gltf_path = export_uasset_gltf(
        umodel_exe=umodel_exe,
        content_dir=content_dir,
        asset_relpath=asset_relpath,
        cache_dir=asset_cache_dir,
        game_tag=game_tag,
    )
    vertices_gltf, faces = load_simple_gltf_mesh(gltf_path)
    vertices_local = convert_umodel_vertices_to_carla_local(vertices_gltf)
    raw_aabb = compute_aabb(vertices_local)
    normalized_local = normalize_mesh_to_aabb_center(vertices_local)
    local_aabb = compute_aabb(normalized_local)

    bbox_size = actor_bbox_dimensions(actor)
    mesh_size = [float(value) for value in raw_aabb["size"]]
    errors = size_error_stats(mesh_size, bbox_size)
    if max(errors["rel"]) > VEHICLE_SIZE_MISMATCH_RATIO and not bool(catalog_entry.get("approximate_match", False)):
        raise RuntimeError(
            f"Mesh AABB mismatch too large for {actor['vehicle_type']}: "
            f"mesh={mesh_size}, bbox={bbox_size}, rel={errors['rel']}"
        )

    asset_yaw_offset_deg = float(catalog_entry.get("asset_yaw_offset_deg", 0.0))
    center_z_offset = float(local_aabb["size"][2]) / 2.0 + bottom_clearance
    world_center = actor_world_center(actor, center_z_offset)
    world_vertices = transform_local_mesh_to_world(
        normalized_local,
        center_world=world_center,
        yaw_deg=float(actor["transform"]["yaw"]) + asset_yaw_offset_deg,
        scale_xyz=catalog_entry.get("scale_xyz"),
    )
    world_faces = faces.astype(int)
    return {
        "geometry_mode": "catalog_mesh",
        "asset_source": str(catalog_entry.get("asset_source", "carla_static_mesh")),
        "fallback_reason": None,
        "mesh_vertices": [tuple(float(value) for value in row) for row in world_vertices.tolist()],
        "mesh_faces": [tuple(int(value) for value in row) for row in world_faces.tolist()],
        "mesh_file": None,
        "center_z_offset_m": center_z_offset,
        "mesh_reference_yaw_deg": float(actor["transform"]["yaw"]),
        "asset_yaw_offset_deg": asset_yaw_offset_deg,
        "mesh_aabb_local": local_aabb,
        "bbox_size_m": bbox_size,
        "mesh_size_m": mesh_size,
        "size_error": errors,
        "geometry_proxy": "catalog_static_mesh",
        "approximate_match": bool(catalog_entry.get("approximate_match", False)),
        "asset_relpath": asset_relpath,
        "cached_gltf": str(gltf_path),
    }


def build_vehicle_geometry(
    actor: Dict[str, object],
    args: argparse.Namespace,
    catalog: Dict[str, Dict[str, object]],
    content_dir: Path,
    asset_cache_dir: Path,
    umodel_exe: Optional[Path],
) -> Dict[str, object]:
    vehicle_type = str(actor["vehicle_type"])
    if args.vehicle_geometry == "bbox":
        return make_vehicle_box_geometry(actor, args.vehicle_bottom_clearance, args.vehicle_material)

    catalog_entry = catalog.get(vehicle_type)
    if catalog_entry is None:
        geometry = make_vehicle_box_geometry(actor, args.vehicle_bottom_clearance, args.vehicle_material)
        geometry["fallback_reason"] = "catalog_missing"
        return geometry

    if umodel_exe is None:
        geometry = make_vehicle_box_geometry(actor, args.vehicle_bottom_clearance, args.vehicle_material)
        geometry["fallback_reason"] = "umodel_missing"
        geometry["asset_source"] = "bbox_proxy"
        return geometry

    try:
        return make_vehicle_catalog_geometry(
            actor=actor,
            catalog_entry=catalog_entry,
            content_dir=content_dir,
            asset_cache_dir=asset_cache_dir,
            umodel_exe=umodel_exe,
            bottom_clearance=args.vehicle_bottom_clearance,
        )
    except Exception as exc:  # noqa: BLE001
        geometry = make_vehicle_box_geometry(actor, args.vehicle_bottom_clearance, args.vehicle_material)
        geometry["fallback_reason"] = f"{exc.__class__.__name__}:{exc}"
        geometry["asset_source"] = "bbox_proxy"
        geometry["approximate_match"] = bool(catalog_entry.get("approximate_match", False))
        return geometry


def actor_to_motion(
    actor: Dict[str, object],
    center_z_offset_m: float,
    material: str,
    reference_yaw_deg: float = 0.0,
    geometry_mode: str = "bbox",
    asset_source: str = "bbox_proxy",
    fallback_reason: Optional[str] = None,
) -> Dict[str, object]:
    tf = actor["transform"]
    yaw_deg = float(tf["yaw"])
    yaw_delta_deg = normalize_angle_deg(yaw_deg - reference_yaw_deg)
    return {
        "sionna_object": f"car_{actor['actor_id']}",
        "actor_id": int(actor["actor_id"]),
        "role": actor["role"],
        "vehicle_type": actor["vehicle_type"],
        "position": [float(tf["x"]), float(tf["y"]), float(tf["z"]) + float(center_z_offset_m)],
        "yaw_deg_carla": yaw_deg,
        "mesh_reference_yaw_deg": float(reference_yaw_deg),
        "yaw_delta_deg_sionna": yaw_delta_deg,
        "yaw_rad_sionna_like": math.radians(yaw_delta_deg),
        "velocity": [float(actor["velocity"]["x"]), float(actor["velocity"]["y"]), float(actor["velocity"]["z"])],
        "bbox_extent": actor["bbox_extent"],
        "radio_material": material,
        "geometry_mode": geometry_mode,
        "asset_source": asset_source,
        "fallback_reason": fallback_reason,
        "mesh_center_z_offset_m": float(center_z_offset_m),
        "in_support_region": bool(actor["in_support_region"]),
        "in_valid_crop": bool(actor["in_valid_crop"]),
    }


def write_motion_jsonl(
    src_tracks: Path,
    dst_motion: Path,
    include_outside_support: bool,
    material: str,
    vehicle_geometry_by_id: Dict[int, Dict[str, object]],
    reference_yaws: Dict[int, float],
) -> Tuple[int, int]:
    frame_count = 0
    actor_updates = 0
    with dst_motion.open("w", encoding="utf-8") as out:
        for frame in iter_frames(src_tracks):
            actors = []
            for actor in frame["actors"]:
                if include_outside_support or actor["in_support_region"]:
                    actor_id = int(actor["actor_id"])
                    geometry = vehicle_geometry_by_id[actor_id]
                    actors.append(
                        actor_to_motion(
                            actor=actor,
                            center_z_offset_m=float(geometry["center_z_offset_m"]),
                            material=material,
                            reference_yaw_deg=reference_yaws.get(actor_id, float(actor["transform"]["yaw"])),
                            geometry_mode=str(geometry["geometry_mode"]),
                            asset_source=str(geometry["asset_source"]),
                            fallback_reason=geometry.get("fallback_reason"),
                        )
                    )
            out.write(
                json.dumps(
                    {
                        "frame_index": int(frame["frame_index"]),
                        "world_frame": int(frame["world_frame"]),
                        "timestamp": float(frame["timestamp"]),
                        "vehicles": actors,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
            frame_count += 1
            actor_updates += len(actors)
    return frame_count, actor_updates


def build_vehicle_manifest_entry(
    actor: Dict[str, object],
    geometry: Dict[str, object],
    mesh_name: str,
    material: str,
) -> Dict[str, object]:
    center_z_offset = float(geometry["center_z_offset_m"])
    motion_entry = actor_to_motion(
        actor=actor,
        center_z_offset_m=center_z_offset,
        material=material,
        reference_yaw_deg=float(actor["transform"]["yaw"]),
        geometry_mode=str(geometry["geometry_mode"]),
        asset_source=str(geometry["asset_source"]),
        fallback_reason=geometry.get("fallback_reason"),
    )
    motion_entry.update(
        {
            "mesh_file": f"meshes/{mesh_name}",
            "requested_vehicle_geometry_mode": None,
            "asset_relpath": geometry.get("asset_relpath"),
            "cached_gltf": geometry.get("cached_gltf"),
            "asset_yaw_offset_deg": float(geometry.get("asset_yaw_offset_deg", 0.0)),
            "mesh_aabb_local": geometry["mesh_aabb_local"],
            "bbox_size_m": geometry["bbox_size_m"],
            "mesh_size_m": geometry["mesh_size_m"],
            "size_error": geometry["size_error"],
            "approximate_match": bool(geometry.get("approximate_match", False)),
            "geometry_proxy": geometry["geometry_proxy"],
        }
    )
    return motion_entry


def main() -> int:
    args = parse_args()
    tracks_path = args.dataset_dir / "frames" / "actor_states.jsonl"
    meta_path = args.dataset_dir / "scene_meta.json"
    if not tracks_path.exists():
        raise FileNotFoundError(tracks_path)
    if not meta_path.exists():
        raise FileNotFoundError(meta_path)

    repo_root = _find_repo_root(Path(__file__).resolve())
    content_dir = args.content_dir or (repo_root / "CarlaUE4" / "Content")
    ensure_empty_dir(args.output_dir, args.keep_existing)
    mesh_dir = args.output_dir / "meshes"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    meta = load_json(meta_path)
    snapshot = load_frame(tracks_path, args.snapshot_frame)
    scene_actors = collect_unique_actors(tracks_path, snapshot, args.include_outside_support)
    reference_yaws = {int(actor["actor_id"]): float(actor["transform"]["yaw"]) for actor in scene_actors}

    catalog = {}
    umodel_exe: Optional[Path] = None
    if args.vehicle_geometry != "bbox":
        catalog = load_vehicle_asset_catalog(args.vehicle_asset_catalog)
        try:
            umodel_exe = resolve_umodel_executable(args.asset_tool_dir)
        except FileNotFoundError as exc:
            if args.require_vehicle_meshes:
                raise
            print(f"[WARN] {exc}; vehicles without cached meshes will fall back to bbox.")
        args.asset_cache_dir.mkdir(parents=True, exist_ok=True)

    shapes: List[Tuple[str, str, str]] = []
    ground_vertices, ground_faces = make_ground_mesh(meta["support_region"], args.ground_z)
    write_ascii_ply(
        mesh_dir / "road_support_region.ply",
        vertices=np.array(ground_vertices, dtype=float),
        faces=np.array(ground_faces, dtype=int),
    )
    shapes.append(("road_support_region", "road_support_region.ply", args.ground_material))

    building_manifest: List[Dict[str, object]] = []
    building_stats = {
        "building_geometry_mode": args.building_geometry if args.include_buildings else "disabled",
        "matched_building_mesh_count": 0,
        "unmatched_building_proxy_count": 0,
        "environment_building_object_count": 0,
        "environment_building_name_examples": [],
    }
    if args.include_buildings:
        if args.reuse_building_proxy_from_export is not None:
            mesh_src, building_manifest, building_stats = load_building_proxy_from_export(args.reuse_building_proxy_from_export)
            shutil.copy2(mesh_src, mesh_dir / "static_buildings_proxy.ply")
            shapes.append(("static_buildings_proxy", "static_buildings_proxy.ply", args.building_material))
        else:
            building_vertices, building_faces, building_manifest, building_stats = collect_building_bbox_mesh(args, meta)
            if building_vertices:
                write_ascii_ply(
                    mesh_dir / "static_buildings_proxy.ply",
                    vertices=np.array(building_vertices, dtype=float),
                    faces=np.array(building_faces, dtype=int),
                )
                shapes.append(("static_buildings_proxy", "static_buildings_proxy.ply", args.building_material))

    vehicle_manifest = []
    vehicle_geometry_by_id: Dict[int, Dict[str, object]] = {}
    vehicle_mesh_fallbacks: List[Dict[str, object]] = []
    for actor in scene_actors:
        actor_id = int(actor["actor_id"])
        geometry = build_vehicle_geometry(
            actor=actor,
            args=args,
            catalog=catalog,
            content_dir=content_dir,
            asset_cache_dir=args.asset_cache_dir,
            umodel_exe=umodel_exe,
        )
        object_name = f"car_{actor_id}"
        mesh_name = f"{object_name}.ply"
        geometry["mesh_file"] = f"meshes/{mesh_name}"
        write_ascii_ply(
            mesh_dir / mesh_name,
            vertices=np.array(geometry["mesh_vertices"], dtype=float),
            faces=np.array(geometry["mesh_faces"], dtype=int),
        )
        shapes.append((object_name, mesh_name, args.vehicle_material))
        manifest_entry = build_vehicle_manifest_entry(actor, geometry, mesh_name, args.vehicle_material)
        manifest_entry["requested_vehicle_geometry_mode"] = args.vehicle_geometry
        vehicle_manifest.append(manifest_entry)
        vehicle_geometry_by_id[actor_id] = geometry
        if geometry.get("fallback_reason"):
            vehicle_mesh_fallbacks.append(
                {
                    "actor_id": actor_id,
                    "vehicle_type": actor["vehicle_type"],
                    "reason": geometry["fallback_reason"],
                }
            )

    if args.require_vehicle_meshes and args.vehicle_geometry != "bbox" and vehicle_mesh_fallbacks:
        raise RuntimeError(f"Vehicle mesh fallback occurred with --require-vehicle-meshes: {vehicle_mesh_fallbacks}")

    write_scene_xml(args.output_dir / "scene.xml", shapes, add_world_emitter=not args.no_world_emitter)
    frame_count, actor_updates = write_motion_jsonl(
        src_tracks=tracks_path,
        dst_motion=args.output_dir / "motion.jsonl",
        include_outside_support=args.include_outside_support,
        material=args.vehicle_material,
        vehicle_geometry_by_id=vehicle_geometry_by_id,
        reference_yaws=reference_yaws,
    )

    manifest = {
        "schema": "carla_to_sionna_dynamic_scene_v3",
        "source_dataset": str(args.dataset_dir),
        "snapshot_frame": int(args.snapshot_frame),
        "scene_xml": "scene.xml",
        "motion_jsonl": "motion.jsonl",
        "coordinate_system": (
            "CARLA world coordinates, z-up; vehicle meshes are baked at a per-actor reference pose; "
            "motion orientation is the relative yaw from that reference pose."
        ),
        "asset_pipeline": {
            "vehicle_geometry_mode": args.vehicle_geometry,
            "building_geometry_mode": args.building_geometry if args.include_buildings else "disabled",
            "reuse_building_proxy_from_export": str(args.reuse_building_proxy_from_export) if args.reuse_building_proxy_from_export else None,
            "vehicle_asset_catalog": str(args.vehicle_asset_catalog),
            "asset_tool_dir": str(args.asset_tool_dir),
            "asset_cache_dir": str(args.asset_cache_dir),
            "content_dir": str(content_dir),
            "require_vehicle_meshes": bool(args.require_vehicle_meshes),
            "vehicle_bottom_clearance_m": float(args.vehicle_bottom_clearance),
        },
        "materials": {
            "vehicles": args.vehicle_material,
            "road_support_region": args.ground_material,
            "buildings": args.building_material if args.include_buildings else "not exported",
        },
        "static_geometry": {
            "buildings_enabled": bool(args.include_buildings),
            "building_source": (
                "CARLA world.get_level_bbs(CityObjectLabel.Buildings) bbox proxies with optional environment-object "
                "enumeration for future mesh matching"
                if args.include_buildings
                else None
            ),
            "building_geometry_mode": building_stats["building_geometry_mode"],
            "building_count": len(building_manifest),
            "matched_building_mesh_count": int(building_stats["matched_building_mesh_count"]),
            "unmatched_building_proxy_count": int(building_stats["unmatched_building_proxy_count"]),
            "environment_building_object_count": int(building_stats["environment_building_object_count"]),
            "environment_building_name_examples": building_stats["environment_building_name_examples"],
            "static_region_margin_m": float(args.static_region_margin),
            "min_building_height_m": float(args.min_building_height),
            "mesh": "meshes/static_buildings_proxy.ply" if building_manifest else None,
        },
        "support_region": meta["support_region"],
        "valid_crop": meta["valid_crop"],
        "snapshot_vehicle_count": len(vehicle_manifest),
        "vehicle_mesh_count": sum(1 for vehicle in vehicle_manifest if vehicle["geometry_mode"] == "catalog_mesh"),
        "vehicle_bbox_fallback_count": sum(1 for vehicle in vehicle_manifest if vehicle["geometry_mode"] == "bbox"),
        "vehicle_mesh_fallbacks": vehicle_mesh_fallbacks,
        "motion_frame_count": frame_count,
        "motion_vehicle_updates": actor_updates,
        "verification": {
            "sionna_import_ok": None,
            "topdown_render_ok": None,
            "verification_report": None,
        },
        "buildings": building_manifest,
        "vehicles": vehicle_manifest,
    }
    (args.output_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] Wrote scene XML: {args.output_dir / 'scene.xml'}")
    print(f"[OK] Wrote meshes: {mesh_dir}")
    print(f"[OK] Vehicle catalog meshes: {manifest['vehicle_mesh_count']}")
    print(f"[OK] Vehicle bbox fallbacks: {manifest['vehicle_bbox_fallback_count']}")
    if args.include_buildings:
        print(f"[OK] Wrote building bbox proxies: {len(building_manifest)}")
    print(f"[OK] Wrote motion: {args.output_dir / 'motion.jsonl'} ({frame_count} frames, {actor_updates} updates)")
    print(f"[OK] Wrote manifest: {args.output_dir / 'manifest.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
