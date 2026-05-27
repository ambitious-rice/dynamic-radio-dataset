#!/usr/bin/env python3
"""
Run consistency checks and lightweight visual verification for a Sionna export.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, Iterable, List, Sequence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Verify a CARLA-to-Sionna scene export.")
    parser.add_argument("--export-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo/sionna_export"))
    parser.add_argument("--scene-file", type=str, default="scene.xml")
    parser.add_argument("--motion-file", type=str, default="motion.jsonl")
    parser.add_argument("--frequency", type=float, default=5.89e9)
    parser.add_argument("--update-frame", type=int, default=80)
    parser.add_argument("--update-object", type=str, default="car_58")
    parser.add_argument("--preview-frames", type=str, default="", help="Comma-separated motion frame indices.")
    parser.add_argument("--preview-resolution", type=int, nargs=2, default=[640, 640], metavar=("W", "H"))
    parser.add_argument("--full-resolution", type=int, nargs=2, default=[640, 640], metavar=("W", "H"))
    parser.add_argument("--fov", type=float, default=52.0)
    parser.add_argument("--camera-height", type=float, default=0.0)
    parser.add_argument("--camera-target-z", type=float, default=0.0)
    parser.add_argument("--preview-num-samples", type=int, default=48)
    parser.add_argument("--full-num-samples", type=int, default=48)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--render-full-video", action="store_true")
    parser.add_argument("--output-video", type=Path, default=None)
    parser.add_argument("--verification-dir", type=Path, default=None)
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_motion(path: Path) -> Iterable[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def computed_camera_height(region: Dict[str, object], fov_deg: float) -> float:
    width = float(region["width_m"])
    height = float(region["height_m"])
    half_extent = max(width, height) / 2.0
    return half_extent / math.tan(math.radians(fov_deg) / 2.0) * 1.08


def apply_motion_frame(scene, car_names: Sequence[str], row: Dict[str, object], hidden_position: Sequence[float]) -> None:
    present = set()
    for vehicle in row["vehicles"]:
        name = str(vehicle["sionna_object"])
        obj = scene.get(name)
        if obj is None:
            continue
        obj.position = vehicle["position"]
        obj.orientation = [vehicle["yaw_rad_sionna_like"], 0.0, 0.0]
        obj.velocity = vehicle["velocity"]
        present.add(name)

    for name in car_names:
        if name in present:
            continue
        obj = scene.get(name)
        if obj is not None:
            obj.position = hidden_position


def find_ffmpeg() -> str:
    try:
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            return ffmpeg
    raise RuntimeError("ffmpeg not found. Install imageio-ffmpeg in the active environment.")


def encode_video(frames_dir: Path, output_video: Path, fps: float) -> None:
    output_video.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        find_ffmpeg(),
        "-y",
        "-framerate",
        str(fps),
        "-i",
        str(frames_dir / "frame_%06d.png"),
        "-vf",
        "pad=ceil(iw/2)*2:ceil(ih/2)*2",
        "-c:v",
        "libx264",
        "-pix_fmt",
        "yuv420p",
        str(output_video),
    ]
    subprocess.run(cmd, check=True)


def parse_scene_xml(scene_path: Path) -> Dict[str, object]:
    tree = ET.parse(scene_path)
    root = tree.getroot()
    materials = set()
    shapes = []
    missing_material_refs = []
    for child in root:
        if child.tag == "bsdf":
            material_id = child.attrib.get("id")
            if material_id:
                materials.add(material_id)
    for child in root:
        if child.tag != "shape":
            continue
        shape_id = child.attrib.get("id")
        ref_id = None
        filename = None
        for sub in child:
            if sub.tag == "ref":
                ref_id = sub.attrib.get("id")
            if sub.tag == "string" and sub.attrib.get("name") == "filename":
                filename = sub.attrib.get("value")
        if ref_id and ref_id not in materials:
            missing_material_refs.append({"shape_id": shape_id, "material_id": ref_id})
        shapes.append({"shape_id": shape_id, "material_id": ref_id, "filename": filename})
    return {
        "materials": sorted(materials),
        "shapes": shapes,
        "missing_material_refs": missing_material_refs,
    }


def pick_preview_frames(manifest: Dict[str, object], motion_rows: List[Dict[str, object]], explicit: str) -> List[int]:
    if explicit.strip():
        return sorted({int(item.strip()) for item in explicit.split(",") if item.strip()})
    first = int(motion_rows[0]["frame_index"])
    last = int(motion_rows[-1]["frame_index"])
    snapshot = int(manifest.get("snapshot_frame", motion_rows[len(motion_rows) // 2]["frame_index"]))
    midpoint = int(motion_rows[len(motion_rows) // 2]["frame_index"])
    frames = {first, snapshot, midpoint, last}
    return sorted(frames)


def nearest_motion_rows(rows: Sequence[Dict[str, object]], frame_indices: Sequence[int]) -> List[Dict[str, object]]:
    result = []
    for target in frame_indices:
        nearest = min(rows, key=lambda row: abs(int(row["frame_index"]) - int(target)))
        result.append(nearest)
    unique = []
    seen = set()
    for row in result:
        frame_index = int(row["frame_index"])
        if frame_index in seen:
            continue
        seen.add(frame_index)
        unique.append(row)
    return unique


def summarize_vehicle_geometry(manifest: Dict[str, object], expected_clearance: float) -> Dict[str, object]:
    summary_rows = []
    warnings = []
    blockers = []
    for vehicle in manifest.get("vehicles", []):
        local_aabb = vehicle.get("mesh_aabb_local") or {}
        mesh_size = vehicle.get("mesh_size_m") or []
        size_error = vehicle.get("size_error") or {"rel": [0.0, 0.0, 0.0], "abs_m": [0.0, 0.0, 0.0]}
        half_height = 0.0
        if isinstance(local_aabb, dict):
            local_size = local_aabb.get("size") or [0.0, 0.0, 0.0]
            if len(local_size) == 3:
                half_height = float(local_size[2]) / 2.0
        center_z_offset = float(vehicle.get("mesh_center_z_offset_m", 0.0))
        bottom_clearance = center_z_offset - half_height
        bottom_alignment_error = abs(bottom_clearance - expected_clearance)
        row = {
            "actor_id": int(vehicle["actor_id"]),
            "vehicle_type": vehicle["vehicle_type"],
            "geometry_mode": vehicle.get("geometry_mode"),
            "asset_source": vehicle.get("asset_source"),
            "fallback_reason": vehicle.get("fallback_reason"),
            "mesh_file": vehicle.get("mesh_file"),
            "approximate_match": bool(vehicle.get("approximate_match", False)),
            "mesh_reference_yaw_deg": float(vehicle.get("mesh_reference_yaw_deg", 0.0)),
            "asset_yaw_offset_deg": float(vehicle.get("asset_yaw_offset_deg", 0.0)),
            "mesh_size_m": mesh_size,
            "bbox_size_m": vehicle.get("bbox_size_m"),
            "size_error": size_error,
            "bottom_clearance_m": bottom_clearance,
            "bottom_alignment_error_m": bottom_alignment_error,
        }
        summary_rows.append(row)
        if max(float(value) for value in size_error.get("rel", [0.0, 0.0, 0.0])) > 0.35 and row["geometry_mode"] != "bbox":
            blockers.append(f"size_mismatch_actor_{row['actor_id']}")
        if bottom_alignment_error > 0.05:
            blockers.append(f"bottom_alignment_actor_{row['actor_id']}")
        if row["fallback_reason"]:
            warnings.append(f"fallback_actor_{row['actor_id']}")
    return {
        "rows": summary_rows,
        "warnings": warnings,
        "blockers": blockers,
    }


def main() -> int:
    args = parse_args()
    export_dir = args.export_dir
    scene_path = export_dir / args.scene_file
    motion_path = export_dir / args.motion_file
    manifest_path = export_dir / "manifest.json"
    verification_dir = args.verification_dir or (export_dir / "verification")
    preview_dir = verification_dir / "preview_frames"
    full_frames_dir = verification_dir / "topdown_frames"
    report_path = verification_dir / "verification_report.json"
    output_video = args.output_video or (verification_dir / "sionna_topdown_verified.mp4")

    verification_dir.mkdir(parents=True, exist_ok=True)
    if preview_dir.exists():
        shutil.rmtree(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    if args.render_full_video:
        if full_frames_dir.exists():
            shutil.rmtree(full_frames_dir)
        full_frames_dir.mkdir(parents=True, exist_ok=True)

    manifest = load_json(manifest_path)
    motion_rows = list(iter_motion(motion_path))
    scene_xml_check = parse_scene_xml(scene_path)
    vehicle_geometry_check = summarize_vehicle_geometry(
        manifest,
        expected_clearance=float(manifest.get("asset_pipeline", {}).get("vehicle_bottom_clearance_m", 0.02)),
    )

    motion_objects = sorted(
        {
            str(vehicle["sionna_object"])
            for row in motion_rows
            for vehicle in row["vehicles"]
        }
    )

    report: Dict[str, object] = {
        "scene_xml": {
            "scene_file": str(scene_path),
            "material_count": len(scene_xml_check["materials"]),
            "shape_count": len(scene_xml_check["shapes"]),
            "missing_material_refs": scene_xml_check["missing_material_refs"],
        },
        "motion": {
            "motion_file": str(motion_path),
            "motion_frame_count": len(motion_rows),
            "motion_object_count": len(motion_objects),
        },
        "vehicles": vehicle_geometry_check["rows"],
        "buildings": {
            "building_geometry_mode": manifest.get("static_geometry", {}).get("building_geometry_mode"),
            "matched_building_mesh_count": manifest.get("static_geometry", {}).get("matched_building_mesh_count"),
            "unmatched_building_proxy_count": manifest.get("static_geometry", {}).get("unmatched_building_proxy_count"),
        },
        "preview_frames": [],
        "video": {
            "requested": bool(args.render_full_video),
            "output_video": str(output_video) if args.render_full_video else None,
            "topdown_render_ok": False,
        },
        "warnings": list(scene_xml_check["missing_material_refs"]) + vehicle_geometry_check["warnings"],
        "blockers": list(vehicle_geometry_check["blockers"]),
    }

    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("MI_DEFAULT_VARIANT", args.mitsuba_variant)
    if not args.use_gpu:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    try:
        from sionna.rt import Camera, load_scene
        import mitsuba as mi

        scene = load_scene(str(scene_path))
        scene.frequency = args.frequency
        object_names = list(scene.objects.keys())
        scene_object_names = set(object_names)
        missing_motion_objects = [name for name in motion_objects if name not in scene_object_names]
        if missing_motion_objects:
            report["blockers"].append(f"missing_motion_objects:{missing_motion_objects[:8]}")

        if args.update_object:
            selected = None
            for row in motion_rows:
                if int(row["frame_index"]) != args.update_frame:
                    continue
                for vehicle in row["vehicles"]:
                    if vehicle["sionna_object"] == args.update_object:
                        selected = vehicle
                        break
                break
            if selected is None:
                report["blockers"].append(f"update_object_missing:{args.update_object}@{args.update_frame}")
            else:
                obj = scene.get(args.update_object)
                if obj is None:
                    report["blockers"].append(f"scene_object_missing:{args.update_object}")
                else:
                    obj.position = selected["position"]
                    obj.orientation = [selected["yaw_rad_sionna_like"], 0.0, 0.0]
                    obj.velocity = selected["velocity"]

        region = manifest["support_region"]
        center = region["center"]
        cx = float(center["x"])
        cy = float(center["y"])
        camera_height = args.camera_height or computed_camera_height(region, args.fov)
        hidden_position = [
            cx + 10.0 * float(region["width_m"]),
            cy + 10.0 * float(region["height_m"]),
            -1000.0,
        ]

        preview_rows = nearest_motion_rows(motion_rows, pick_preview_frames(manifest, motion_rows, args.preview_frames))
        preview_scene = load_scene(str(scene_path))
        preview_scene.frequency = args.frequency
        preview_scene.add(
            Camera(
                "topdown_preview",
                position=[cx, cy, camera_height],
                look_at=[cx, cy, float(args.camera_target_z)],
            )
        )
        car_names = sorted(
            [name for name in preview_scene.objects.keys() if name.startswith("car_")],
            key=lambda item: int(item.split("_")[1]),
        )

        for seq, row in enumerate(preview_rows):
            apply_motion_frame(preview_scene, car_names, row, hidden_position)
            frame_path = preview_dir / f"frame_{seq:02d}_motion_{int(row['frame_index']):06d}.png"
            preview_scene.render_to_file(
                "topdown_preview",
                str(frame_path),
                show_paths=False,
                show_devices=False,
                num_samples=args.preview_num_samples,
                resolution=tuple(args.preview_resolution),
                fov=args.fov,
            )
            report["preview_frames"].append(str(frame_path))

        report["sionna_import"] = {
            "loaded_scene_ok": True,
            "mitsuba_variant": mi.variant(),
            "scene_object_count": len(object_names),
            "motion_objects_missing_from_scene": missing_motion_objects,
            "sionna_import_ok": len(report["blockers"]) == 0 and not scene_xml_check["missing_material_refs"],
        }

        if args.render_full_video:
            video_scene = load_scene(str(scene_path))
            video_scene.frequency = args.frequency
            video_scene.add(
                Camera(
                    "topdown_video",
                    position=[cx, cy, camera_height],
                    look_at=[cx, cy, float(args.camera_target_z)],
                )
            )
            video_car_names = sorted(
                [name for name in video_scene.objects.keys() if name.startswith("car_")],
                key=lambda item: int(item.split("_")[1]),
            )
            for seq, row in enumerate(motion_rows):
                frame_path = full_frames_dir / f"frame_{seq:06d}.png"
                apply_motion_frame(video_scene, video_car_names, row, hidden_position)
                video_scene.render_to_file(
                    "topdown_video",
                    str(frame_path),
                    show_paths=False,
                    show_devices=False,
                    num_samples=args.full_num_samples,
                    resolution=tuple(args.full_resolution),
                    fov=args.fov,
                )
            encode_video(full_frames_dir, output_video, args.fps)
            report["video"]["topdown_render_ok"] = output_video.exists()

    except Exception as exc:  # noqa: BLE001
        report["blockers"].append(f"sionna_runtime_error:{exc.__class__.__name__}:{exc}")
        report["sionna_import"] = {
            "loaded_scene_ok": False,
            "sionna_import_ok": False,
            "error": f"{exc.__class__.__name__}:{exc}",
        }

    report["summary"] = {
        "blocker_count": len(report["blockers"]),
        "warning_count": len(report["warnings"]),
        "sionna_import_ok": bool(report.get("sionna_import", {}).get("sionna_import_ok", False)),
        "topdown_render_ok": bool(report["video"]["topdown_render_ok"]),
        "preview_frame_count": len(report["preview_frames"]),
    }

    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    manifest.setdefault("verification", {})
    manifest["verification"]["sionna_import_ok"] = report["summary"]["sionna_import_ok"]
    manifest["verification"]["topdown_render_ok"] = report["summary"]["topdown_render_ok"]
    manifest["verification"]["verification_report"] = str(report_path.relative_to(export_dir))
    manifest["verification"]["preview_frames"] = [str(Path(path).relative_to(export_dir)) for path in report["preview_frames"]]
    if args.render_full_video:
        manifest["verification"]["topdown_video"] = str(output_video.relative_to(export_dir))
    manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"[OK] Wrote verification report: {report_path}")
    print(f"[OK] Preview frames: {preview_dir}")
    if args.render_full_video:
        print(f"[OK] Topdown video: {output_video}")
    print(f"[OK] Sionna import: {report['summary']['sionna_import_ok']}")
    print(f"[OK] Topdown render: {report['summary']['topdown_render_ok']}")
    return 0 if report["summary"]["blocker_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
