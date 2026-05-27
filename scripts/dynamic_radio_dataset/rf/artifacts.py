from __future__ import annotations

from pathlib import Path
from typing import Any, Dict

import numpy as np

from dynamic_radio_dataset.json_utils import iter_jsonl


JsonDict = Dict[str, Any]


def load_motion_rows_by_frame(motion_path: Path) -> dict[int, JsonDict]:
    return {int(row["frame_index"]): row for row in iter_jsonl(motion_path)}


def load_frame_indices_from_rss(tx_dir: Path) -> list[int]:
    with np.load(tx_dir / "rss_maps.npz") as data:
        return [int(item) for item in data["frame_indices"].tolist()]
