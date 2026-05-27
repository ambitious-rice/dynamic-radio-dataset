from __future__ import annotations

import json
import shutil
from collections import Counter
from pathlib import Path
from typing import Any, Sequence

from dynamic_radio_dataset.indexing.finalize import finalize_index
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root, ensure_dataset_dirs, resolve_repo_path
from dynamic_radio_dataset.rf.processing import expected_tx_count, expected_tx_ids, process_rf, rf_episode_complete, trajectory_qc_pass


def prepare_scene(config: dict) -> dict:
    """Create dataset-root static and reference bundles from an existing fixed scene."""
    dirs = ensure_dataset_dirs(config)
    source_dir = resolve_repo_path(config["scene"]["static_dir"])
    target_dir = dirs["scene_static"]
    if not source_dir.exists():
        raise FileNotFoundError(f"scene.static_dir does not exist: {source_dir}")
    for item in source_dir.iterdir():
        dst = target_dir / item.name
        if item.is_dir():
            shutil.copytree(item, dst, dirs_exist_ok=True)
        else:
            shutil.copy2(item, dst)
    reference_result = _copy_reference_scene_bundle(config, dirs)
    save_json(dirs["configs"] / "resolved_config.json", config)
    return {
        "scene_static_dir": str(target_dir),
        "reference_scene_dir": str(reference_result["reference_scene_dir"]) if reference_result else None,
        "reference_export_dir": str(reference_result["reference_export_dir"]) if reference_result else None,
        "tx_catalog_exists": (target_dir / "tx_catalog.json").exists(),
    }


def _copy_reference_scene_bundle(config: dict, dirs: dict[str, Path]) -> dict[str, Path] | None:
    reference_export = resolve_repo_path(config["scene"]["reference_export_dir"])
    if not reference_export.exists():
        raise FileNotFoundError(f"scene.reference_export_dir does not exist: {reference_export}")
    source_reference = reference_export.parent
    target_reference = dirs["reference_scene"]
    shutil.copytree(source_reference, target_reference, dirs_exist_ok=True)
    local_export = target_reference / reference_export.name
    if not local_export.exists():
        raise FileNotFoundError(f"Copied reference export is missing: {local_export}")
    meta_path = dirs["scene_static"] / "scene_static_meta.json"
    if meta_path.exists():
        meta = load_json(meta_path)
        meta["source_reference_dataset_dir"] = str(source_reference)
        meta["source_reference_export_dir"] = str(reference_export)
        meta["reference_dataset_dir"] = str(target_reference)
        meta["reference_export_dir"] = str(local_export)
        save_json(meta_path, meta)
    return {"reference_scene_dir": target_reference, "reference_export_dir": local_export}


def finalize(config: dict, split_seed: int | None = None) -> dict:
    root = dataset_root(config)
    seed = int(split_seed if split_seed is not None else config.get("dataset", {}).get("split_seed", 7))
    _run_existing_finalize(root, seed)
    indexes_dir = root / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in ("episode_index.jsonl", "splits.json"):
        src = root / name
        if src.exists():
            shutil.copy2(src, indexes_dir / name)
            copied.append(name)
    _write_bucket_summary(root)
    from dynamic_radio_dataset.pipeline.reports import write_timing_report

    timing_result = write_timing_report(config)
    return {"index_dir": str(indexes_dir), "copied": copied, "timing_report": timing_result["timing_report"]}


