"""Utility helpers for the standalone offline QA audit."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator

from schemas import UNAVAILABLE


JsonDict = Dict[str, Any]


def load_json(path: Path, warnings: list[JsonDict] | None = None, *, required: bool = False) -> JsonDict | None:
    path = Path(path)
    if not path.exists():
        if required and warnings is not None:
            warnings.append({"path": str(path), "issue": "missing_file"})
        return None
    try:
        with path.open("r", encoding="utf-8") as f:
            value = json.load(f)
    except Exception as exc:  # noqa: BLE001 - audit must continue over corrupt residue
        if warnings is not None:
            warnings.append({"path": str(path), "issue": "json_read_failed", "error": str(exc)})
        return None
    if not isinstance(value, dict):
        if warnings is not None:
            warnings.append({"path": str(path), "issue": "json_not_object"})
        return None
    return value


def iter_jsonl(path: Path, warnings: list[JsonDict] | None = None) -> Iterator[JsonDict]:
    path = Path(path)
    if not path.exists():
        if warnings is not None:
            warnings.append({"path": str(path), "issue": "missing_file"})
        return
    try:
        with path.open("r", encoding="utf-8") as f:
            for line_number, line in enumerate(f, start=1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    if warnings is not None:
                        warnings.append(
                            {
                                "path": str(path),
                                "line_number": line_number,
                                "issue": "jsonl_decode_failed",
                                "error": str(exc),
                            }
                        )
                    continue
                if isinstance(value, dict):
                    yield value
                elif warnings is not None:
                    warnings.append({"path": str(path), "line_number": line_number, "issue": "jsonl_row_not_object"})
    except Exception as exc:  # noqa: BLE001
        if warnings is not None:
            warnings.append({"path": str(path), "issue": "jsonl_read_failed", "error": str(exc)})


def write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(to_jsonable(value), f, indent=2, ensure_ascii=False, sort_keys=True)
        f.write("\n")


def write_csv(path: Path, rows: Iterable[JsonDict], headers: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=headers, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(row.get(key)) for key in headers})


def csv_value(value: Any) -> Any:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True)
    return value


def to_jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(k): to_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [to_jsonable(v) for v in value]
    if isinstance(value, float):
        if math.isnan(value) or math.isinf(value):
            return None
    return value


def compact_json(value: Any) -> str:
    return json.dumps(to_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def safe_float(value: Any, default: float | None = None) -> float | None:
    try:
        if value is None or value == UNAVAILABLE:
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(result) or math.isinf(result):
        return default
    return result


def safe_int(value: Any, default: int | None = None) -> int | None:
    try:
        if value is None or value == UNAVAILABLE:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def first_present(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def nested_get(row: JsonDict | None, path: Iterable[str], default: Any = None) -> Any:
    current: Any = row
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def percentile(values: Iterable[Any], q: float) -> float | str:
    numeric = sorted(v for v in (safe_float(value) for value in values) if v is not None)
    if not numeric:
        return UNAVAILABLE
    if len(numeric) == 1:
        return float(numeric[0])
    q = max(0.0, min(100.0, float(q)))
    pos = (len(numeric) - 1) * q / 100.0
    lower = int(math.floor(pos))
    upper = int(math.ceil(pos))
    if lower == upper:
        return float(numeric[lower])
    weight = pos - lower
    return float(numeric[lower] * (1.0 - weight) + numeric[upper] * weight)


def mean(values: Iterable[Any]) -> float | str:
    numeric = [v for v in (safe_float(value) for value in values) if v is not None]
    if not numeric:
        return UNAVAILABLE
    return float(sum(numeric) / len(numeric))


def max_value(values: Iterable[Any]) -> float | str:
    numeric = [v for v in (safe_float(value) for value in values) if v is not None]
    if not numeric:
        return UNAVAILABLE
    return float(max(numeric))


def min_value(values: Iterable[Any]) -> float | str:
    numeric = [v for v in (safe_float(value) for value in values) if v is not None]
    if not numeric:
        return UNAVAILABLE
    return float(min(numeric))


def summarize_numeric(values: Iterable[Any]) -> JsonDict:
    numeric = [v for v in (safe_float(value) for value in values) if v is not None]
    if not numeric:
        return {"count": 0, "mean": UNAVAILABLE, "p50": UNAVAILABLE, "p95": UNAVAILABLE, "max": UNAVAILABLE}
    return {
        "count": int(len(numeric)),
        "mean": float(sum(numeric) / len(numeric)),
        "p50": percentile(numeric, 50),
        "p95": percentile(numeric, 95),
        "max": float(max(numeric)),
    }


def sorted_counter(counter: Counter[Any]) -> dict[str, int]:
    return {str(k): int(v) for k, v in sorted(counter.items(), key=lambda item: (-item[1], str(item[0])))}


def histogram(rows: Iterable[JsonDict], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if value is not None and value != "":
            counter[str(value)] += 1
    return sorted_counter(counter)


def top_dict_items(mapping: dict[str, int], limit: int = 10) -> list[JsonDict]:
    return [{"key": key, "count": int(value)} for key, value in list(mapping.items())[:limit]]


def as_semicolon_list(values: Iterable[Any]) -> str:
    return ";".join(str(value) for value in values if value is not None and str(value) != "")


def make_unique_output_dir(path: Path) -> Path:
    path = Path(path)
    if not path.exists():
        return path
    for index in range(1, 1000):
        candidate = path.with_name(f"{path.name}_{index:02d}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"Could not find unique output directory for {path}")
