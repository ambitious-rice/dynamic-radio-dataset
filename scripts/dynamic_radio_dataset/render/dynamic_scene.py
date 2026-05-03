#!/usr/bin/env python3
"""
Render a dynamic CARLA-to-Sionna scene as a top-down video.

The script loads a Sionna/Mitsuba scene.xml, applies per-frame vehicle
transforms from motion.jsonl, renders PNG frames using Sionna RT's renderer,
and encodes them into an MP4 video.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Render a dynamic Sionna scene video from motion.jsonl.")
    parser.add_argument("--export-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo/sionna_export"))
    parser.add_argument("--scene-file", type=str, default="scene.xml")
    parser.add_argument("--motion-file", type=str, default="motion.jsonl")
    parser.add_argument("--output-video", type=Path, default=None)
    parser.add_argument("--frames-dir", type=Path, default=None)
    parser.add_argument("--start-frame", type=int, default=0)
    parser.add_argument("--end-frame", type=int, default=-1, help="-1 means through the final motion frame.")
    parser.add_argument("--stride", type=int, default=1)
    parser.add_argument("--fps", type=float, default=10.0)
    parser.add_argument("--resolution", type=int, nargs=2, default=[640, 640], metavar=("W", "H"))
    parser.add_argument("--fov", type=float, default=52.0)
    parser.add_argument("--camera-height", type=float, default=0.0, help="0 computes a height from support size and FOV.")
    parser.add_argument("--camera-target-z", type=float, default=0.0)
    parser.add_argument(
        "--camera-position",
        type=float,
        nargs=3,
        default=None,
        metavar=("X", "Y", "Z"),
        help="Optional explicit camera position. When omitted, use the default top-down camera.",
    )
    parser.add_argument(
        "--look-at",
        type=float,
        nargs=3,
        default=None,
        metavar=("X", "Y", "Z"),
        help="Optional explicit camera look-at target. When omitted, use support-region center.",
    )
    parser.add_argument(
        "--include-vehicles",
        type=str,
        default="",
        help="Comma-separated vehicle object names to keep visible, e.g. car_58,car_61. Empty keeps all cars.",
    )
    parser.add_argument("--num-samples", type=int, default=64)
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")
    parser.add_argument("--keep-frames", action="store_true")
    parser.add_argument("--skip-existing-frames", action="store_true")
    return parser.parse_args()


def load_json(path: Path) -> Dict[str, object]:
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def iter_motion(path: Path) -> Iterable[Dict[str, object]]:
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                yield json.loads(line)


def select_motion_frames(path: Path, start: int, end: int, stride: int) -> List[Dict[str, object]]:
    frames = []
    for row in iter_motion(path):
        frame_index = int(row["frame_index"])
        if frame_index < start:
            continue
        if end >= 0 and frame_index > end:
            break
        if (frame_index - start) % stride == 0:
            frames.append(row)
    if not frames:
        raise RuntimeError(f"No motion frames selected from {path}")
    return frames


def ensure_empty_dir(path: Path, keep_existing: bool) -> None:
    if path.exists() and not keep_existing:
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)


def computed_camera_height(region: Dict[str, object], fov_deg: float) -> float:
    width = float(region["width_m"])
    height = float(region["height_m"])
    half_extent = max(width, height) / 2.0
    return half_extent / math.tan(math.radians(fov_deg) / 2.0) * 1.08


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


def parse_vehicle_filter(arg: str) -> Optional[set]:
    names = {item.strip() for item in arg.split(",") if item.strip()}
    return names or None


def main() -> int:
    args = parse_args()
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("MI_DEFAULT_VARIANT", args.mitsuba_variant)
    if not args.use_gpu:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    from sionna.rt import Camera, load_scene
    import mitsuba as mi

    export_dir = args.export_dir
    scene_path = export_dir / args.scene_file
    motion_path = export_dir / args.motion_file
    manifest_path = export_dir / "manifest.json"
    if not scene_path.exists():
        raise FileNotFoundError(scene_path)
    if not motion_path.exists():
        raise FileNotFoundError(motion_path)
    if not manifest_path.exists():
        raise FileNotFoundError(manifest_path)

    manifest = load_json(manifest_path)
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
    visible_vehicle_filter = parse_vehicle_filter(args.include_vehicles)

    output_video = args.output_video or (export_dir / "sionna_topdown_dynamic.mp4")
    frames_dir = args.frames_dir or (export_dir / "sionna_topdown_frames")
    ensure_empty_dir(frames_dir, keep_existing=args.keep_frames or args.skip_existing_frames)

    rows = select_motion_frames(motion_path, args.start_frame, args.end_frame, args.stride)
    scene = load_scene(str(scene_path))
    camera_position = list(args.camera_position) if args.camera_position is not None else [cx, cy, camera_height]
    look_at = list(args.look_at) if args.look_at is not None else [cx, cy, float(args.camera_target_z)]
    camera = Camera(
        "topdown",
        position=camera_position,
        look_at=look_at,
    )
    scene.add(camera)
    car_names = sorted(
        [name for name in scene.objects.keys() if name.startswith("car_")],
        key=lambda item: int(item.split("_")[1]),
    )

    print(f"[INFO] Loaded scene: {scene_path}")
    print(f"[INFO] Mitsuba variant: {mi.variant()}")
    print(f"[INFO] Scene objects: {len(scene.objects)} total, {len(car_names)} cars")
    print(f"[INFO] Rendering {len(rows)} frames to {frames_dir}")
    print(f"[INFO] Camera: position={camera_position}, look_at={look_at}")
    if visible_vehicle_filter is not None:
        print(f"[INFO] Visible vehicle filter: {sorted(visible_vehicle_filter)}")

    for seq, row in enumerate(rows):
        frame_path = frames_dir / f"frame_{seq:06d}.png"
        if args.skip_existing_frames and frame_path.exists():
            continue
        if visible_vehicle_filter is not None:
            filtered_row = {
                **row,
                "vehicles": [vehicle for vehicle in row["vehicles"] if vehicle["sionna_object"] in visible_vehicle_filter],
            }
        else:
            filtered_row = row
        apply_motion_frame(scene, car_names, filtered_row, hidden_position)
        scene.render_to_file(
            "topdown",
            str(frame_path),
            show_paths=False,
            show_devices=False,
            num_samples=args.num_samples,
            resolution=tuple(args.resolution),
            fov=args.fov,
        )
        if seq == 0 or (seq + 1) % 10 == 0 or seq == len(rows) - 1:
            print(f"[INFO] Rendered {seq + 1}/{len(rows)} frames (motion frame {row['frame_index']})")

    encode_video(frames_dir, output_video, args.fps / max(args.stride, 1))
    print(f"[OK] Wrote video: {output_video}")
    if not args.keep_frames:
        print(f"[INFO] Kept rendered frames at: {frames_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
