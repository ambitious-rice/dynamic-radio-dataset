#!/usr/bin/env python3
"""
Helpers for extracting Unreal static meshes and converting them into simple
triangle meshes that can be baked into Sionna/Mitsuba PLY assets.
"""

from __future__ import annotations

import json
import math
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np


Vec3 = Tuple[float, float, float]
Face = Tuple[int, int, int]

GLTF_COMPONENT_DTYPES = {
    5120: np.int8,
    5121: np.uint8,
    5122: np.int16,
    5123: np.uint16,
    5125: np.uint32,
    5126: np.float32,
}

GLTF_TYPE_COMPONENTS = {
    "SCALAR": 1,
    "VEC2": 2,
    "VEC3": 3,
    "VEC4": 4,
    "MAT2": 4,
    "MAT3": 9,
    "MAT4": 16,
}


def resolve_umodel_executable(asset_tool_dir: Path) -> Path:
    candidates = [
        asset_tool_dir / "umodel",
        asset_tool_dir / "UEViewer" / "umodel",
        asset_tool_dir / "src" / "UEViewer" / "umodel",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    raise FileNotFoundError(
        f"Could not find umodel under {asset_tool_dir}. "
        "Pass --asset-tool-dir to the built UEViewer directory."
    )


def export_uasset_gltf(
    umodel_exe: Path,
    content_dir: Path,
    asset_relpath: str,
    cache_dir: Path,
    game_tag: str,
) -> Path:
    asset_relpath = asset_relpath.replace("\\", "/").lstrip("/")
    asset_abs = content_dir / asset_relpath
    if not asset_abs.exists():
        raise FileNotFoundError(asset_abs)

    gltf_path = cache_dir / Path(asset_relpath).with_suffix(".gltf")
    if gltf_path.exists():
        return gltf_path

    gltf_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        str(umodel_exe),
        "-export",
        "-gltf",
        f"-game={game_tag}",
        f"-path={content_dir}",
        asset_relpath,
        f"-out={cache_dir}",
    ]
    subprocess.run(cmd, check=True)
    if not gltf_path.exists():
        raise RuntimeError(f"umodel export finished but {gltf_path} was not created")
    return gltf_path


def _load_gltf_buffers(gltf_path: Path, gltf: Dict[str, object]) -> List[bytes]:
    buffers = []
    for buffer_info in gltf.get("buffers", []):
        uri = buffer_info.get("uri")
        if not uri:
            raise RuntimeError(f"Unsupported glTF buffer without URI in {gltf_path}")
        buffers.append((gltf_path.parent / str(uri)).read_bytes())
    return buffers


def _read_accessor(
    gltf: Dict[str, object],
    buffers: Sequence[bytes],
    accessor_index: int,
) -> np.ndarray:
    accessor = gltf["accessors"][accessor_index]
    view = gltf["bufferViews"][accessor["bufferView"]]
    buffer_data = buffers[view["buffer"]]
    dtype = np.dtype(GLTF_COMPONENT_DTYPES[int(accessor["componentType"])])
    components = GLTF_TYPE_COMPONENTS[str(accessor["type"])]
    count = int(accessor["count"])
    view_offset = int(view.get("byteOffset", 0))
    accessor_offset = int(accessor.get("byteOffset", 0))
    offset = view_offset + accessor_offset
    stride = int(view.get("byteStride", dtype.itemsize * components))
    itemsize = dtype.itemsize * components

    if stride == itemsize:
        raw = np.frombuffer(buffer_data, dtype=dtype, count=count * components, offset=offset)
        return raw.reshape((count, components)).copy()

    result = np.empty((count, components), dtype=dtype)
    for row_index in range(count):
        row_offset = offset + row_index * stride
        row = np.frombuffer(buffer_data, dtype=dtype, count=components, offset=row_offset)
        result[row_index] = row
    return result


