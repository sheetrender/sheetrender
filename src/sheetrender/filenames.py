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

_MAX_STEM_CHARS = 150
# Most filesystems cap a name at 255 bytes, not characters. 200 leaves room for
# the extension and a dedupe suffix such as " (12)".
_MAX_STEM_BYTES = 200


def _truncate_stem(stem: str) -> str:
    stem = stem[:_MAX_STEM_CHARS]
    encoded = stem.encode("utf-8")
    if len(encoded) > _MAX_STEM_BYTES:
        # errors="ignore" drops a multi-byte character cut in half by the slice.
        stem = encoded[:_MAX_STEM_BYTES].decode("utf-8", errors="ignore")
    return stem.rstrip(" .")


def _fallback(row_index: int) -> str:
    return f"row_{row_index + 1}.pdf"


def render_filename(
    template_str: str | None,
    row: dict,
    row_index: int,
    *,
    grouped: bool = False,
    day_first: bool = False,
) -> str:
    fallback = f"group_{row_index + 1}.pdf" if grouped else _fallback(row_index)
    if grouped and row.get("group_key") is not None:
        group_key = str(row["group_key"]).strip()
        if group_key:
            fallback = _sanitize_filename(group_key, fallback)
    if not template_str:
        return fallback
    try:
        rendered = render_text(template_str, row, day_first=day_first)
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
    stem = _truncate_stem(stem)
    # Windows reserves the device name whatever follows the first dot: CON.txt.pdf
    # is as unusable as CON.pdf.
    if not stem or stem.split(".")[0].strip().upper() in _WINDOWS_RESERVED:
        return fallback
    return f"{stem}{extension}"


def dedupe_filenames(names: list[str]) -> list[str]:
    """Make every name unique by appending " (2)", " (3)", ... before the extension.

    Names are compared case-insensitively: macOS and Windows would otherwise
    write "Acme.pdf" and "ACME.pdf" to the same file.
    """
    used: set[str] = set()
    result: list[str] = []
    for name in names:
        candidate = name
        stem, extension = os.path.splitext(name)
        suffix = 2
        while candidate.casefold() in used:
            candidate = f"{stem} ({suffix}){extension}"
            suffix += 1
        used.add(candidate.casefold())
        result.append(candidate)
    return result
