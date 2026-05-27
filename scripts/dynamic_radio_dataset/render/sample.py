from __future__ import annotations

import os
import random
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Sequence

from dynamic_radio_dataset.json_utils import iter_jsonl, load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, repo_root
from dynamic_radio_dataset.pipeline.reports import write_timing_report


GREEN_ABSOLUTE_SAMPLE_STYLE = {
    "display_mode": "absolute",
    "cmap": "viridis",
    "plot_style": "clean",
    "rx_region": "valid_crop",
    "view_region": "valid",
    "vehicle_overlay": "dark",
    "vmin_dbm": -108.0,
    "vmax_dbm": -42.0,
    "visual_smooth_sigma": 0.0,
    "interpolation": "nearest",
    "visual_fill": False,
    "tx_marker_visible": False,
    "rx_height_m": 0.8,
}


def render_green_absolute_sample(
    config: dict,
    sample_count: int = 15,
    seed: int | None = None,
    output_dir: Path | None = None,
    allow_partial: bool = False,
) -> dict:
    root = dataset_root(config)
    index_path = root / "episode_index.jsonl"
    if not index_path.exists():
        raise FileNotFoundError(f"episode_index.jsonl not found; run finalize first: {index_path}")
    accepted_rows = [row for row in iter_jsonl(index_path) if bool(row.get("accepted"))]
    requested_count = int(sample_count)
    if requested_count <= 0:
        raise ValueError("--sample-count must be positive.")
    if len(accepted_rows) < requested_count and not allow_partial:
        raise RuntimeError(
            f"Requested {requested_count} visualization rows, but only {len(accepted_rows)} accepted episode-TX rows exist."
        )
    sample_size = min(requested_count, len(accepted_rows))
    rng_seed = int(seed if seed is not None else config.get("dataset", {}).get("split_seed", 7))
    rng = random.Random(rng_seed)
    sampled_rows = _sample_visualization_rows(config, root, accepted_rows, sample_size, rng)
    base_output_dir = output_dir or (root / "renders" / "green_absolute_sample")
    base_output_dir.mkdir(parents=True, exist_ok=True)

    manifest_rows = []
    total_started = time.time()
    for sequence_index, row in enumerate(sampled_rows):
        episode_id = str(row["episode_id"])
        tx_id = str(row["tx_id"])
        episode_dir = root / "episodes" / episode_id
        export_dir = episode_dir / "sionna_export"
        source_rss_dir = episode_dir / tx_id
        if not export_dir.exists():
            raise FileNotFoundError(f"Missing Sionna export for sampled row {episode_id}/{tx_id}: {export_dir}")
        if not (source_rss_dir / "rss_maps.npz").exists():
            raise FileNotFoundError(f"Missing cached RSS for sampled row {episode_id}/{tx_id}: {source_rss_dir / 'rss_maps.npz'}")

        row_output_dir = base_output_dir / f"{sequence_index:02d}_{episode_id}_{tx_id}"
        row_output_dir.mkdir(parents=True, exist_ok=True)
        output_video = row_output_dir / f"{episode_id}_{tx_id}_green_absolute.mp4"
        stdout_path = row_output_dir / "render_stdout.log"
        stderr_path = row_output_dir / "render_stderr.log"
        tx_position = _load_cached_tx_position(source_rss_dir)
        cmd = _render_command(
            config=config,
            export_dir=export_dir,
            source_rss_dir=source_rss_dir,
            tx_position=tx_position,
            output_dir=row_output_dir,
            output_video=output_video,
        )
        started = time.time()
        with stdout_path.open("w", encoding="utf-8") as stdout_f, stderr_path.open("w", encoding="utf-8") as stderr_f:
            proc = subprocess.run(
                [str(item) for item in cmd],
                cwd=str(repo_root()),
                stdout=stdout_f,
                stderr=stderr_f,
                env=_render_env(),
            )
        elapsed_s = float(time.time() - started)
        manifest_row = {
            "sequence_index": int(sequence_index),
            "episode_id": episode_id,
            "tx_id": tx_id,
            "accepted": bool(row.get("accepted")),
            "selection_metadata": _episode_selection_metadata(root, episode_id),
            "output_dir": str(row_output_dir),
            "video_path": str(output_video),
            "source_rss_dir": str(source_rss_dir),
            "source_rss_used_for": "cached_rss_reuse",
            "tx_position": [float(item) for item in tx_position],
            "export_dir": str(export_dir),
            "command": [str(item) for item in cmd],
            "returncode": int(proc.returncode),
            "elapsed_s": elapsed_s,
            "stdout_log": str(stdout_path),
            "stderr_log": str(stderr_path),
        }
        manifest_rows.append(manifest_row)
        _write_manifest(base_output_dir, config, rng_seed, requested_count, len(accepted_rows), manifest_rows, total_started)
        if proc.returncode != 0:
            raise RuntimeError(
                f"RSS sample render failed for {episode_id}/{tx_id} with return code {proc.returncode}; "
                f"see {stderr_path}"
            )

    manifest = _write_manifest(base_output_dir, config, rng_seed, requested_count, len(accepted_rows), manifest_rows, total_started)
    timing_result = write_timing_report(config)
    return {
        "sampled_visualizations": str(base_output_dir / "sampled_visualizations.json"),
        "sample_count": len(manifest_rows),
        "output_dir": str(base_output_dir),
        "timing_report": timing_result["timing_report"],
        "summary": manifest["summary"],
    }


