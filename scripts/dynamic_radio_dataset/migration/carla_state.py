from __future__ import annotations

import argparse
import shutil
import tarfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from dynamic_radio_dataset.configs import load_config
from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.multi_scene.config import (
    load_multi_scene_config,
    make_single_scene_config,
    multi_scene_root,
)
from dynamic_radio_dataset.paths import dataset_root, repo_root, resolve_repo_path


ROOT_KEEP_DIRS = ("configs", "indexes")
ROOT_KEEP_SUFFIXES = (".json", ".jsonl", ".yaml", ".yml")
SCENE_ROOT_KEEP = (
    "collection_summary.json",
    "collection_worker_summary.json",
    "scene_failure.json",
)
REFERENCE_KEEP = (
    "collection_stage.json",
    "validation_report.json",
    "scene_meta.json",
    "routes.json",
    "frames/actor_states.jsonl",
)
SCENE_STATIC_KEEP_NAMES = {
    "scene_signature.json",
    "scene_static_meta.json",
    "tx_catalog.json",
    "tx_drivable_exclusion.json",
    "tx_placement_summary.json",
}
SCENE_STATIC_KEEP_PREFIXES = (
    "tx_catalog_candidate_",
    "tx_placement_summary_candidate_",
)
EPISODE_KEEP = (
    "attempt_meta.json",
    "carla_plan.json",
    "collection_stage.json",
    "episode_meta.json",
    "plan.json",
    "routes.json",
    "scene_meta.json",
    "trajectory_qa.json",
    "tx_assignment.json",
    "validation_report.json",
    "frames/actor_states.jsonl",
)
REFERENCE_EXPORT_KEEP = (
    "manifest.json",
    "motion.jsonl",
    "scene.xml",
)


def export_carla_state(
    config_path: Path,
    output_dir: Path,
    *,
    archive_path: Path | None = None,
    include_reference_export: bool = True,
    accepted_only: bool = True,
    dry_run: bool = False,
    overwrite: bool = False,
) -> dict[str, Any]:
    config_path = resolve_repo_path(config_path)
    output_dir = resolve_repo_path(output_dir)
    if output_dir.exists():
        if not overwrite:
            raise FileExistsError(f"Output directory already exists: {output_dir}")
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    repo = repo_root()
    file_rows: list[dict[str, Any]] = []
    skipped: dict[str, int] = {"missing": 0, "episode_not_accepted": 0}

    if _looks_multi_scene_config(config_path):
        config = load_multi_scene_config(config_path)
        source_root = multi_scene_root(config)
        target_root = output_dir / _rel_to_repo(source_root)
        scene_configs = [(scene, make_single_scene_config(config, scene)) for scene in config.get("scenes", [])]
    else:
        config = load_config(config_path)
        source_root = dataset_root(config)
        target_root = output_dir / _rel_to_repo(source_root)
        scene_configs = [({"scene_id": source_root.name}, config)]

    _copy_if_exists(config_path, output_dir / _rel_to_repo(config_path), file_rows, skipped, repo=repo, dry_run=dry_run)
    _copy_root_metadata(source_root, target_root, file_rows, skipped, repo=repo, dry_run=dry_run)

    scene_summaries = []
    for scene, scene_config in scene_configs:
        scene_source = dataset_root(scene_config)
        scene_target = output_dir / _rel_to_repo(scene_source)
        summary = _copy_scene_state(
            scene_source,
            scene_target,
            file_rows,
            skipped,
            include_reference_export=include_reference_export,
            accepted_only=accepted_only,
            repo=repo,
            dry_run=dry_run,
        )
        summary["scene_id"] = str(scene.get("scene_id", scene_source.name))
        scene_summaries.append(summary)

    total_bytes = sum(int(row["size_bytes"]) for row in file_rows)
    manifest = {
        "schema": "dynamic_radio_carla_state_export_v1",
        "created_utc": datetime.now(timezone.utc).isoformat(),
        "config_path": _rel_to_repo(config_path),
        "source_dataset_root": _rel_to_repo(source_root),
        "output_dir": str(output_dir),
        "include_reference_export": bool(include_reference_export),
        "accepted_only": bool(accepted_only),
        "dry_run": bool(dry_run),
        "file_count": len(file_rows),
        "total_bytes": total_bytes,
        "total_mib": round(total_bytes / (1024 * 1024), 3),
        "skipped": skipped,
        "scenes": scene_summaries,
        "files": file_rows,
        "excluded_rf_outputs": [
            "rss_dynamic_dbm.npz",
            "rss_delta_from_static_db.npz",
            "traffic_grid_uint8.npz",
            "rf_process_meta.json",
            "scene_static/tx_*/static_rss_dbm.npy",
            "episode/tx_*/rss_maps.npz",
            "episode/sionna_export",
        ],
    }
    manifest_path = output_dir / "carla_state_manifest.json"
    save_json(manifest_path, manifest)
    manifest["manifest_path"] = str(manifest_path)

    if archive_path is not None and not dry_run:
        archive_path = resolve_repo_path(archive_path)
        if archive_path.exists() and not overwrite:
            raise FileExistsError(f"Archive already exists: {archive_path}")
        archive_path.parent.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(output_dir, arcname=output_dir.name)
        manifest["archive_path"] = str(archive_path)
        manifest["archive_size_bytes"] = archive_path.stat().st_size
        manifest["archive_size_mib"] = round(archive_path.stat().st_size / (1024 * 1024), 3)
        save_json(manifest_path, manifest)

    return _summary(manifest)


