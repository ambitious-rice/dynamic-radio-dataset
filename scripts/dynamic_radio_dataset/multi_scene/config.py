from __future__ import annotations

import ast
import re
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.configs import deep_merge, load_config
from dynamic_radio_dataset.paths import resolve_repo_path


def load_multi_scene_config(path: Path) -> dict[str, Any]:
    config = load_yaml_subset(path)
    config["config_path"] = str(Path(path).resolve())
    validate_multi_scene_config(config, require_scenes=False)
    return config


def validate_multi_scene_config(config: Mapping[str, Any], *, require_scenes: bool = True) -> dict[str, Any]:
    dataset = _as_mapping(config.get("dataset"), "dataset")
    if not str(dataset.get("root", "")).strip():
        raise ValueError("multi-scene config requires dataset.root")
    if int(dataset.get("tx_candidates_per_scene", config.get("tx_candidates_per_scene", 20))) <= 0:
        raise ValueError("tx_candidates_per_scene must be positive")
    selected = int(dataset.get("selected_tx_per_episode", config.get("selected_tx_per_episode", 5)))
    if selected <= 0:
        raise ValueError("selected_tx_per_episode must be positive")
    scenes = config.get("scenes", [])
    if scenes in (None, ""):
        scenes = []
    if not isinstance(scenes, list):
        raise ValueError("multi-scene config scenes must be a list")
    if require_scenes and not scenes:
        raise ValueError("multi-scene config must include at least one scene")
    seen_scene_ids: set[str] = set()
    for scene in scenes:
        if not isinstance(scene, Mapping):
            raise ValueError(f"scene entry must be a mapping: {scene!r}")
        scene_id = str(scene.get("scene_id", "")).strip()
        if not scene_id:
            raise ValueError("each scene needs scene_id")
        if scene_id in seen_scene_ids:
            raise ValueError(f"duplicate scene_id: {scene_id}")
        seen_scene_ids.add(scene_id)
        if not str(scene.get("town", "")).strip():
            raise ValueError(f"scene {scene_id} needs town")
        selector = scene.get("selector", {})
        if selector and not isinstance(selector, Mapping):
            raise ValueError(f"scene {scene_id} selector must be a mapping")
    return {"scene_count": len(scenes), "selected_tx_per_episode": selected}


def selected_scene_manifest_ids(manifest: Mapping[str, Any]) -> tuple[list[str], list[str]]:
    selected = [str(item).strip() for item in manifest.get("selected_candidate_ids", []) if str(item).strip()]
    backups = [str(item).strip() for item in manifest.get("backup_candidate_ids", []) if str(item).strip()]
    if not selected:
        scenes = manifest.get("scenes", [])
        if isinstance(scenes, list):
            selected = [str(row.get("candidate_id", "")).strip() for row in scenes if isinstance(row, Mapping)]
            selected = [item for item in selected if item]
    return selected, backups


def multi_scene_root(config: Mapping[str, Any]) -> Path:
    return resolve_repo_path(str(_as_mapping(config.get("dataset"), "dataset")["root"]))


def scene_dataset_root(config: Mapping[str, Any], scene: Mapping[str, Any]) -> Path:
    return multi_scene_root(config) / "scenes" / str(scene["scene_id"])


def discovery_output_dir(config: Mapping[str, Any]) -> Path:
    return multi_scene_root(config) / "discovery"


def candidate_catalog_path(config: Mapping[str, Any]) -> Path:
    raw = _as_mapping(config.get("discovery", {}), "discovery").get("candidate_catalog")
    if raw:
        path = Path(str(raw))
        return path if path.is_absolute() else multi_scene_root(config) / path
    return discovery_output_dir(config) / "scene_candidates.jsonl"


def preview_dir(config: Mapping[str, Any]) -> Path:
    raw = _as_mapping(_as_mapping(config.get("discovery", {}), "discovery").get("preview", {}), "discovery.preview").get("output_dir")
    if raw:
        path = Path(str(raw))
        return path if path.is_absolute() else multi_scene_root(config) / path
    return discovery_output_dir(config) / "previews"


