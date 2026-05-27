from __future__ import annotations

from pathlib import Path

from dynamic_radio_dataset.indexing.episode_index import deterministic_split, summarize_episode_for_index
from dynamic_radio_dataset.json_utils import load_json, save_json, write_jsonl


def finalize_index(dataset_root: Path, split_seed: int) -> int:
    episodes_dir = dataset_root / "episodes"
    rows = []
    accepted_episode_ids = []
    for episode_dir in sorted(episodes_dir.glob("episode_*")):
        qa_path = episode_dir / "qa_report.json"
        meta_path = episode_dir / "episode_meta.json"
        if not qa_path.exists() or not meta_path.exists():
            continue
        qa_report = load_json(qa_path)
        episode_meta = load_json(meta_path)
        tx_reports = qa_report.get("tx_reports", [])
        frame_count = int(episode_meta.get("frame_count", 0))
        episode_rows = summarize_episode_for_index(episode_dir.name, episode_meta, qa_report["scene_qa"], tx_reports, frame_count)
        rows.extend(row for row in episode_rows if bool(row.get("accepted")))
        if qa_report.get("accepted_tx_ids"):
            accepted_episode_ids.append(episode_dir.name)
    write_jsonl(dataset_root / "episode_index.jsonl", rows)
    save_json(dataset_root / "splits.json", deterministic_split(sorted(set(accepted_episode_ids)), seed=split_seed))
    print(f"[OK] Wrote dataset index: {dataset_root / 'episode_index.jsonl'}")
    print(f"[OK] Wrote splits: {dataset_root / 'splits.json'}")
    return 0
