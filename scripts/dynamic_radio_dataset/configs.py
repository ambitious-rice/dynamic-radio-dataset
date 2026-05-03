from __future__ import annotations

import ast
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping


def _parse_scalar(text: str) -> Any:
    raw = text.strip()
    if raw in {"", "null", "None"}:
        return None
    if raw in {"true", "True"}:
        return True
    if raw in {"false", "False"}:
        return False
    if (raw.startswith("[") and raw.endswith("]")) or (raw.startswith("{") and raw.endswith("}")):
        return ast.literal_eval(raw)
    try:
        if any(ch in raw for ch in [".", "e", "E"]):
            return float(raw)
        return int(raw)
    except ValueError:
        return raw.strip("\"'")


def load_simple_yaml(path: Path) -> dict:
    """Load the limited YAML subset used by this repo's config files."""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]
    for raw_line in Path(path).read_text(encoding="utf-8").splitlines():
        if not raw_line.strip() or raw_line.lstrip().startswith("#"):
            continue
        indent = len(raw_line) - len(raw_line.lstrip(" "))
        key, sep, value = raw_line.strip().partition(":")
        if not sep:
            raise ValueError(f"Unsupported config line: {raw_line}")
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if value.strip():
            parent[key] = _parse_scalar(value)
        else:
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
    return root


def deep_merge(base: dict, override: Mapping[str, Any]) -> dict:
    result = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, Mapping) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = deepcopy(value)
    return result


def load_config(path: Path, profile_override: str | None = None) -> dict:
    config = load_simple_yaml(path)
    profiles = config.get("profiles", {})
    profile_name = profile_override or str(config.get("profile", {}).get("name", "smoke"))
    if isinstance(profiles, dict) and profile_name in profiles:
        config = deep_merge(config, profiles[profile_name])
    config.setdefault("profile", {})["name"] = profile_name
    config["config_path"] = str(Path(path).resolve())
    return config

