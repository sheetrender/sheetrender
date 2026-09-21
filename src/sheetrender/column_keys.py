"""Turn spreadsheet headers into template variable names."""
from __future__ import annotations

import keyword
import re

# Jinja reads these as constants, so a column with one of these keys could
# never be referenced from a template.
_JINJA_CONSTANTS = {"true", "false", "none"}


def sanitize_column(name: object) -> str:
    """One header as a lowercase snake_case identifier, e.g. "Growth %" -> "growth_pct"."""
    value = "" if name is None else str(name)
    value = value.replace("%", " pct").replace("#", " num").replace("&", " and")
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "_", value)
    value = value.strip("_")
    if (
        not value
        or value[0].isdigit()
        or keyword.iskeyword(value)
        or value in _JINJA_CONSTANTS
    ):
        value = f"col_{value}"
    return value


def sanitize_columns(names: list[object]) -> list[str]:
    """Return one unique template key per header, in order.

    Repeats get a numeric suffix: "a", "a" becomes "a", "a_2". The suffix keeps
    counting past any key already taken, so a later header that is literally
    "a_2" cannot collide with it.
    """
    used: set[str] = set()
    result: list[str] = []
    for name in names:
        base = sanitize_column(name)
        key = base
        suffix = 2
        while key in used:
            key = f"{base}_{suffix}"
            suffix += 1
        used.add(key)
        result.append(key)
    return result