def _render_command(
    *,
    config: dict,
    export_dir: Path,
    source_rss_dir: Path,
    tx_position: Sequence[float],
    output_dir: Path,
    output_video: Path,
) -> list[object]:
    command: list[object] = [
        _render_python(config),
        "-m",
        "dynamic_radio_dataset.render.rss_video",
        "--export-dir",
        export_dir,
        "--output-dir",
        output_dir,
        "--output-video",
        output_video,
        "--reuse-rss-dir",
        source_rss_dir,
        "--tx-placement",
        "custom",
        "--tx-x",
        float(tx_position[0]),
        "--tx-y",
        float(tx_position[1]),
        "--tx-z",
        float(tx_position[2]),
        "--rx-region",
        GREEN_ABSOLUTE_SAMPLE_STYLE["rx_region"],
        "--rx-height",
        float(GREEN_ABSOLUTE_SAMPLE_STYLE["rx_height_m"]),
        "--max-depth",
        int(config.get("sionna", {}).get("max_depth", 3)),
        "--num-runs",
        int(config.get("sionna", {}).get("num_runs", 1)),
        "--frequency",
        float(config.get("sionna", {}).get("frequency_hz", 5.89e9)),
        "--bandwidth",
        float(config.get("sionna", {}).get("bandwidth_hz", 10e6)),
        "--tx-power-dbm",
        float(config.get("sionna", {}).get("tx_power_dbm", 23.0)),
        "--display-mode",
        GREEN_ABSOLUTE_SAMPLE_STYLE["display_mode"],
        "--cmap",
        GREEN_ABSOLUTE_SAMPLE_STYLE["cmap"],
        "--plot-style",
        GREEN_ABSOLUTE_SAMPLE_STYLE["plot_style"],
        "--view-region",
        GREEN_ABSOLUTE_SAMPLE_STYLE["view_region"],
        "--vehicle-overlay",
        GREEN_ABSOLUTE_SAMPLE_STYLE["vehicle_overlay"],
        "--vmin-dbm",
        float(GREEN_ABSOLUTE_SAMPLE_STYLE["vmin_dbm"]),
        "--vmax-dbm",
        float(GREEN_ABSOLUTE_SAMPLE_STYLE["vmax_dbm"]),
        "--visual-smooth-sigma",
        float(GREEN_ABSOLUTE_SAMPLE_STYLE["visual_smooth_sigma"]),
        "--interpolation",
        str(GREEN_ABSOLUTE_SAMPLE_STYLE["interpolation"]),
        "--hide-tx-marker",
        "--fps",
        float(config.get("traffic", {}).get("fps", 10.0)),
        "--stride",
        1,
        "--resolution",
        int(config.get("sionna", {}).get("resolution", 128)),
    ]
    if bool(GREEN_ABSOLUTE_SAMPLE_STYLE.get("visual_fill", False)):
        command.append("--visual-fill")
    return command