def base_single_scene_config(config: Mapping[str, Any]) -> dict[str, Any]:
    raw = config.get("base_single_scene_config") or config.get("single_scene_template_config")
    if raw:
        return load_config(resolve_repo_path(str(raw)))
    inline = config.get("single_scene_template", {})
    if not isinstance(inline, Mapping):
        raise ValueError("single_scene_template must be a mapping when no base config path is provided")
    return deepcopy(dict(inline))


def make_single_scene_config(config: Mapping[str, Any], scene: Mapping[str, Any]) -> dict[str, Any]:
    base = base_single_scene_config(config)
    root = scene_dataset_root(config, scene)
    dataset = _as_mapping(config.get("dataset"), "dataset")
    tx_defaults = deepcopy(dict(config.get("tx", {}))) if isinstance(config.get("tx"), Mapping) else {}
    tx_defaults.update(
        {
            "candidate_count": int(dataset.get("tx_candidates_per_scene", tx_defaults.get("candidate_count", 20))),
            "selected_tx_per_episode": int(dataset.get("selected_tx_per_episode", tx_defaults.get("selected_tx_per_episode", 5))),
            "placement_seed": int(scene.get("placement_seed", tx_defaults.get("placement_seed", dataset.get("seed", 1701)))),
        }
    )
    scene_cfg = {
        "dataset": {
            "root": _repo_relative_text(root),
            "seed": int(scene.get("seed", dataset.get("seed", base.get("dataset", {}).get("seed", 17)))),
            "split_seed": int(dataset.get("split_seed", base.get("dataset", {}).get("split_seed", 7))),
        },
        "scene": {
            "town": scene.get("town"),
            "scene_mode": _selector_mode(scene),
            "scene_id": scene.get("scene_id"),
            "scene_type": scene.get("scene_type", "unknown"),
            "selector": dict(scene.get("selector", {})) if isinstance(scene.get("selector", {}), Mapping) else {},
            "static_dir": str(root / "scene_static"),
            "reference_export_dir": str(root / "reference_scene" / "sionna_export"),
        },
        "routes": {"source_dir": str(root / "reference_scene")},
        "traffic": {
            "duration_s": float(dataset.get("episode_duration_s", base.get("traffic", {}).get("duration_s", 10.0))),
            "fps": float(dataset.get("fps", base.get("traffic", {}).get("fps", 10.0))),
        },
        "sionna": {
            "resolution": int((dataset.get("rss_grid") or [base.get("sionna", {}).get("resolution", 128)])[0])
            if isinstance(dataset.get("rss_grid", []), list)
            else int(base.get("sionna", {}).get("resolution", 128)),
        },
        "tx": tx_defaults,
        "collection": {
            "selection_manifest": None,
            "bucket_targets": {},
        },
        "profile": {
            "target_accepted_episodes": int(scene.get("target_accepted", dataset.get("accepted_trajectories_per_scene", 0) or 0)),
        },
    }
    scene_override = scene.get("single_scene_overrides", {})
    if scene_override:
        if not isinstance(scene_override, Mapping):
            raise ValueError(f"single_scene_overrides must be a mapping for {scene.get('scene_id')}")
        scene_cfg = deep_merge(scene_cfg, scene_override)
    merged = deep_merge(base, scene_cfg)
    _clear_single_scene_collection_targets(merged)
    merged["multi_scene"] = {
        "parent_root": str(multi_scene_root(config)),
        "scene_id": str(scene["scene_id"]),
        "scene_type": str(scene.get("scene_type", "unknown")),
        "candidate_id": str(scene.get("candidate_id", "")),
    }
    return merged


def _clear_single_scene_collection_targets(config: dict[str, Any]) -> None:
    """Remove target quotas inherited from single-scene release configs."""

    collection = config.setdefault("collection", {})
    collection["bucket_targets"] = {}
    collection["bucket_order"] = []
    collection.pop("selection_matrix", None)
    collection["selection_manifest"] = None



def _repo_relative_text(path: Path) -> str:
    repo = resolve_repo_path(".")
    try:
        return str(path.relative_to(repo))
    except ValueError:
        return str(path)

def _selector_mode(scene: Mapping[str, Any]) -> str:
    selector = scene.get("selector", {}) if isinstance(scene.get("selector", {}), Mapping) else {}
    return str(selector.get("mode", scene.get("scene_mode", "junction")))


