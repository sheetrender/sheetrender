from __future__ import annotations

import keyword
import re


def _base_sanitize(name: object) -> str:
    value = "" if name is None else str(name)
    value = value.replace("%", " pct").replace("#", " num").replace("&", " and")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    if not value or value[0].isdigit() or keyword.iskeyword(value):
        value = f"col_{value}"
    return value


def sanitize_column(name: object) -> str:
    return _base_sanitize(name)


def sanitize_columns(names: list[object]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for name in names:
        base = _base_sanitize(name)
        counts[base] = counts.get(base, 0) + 1
        if counts[base] == 1:
            result.append(base)
        else:
            result.append(f"{base}_{counts[base]}")
    return result