def _load_cached_tx_position(source_rss_dir: Path) -> list[float]:
    import numpy as np

    with np.load(source_rss_dir / "rss_maps.npz") as data:
        if "tx_position" not in data.files:
            raise RuntimeError(f"Cached RSS is missing tx_position: {source_rss_dir / 'rss_maps.npz'}")
        tx_position = np.asarray(data["tx_position"], dtype=float).reshape(-1)
    if tx_position.size != 3:
        raise RuntimeError(f"Cached tx_position must have 3 values, got shape {tx_position.shape}")
    return [float(item) for item in tx_position.tolist()]


def _sample_visualization_rows(
    config: dict,
    root: Path,
    accepted_rows: list[dict],
    sample_size: int,
    rng: random.Random,
) -> list[dict]:
    if not _coverage_sample_enabled(config):
        return rng.sample(accepted_rows, sample_size)
    by_episode: dict[str, list[dict]] = {}
    for row in accepted_rows:
        by_episode.setdefault(str(row["episode_id"]), []).append(row)
    episode_meta = {
        episode_id: _episode_selection_metadata(root, episode_id)
        for episode_id in sorted(by_episode)
    }
    selected_episode_ids: list[str] = []

    def add_episode(episode_id: str | None) -> None:
        if episode_id and episode_id not in selected_episode_ids and len(selected_episode_ids) < sample_size:
            selected_episode_ids.append(episode_id)

    for vehicle_count in sorted({int(meta.get("vehicle_count", -1)) for meta in episode_meta.values()}):
        add_episode(_first_matching_episode(episode_meta, selected_episode_ids, vehicle_count=vehicle_count))
    for target_large in sorted({int(meta.get("target_large_vehicle_count", -1)) for meta in episode_meta.values()}):
        add_episode(_first_matching_episode(episode_meta, selected_episode_ids, target_large_vehicle_count=target_large))
    heavy_candidates = [
        episode_id
        for episode_id, meta in sorted(episode_meta.items())
        if int(meta.get("vehicle_count", 0)) >= 6 and int(meta.get("target_large_vehicle_count", 0)) == 3
    ]
    for episode_id in heavy_candidates[: min(3, len(heavy_candidates))]:
        add_episode(episode_id)

    remaining_episode_ids = [episode_id for episode_id in sorted(by_episode) if episode_id not in selected_episode_ids]
    rng.shuffle(remaining_episode_ids)
    for episode_id in remaining_episode_ids:
        add_episode(episode_id)
        if len(selected_episode_ids) >= sample_size:
            break

    sampled: list[dict] = []
    used_rows: set[tuple[str, str]] = set()
    for episode_id in selected_episode_ids:
        rows = list(by_episode[episode_id])
        rows.sort(key=lambda row: str(row.get("tx_id", "")))
        row = rows[len(sampled) % len(rows)]
        sampled.append(row)
        used_rows.add((str(row["episode_id"]), str(row["tx_id"])))
        if len(sampled) >= sample_size:
            return sampled

    remaining_rows = [
        row
        for row in accepted_rows
        if (str(row["episode_id"]), str(row["tx_id"])) not in used_rows
    ]
    rng.shuffle(remaining_rows)
    sampled.extend(remaining_rows[: max(0, sample_size - len(sampled))])
    return sampled


def _coverage_sample_enabled(config: dict) -> bool:
    collection = config.get("collection", {}) if isinstance(config.get("collection"), dict) else {}
    return bool(collection.get("selection_matrix") or collection.get("selection_manifest"))


def _first_matching_episode(
    episode_meta: dict[str, dict],
    excluded: list[str],
    *,
    vehicle_count: int | None = None,
    target_large_vehicle_count: int | None = None,
) -> str | None:
    for episode_id, meta in sorted(episode_meta.items()):
        if episode_id in excluded:
            continue
        if vehicle_count is not None and int(meta.get("vehicle_count", -1)) != int(vehicle_count):
            continue
        if target_large_vehicle_count is not None and int(meta.get("target_large_vehicle_count", -1)) != int(target_large_vehicle_count):
            continue
        return episode_id
    return None


