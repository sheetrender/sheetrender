from __future__ import annotations

import os
import re

from sheetrender.templating import render_text

_INVALID_CHARS_RE = re.compile(r"[/\\<>:\"|?*\x00-\x1f\x7f-\x9f]")
_WHITESPACE_RE = re.compile(r"\s+")
# Windows refuses to create files with these stems regardless of extension.
_WINDOWS_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {
    f"LPT{i}" for i in range(1, 10)
}


def _fallback(row_index: int) -> str:
    return f"row_{row_index + 1}.pdf"


def render_filename(
    template_str: str | None,
    row: dict,
    row_index: int,
    *,
    grouped: bool = False,
) -> str:
    fallback = f"group_{row_index + 1}.pdf" if grouped else _fallback(row_index)
    if grouped and row.get("group_key") is not None:
        group_key = str(row["group_key"]).strip()
        if group_key:
            fallback = _sanitize_filename(group_key, fallback)
    if not template_str:
        return fallback
    try:
        rendered = render_text(template_str, row)
    except Exception:
        return fallback
    if not rendered.strip():
        return fallback

    return _sanitize_filename(rendered, fallback)


def _sanitize_filename(rendered: str, fallback: str) -> str:
    sanitized = _INVALID_CHARS_RE.sub("", rendered)
    sanitized = _WHITESPACE_RE.sub(" ", sanitized).strip(" .")
    if not sanitized:
        return fallback

    if sanitized.lower().endswith(".pdf"):
        stem = sanitized[:-4].rstrip(" .")
        extension = sanitized[-4:]
    else:
        stem = sanitized
        extension = ".pdf"
    stem = stem[:150].rstrip(" .")
    if not stem or stem.upper() in _WINDOWS_RESERVED:
        return fallback
    return f"{stem}{extension}"


def dedupe_filenames(names: list[str]) -> list[str]:
    used: set[str] = set()
    result: list[str] = []
    for name in names:
        candidate = name
        stem, extension = os.path.splitext(name)
        suffix = 2
        while candidate in used:
            candidate = f"{stem} ({suffix}){extension}"
            suffix += 1
        used.add(candidate)
        result.append(candidate)
    return result
