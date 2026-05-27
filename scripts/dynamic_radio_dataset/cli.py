from __future__ import annotations

import argparse
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dynamic_radio_dataset.configs import load_config
from dynamic_radio_dataset.carla.runner import collect_from_plan_catalog
from dynamic_radio_dataset.pipeline import finalize, prepare_scene, process_rf, run_all
from dynamic_radio_dataset.pipeline.stages import prune_release_dataset, verify_release_dataset
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.plans.sampler import generate_plan_bank
from dynamic_radio_dataset.routes.route_library import build_route_library


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Plan-first dynamic radio dataset builder.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in (
        "prepare-scene",
        "build-route-library",
        "generate-plans",
        "collect",
        "process-rf",
        "finalize",
        "render-sample",
        "timing-report",
        "run",
        "run-supervised",
        "verify-release",
        "prune-release",
        "failure-report",
        "select-pilot-plans",
        "select-validation-plans",
        "discover-scenes",
        "render-scene-previews",
        "validate-multi-scene",
        "validate-selected-scenes",
        "prepare-multi-scene",
        "repair-multi-scene-metadata",
        "prepare-rf-cache",
        "process-multi-scene-rf",
        "collect-multi-scene",
        "run-multi-scene-supervised",
        "finalize-multi-scene",
        "assign-tx",
        "regenerate-tx-catalogs",
        "promote-tx-catalogs",
    ):
        sub = subparsers.add_parser(command)
        sub.add_argument("--config", type=Path, required=True)
        sub.add_argument("--profile", type=str, default=None)
        if command in {"verify-release", "prune-release"}:
            sub.add_argument("--expected-episodes", type=int, default=None)
        if command == "failure-report":
            sub.add_argument("--output-json", type=Path, default=None)
            sub.add_argument("--max-examples", type=int, default=5)
        if command in {"generate-plans", "run", "run-supervised"}:
            sub.add_argument("--num-plans", type=int, default=None)
        if command in {"discover-scenes"}:
            sub.add_argument("--max-candidates", type=int, default=None)
        if command in {"render-scene-previews"}:
            sub.add_argument("--limit", type=int, default=None)
        if command in {"prepare-multi-scene", "process-multi-scene-rf", "run-multi-scene-supervised", "collect-multi-scene"}:
            sub.add_argument("--max-scenes", type=int, default=None)
        if command == "collect-multi-scene":
            sub.add_argument("--carla-workers", type=int, default=4)
            sub.add_argument("--gpu-id", type=str, default="1")
            sub.add_argument(
                "--gpu-ids",
                type=str,
                default=None,
                help=(
                    "Comma-separated CARLA GPU ids assigned round-robin to workers. "
                    "Takes precedence over --gpu-id when provided."
                ),
            )
            sub.add_argument("--rpc-ports", type=str, default=None, help="Comma-separated CARLA RPC ports.")
            sub.add_argument("--tm-ports", type=str, default=None, help="Comma-separated Traffic Manager ports.")
            sub.add_argument("--target-accepted-per-scene", type=int, default=None)
            sub.add_argument("--max-attempts-per-scene", type=int, default=None)
            sub.add_argument("--max-carla-restarts-per-scene", type=int, default=120)
            sub.add_argument(
                "--no-rendering-mode",
                dest="no_rendering_mode",
                action="store_true",
                default=True,
                help="Enable CARLA no_rendering_mode for trajectory-only collection (default).",
            )
            sub.add_argument(
                "--rendering-mode",
                dest="no_rendering_mode",
                action="store_false",
                help="Disable CARLA no_rendering_mode, for visual/RGB debugging only.",
            )
            sub.add_argument("--no-resume", action="store_true")
            sub.add_argument("--keep-carla-running", action="store_true")
            sub.add_argument("--no-jitter-scene-seeds", action="store_true")
            sub.add_argument("--dry-run", action="store_true")
        if command in {"prepare-rf-cache"}:
            sub.add_argument("--force", action="store_true")
        if command in {"collect", "run"}:
            sub.add_argument("--max-attempts", type=int, default=None)
            sub.add_argument(
                "--target-accepted",
                type=int,
                default=None,
                help="Stop collection once this many trajectory-QA-accepted episodes exist in the dataset.",
            )
            sub.add_argument("--no-resume", action="store_true", help="Do not skip already trajectory-accepted plans.")
            sub.add_argument(
                "--bucket-targets",
                type=str,
                default=None,
                help="Optional comma-separated vehicle-count quotas, e.g. 4:5,5:5,6:5,7:5.",
            )
            sub.add_argument(
                "--selection-manifest",
                type=Path,
                default=None,
                help="Optional validation selection manifest. Relative paths are resolved under the dataset root.",
            )
        if command == "run-supervised":
            sub.add_argument("--max-attempts", type=int, default=None)
            sub.add_argument("--target-accepted", type=int, default=None)
            sub.add_argument("--no-resume", action="store_true")
            sub.add_argument("--bucket-targets", type=str, default=None)
            sub.add_argument("--selection-manifest", type=Path, default=None)
            sub.add_argument("--render-sample-count", type=int, default=None)
            sub.add_argument("--no-render-sample", action="store_true")
            sub.add_argument("--restart-every-accepted", type=int, default=None)
        if command in {"process-rf", "process-multi-scene-rf", "run", "run-supervised"}:
            sub.add_argument("--max-episodes", type=int, default=None)
            sub.add_argument("--rf-policy", type=str, default=None)
            sub.add_argument("--use-gpu", action="store_true", default=None)
            sub.add_argument("--gpu-ids", type=str, default=None, help="Comma-separated CUDA device ids for episode workers.")
            sub.add_argument("--rf-workers", type=int, default=None)
        if command == "finalize":
            sub.add_argument("--split-seed", type=int, default=None)
        if command == "render-sample":
            sub.add_argument("--sample-count", type=int, default=15)
            sub.add_argument("--seed", type=int, default=None)
            sub.add_argument("--output-dir", type=Path, default=None)
            sub.add_argument("--allow-partial", action="store_true")
        if command == "select-pilot-plans":
            sub.add_argument("--output-json", type=Path, default=None)
        if command == "select-validation-plans":
            sub.add_argument("--output-json", type=Path, default=None)
        if command == "run-multi-scene-supervised":
            sub.add_argument("--max-episodes", type=int, default=None)
        if command == "regenerate-tx-catalogs":
            sub.add_argument("--placement-method", type=str, default=None)
            sub.add_argument("--sidecar", action="store_true")
            sub.add_argument("--max-scenes", type=int, default=None)
            sub.add_argument("--no-visualization", action="store_true")
            sub.add_argument("--allow-active-collection", action="store_true")
        if command == "promote-tx-catalogs":
            sub.add_argument("--source", type=str, default="candidate_lane_exclusion")
            sub.add_argument("--allow-active-collection", action="store_true")
    review = subparsers.add_parser("render-review")
    review.add_argument("--episode-dir", type=Path, required=True)
    review.add_argument("--output-dir", type=Path, required=True)
    review.add_argument("--frames", type=str, default="0,40,79")
    review.add_argument("--vmin-db", type=float, default=-20.0)
    review.add_argument("--vmax-db", type=float, default=20.0)

    check = subparsers.add_parser("check-contract")
    check.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    started = time.time()
    config: dict | None = None
    status = "ok"
    error: str | None = None
    try:
        if args.command == "check-contract":
            from dynamic_radio_dataset.checks.check_contract import run_checks

            result = run_checks(args.repo_root.resolve())
        elif args.command == "render-review":
            from dynamic_radio_dataset.render.review_pack import render_delta_review

            result = render_delta_review(
                episode_dir=args.episode_dir,
                output_dir=args.output_dir,
                frames=args.frames,
                vmin_db=float(args.vmin_db),
                vmax_db=float(args.vmax_db),
            )
        elif args.command in {
            "discover-scenes",
            "render-scene-previews",
            "validate-multi-scene",
            "validate-selected-scenes",
            "prepare-multi-scene",
            "repair-multi-scene-metadata",
            "collect-multi-scene",
            "process-multi-scene-rf",
            "run-multi-scene-supervised",
            "finalize-multi-scene",
        }:
            result = _run_multi_scene_command(args)
        elif args.command in {"regenerate-tx-catalogs", "promote-tx-catalogs"}:
            result = _run_tx_catalog_command(args)
        elif args.command == "prepare-rf-cache" and _looks_multi_scene_config(args.config):
            from dynamic_radio_dataset.multi_scene.runner import prepare_multi_scene_rf_cache

            result = prepare_multi_scene_rf_cache(args.config, force=bool(args.force))
        else:
            config = load_config(args.config, profile_override=args.profile)
            _apply_cli_config_overrides(config, args)
            if args.command == "prepare-scene":
                result = prepare_scene(config)
            elif args.command == "build-route-library":
                result = build_route_library(config)
            elif args.command == "generate-plans":
                result = generate_plan_bank(config, num_plans=args.num_plans)
            elif args.command == "collect":
                result = collect_from_plan_catalog(
                    config,
                    max_attempts=args.max_attempts,
                    target_accepted=args.target_accepted,
                    resume=not args.no_resume,
                    bucket_targets=_parse_bucket_targets(args.bucket_targets),
                    selection_manifest=args.selection_manifest,
                )
            elif args.command == "process-rf":
                result = process_rf(
                    config,
                    max_episodes=args.max_episodes,
                    rf_policy=args.rf_policy,
                    use_gpu=args.use_gpu,
                    gpu_ids=_parse_gpu_ids(args.gpu_ids),
                    workers=args.rf_workers,
                )
            elif args.command == "finalize":
                result = finalize(config, split_seed=args.split_seed)
            elif args.command == "render-sample":
                from dynamic_radio_dataset.render.sample import render_green_absolute_sample

                result = render_green_absolute_sample(
                    config,
                    sample_count=args.sample_count,
                    seed=args.seed,
                    output_dir=args.output_dir,
                    allow_partial=args.allow_partial,
                )
            elif args.command == "timing-report":
                from dynamic_radio_dataset.pipeline.reports import write_timing_report

                result = write_timing_report(config)
            elif args.command == "verify-release":
                result = verify_release_dataset(config, expected_episodes=args.expected_episodes)
            elif args.command == "prune-release":
                result = prune_release_dataset(config, expected_episodes=args.expected_episodes)
            elif args.command == "failure-report":
                from dynamic_radio_dataset.diagnostics.failure_report import write_failure_report

                result = write_failure_report(config, output_path=args.output_json, max_examples=args.max_examples)
            elif args.command == "select-pilot-plans":
                from dynamic_radio_dataset.plans.pilot_selector import select_pilot_plans

                result = select_pilot_plans(config, output_path=args.output_json)
            elif args.command == "select-validation-plans":
                from dynamic_radio_dataset.plans.validation_selector import select_validation_plans

                result = select_validation_plans(config, output_path=args.output_json)
            elif args.command == "prepare-rf-cache":
                from dynamic_radio_dataset.rf.static_cache import prepare_rf_static_cache

                result = prepare_rf_static_cache(config, force=bool(args.force))
            elif args.command == "assign-tx":
                from dynamic_radio_dataset.tx.assignment import ensure_tx_assignments

                result = ensure_tx_assignments(config)
            elif args.command == "run":
                result = run_all(
                    config,
                    num_plans=args.num_plans,
                    max_attempts=args.max_attempts,
                    target_accepted=args.target_accepted,
                    max_episodes=args.max_episodes,
                    resume=not args.no_resume,
                    selection_manifest=args.selection_manifest,
                )
            elif args.command == "run-supervised":
                from dynamic_radio_dataset.pipeline.supervisor import run_supervised

                result = run_supervised(
                    config,
                    num_plans=args.num_plans,
                    max_attempts=args.max_attempts,
                    target_accepted=args.target_accepted,
                    max_episodes=args.max_episodes,
                    resume=not args.no_resume,
                    render_sample=not args.no_render_sample,
                    render_sample_count=args.render_sample_count,
                    restart_every_accepted=args.restart_every_accepted,
                    selection_manifest=args.selection_manifest,
                )
            else:
                raise ValueError(f"Unsupported command: {args.command}")
        print(result)
        return 0
    except Exception as exc:
        status = "error"
        error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        if config is not None and args.command not in {"verify-release", "prune-release", "failure-report"}:
            _record_cli_timing(config, args, elapsed_s=time.time() - started, status=status, error=error)