def _episode_selection_metadata(root: Path, episode_id: str) -> dict:
    plan_path = root / "episodes" / episode_id / "plan.json"
    qa_path = root / "episodes" / episode_id / "trajectory_qa.json"
    plan = load_json(plan_path) if plan_path.exists() else {}
    qa = load_json(qa_path) if qa_path.exists() else {}
    bucket = plan.get("bucket", {}) if isinstance(plan.get("bucket"), dict) else {}
    metrics = plan.get("expected_metrics", {}) if isinstance(plan.get("expected_metrics"), dict) else {}
    vehicle_count = int(bucket.get("vehicle_count", plan.get("vehicle_count", 0)))
    target_large = int(bucket.get("target_large_vehicle_count", metrics.get("target_large_vehicle_count", 0)))
    role_counts = qa.get("vehicle_role_counts", {}) if isinstance(qa.get("vehicle_role_counts"), dict) else {}
    vehicle_mix = qa.get("vehicle_mix", {}) if isinstance(qa.get("vehicle_mix"), dict) else {}
    return {
        "vehicle_count": int(vehicle_count),
        "target_large_vehicle_count": int(target_large),
        "actual_total_vehicle_count": int(role_counts.get("actual_total_vehicle_count", 0)),
        "actual_large_vehicle_count": int(vehicle_mix.get("large_vehicle_count", 0)),
        "requested_background_count": int(role_counts.get("requested_background_count", 0)),
        "actual_background_count": int(role_counts.get("actual_background_count", 0)),
        "plan_id": str(plan.get("plan_id", "")),
    }


def _render_python(config: dict) -> str:
    value = str(config.get("sionna", {}).get("python") or sys.executable)
    path = Path(value)
    if path.is_absolute() or "/" in value:
        return str(path)
    return value


def _render_env() -> dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONNOUSERSITE", "1")
    scripts_dir = str(repo_root() / "scripts")
    existing = env.get("PYTHONPATH")
    env["PYTHONPATH"] = scripts_dir if not existing else f"{scripts_dir}:{existing}"
    share_home = Path("/share1/fzj")
    if share_home.exists():
        env.setdefault("HOME", str(share_home))
        env.setdefault("XDG_CACHE_HOME", str(share_home / ".cache"))
        env.setdefault("DRJIT_CACHE_DIR", str(share_home / ".cache" / "drjit"))
    return env


def _write_manifest(
    base_output_dir: Path,
    config: dict,
    seed: int,
    requested_count: int,
    accepted_row_count: int,
    rows: Sequence[dict],
    total_started: float,
) -> dict:
    total_elapsed = float(time.time() - total_started)
    selection_meta = [
        row.get("selection_metadata", {})
        for row in rows
        if isinstance(row.get("selection_metadata"), dict)
    ]
    manifest = {
        "schema": "green_absolute_rss_visualization_sample_v1",
        "dataset_root": str(dataset_root(config)),
        "seed": int(seed),
        "sample_count_requested": int(requested_count),
        "available_accepted_episode_tx_rows": int(accepted_row_count),
        "style": {
            **GREEN_ABSOLUTE_SAMPLE_STYLE,
            "style_source": "cached_rss_green_absolute_valid_crop_faithful_v3",
        },
        "summary": {
            "rendered_count": len(rows),
            "failed_count": sum(1 for row in rows if int(row.get("returncode", 1)) != 0),
            "total_elapsed_s": total_elapsed,
            "vehicle_count_coverage": dict(Counter(str(meta.get("vehicle_count")) for meta in selection_meta)),
            "target_large_vehicle_count_coverage": dict(
                Counter(str(meta.get("target_large_vehicle_count")) for meta in selection_meta)
            ),
            "six_or_seven_vehicle_three_large_count": sum(
                1
                for meta in selection_meta
                if int(meta.get("vehicle_count", 0)) >= 6 and int(meta.get("target_large_vehicle_count", 0)) == 3
            ),
        },
        "rows": list(rows),
    }
    save_json(base_output_dir / "sampled_visualizations.json", manifest)
    return manifest
