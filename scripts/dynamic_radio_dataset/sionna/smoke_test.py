#!/usr/bin/env python3
"""
Smoke-test a Sionna RT scene export.

Run this with an environment that has Sionna installed, e.g.
    /home/fzj/.venvs/sionna019/bin/python scripts/sionna_import_smoke_test.py \
        --export-dir datasets/dynamic_radio_scene_demo/sionna_export
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Dict, Optional


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Load a Sionna scene XML and optionally apply one motion frame.")
    parser.add_argument("--export-dir", type=Path, default=Path("datasets/dynamic_radio_scene_demo/sionna_export"))
    parser.add_argument("--frequency", type=float, default=5.89e9)
    parser.add_argument("--update-frame", type=int, default=80)
    parser.add_argument("--update-object", type=str, default="car_58")
    parser.add_argument("--mitsuba-variant", type=str, default="llvm_ad_rgb")
    parser.add_argument("--use-gpu", action="store_true")
    return parser.parse_args()


def load_motion_update(motion_path: Path, frame_index: int, object_name: str) -> Optional[Dict[str, object]]:
    with motion_path.open("r", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if int(row["frame_index"]) != frame_index:
                continue
            for vehicle in row["vehicles"]:
                if vehicle["sionna_object"] == object_name:
                    return vehicle
            return None
    return None


def main() -> int:
    args = parse_args()
    os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
    os.environ.setdefault("MI_DEFAULT_VARIANT", args.mitsuba_variant)
    if not args.use_gpu:
        os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")

    from sionna.rt import load_scene
    import mitsuba as mi

    scene_path = args.export_dir / "scene.xml"
    motion_path = args.export_dir / "motion.jsonl"
    scene = load_scene(str(scene_path))
    scene.frequency = args.frequency

    object_names = list(scene.objects.keys())
    car_names = sorted([name for name in object_names if name.startswith("car_")], key=lambda name: int(name.split("_")[1]))
    print(f"[OK] Loaded scene: {scene_path}")
    print(f"[OK] Mitsuba variant: {mi.variant()}")
    print(f"[OK] Objects: {len(object_names)} total, {len(car_names)} cars")
    print(f"[OK] Frequency: {float(scene.frequency.numpy())} Hz")

    for name in ["road_support_region"] + car_names[:3]:
        obj = scene.get(name)
        material = getattr(obj, "radio_material", None)
        material_name = getattr(material, "name", None) if material is not None else None
        print(f"[OK] Object {name}: position={obj.position.numpy().tolist()} material={material_name}")

    if motion_path.exists() and args.update_object:
        update = load_motion_update(motion_path, args.update_frame, args.update_object)
        if update is None:
            print(f"[WARN] No update found for {args.update_object} at frame {args.update_frame}")
            return 0
        obj = scene.get(args.update_object)
        if obj is None:
            raise RuntimeError(f"Scene object not found: {args.update_object}")
        before = obj.position.numpy().tolist()
        obj.position = update["position"]
        obj.orientation = [update["yaw_rad_sionna_like"], 0, 0]
        obj.velocity = update["velocity"]
        print(f"[OK] Updated {args.update_object} from frame {args.update_frame}")
        print(f"[OK] Position: {before} -> {obj.position.numpy().tolist()}")
        print(f"[OK] Orientation: {obj.orientation.numpy().tolist()}")
        print(f"[OK] Velocity: {obj.velocity.numpy().tolist()}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