def _quaternion_to_matrix(quat_xyzw: Sequence[float]) -> np.ndarray:
    x, y, z, w = [float(value) for value in quat_xyzw]
    xx = x * x
    yy = y * y
    zz = z * z
    xy = x * y
    xz = x * z
    yz = y * z
    wx = w * x
    wy = w * y
    wz = w * z
    return np.array(
        [
            [1.0 - 2.0 * (yy + zz), 2.0 * (xy - wz), 2.0 * (xz + wy), 0.0],
            [2.0 * (xy + wz), 1.0 - 2.0 * (xx + zz), 2.0 * (yz - wx), 0.0],
            [2.0 * (xz - wy), 2.0 * (yz + wx), 1.0 - 2.0 * (xx + yy), 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )


def _node_matrix(node: Dict[str, object]) -> np.ndarray:
    if "matrix" in node:
        return np.array(node["matrix"], dtype=np.float64).reshape((4, 4), order="F")

    translation = np.eye(4, dtype=np.float64)
    translation[:3, 3] = np.array(node.get("translation", [0.0, 0.0, 0.0]), dtype=np.float64)

    rotation = _quaternion_to_matrix(node.get("rotation", [0.0, 0.0, 0.0, 1.0]))

    scale = np.eye(4, dtype=np.float64)
    scale_values = np.array(node.get("scale", [1.0, 1.0, 1.0]), dtype=np.float64)
    scale[0, 0], scale[1, 1], scale[2, 2] = scale_values.tolist()
    return translation @ rotation @ scale


def _iter_scene_nodes(gltf: Dict[str, object], scene_index: int) -> Iterable[int]:
    scene = gltf["scenes"][scene_index]
    for node_index in scene.get("nodes", []):
        yield int(node_index)


def load_simple_gltf_mesh(gltf_path: Path) -> Tuple[np.ndarray, np.ndarray]:
    with gltf_path.open("r", encoding="utf-8") as f:
        gltf = json.load(f)

    buffers = _load_gltf_buffers(gltf_path, gltf)
    nodes = gltf.get("nodes", [])
    meshes = gltf.get("meshes", [])
    scene_index = int(gltf.get("scene", 0))
    vertices_out: List[np.ndarray] = []
    faces_out: List[np.ndarray] = []

    def visit(node_index: int, parent_matrix: np.ndarray) -> None:
        node = nodes[node_index]
        world_matrix = parent_matrix @ _node_matrix(node)

        mesh_index = node.get("mesh")
        if mesh_index is not None:
            mesh = meshes[int(mesh_index)]
            for primitive in mesh.get("primitives", []):
                position_accessor = primitive.get("attributes", {}).get("POSITION")
                if position_accessor is None:
                    continue
                positions = _read_accessor(gltf, buffers, int(position_accessor)).astype(np.float64)
                positions_h = np.concatenate([positions, np.ones((positions.shape[0], 1), dtype=np.float64)], axis=1)
                transformed = (world_matrix @ positions_h.T).T[:, :3]
                index_accessor = primitive.get("indices")
                if index_accessor is None:
                    indices = np.arange(transformed.shape[0], dtype=np.int64).reshape((-1, 3))
                else:
                    indices = _read_accessor(gltf, buffers, int(index_accessor)).reshape((-1, 1)).astype(np.int64)
                    indices = indices.reshape((-1, 3))
                vertices_out.append(transformed)
                faces_out.append(indices)

        for child in node.get("children", []):
            visit(int(child), world_matrix)

    identity = np.eye(4, dtype=np.float64)
    for node_index in _iter_scene_nodes(gltf, scene_index):
        visit(node_index, identity)

    if not vertices_out:
        raise RuntimeError(f"No triangle mesh data found in {gltf_path}")

    merged_vertices: List[np.ndarray] = []
    merged_faces: List[np.ndarray] = []
    vertex_offset = 0
    for vertices, faces in zip(vertices_out, faces_out):
        merged_vertices.append(vertices)
        merged_faces.append(faces + vertex_offset)
        vertex_offset += vertices.shape[0]
    return np.vstack(merged_vertices), np.vstack(merged_faces)


def convert_umodel_vertices_to_carla_local(vertices: np.ndarray) -> np.ndarray:
    # UModel's glTF export for CARLA static meshes keeps X as longitudinal while Y/Z
    # follow glTF's right-handed convention. Swapping Y and Z yields the local CARLA
    # mesh convention used by the exporter: X forward, Y lateral, Z up.
    return vertices[:, [0, 2, 1]].astype(np.float64, copy=True)


def compute_aabb(vertices: np.ndarray) -> Dict[str, List[float]]:
    mins = vertices.min(axis=0)
    maxs = vertices.max(axis=0)
    center = (mins + maxs) / 2.0
    size = maxs - mins
    return {
        "min": mins.tolist(),
        "max": maxs.tolist(),
        "center": center.tolist(),
        "size": size.tolist(),
    }


def normalize_mesh_to_aabb_center(vertices: np.ndarray) -> np.ndarray:
    aabb = compute_aabb(vertices)
    center = np.array(aabb["center"], dtype=np.float64)
    return vertices - center


def rotate_xy(vertices: np.ndarray, yaw_deg: float) -> np.ndarray:
    yaw = math.radians(float(yaw_deg))
    c = math.cos(yaw)
    s = math.sin(yaw)
    rotated = vertices.copy()
    x = rotated[:, 0].copy()
    y = rotated[:, 1].copy()
    rotated[:, 0] = x * c - y * s
    rotated[:, 1] = x * s + y * c
    return rotated


def transform_local_mesh_to_world(
    vertices_local: np.ndarray,
    center_world: Sequence[float],
    yaw_deg: float,
    scale_xyz: Optional[Sequence[float]] = None,
) -> np.ndarray:
    vertices = vertices_local.astype(np.float64, copy=True)
    if scale_xyz is not None:
        scale = np.array(scale_xyz, dtype=np.float64).reshape((1, 3))
        vertices *= scale
    vertices = rotate_xy(vertices, yaw_deg)
    vertices += np.array(center_world, dtype=np.float64).reshape((1, 3))
    return vertices


def write_ascii_ply(path: Path, vertices: np.ndarray, faces: np.ndarray) -> None:
    with path.open("w", encoding="ascii") as f:
        f.write("ply\n")
        f.write("format ascii 1.0\n")
        f.write(f"element vertex {vertices.shape[0]}\n")
        f.write("property float x\n")
        f.write("property float y\n")
        f.write("property float z\n")
        f.write(f"element face {faces.shape[0]}\n")
        f.write("property list uchar int vertex_indices\n")
        f.write("end_header\n")
        for x, y, z in vertices:
            f.write(f"{float(x):.6f} {float(y):.6f} {float(z):.6f}\n")
        for a, b, c in faces:
            f.write(f"3 {int(a)} {int(b)} {int(c)}\n")


def load_vehicle_asset_catalog(path: Path) -> Dict[str, Dict[str, object]]:
    with path.open("r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict):
        raise RuntimeError(f"Vehicle asset catalog must be a JSON object: {path}")
    return {str(key): value for key, value in data.items()}