def verify_release_dataset(config: dict, expected_episodes: int | None = None) -> dict[str, Any]:
    root = dataset_root(config)
    expected_episode_count = int(
        expected_episodes
        if expected_episodes is not None
        else config.get("profile", {}).get("target_accepted_episodes", 0)
    )
    if expected_episode_count <= 0:
        raise ValueError("release verification needs an expected accepted episode count.")
    tx_ids = expected_tx_ids(config)
    tx_count = expected_tx_count(config)
    expected_shape = [
        tx_count,
        int(round(float(config["traffic"]["duration_s"]) * float(config["traffic"]["fps"]))),
        int(config.get("sionna", {}).get("resolution", 128)),
        int(config.get("sionna", {}).get("resolution", 128)),
    ]
    episode_dirs = sorted((root / "episodes").glob("episode_*"))
    accepted_episode_dirs = [path for path in episode_dirs if trajectory_qc_pass(path)]
    if len(accepted_episode_dirs) != expected_episode_count:
        raise RuntimeError(
            f"Release episode count mismatch: expected {expected_episode_count}, found {len(accepted_episode_dirs)}."
        )
    shape_failures: list[dict[str, Any]] = []
    gpu_counter: Counter[str] = Counter()
    for episode_dir in accepted_episode_dirs:
        complete, details = rf_episode_complete(config, episode_dir)
        if not complete:
            shape_failures.append({"episode_id": episode_dir.name, **details})
            continue
        meta_path = episode_dir / "rf_process_meta.json"
        if meta_path.exists():
            meta = load_json(meta_path)
            if meta.get("use_gpu") and meta.get("gpu_id") is not None:
                gpu_counter[str(meta.get("gpu_id"))] += 1
    if shape_failures:
        raise RuntimeError(f"Release RF completeness check failed for {len(shape_failures)} episode(s): {shape_failures[:3]}")
    index_path = root / "episode_index.jsonl"
    if index_path.exists():
        rows = _read_jsonl(index_path)
        row_source = str(index_path)
    else:
        rows = _release_rows_from_episode_reports(accepted_episode_dirs)
        row_source = "episode_qa_reports"
    expected_rows = expected_episode_count * tx_count
    if len(rows) != expected_rows:
        raise RuntimeError(f"Release episode-TX row mismatch: expected {expected_rows}, found {len(rows)}.")
    configured_gpu_ids = [str(item) for item in config.get("sionna", {}).get("gpu_ids", [])]
    missing_gpu_ids = [gpu_id for gpu_id in configured_gpu_ids if gpu_counter.get(gpu_id, 0) <= 0]
    if bool(config.get("sionna", {}).get("use_gpu", False)) and missing_gpu_ids:
        raise RuntimeError(f"Release GPU coverage mismatch; no processed episode recorded for GPU(s): {missing_gpu_ids}.")
    return {
        "dataset_root": str(root),
        "accepted_episode_count": int(len(accepted_episode_dirs)),
        "episode_tx_row_count": int(len(rows)),
        "episode_tx_row_source": row_source,
        "expected_dynamic_rss_shape": expected_shape,
        "gpu_episode_counts": {key: int(gpu_counter[key]) for key in sorted(gpu_counter)},
    }


def prune_release_dataset(config: dict, expected_episodes: int | None = None) -> dict[str, Any]:
    verification = verify_release_dataset(config, expected_episodes=expected_episodes)
    root = dataset_root(config)
    keep = {"episodes", "reference_scene", "scene_static"}
    required = [root / name for name in sorted(keep)]
    missing_required = [path.name for path in required if not path.exists()]
    if missing_required:
        raise RuntimeError(f"Cannot prune release dataset; required directory missing: {missing_required}")
    removed: list[str] = []
    for item in sorted(root.iterdir(), key=lambda path: path.name):
        if item.name in keep:
            continue
        if item.is_dir():
            shutil.rmtree(item)
        else:
            item.unlink()
        removed.append(item.name)
    final_entries = sorted(item.name for item in root.iterdir())
    if final_entries != sorted(keep):
        raise RuntimeError(f"Release prune left unexpected root entries: {final_entries}")
    return {
        "dataset_root": str(root),
        "kept": final_entries,
        "removed": removed,
        "verification": verification,
    }


def run_all(
    config: dict,
    num_plans: int | None = None,
    max_attempts: int | None = None,
    target_accepted: int | None = None,
    max_episodes: int | None = None,
    resume: bool = True,
    selection_manifest: Path | None = None,
) -> dict:
    from dynamic_radio_dataset.carla.runner import collect_from_plan_catalog
    from dynamic_radio_dataset.plans.sampler import generate_plan_bank
    from dynamic_radio_dataset.routes.route_library import build_route_library

    results = {"prepare_scene": prepare_scene(config)}
    results["route_library"] = build_route_library(config)
    results["plan_catalog"] = generate_plan_bank(config, num_plans=num_plans)
    results["collect"] = collect_from_plan_catalog(
        config,
        max_attempts=max_attempts,
        target_accepted=target_accepted,
        resume=resume,
        selection_manifest=selection_manifest,
    )
    results["process_rf"] = process_rf(config, max_episodes=max_episodes)
    results["finalize"] = finalize(config)
    if bool(config.get("visualization", {}).get("auto_render_sample", False)):
        from dynamic_radio_dataset.render.sample import render_green_absolute_sample

        viz_cfg = config.get("visualization", {})
        results["render_sample"] = render_green_absolute_sample(
            config,
            sample_count=int(viz_cfg.get("sample_count", 15)),
            seed=viz_cfg.get("sample_seed"),
            allow_partial=bool(viz_cfg.get("allow_partial", False)),
        )
    return results


def _release_rows_from_episode_reports(episode_dirs: Sequence[Path]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for episode_dir in episode_dirs:
        qa_path = episode_dir / "qa_report.json"
        if not qa_path.exists():
            continue
        report = load_json(qa_path)
        scene_pass = bool(report.get("scene_qc_pass"))
        for tx_report in report.get("tx_reports", []) if isinstance(report.get("tx_reports"), list) else []:
            if scene_pass and bool(tx_report.get("pair_qc_pass")):
                rows.append(
                    {
                        "episode_id": episode_dir.name,
                        "tx_id": str(tx_report.get("tx_id", "")),
                    }
                )
    return rows


def _run_existing_finalize(root: Path, split_seed: int) -> None:
    finalize_index(root, int(split_seed))


def _write_bucket_summary(root: Path) -> None:
    rows = []
    index_path = root / "episode_index.jsonl"
    if index_path.exists():
        rows = _read_jsonl(index_path)
    trajectory_reports = _trajectory_reports_for_summary(root)
    rf_rows = _rf_rows_for_summary(root)
    save_json(
        root / "indexes" / "bucket_summary.json",
        {
            "schema": "vehicle_role_bucket_summary_v2",
            "indexed_row_count": len(rows),
            "trajectory_report_count": len(trajectory_reports),
            "controlled_vehicle_pass_rate": _controlled_vehicle_pass_rate(trajectory_reports),
            "background_requested_vs_actual_distribution": _background_requested_vs_actual_distribution(trajectory_reports),
            "actual_total_vehicle_count_distribution": _actual_total_distribution(trajectory_reports),
            "trajectory_pass_actual_total_vehicle_count_distribution": _actual_total_distribution(
                [report for report in trajectory_reports if bool(report.get("trajectory_qc_pass"))]
            ),
            "six_plus_actual_vehicle_episode_count": sum(
                1
                for report in trajectory_reports
                if bool(report.get("trajectory_qc_pass"))
                and int(_vehicle_role_counts(report).get("actual_total_vehicle_count", 0)) >= 6
            ),
            "fusorosa_role_distribution": _fusorosa_role_distribution(trajectory_reports),
            "collision_rate_by_role": _collision_rate_by_role(trajectory_reports),
            "rf_pass_rate_by_actual_total_vehicle_count": _rf_pass_rate_by_actual_total_vehicle_count(rf_rows),
        },
    )


def _read_jsonl(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _trajectory_reports_for_summary(root: Path) -> list[dict]:
    reports: list[dict] = []
    for base in (root / "episodes", root / "failed_attempts"):
        if not base.exists():
            continue
        for item_dir in sorted(base.glob("*")):
            path = item_dir / "trajectory_qa.json"
            if path.exists():
                report = load_json(path)
                report["_source_dir"] = str(item_dir)
                reports.append(report)
    return reports


def _rf_rows_for_summary(root: Path) -> list[dict]:
    rows: list[dict] = []
    for episode_dir in sorted((root / "episodes").glob("episode_*")):
        qa_path = episode_dir / "qa_report.json"
        meta_path = episode_dir / "episode_meta.json"
        if not qa_path.exists() or not meta_path.exists():
            continue
        qa_report = load_json(qa_path)
        meta = load_json(meta_path)
        actual_count = int(meta.get("actual_total_vehicle_count", 0))
        if actual_count <= 0 and (episode_dir / "trajectory_qa.json").exists():
            actual_count = int(_vehicle_role_counts(load_json(episode_dir / "trajectory_qa.json")).get("actual_total_vehicle_count", 0))
        tx_reports = qa_report.get("tx_reports", []) if isinstance(qa_report.get("tx_reports"), list) else []
        for tx_report in tx_reports:
            rows.append(
                {
                    "actual_total_vehicle_count": actual_count,
                    "pair_qc_pass": bool(tx_report.get("pair_qc_pass")) and bool(qa_report.get("scene_qc_pass")),
                }
            )
    return rows


def _controlled_vehicle_pass_rate(reports: list[dict]) -> dict[str, object]:
    if not reports:
        return {
            "required_presence_pass_rate": None,
            "trajectory_qc_pass_rate": None,
            "required_presence_pass_count": 0,
            "trajectory_qc_pass_count": 0,
            "report_count": 0,
        }
    required_pass = sum(1 for report in reports if _required_controlled_present(report))
    trajectory_pass = sum(1 for report in reports if bool(report.get("trajectory_qc_pass")))
    return {
        "required_presence_pass_rate": float(required_pass / len(reports)),
        "trajectory_qc_pass_rate": float(trajectory_pass / len(reports)),
        "required_presence_pass_count": int(required_pass),
        "trajectory_qc_pass_count": int(trajectory_pass),
        "report_count": int(len(reports)),
    }


def _background_requested_vs_actual_distribution(reports: list[dict]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for report in reports:
        counts = _vehicle_role_counts(report)
        key = f"{int(counts.get('requested_background_count', 0))}->{int(counts.get('actual_background_count', 0))}"
        counter[key] += 1
    return dict(counter)


def _actual_total_distribution(reports: list[dict]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for report in reports:
        counts = _vehicle_role_counts(report)
        counter[str(int(counts.get("actual_total_vehicle_count", 0)))] += 1
    return dict(counter)


def _vehicle_role_counts(report: dict) -> dict[str, int]:
    counts = report.get("vehicle_role_counts", {}) if isinstance(report.get("vehicle_role_counts"), dict) else {}
    if counts and int(counts.get("actual_total_vehicle_count", 0)) > 0:
        return {str(key): int(value) for key, value in counts.items() if isinstance(value, (int, float))}
    primary = report.get("primary_vehicle_metrics", []) if isinstance(report.get("primary_vehicle_metrics"), list) else []
    auxiliary = report.get("auxiliary_vehicle_metrics", []) if isinstance(report.get("auxiliary_vehicle_metrics"), list) else []
    background = report.get("background_vehicle_metrics", []) if isinstance(report.get("background_vehicle_metrics"), list) else []
    if not auxiliary and isinstance(report.get("context_vehicle_metrics"), list):
        auxiliary = [
            row
            for row in report.get("context_vehicle_metrics", [])
            if str(row.get("role", "")) not in {"background", "background_tm"}
        ]
        background = [
            row
            for row in report.get("context_vehicle_metrics", [])
            if str(row.get("role", "")) in {"background", "background_tm"}
        ]
    plan_consistency = report.get("plan_consistency", {}) if isinstance(report.get("plan_consistency"), dict) else {}
    requested_background = int(plan_consistency.get("vehicle_role_counts", {}).get("requested_background_count", 0)) if isinstance(plan_consistency.get("vehicle_role_counts"), dict) else 0
    return {
        "planned_required_controlled_count": int(plan_consistency.get("planned_required_controlled_count", len(primary))),
        "planned_optional_controlled_count": int(plan_consistency.get("planned_optional_controlled_count", len(auxiliary))),
        "requested_background_count": requested_background,
        "actual_required_controlled_count": len(primary),
        "actual_optional_controlled_count": len(auxiliary),
        "actual_background_count": len(background),
        "actual_total_vehicle_count": int(len(primary) + len(auxiliary) + len(background)),
    }


def _required_controlled_present(report: dict) -> bool:
    plan_consistency = report.get("plan_consistency", {}) if isinstance(report.get("plan_consistency"), dict) else {}
    if "required_controlled_present" in plan_consistency:
        return bool(plan_consistency.get("required_controlled_present"))
    if "matches_plan" in plan_consistency:
        return bool(plan_consistency.get("matches_plan"))
    counts = _vehicle_role_counts(report)
    return int(counts.get("actual_required_controlled_count", 0)) >= int(counts.get("planned_required_controlled_count", 1))


def _fusorosa_role_distribution(reports: list[dict]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for report in reports:
        for key in ("primary_vehicle_metrics", "auxiliary_vehicle_metrics", "background_vehicle_metrics", "context_vehicle_metrics"):
            rows = report.get(key, []) if isinstance(report.get(key), list) else []
            for row in rows:
                if "fusorosa" in str(row.get("vehicle_type", "")).lower():
                    counter[str(row.get("role", "unknown"))] += 1
    return dict(counter)


def _collision_rate_by_role(reports: list[dict]) -> dict[str, dict[str, object]]:
    report_count = max(len(reports), 1)
    pair_counts: Counter[str] = Counter()
    pair_attempts: Counter[str] = Counter()
    for report in reports:
        role_pairs = report.get("collision_summary", {}).get("vehicle_vehicle_by_role_pair", {})
        if not isinstance(role_pairs, dict):
            continue
        for pair, count in role_pairs.items():
            pair_counts[str(pair)] += int(count)
            if int(count) > 0:
                pair_attempts[str(pair)] += 1
    return {
        pair: {
            "collision_pair_count": int(pair_counts[pair]),
            "attempt_count_with_collision": int(pair_attempts[pair]),
            "attempt_rate": float(pair_attempts[pair] / report_count),
        }
        for pair in sorted(pair_counts)
    }


def _rf_pass_rate_by_actual_total_vehicle_count(rows: list[dict]) -> dict[str, dict[str, object]]:
    grouped: dict[int, list[dict]] = {}
    for row in rows:
        grouped.setdefault(int(row.get("actual_total_vehicle_count", 0)), []).append(row)
    return {
        str(count): {
            "pair_count": int(len(group)),
            "passed_pair_count": int(sum(1 for row in group if bool(row.get("pair_qc_pass")))),
            "pass_rate": float(sum(1 for row in group if bool(row.get("pair_qc_pass"))) / max(len(group), 1)),
        }
        for count, group in sorted(grouped.items())
    }
