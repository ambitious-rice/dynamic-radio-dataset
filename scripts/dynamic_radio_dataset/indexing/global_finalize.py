from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Mapping

from dynamic_radio_dataset.indexing.episode_index import deterministic_split
from dynamic_radio_dataset.json_utils import save_json, write_jsonl
from dynamic_radio_dataset.multi_scene.config import multi_scene_root, scene_dataset_root


def finalize_global_index(config: Mapping[str, Any]) -> dict[str, Any]:
    root = multi_scene_root(config)
    indexes_dir = root / "indexes"
    indexes_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict[str, Any]] = []
    episode_ids: list[str] = []
    tx_counter: Counter[str] = Counter()
    scene_rows = []
    for scene in config.get("scenes", []) if isinstance(config.get("scenes"), list) else []:
        scene_id = str(scene.get("scene_id"))
        scene_type = str(scene.get("scene_type", "unknown"))
        scene_root = scene_dataset_root(config, scene)
        index_path = scene_root / "episode_index.jsonl"
        count = 0
        if index_path.exists():
            for row in _read_jsonl(index_path):
                episode_id = str(row.get("episode_id", ""))
                global_episode_id = f"{scene_id}/{episode_id}"
                out = dict(row)
                out["scene_id"] = scene_id
                out["scene_type"] = scene_type
                out["local_episode_id"] = episode_id
                out["global_episode_id"] = global_episode_id
                out["tx_candidate_id"] = str(out.get("tx_candidate_id", out.get("tx_id", "")))
                rows.append(out)
                episode_ids.append(global_episode_id)
                tx_counter[f"{scene_id}:{out.get('tx_id')}"] += 1
                count += 1
        scene_rows.append({"scene_id": scene_id, "scene_type": scene_type, "index_row_count": count, "index_path": str(index_path)})
    write_jsonl(indexes_dir / "global_episode_index.jsonl", rows)
    save_json(indexes_dir / "global_splits.json", deterministic_split(sorted(set(episode_ids)), seed=int(config.get("dataset", {}).get("split_seed", 7) if isinstance(config.get("dataset"), Mapping) else 7)))
    save_json(indexes_dir / "tx_summary.json", {"schema": "multi_scene_tx_summary_v1", "tx_episode_row_counts": dict(tx_counter)})
    save_json(indexes_dir / "global_index_summary.json", {"schema": "multi_scene_global_index_summary_v1", "row_count": len(rows), "scene_count": len(scene_rows), "scenes": scene_rows})
    return {"global_index": str(indexes_dir / "global_episode_index.jsonl"), "row_count": len(rows), "scene_count": len(scene_rows)}


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows
