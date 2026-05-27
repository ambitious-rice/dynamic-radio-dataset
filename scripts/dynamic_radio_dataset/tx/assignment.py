from __future__ import annotations

import hashlib
import random
from collections import Counter
from pathlib import Path
from typing import Any, Mapping, Sequence

import numpy as np

from dynamic_radio_dataset.json_utils import load_json, save_json
from dynamic_radio_dataset.paths import dataset_root
from dynamic_radio_dataset.rf.processing import trajectory_qc_pass


def ensure_tx_assignments(config: dict) -> dict[str, Any]:
    selected_count = int(config.get("tx", {}).get("selected_tx_per_episode", 0)) if isinstance(config.get("tx"), Mapping) else 0
    if selected_count <= 0:
        return {"mode": "all_tx", "assigned_episode_count": 0}
    root = dataset_root(config)
    tx_catalog = load_json(root / "scene_static" / "tx_catalog.json").get("tx_catalog", [])
    if len(tx_catalog) < selected_count:
        raise RuntimeError(f"Need at least {selected_count} TX candidates, found {len(tx_catalog)}")
    usage = _existing_usage(root / "episodes")
    assigned = 0
    skipped = 0
    for episode_dir in sorted((root / "episodes").glob("episode_*")):
        if not trajectory_qc_pass(episode_dir):
            continue
        assignment_path = episode_dir / "tx_assignment.json"
        if assignment_path.exists():
            skipped += 1
            continue
        row = assign_episode_txs(config, tx_catalog, episode_dir.name, usage)
        save_json(assignment_path, row)
        for tx_id in row["selected_tx_ids"]:
            usage[str(tx_id)] += 1
        assigned += 1
    return {
        "mode": "selected_tx_per_episode",
        "selected_tx_per_episode": selected_count,
        "assigned_episode_count": int(assigned),
        "skipped_existing_assignment_count": int(skipped),
        "usage_counts": dict(sorted(usage.items())),
    }


def assign_episode_txs(config: Mapping[str, Any], tx_catalog: Sequence[Mapping[str, Any]], episode_id: str, usage: Counter[str]) -> dict[str, Any]:
    selected_count = int(config.get("tx", {}).get("selected_tx_per_episode", 5)) if isinstance(config.get("tx"), Mapping) else 5
    seed = int(config.get("tx", {}).get("assignment_seed", config.get("dataset", {}).get("seed", 1701) if isinstance(config.get("dataset"), Mapping) else 1701))
    rng = random.Random(_episode_seed(seed, episode_id))
    rows = [dict(tx) for tx in tx_catalog]
    selected: list[dict[str, Any]] = []
    min_dist = float(config.get("tx", {}).get("assignment_min_pairwise_distance_m", 6.0)) if isinstance(config.get("tx"), Mapping) else 6.0
    while len(selected) < selected_count and rows:
        ranked = sorted(rows, key=lambda tx: (int(usage.get(str(tx["tx_id"]), 0)), _cluster_penalty(tx, selected), rng.random(), str(tx["tx_id"])))
        chosen = None
        for tx in ranked:
            if _far_enough(tx, selected, min_dist) or len(rows) <= selected_count - len(selected):
                chosen = tx
                break
        if chosen is None:
            chosen = ranked[0]
        selected.append(chosen)
        rows = [tx for tx in rows if str(tx["tx_id"]) != str(chosen["tx_id"])]
    ids = [str(tx["tx_id"]) for tx in selected]
    return {
        "schema": "tx_assignment_v1",
        "episode_id": str(episode_id),
        "assignment_seed": int(seed),
        "assignment_policy": "balanced_usage_spatial_greedy",
        "selected_tx_per_episode": int(selected_count),
        "selected_tx_ids": ids,
        "selected_tx_candidate_ids": [str(tx.get("tx_candidate_id", tx["tx_id"])) for tx in selected],
        "usage_counts_before": {tx_id: int(usage.get(tx_id, 0)) for tx_id in ids},
    }


def selected_tx_catalog(static_dir: Path, episode_dir: Path) -> dict[str, Any]:
    bundle = load_json(static_dir / "tx_catalog.json")
    assignment_path = episode_dir / "tx_assignment.json"
    if not assignment_path.exists():
        return bundle
    assignment = load_json(assignment_path)
    selected_ids = [str(item) for item in assignment.get("selected_tx_ids", [])]
    by_id = {str(tx["tx_id"]): tx for tx in bundle.get("tx_catalog", [])}
    missing = [tx_id for tx_id in selected_ids if tx_id not in by_id]
    if missing:
        raise RuntimeError(f"TX assignment references unknown tx_id(s): {missing}")
    selected = [by_id[tx_id] for tx_id in selected_ids]
    result = dict(bundle)
    result["source_tx_catalog_count"] = len(bundle.get("tx_catalog", []))
    result["selected_tx_assignment"] = assignment
    result["tx_catalog"] = selected
    return result


def _existing_usage(episodes_dir: Path) -> Counter[str]:
    usage: Counter[str] = Counter()
    for path in sorted(episodes_dir.glob("episode_*/tx_assignment.json")):
        try:
            row = load_json(path)
        except Exception:  # noqa: BLE001
            continue
        for tx_id in row.get("selected_tx_ids", []):
            usage[str(tx_id)] += 1
    return usage


def _episode_seed(seed: int, episode_id: str) -> int:
    digest = hashlib.sha1(f"{seed}:{episode_id}".encode("utf-8")).hexdigest()[:12]
    return int(digest, 16)


def _xy(tx: Mapping[str, Any]) -> np.ndarray:
    pos = tx.get("position", {}) if isinstance(tx.get("position"), Mapping) else {}
    return np.array([float(pos.get("x", 0.0)), float(pos.get("y", 0.0))], dtype=np.float64)


def _far_enough(tx: Mapping[str, Any], selected: Sequence[Mapping[str, Any]], min_dist: float) -> bool:
    if not selected:
        return True
    p = _xy(tx)
    return all(float(np.linalg.norm(p - _xy(other))) >= min_dist for other in selected)


def _cluster_penalty(tx: Mapping[str, Any], selected: Sequence[Mapping[str, Any]]) -> float:
    if not selected:
        return 0.0
    p = _xy(tx)
    nearest = min(float(np.linalg.norm(p - _xy(other))) for other in selected)
    return -nearest
