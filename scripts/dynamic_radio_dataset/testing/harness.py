from __future__ import annotations

import argparse
from collections import Counter
from pathlib import Path
from typing import Any

import numpy as np

from dynamic_radio_dataset.configs import load_config
from dynamic_radio_dataset.json_utils import load_json
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.rf.processing import expected_tx_ids, rf_episode_complete, trajectory_accepted_episode_dirs


def assert_rf_scratch_outputs(
    config: dict,
    *,
    expected_episodes: int | None = None,
    expected_gpu_ids: list[str] | None = None,
) -> dict[str, Any]:
    root = dataset_root(config)
    episodes = trajectory_accepted_episode_dirs(root)
    if expected_episodes is not None and len(episodes) != int(expected_episodes):
        raise RuntimeError(f"Expected {expected_episodes} trajectory-accepted episodes, found {len(episodes)}.")

    tx_ids = expected_tx_ids(config)
    expected_shape = (
        len(tx_ids),
        int(round(float(config["traffic"]["duration_s"]) * float(config["traffic"]["fps"]))),
        int(config.get("sionna", {}).get("resolution", 128)),
        int(config.get("sionna", {}).get("resolution", 128)),
    )
    shape_failures: list[dict[str, Any]] = []
    gpu_counter: Counter[str] = Counter()
    for episode_dir in episodes:
        complete, details = rf_episode_complete(config, episode_dir)
        if not complete:
            shape_failures.append({"episode_id": episode_dir.name, **details})
            continue
        with np.load(episode_dir / "rss_dynamic_dbm.npz") as data:
            actual_shape = tuple(np.asarray(data["dynamic_rss_dbm"]).shape)
        if actual_shape != expected_shape:
            shape_failures.append(
                {
                    "episode_id": episode_dir.name,
                    "failure_code": "dynamic_rss_shape_mismatch",
                    "actual_shape": list(actual_shape),
                    "expected_shape": list(expected_shape),
                }
            )
        meta_path = episode_dir / "rf_process_meta.json"
        if meta_path.exists():
            meta = load_json(meta_path)
            if meta.get("use_gpu") and meta.get("gpu_id") is not None:
                gpu_counter[str(meta["gpu_id"])] += 1
    if shape_failures:
        raise RuntimeError(f"RF scratch assertion failed for {len(shape_failures)} episode(s): {shape_failures[:3]}")
    missing_gpu_ids = [gpu_id for gpu_id in (expected_gpu_ids or []) if gpu_counter.get(str(gpu_id), 0) <= 0]
    if missing_gpu_ids:
        raise RuntimeError(f"Expected GPU ids with processed episodes, missing: {missing_gpu_ids}")
    return {
        "dataset_root": str(root),
        "accepted_episode_count": int(len(episodes)),
        "expected_dynamic_rss_shape": list(expected_shape),
        "gpu_episode_counts": {key: int(gpu_counter[key]) for key in sorted(gpu_counter)},
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Assert RF scratch outputs without modifying formal generation paths.")
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--profile", type=str, default=None)
    parser.add_argument("--expected-episodes", type=int, default=None)
    parser.add_argument("--expected-gpu-ids", type=str, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    config = load_config(args.config, profile_override=args.profile)
    expected_gpu_ids = [item.strip() for item in str(args.expected_gpu_ids).split(",") if item.strip()] if args.expected_gpu_ids else None
    print(
        assert_rf_scratch_outputs(
            config,
            expected_episodes=args.expected_episodes,
            expected_gpu_ids=expected_gpu_ids,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