def import_carla_state(source: Path, destination_root: Path | None = None, *, overwrite: bool = False) -> dict[str, Any]:
    source = resolve_repo_path(source)
    destination_root = resolve_repo_path(destination_root or ".")
    if source.is_file():
        with tarfile.open(source, "r:gz") as tar:
            members = tar.getmembers()
            _guard_tar_members(members)
            stripped = _strip_archive_top_level(members)
            targets = {destination_root / stripped_name.parts[0] for stripped_name in stripped.values() if stripped_name.parts}
            _prepare_import_targets(targets, overwrite=overwrite)
            for member in members:
                stripped_name = stripped.get(member.name)
                if stripped_name is None or not stripped_name.parts:
                    continue
                member.name = str(stripped_name)
                tar.extract(member, destination_root)
        return {"schema": "dynamic_radio_carla_state_import_v1", "status": "imported_archive", "source": str(source), "destination_root": str(destination_root)}
    if not source.is_dir():
        raise FileNotFoundError(source)
    targets = [destination_root / child.name for child in source.iterdir()]
    _prepare_import_targets(targets, overwrite=overwrite)
    for child in source.iterdir():
        target = destination_root / child.name
        if child.is_dir():
            shutil.copytree(child, target)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(child, target)
    return {"schema": "dynamic_radio_carla_state_import_v1", "status": "imported_directory", "source": str(source), "destination_root": str(destination_root)}


def _copy_scene_state(
    source: Path,
    target: Path,
    file_rows: list[dict[str, Any]],
    skipped: dict[str, int],
    *,
    include_reference_export: bool,
    accepted_only: bool,
    repo: Path,
    dry_run: bool,
) -> dict[str, Any]:
    for rel in SCENE_ROOT_KEEP:
        _copy_if_exists(source / rel, target / rel, file_rows, skipped, repo=repo, dry_run=dry_run)
    for rel in REFERENCE_KEEP:
        _copy_if_exists(source / "reference_scene" / rel, target / "reference_scene" / rel, file_rows, skipped, repo=repo, dry_run=dry_run)
    if include_reference_export:
        _copy_reference_export(
            source / "reference_scene" / "sionna_export",
            target / "reference_scene" / "sionna_export",
            file_rows,
            skipped,
            repo=repo,
            dry_run=dry_run,
        )
    _copy_scene_static(source / "scene_static", target / "scene_static", file_rows, skipped, repo=repo, dry_run=dry_run)

    episode_count = 0
    accepted_count = 0
    for episode_dir in sorted((source / "episodes").glob("episode_*")):
        if not episode_dir.is_dir():
            continue
        episode_count += 1
        if accepted_only and not _trajectory_accepted(episode_dir):
            skipped["episode_not_accepted"] += 1
            continue
        accepted_count += 1
        episode_target = target / "episodes" / episode_dir.name
        for rel in EPISODE_KEEP:
            _copy_if_exists(episode_dir / rel, episode_target / rel, file_rows, skipped, repo=repo, dry_run=dry_run)
    return {
        "source": _rel_to_repo(source),
        "episode_dirs_seen": episode_count,
        "episode_dirs_exported": accepted_count,
    }


def _copy_root_metadata(
    source: Path,
    target: Path,
    file_rows: list[dict[str, Any]],
    skipped: dict[str, int],
    *,
    repo: Path,
    dry_run: bool,
) -> None:
    for dirname in ROOT_KEEP_DIRS:
        root = source / dirname
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file() and path.suffix in ROOT_KEEP_SUFFIXES:
                _copy_if_exists(path, target / path.relative_to(source), file_rows, skipped, repo=repo, dry_run=dry_run)


def _copy_scene_static(
    source: Path,
    target: Path,
    file_rows: list[dict[str, Any]],
    skipped: dict[str, int],
    *,
    repo: Path,
    dry_run: bool,
) -> None:
    if not source.exists():
        skipped["missing"] += 1
        return
    for path in sorted(source.iterdir()):
        if not path.is_file() or path.suffix != ".json":
            continue
        if path.name in SCENE_STATIC_KEEP_NAMES or any(path.name.startswith(prefix) for prefix in SCENE_STATIC_KEEP_PREFIXES):
            _copy_if_exists(path, target / path.name, file_rows, skipped, repo=repo, dry_run=dry_run)