def _as_mapping(value: Any, name: str) -> Mapping[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be a mapping")
    return value


def load_yaml_subset(path: Path) -> dict[str, Any]:
    lines = _yaml_lines(Path(path))
    if not lines:
        return {}
    value, index = _parse_block(lines, 0, lines[0][0])
    if index != len(lines):
        raise ValueError(f"Could not parse all lines in {path}: stopped at {index + 1}")
    if not isinstance(value, dict):
        raise ValueError(f"Top-level YAML document must be a mapping: {path}")
    return value


def _yaml_lines(path: Path) -> list[tuple[int, str]]:
    rows: list[tuple[int, str]] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        text = _strip_comment(raw.rstrip())
        if not text.strip():
            continue
        rows.append((len(text) - len(text.lstrip(" ")), text.strip()))
    return rows


def _parse_block(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[Any, int]:
    if index >= len(lines):
        return {}, index
    if lines[index][1].startswith("- "):
        return _parse_list(lines, index, indent)
    return _parse_dict(lines, index, indent)


def _parse_dict(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[dict[str, Any], int]:
    result: dict[str, Any] = {}
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent or text.startswith("- "):
            break
        if line_indent > indent:
            raise ValueError(f"Unexpected indentation near: {text}")
        key, sep, rest = text.partition(":")
        if not sep:
            raise ValueError(f"Expected key: value line, got: {text}")
        key = key.strip()
        rest = rest.strip()
        index += 1
        if rest:
            result[key] = _parse_scalar(rest)
        elif index < len(lines) and lines[index][0] > line_indent:
            result[key], index = _parse_block(lines, index, lines[index][0])
        else:
            result[key] = {}
    return result, index


def _parse_list(lines: list[tuple[int, str]], index: int, indent: int) -> tuple[list[Any], int]:
    items: list[Any] = []
    while index < len(lines):
        line_indent, text = lines[index]
        if line_indent < indent or not text.startswith("- "):
            break
        if line_indent > indent:
            raise ValueError(f"Unexpected list indentation near: {text}")
        rest = text[2:].strip()
        index += 1
        if not rest:
            item, index = _parse_block(lines, index, lines[index][0]) if index < len(lines) else ({}, index)
        elif _looks_like_mapping_item(rest):
            key, _, value = rest.partition(":")
            item = {key.strip(): _parse_scalar(value.strip()) if value.strip() else {}}
            if not value.strip() and index < len(lines) and lines[index][0] > line_indent:
                item[key.strip()], index = _parse_block(lines, index, lines[index][0])
        else:
            item = _parse_scalar(rest)
        if index < len(lines) and lines[index][0] > line_indent:
            child, index = _parse_block(lines, index, lines[index][0])
            if isinstance(item, dict) and isinstance(child, dict):
                item.update(child)
            else:
                raise ValueError("Nested list continuation is only supported for mapping items")
        items.append(item)
    return items, index


def _looks_like_mapping_item(text: str) -> bool:
    return bool(re.match(r"^[A-Za-z0-9_\-]+\s*:", text))


def _parse_scalar(text: str) -> Any:
    raw = text.strip()
    if raw in {"", "null", "None", "~"}:
        return None
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if raw.startswith("[") and raw.endswith("]"):
        inner = raw[1:-1].strip()
        return [] if not inner else [_parse_scalar(part) for part in _split_top_level(inner)]
    if raw.startswith("{") and raw.endswith("}"):
        inner = raw[1:-1].strip()
        result: dict[str, Any] = {}
        if inner:
            for part in _split_top_level(inner):
                key, sep, value = part.partition(":")
                if not sep:
                    raise ValueError(f"Invalid inline mapping item: {part}")
                result[str(_parse_scalar(key.strip()))] = _parse_scalar(value.strip())
        return result
    try:
        return ast.literal_eval(raw)
    except Exception:
        pass
    try:
        if any(ch in raw for ch in [".", "e", "E"]):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw.strip("\"'")


def _split_top_level(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    quote: str | None = None
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth == 0:
            parts.append(text[start:idx].strip())
            start = idx + 1
    parts.append(text[start:].strip())
    return [part for part in parts if part]


def _strip_comment(text: str) -> str:
    quote: str | None = None
    for idx, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
            continue
        if ch in {'"', "'"}:
            quote = ch
        elif ch == "#" and (idx == 0 or text[idx - 1].isspace()):
            return text[:idx].rstrip()
    return text
