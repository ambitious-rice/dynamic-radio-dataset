from __future__ import annotations

from collections import Counter
from typing import Any, Iterable


def histogram(rows: Iterable[dict[str, Any]], key: str) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for row in rows:
        value = row.get(key)
        if value is not None:
            counter[str(value)] += 1
    return sorted_counter(counter)


def failure_code_histogram(*groups: Iterable[dict[str, Any]]) -> dict[str, int]:
    counter: Counter[str] = Counter()
    for group in groups:
        for row in group:
            code = row.get("failure_code")
            if code:
                counter[str(code)] += 1
    return sorted_counter(counter)


def sorted_counter(counter: Counter[str]) -> dict[str, int]:
    return {key: int(counter[key]) for key in sorted(counter)}