def _copy_reference_export(
    source: Path,
    target: Path,
    file_rows: list[dict[str, Any]],
    skipped: dict[str, int],
    *,
    repo: Path,
    dry_run: bool,
) -> None:
    if not source.exists():
        skipped["missing"] += 1
        return
    for rel in REFERENCE_EXPORT_KEEP:
        _copy_if_exists(source / rel, target / rel, file_rows, skipped, repo=repo, dry_run=dry_run)
    mesh_dir = source / "meshes"
    if mesh_dir.exists():
        for path in sorted(mesh_dir.glob("*.ply")):
            _copy_if_exists(path, target / "meshes" / path.name, file_rows, skipped, repo=repo, dry_run=dry_run)


def _copy_if_exists(
    source: Path,
    target: Path,
    file_rows: list[dict[str, Any]],
    skipped: dict[str, int],
    *,
    repo: Path,
    dry_run: bool,
) -> None:
    if not source.exists():
        skipped["missing"] += 1
        return
    stat = source.stat()
    if not dry_run:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    file_rows.append(
        {
            "source": _rel_to_repo(source),
            "path": _rel_to_repo(target, base=repo),
            "size_bytes": stat.st_size,
        }
    )


def _trajectory_accepted(episode_dir: Path) -> bool:
    qa_path = episode_dir / "trajectory_qa.json"
    if not qa_path.exists():
        return False
    try:
        return bool(load_json(qa_path).get("trajectory_qc_pass"))
    except Exception:
        return False


def _looks_multi_scene_config(path: Path) -> bool:
    text = path.read_text(encoding="utf-8", errors="replace")
    return "\nscenes:" in f"\n{text}" and "\nbase_single_scene_config:" in f"\n{text}"


def _rel_to_repo(path: Path, *, base: Path | None = None) -> str:
    root = base or repo_root()
    path = Path(path)
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(path)


def _guard_tar_members(members: Iterable[tarfile.TarInfo]) -> None:
    for member in members:
        path = Path(member.name)
        if path.is_absolute() or ".." in path.parts:
            raise RuntimeError(f"Unsafe archive member path: {member.name}")


def _strip_archive_top_level(members: Iterable[tarfile.TarInfo]) -> dict[str, Path]:
    paths = [Path(member.name) for member in members if member.name]
    top_names = {path.parts[0] for path in paths if path.parts}
    strip_top = len(top_names) == 1
    result: dict[str, Path] = {}
    for path in paths:
        result[str(path)] = Path(*path.parts[1:]) if strip_top else path
    return result


def _prepare_import_targets(targets: Iterable[Path], *, overwrite: bool) -> None:
    for target in sorted(set(targets)):
        if target.exists() and not overwrite:
            raise FileExistsError(f"Import target already exists: {target}")
    if overwrite:
        for target in sorted(set(targets)):
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()


def _summary(manifest: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema": manifest["schema"],
        "manifest_path": manifest.get("manifest_path"),
        "archive_path": manifest.get("archive_path"),
        "file_count": manifest["file_count"],
        "total_mib": manifest["total_mib"],
        "archive_size_mib": manifest.get("archive_size_mib"),
        "scene_count": len(manifest.get("scenes", [])),
        "episode_dirs_exported": sum(int(scene.get("episode_dirs_exported", 0)) for scene in manifest.get("scenes", [])),
        "skipped": manifest.get("skipped", {}),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Export/import CARLA-only state for RF migration.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    export = subparsers.add_parser("export")
    export.add_argument("--config", type=Path, required=True)
    export.add_argument("--output-dir", type=Path, required=True)
    export.add_argument("--archive", type=Path, default=None)
    export.add_argument("--no-reference-export", action="store_true")
    export.add_argument("--include-unaccepted", action="store_true")
    export.add_argument("--dry-run", action="store_true")
    export.add_argument("--overwrite", action="store_true")

    import_parser = subparsers.add_parser("import")
    import_parser.add_argument("--source", type=Path, required=True)
    import_parser.add_argument("--destination-root", type=Path, default=Path("."))
    import_parser.add_argument("--overwrite", action="store_true")

    args = parser.parse_args(argv)
    if args.command == "export":
        result = export_carla_state(
            args.config,
            args.output_dir,
            archive_path=args.archive,
            include_reference_export=not args.no_reference_export,
            accepted_only=not args.include_unaccepted,
            dry_run=bool(args.dry_run),
            overwrite=bool(args.overwrite),
        )
    elif args.command == "import":
        result = import_carla_state(args.source, args.destination_root, overwrite=bool(args.overwrite))
    else:
        raise ValueError(args.command)
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
