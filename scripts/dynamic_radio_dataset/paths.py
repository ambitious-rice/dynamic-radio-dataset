from __future__ import annotations

from pathlib import Path


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def resolve_repo_path(path_text: str | Path) -> Path:
    path = Path(path_text)
    return path if path.is_absolute() else repo_root() / path


def dataset_root(config: dict) -> Path:
    return resolve_repo_path(config["dataset"]["root"])


def ensure_dataset_dirs(config: dict) -> dict[str, Path]:
    root = dataset_root(config)
    dirs = {
        "root": root,
        "configs": root / "configs",
        "reference_scene": root / "reference_scene",
        "scene_static": root / "scene_static",
        "route_library": root / "route_library",
        "plan_catalog": root / "plan_catalog",
        "attempts": root / "attempts",
        "episodes": root / "episodes",
        "failed_attempts": root / "failed_attempts",
        "indexes": root / "indexes",
        "renders": root / "renders",
    }
    for path in dirs.values():
        path.mkdir(parents=True, exist_ok=True)
    return dirs
