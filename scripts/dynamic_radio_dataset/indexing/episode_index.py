from __future__ import annotations

import random
from typing import Any, Dict, Sequence


JsonDict = Dict[str, Any]


def deterministic_split(episode_ids: Sequence[str], seed: int = 7) -> JsonDict:
    ids = sorted(episode_ids)
    rng = random.Random(seed)
    rng.shuffle(ids)
    n = len(ids)
    train_end = int(round(n * 0.70))
    val_end = int(round(n * 0.85))
    return {
        "seed": int(seed),
        "train": ids[:train_end],
        "val": ids[train_end:val_end],
        "test": ids[val_end:],
    }


def summarize_episode_for_index(
    episode_id: str,
    episode_meta: JsonDict,
    qa_report: JsonDict,
    tx_reports: Sequence[JsonDict],
    frame_count: int,
) -> list[JsonDict]:
    accepted_tx_ids = {str(report["tx_id"]) for report in tx_reports if bool(report.get("pair_qc_pass"))}
    tx_ids = [str(item) for item in episode_meta.get("tx_ids", [])] if isinstance(episode_meta.get("tx_ids"), list) else []
    tx_candidate_ids = [str(item) for item in episode_meta.get("tx_candidate_ids", [])] if isinstance(episode_meta.get("tx_candidate_ids"), list) else []
    candidate_by_tx = {tx_id: tx_candidate_ids[index] for index, tx_id in enumerate(tx_ids) if index < len(tx_candidate_ids)}
    rows = []
    for report in tx_reports:
        tx_id = str(report["tx_id"])
        rows.append(
            {
                "episode_id": episode_id,
                "scene_id": episode_meta.get("scene_id"),
                "tx_id": tx_id,
                "tx_candidate_id": candidate_by_tx.get(tx_id, tx_id),
                "accepted": bool(report.get("pair_qc_pass")) and bool(qa_report.get("scene_qc_pass")),
                "scene_qc_pass": bool(qa_report.get("scene_qc_pass")),
                "tx_qc_pass": bool(report.get("pair_qc_pass")),
                "accepted_tx_ids": sorted(accepted_tx_ids),
                "frame_count": int(frame_count),
                "fps": float(episode_meta.get("fps", 0.0)),
                "town": episode_meta.get("town"),
                "scene_mode": episode_meta.get("scene_mode"),
                "scene_type": episode_meta.get("scene_type"),
                "actual_total_vehicle_count": int(episode_meta.get("actual_total_vehicle_count", 0)),
                "actual_background_count": int(episode_meta.get("actual_background_count", 0)),
                "actual_required_controlled_count": int(episode_meta.get("actual_required_controlled_count", 0)),
            }
        )
    return rows