def _record_cli_timing(
    config: dict,
    args: argparse.Namespace,
    *,
    elapsed_s: float,
    status: str,
    error: str | None,
) -> None:
    try:
        root = dataset_root(config)
        root.mkdir(parents=True, exist_ok=True)
        row = {
            "schema": "dynamic_radio_dataset_cli_timing_v1",
            "timestamp_utc": datetime.now(timezone.utc).isoformat(),
            "command": str(args.command),
            "elapsed_s": float(elapsed_s),
            "status": status,
            "error": error,
            "config_path": str(config.get("config_path", "")),
            "dataset_root": str(root),
            "args": {key: _jsonable(value) for key, value in vars(args).items()},
        }
        with (root / "command_timings.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _parse_bucket_targets(text: str | None) -> dict[int, int] | None:
    if text is None or not str(text).strip():
        return None
    result: dict[int, int] = {}
    for item in str(text).split(","):
        if not item.strip():
            continue
        key, sep, value = item.partition(":")
        if not sep:
            raise ValueError(f"Invalid bucket target {item!r}; expected COUNT:TARGET.")
        result[int(key.strip())] = int(value.strip())
    return result


def _parse_gpu_ids(text: str | None) -> list[str] | None:
    if text is None or not str(text).strip():
        return None
    return [item.strip() for item in str(text).split(",") if item.strip()]


def _parse_int_list(text: str | None) -> list[int] | None:
    if text is None or not str(text).strip():
        return None
    return [int(item.strip()) for item in str(text).split(",") if item.strip()]


def _run_multi_scene_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "discover-scenes":
        from dynamic_radio_dataset.carla.scene_discovery import discover_scenes

        return discover_scenes(args.config, max_candidates=args.max_candidates)
    if args.command == "render-scene-previews":
        from dynamic_radio_dataset.multi_scene.preview import render_scene_previews

        return render_scene_previews(args.config, limit=args.limit)
    if args.command == "validate-multi-scene":
        from dynamic_radio_dataset.multi_scene.runner import validate_multi_scene

        return validate_multi_scene(args.config)
    if args.command == "validate-selected-scenes":
        from dynamic_radio_dataset.multi_scene.runner import validate_selected_scenes

        return validate_selected_scenes(args.config)
    if args.command == "prepare-multi-scene":
        from dynamic_radio_dataset.multi_scene.runner import prepare_multi_scene

        return prepare_multi_scene(args.config, max_scenes=args.max_scenes)
    if args.command == "repair-multi-scene-metadata":
        from dynamic_radio_dataset.multi_scene.repair import repair_multi_scene_metadata

        return repair_multi_scene_metadata(args.config)
    if args.command == "collect-multi-scene":
        from dynamic_radio_dataset.multi_scene.carla_parallel import collect_multi_scene_carla_parallel

        return collect_multi_scene_carla_parallel(
            args.config,
            carla_workers=args.carla_workers,
            gpu_id=args.gpu_id,
            gpu_ids=_parse_gpu_ids(args.gpu_ids),
            rpc_ports=_parse_int_list(args.rpc_ports),
            traffic_manager_ports=_parse_int_list(args.tm_ports),
            target_accepted_per_scene=args.target_accepted_per_scene,
            max_scenes=args.max_scenes,
            max_attempts_per_scene=args.max_attempts_per_scene,
            resume=not args.no_resume,
            keep_carla_running=bool(args.keep_carla_running),
            jitter_scene_seeds=not args.no_jitter_scene_seeds,
            max_carla_restarts_per_scene=args.max_carla_restarts_per_scene,
            no_rendering_mode=bool(args.no_rendering_mode),
            dry_run=bool(args.dry_run),
        )
    if args.command == "process-multi-scene-rf":
        from dynamic_radio_dataset.multi_scene.runner import process_multi_scene_rf

        return process_multi_scene_rf(
            args.config,
            max_scenes=args.max_scenes,
            max_episodes=args.max_episodes,
            rf_policy=args.rf_policy,
            use_gpu=args.use_gpu,
            gpu_ids=_parse_gpu_ids(args.gpu_ids),
            workers=args.rf_workers,
        )
    if args.command == "run-multi-scene-supervised":
        from dynamic_radio_dataset.multi_scene.runner import run_multi_scene_supervised

        return run_multi_scene_supervised(args.config, max_scenes=args.max_scenes, max_episodes=args.max_episodes)
    if args.command == "finalize-multi-scene":
        from dynamic_radio_dataset.multi_scene.runner import finalize_multi_scene

        return finalize_multi_scene(args.config)
    raise ValueError(f"Unsupported multi-scene command: {args.command}")


def _run_tx_catalog_command(args: argparse.Namespace) -> dict[str, Any]:
    if args.command == "regenerate-tx-catalogs":
        from dynamic_radio_dataset.tx.catalog_tools import regenerate_tx_catalogs

        return regenerate_tx_catalogs(
            args.config,
            placement_method=args.placement_method,
            sidecar=bool(args.sidecar),
            max_scenes=args.max_scenes,
            render_visualization=not bool(args.no_visualization),
            allow_active_collection=bool(args.allow_active_collection),
        )
    if args.command == "promote-tx-catalogs":
        from dynamic_radio_dataset.tx.catalog_tools import promote_tx_catalogs

        return promote_tx_catalogs(
            args.config,
            source=args.source,
            allow_active_collection=bool(args.allow_active_collection),
        )
    raise ValueError(f"Unsupported TX catalog command: {args.command}")


def _looks_multi_scene_config(path: Path) -> bool:
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False
    markers = ("base_single_scene_config", "single_scene_template_config", "selected_tx_per_episode", "tx_candidates_per_scene")
    return "scenes:" in text and any(marker in text for marker in markers)


def _apply_cli_config_overrides(config: dict, args: argparse.Namespace) -> None:
    bucket_targets = _parse_bucket_targets(getattr(args, "bucket_targets", None))
    if bucket_targets is not None:
        config.setdefault("collection", {})["bucket_targets"] = bucket_targets
    gpu_ids = _parse_gpu_ids(getattr(args, "gpu_ids", None))
    if gpu_ids is not None:
        config.setdefault("sionna", {})["gpu_ids"] = gpu_ids
    if getattr(args, "use_gpu", None) is not None:
        config.setdefault("sionna", {})["use_gpu"] = bool(args.use_gpu)
    if getattr(args, "rf_workers", None) is not None:
        config.setdefault("sionna", {})["rf_workers"] = int(args.rf_workers)
    if getattr(args, "rf_policy", None) is not None:
        config.setdefault("sionna", {})["rf_policy"] = str(args.rf_policy)
    selection_manifest = getattr(args, "selection_manifest", None)
    if selection_manifest is not None:
        config.setdefault("collection", {})["selection_manifest"] = str(selection_manifest)


if __name__ == "__main__":
    raise SystemExit(main())
