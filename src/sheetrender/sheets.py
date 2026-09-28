from __future__ import annotations

import csv as _csv
import re
from collections.abc import Iterable, Iterator, Sequence
from datetime import date, time, timedelta
from functools import lru_cache
from typing import Any

from openpyxl import load_workbook

from sheetrender.column_keys import sanitize_columns
from sheetrender.templating import date_needs_confirmation, parse_date


class TooManyCellsError(ValueError):
    pass


DEFAULT_MAX_CELLS = 2_000_000
_SAMPLE_ROW_COUNT = 5

MAX_XLSX_CELL_CHARACTERS = 32_767


class CellTooLongError(ValueError):
    pass


def _too_many_cells_message(max_cells: int) -> str:
    return (
        f"This file has too many populated cells (max {max_cells:,}). "
        "Split it into smaller sheets and try again."
    )


# Only plain decimal notation becomes a number. Anything float() or int() would
# also accept stays text, because it changes what the cell says: "02134" is a
# zip code, "Nan" and "Infinity" are names, "1_000" and "1e3" are codes.
_CSV_INT_RE = re.compile(r"-?(0|[1-9][0-9]*)")
_CSV_FLOAT_RE = re.compile(r"-?(0|[1-9][0-9]*)\.[0-9]+")


def _convert_csv_cell(cell: str) -> int | float | str | None:
    cell = cell.strip()
    if cell == "":
        return None
    if _CSV_INT_RE.fullmatch(cell):
        return int(cell)
    if _CSV_FLOAT_RE.fullmatch(cell):
        return float(cell)
    return cell


def _fit_row(row: Sequence[Any], width: int) -> tuple[Any, ...]:
    """Trim or None-pad a row to exactly `width` values.

    Padding matters: templates run under StrictUndefined, so a short row that
    left its trailing keys out of the row dict would fail to render.
    """
    return tuple(row[:width]) + (None,) * (width - len(row))


def _is_empty_row(values: tuple[Any, ...]) -> bool:
    return all(v is None for v in values)


def _json_cell(value: Any) -> Any:
    # Time-of-day (09:30) and duration ([h]:mm) cells come out of openpyxl as
    # time and timedelta; left alone they make sample_rows unserialisable.
    # The text matches what a template prints for the native value.
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, timedelta):
        return str(value)
    return value


def _json_row(keys: list[str], values: tuple[Any, ...]) -> dict[str, Any]:
    # Only preview samples cross the JSON boundary; iter_rows preserves cells.
    return {key: _json_cell(value) for key, value in zip(keys, values, strict=True)}


# Zero-padding number formats: Excel's "Zip Code" (00000), "Zip Code + 4"
# (00000-0000), "Social Security Number" (000-00-0000) and fixed-width IDs.
# The cell stores 2134 and Excel shows 02134. Only formats made of zero
# placeholders in groups split by one literal dash or space count; a quoted or
# escaped "0" is a literal digit (0"0" shows 5 as "50"), not padding. The
# format comes from the uploaded file, so its size is capped before any work:
# a 100,000-zero format would otherwise turn every small integer into a
# 100 KB string. Two placeholders ("00" on a day or a quantity) are not an
# ID format; three to twenty are.
_MAX_PAD_FORMAT_CHARS = 64
_MIN_PAD_PLACEHOLDERS = 3
_MAX_PAD_PLACEHOLDERS = 20
_PAD_SEPARATORS = frozenset("- ")


@lru_cache(maxsize=256)
def _parse_pad_format(number_format: str) -> tuple[tuple[int, ...], tuple[str, ...]] | None:
    """(placeholder group sizes, separators) for a zero-padding format, else None."""
    tokens: list[str | None] = []  # None is a placeholder, a str a literal
    index = 0
    while index < len(number_format):
        char = number_format[index]
        if char == "0":
            tokens.append(None)
        elif char == "\\" and index + 1 < len(number_format):
            index += 1
            tokens.append(number_format[index])
        elif char == '"':
            close = number_format.find('"', index + 1)
            if close < 0:
                return None
            tokens.extend(number_format[index + 1:close])
            index = close
        elif char in _PAD_SEPARATORS:
            tokens.append(char)
        else:
            return None
        index += 1
    groups: list[int] = []
    separators: list[str] = []
    previous_literal = True  # a format may not open with a literal
    for token in tokens:
        if token is None:
            if previous_literal:
                groups.append(0)
            groups[-1] += 1
            previous_literal = False
        elif token in _PAD_SEPARATORS and not previous_literal:
            separators.append(token)
            previous_literal = True
        else:
            return None
    if previous_literal or not _MIN_PAD_PLACEHOLDERS <= sum(groups) <= _MAX_PAD_PLACEHOLDERS:
        return None
    return tuple(groups), tuple(separators)


def _pad_layout(number_format: Any) -> tuple[tuple[int, ...], tuple[str, ...]] | None:
    if (
        not isinstance(number_format, str)
        or len(number_format) > _MAX_PAD_FORMAT_CHARS
        or "0" not in number_format
    ):
        return None
    return _parse_pad_format(number_format)


def _zero_pad_text(value: int, layout: tuple[tuple[int, ...], tuple[str, ...]]) -> str:
    """The text Excel shows for an integer under a zero-padding format."""
    groups, separators = layout
    width = sum(groups)
    digits = str(abs(value)).zfill(width)
    # Digits beyond the format's width widen the first group, as Excel does.
    sizes = [groups[0] + len(digits) - width, *groups[1:]]
    parts, start = [], 0
    for size in sizes:
        parts.append(digits[start:start + size])
        start += size
    text = parts[0] + "".join(sep + part for sep, part in zip(separators, parts[1:], strict=True))
    return f"-{text}" if value < 0 else text


class _Padded(int):
    """An integer cell under a zero-padding format, carrying the text Excel shows.

    Only parse_xlsx sees these: the column becomes text if any of its cells
    shows something other than the bare number, and stays numeric otherwise.
    """

    def __new__(cls, value: int, text: str) -> _Padded:
        padded = super().__new__(cls, value)
        padded.text = text
        padded.shows_padding = text != str(value)
        return padded


def _padded_cell(cell: Any) -> Any:
    value = cell.value
    if type(value) is int:
        layout = _pad_layout(getattr(cell, "number_format", None))
        if layout is not None:
            return _Padded(value, _zero_pad_text(value, layout))
    return value


def _padded_text(cell: Any) -> Any:
    value = _padded_cell(cell)
    return value.text if type(value) is _Padded else value


def _visible_sheet_index(wb: Any) -> int:
    """The first sheet the workbook's owner can see.

    A hidden sheet (a lookup table, a macro's scratch space) is never the data
    the user means, and they cannot see what was imported from it. Only when
    every sheet is hidden does the first one stand in.
    """
    for index, ws in enumerate(wb.worksheets):
        if getattr(ws, "sheet_state", "visible") == "visible":
            return index
    return 0


def _xlsx_data_rows(rows: Iterable[Sequence[Any]], width: int) -> Iterator[tuple[Any, ...]]:
    for row in rows:
        values = _fit_row(row, width)
        if not _is_empty_row(values):
            yield values


def _csv_data_rows(reader: Iterable[list[str]], width: int) -> Iterator[tuple[Any, ...]]:
    for row in reader:
        values = _fit_row([_convert_csv_cell(cell) for cell in row], width)
        if not _is_empty_row(values):
            yield values


def _summarize(
    sheet_name: str,
    originals: list[str],
    data_rows: Iterator[tuple[Any, ...]],
    max_cells: int,
) -> dict[str, Any]:
    """Build the dataset description shared by parse_xlsx and parse_csv.

    `data_rows` yields non-empty rows already fitted to len(originals). An
    .xlsx's integer cells under a zero-padding format arrive as _Padded: their
    column is read as the text Excel shows if any of them shows padding
    (02134), and as numbers otherwise. The result's "zero_padded_columns"
    lists those columns for read_options.
    """
    keys = sanitize_columns(originals)
    # Counts populated cells, header included, so sparse files aren't penalized
    # for their width.
    populated_cells = sum(1 for original in originals if original)
    if populated_cells > max_cells:
        raise TooManyCellsError(_too_many_cells_message(max_cells))

    # Infer within the same bounded window as the app's compatibility shim.
    inferred_types = ["string"] * len(keys)
    pending = set(range(len(keys)))
    candidates: set[int] = set()
    # Ambiguous forms (1.2.2024) are also version numbers, so a column typed
    # from them stays under watch: any later non-date cell makes it a string.
    watched: set[int] = set()
    sample_values: list[tuple[Any, ...]] = []
    padded: set[int] = set()
    row_count = 0
    # Columns past the last named header survive only if some row fills them:
    # an .xlsx formatted a few columns wider than its data, or a CSV with
    # trailing commas, otherwise arrives with a run of nameless empty columns.
    named = [index for index, original in enumerate(originals) if original.strip()]
    width = len(originals)
    tail_start = named[-1] + 1 if named else width
    used_tail = tail_start
    for values in data_rows:
        row_count += 1
        populated_cells += sum(1 for v in values if v is not None)
        padded.update(
            index for index, v in enumerate(values) if type(v) is _Padded and v.shows_padding
        )
        for index in range(width - 1, used_tail - 1, -1):
            if values[index] is not None:
                used_tail = index + 1
                break
        if populated_cells > max_cells:
            raise TooManyCellsError(_too_many_cells_message(max_cells))
        if row_count <= 5000:
            for index in list(pending):
                value = values[index]
                if value is None or (isinstance(value, str) and not value.strip()):
                    continue
                if parse_date(value, excel_serial=False) is not None:
                    if index in watched:
                        continue
                    if index in candidates:
                        inferred_types[index] = "date"
                        watched.add(index)
                        continue
                    if date_needs_confirmation(value):
                        candidates.add(index)
                        continue
                    inferred_types[index] = "date"
                elif index in watched:
                    inferred_types[index] = "string"
                elif index not in candidates and isinstance(value, (int, float)) and not isinstance(value, bool):
                    inferred_types[index] = "number"
                pending.remove(index)
        if len(sample_values) < _SAMPLE_ROW_COUNT:
            sample_values.append(values)

    for index in padded:
        inferred_types[index] = "string"
    sample_rows = [
        _json_row(keys, tuple(_settle_padded(index, value, padded) for index, value in enumerate(values)))
        for values in sample_values
    ]
    if used_tail < width:
        # iter_rows fits rows to the stored columns, so dropping them here is
        # enough for the render pass to ignore them too.
        originals, keys, inferred_types = (
            originals[:used_tail], keys[:used_tail], inferred_types[:used_tail]
        )
        sample_rows = [dict(list(row.items())[:used_tail]) for row in sample_rows]
    columns = [
        {"original": original, "key": key, "inferred_type": inferred_type}
        for original, key, inferred_type in zip(originals, keys, inferred_types, strict=True)
    ]
    return {
        "sheet_name": sheet_name,
        "columns": columns,
        "row_count": row_count,
        "sample_rows": sample_rows,
        "zero_padded_columns": sorted(padded),
    }


def _settle_padded(index: int, value: Any, padded: set[int]) -> Any:
    if type(value) is not _Padded:
        return value
    return value.text if index in padded else int(value)


# read_options, as parse_* returns them, describe how the file was read at
# ingest: {"version": 2, "sheet_index": 1, "header_row": 0,
# "zero_padded_columns": [3]} for an .xlsx, {"version": 2, "header_row": 0}
# for a CSV. A caller that stores columns stores these next to them and hands
# them back to iter_rows, so every later read agrees with the ingest. Columns
# stored without them (every dataset parsed by 0.4.0 or earlier) are read the
# way 0.4.0 read them: the first sheet, hidden or not, the first row as the
# header, and every cell as its stored value.
#
# A caller that lets the user choose the tab or the header row passes the
# choice to parse_* (sheet_index=, header_row=) and stores the read_options
# that come back; nothing else needs to know about the choice.
READ_OPTIONS_VERSION = 2
_READ_OPTION_KEYS = frozenset({"version", "sheet_index", "header_row", "zero_padded_columns"})


def _choice(value: Any, name: str) -> int:
    if type(value) is not int or value < 0:
        raise ValueError(f"{name} must be a non-negative integer, not {value!r}")
    return value


def _skip(rows: Iterator[Any], count: int) -> None:
    for _ in range(count):
        if next(rows, None) is None:
            return


def parse_xlsx(
    path: str,
    max_cells: int | None = None,
    *,
    sheet_index: int | None = None,
    header_row: int = 0,
) -> dict[str, Any]:
    """Describe one sheet of an .xlsx.

    The sheet is `sheet_index` (0-based, hidden sheets included), or by
    default the first visible one. The header is row `header_row` (0-based,
    default the first row); rows above it are ignored. Returns sheet_name,
    columns, row_count, sample_rows and read_options.
    """
    if max_cells is None:
        max_cells = DEFAULT_MAX_CELLS
    header_row = _choice(header_row, "header_row")
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_index is None:
            sheet_index = _visible_sheet_index(wb)
        elif _choice(sheet_index, "sheet_index") >= len(wb.worksheets):
            raise ValueError(
                f"sheet_index {sheet_index} is past this workbook's {len(wb.worksheets)} sheet(s)"
            )
        ws = wb.worksheets[sheet_index]
        rows = ws.iter_rows()
        _skip(rows, header_row)
        header_cells = next(rows, None)
        if header_cells is None:
            raise ValueError("Workbook has no header row")
        originals = ["" if cell.value is None else str(cell.value) for cell in header_cells]
        data_rows = (tuple(_padded_cell(cell) for cell in row) for row in rows)
        result = _summarize(ws.title, originals, _xlsx_data_rows(data_rows, len(originals)), max_cells)
    finally:
        wb.close()
    result["read_options"] = {
        "version": READ_OPTIONS_VERSION,
        "sheet_index": sheet_index,
        "header_row": header_row,
        "zero_padded_columns": result.pop("zero_padded_columns"),
    }
    return result


def parse_csv(path: str, max_cells: int | None = None, *, header_row: int = 0) -> dict[str, Any]:
    """Describe a UTF-8, comma-separated CSV.

    The header is row `header_row` (0-based, default the first row); rows
    above it are ignored. Returns sheet_name ("csv"), columns, row_count,
    sample_rows and read_options.
    """
    if max_cells is None:
        max_cells = DEFAULT_MAX_CELLS
    header_row = _choice(header_row, "header_row")
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.reader(f)
        _skip(reader, header_row)
        header = next(reader, None)
        if header is None:
            raise ValueError("CSV has no header row")
        originals = [h.strip() for h in header]
        result = _summarize("csv", originals, _csv_data_rows(reader, len(originals)), max_cells)
    del result["zero_padded_columns"]
    result["read_options"] = {"version": READ_OPTIONS_VERSION, "header_row": header_row}
    return result


def _checked_index(read_options: dict[str, Any], name: str) -> int:
    return _choice(read_options.get(name, 0), f"read_options {name}")


def _checked_read_options(read_options: Any) -> tuple[int, int, frozenset[int]]:
    """(sheet_index, header_row, zero_padded_columns), or ValueError.

    Options this version cannot honour fail loudly: reading some other tab or
    row instead would put the wrong data under the stored column names.
    """
    if not isinstance(read_options, dict) or read_options.get("version") != READ_OPTIONS_VERSION:
        raise ValueError(
            f"Unsupported read_options {read_options!r}; this sheetrender reads "
            f"version {READ_OPTIONS_VERSION}"
        )
    unknown = set(read_options) - _READ_OPTION_KEYS
    if unknown:
        raise ValueError(f"read_options has keys this sheetrender cannot honour: {sorted(unknown)}")
    padded = read_options.get("zero_padded_columns", [])
    if not isinstance(padded, list) or any(type(i) is not int or i < 0 for i in padded):
        raise ValueError(f"read_options zero_padded_columns must list column indexes, not {padded!r}")
    return (
        _checked_index(read_options, "sheet_index"),
        _checked_index(read_options, "header_row"),
        frozenset(padded),
    )


def _iter_rows_csv(path: str, keys: list[str], header_row: int) -> Iterator[dict[str, Any]]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.reader(f)
        _skip(reader, header_row + 1)
        for values in _csv_data_rows(reader, len(keys)):
            yield dict(zip(keys, values, strict=True))


def _iter_rows_xlsx_0_4(path: str, keys: list[str]) -> Iterator[dict[str, Any]]:
    """0.4.0's reader, unchanged, for columns stored without read_options."""
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        rows = wb.worksheets[0].iter_rows(values_only=True)
        next(rows, None)
        for values in _xlsx_data_rows(rows, len(keys)):
            yield dict(zip(keys, values, strict=True))
    finally:
        wb.close()


def _iter_rows_xlsx(
    path: str, keys: list[str], sheet_index: int, header_row: int, padded: frozenset[int]
) -> Iterator[dict[str, Any]]:
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        if sheet_index >= len(wb.worksheets):
            raise ValueError(
                f"read_options sheet_index {sheet_index} is past this workbook's "
                f"{len(wb.worksheets)} sheet(s)"
            )
        ws = wb.worksheets[sheet_index]
        if padded:
            # Cells, not bare values, only when a column needs its format.
            rows: Iterator[tuple[Any, ...]] = (
                tuple(
                    _padded_text(cell) if index in padded else cell.value
                    for index, cell in enumerate(row)
                )
                for row in ws.iter_rows()
            )
        else:
            rows = ws.iter_rows(values_only=True)
        _skip(rows, header_row + 1)
        for values in _xlsx_data_rows(rows, len(keys)):
            yield dict(zip(keys, values, strict=True))
    finally:
        wb.close()


def iter_rows(
    path: str,
    columns: list[dict[str, Any]],
    read_options: dict[str, Any] | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield each non-empty data row as a dict keyed by the columns' template keys.

    Pass the read_options parse_* returned alongside these columns. Without
    them the file is read as sheetrender 0.4.0 read it, which is right for
    columns stored before read_options existed and wrong for newer ones
    whenever the workbook's first tab is hidden or it uses zero-padded formats.
    """
    keys = [c["key"] for c in columns]
    is_csv = path.lower().endswith(".csv")
    if read_options is None:
        return _iter_rows_csv(path, keys, 0) if is_csv else _iter_rows_xlsx_0_4(path, keys)
    sheet_index, header_row, padded = _checked_read_options(read_options)
    if is_csv:
        return _iter_rows_csv(path, keys, header_row)
    return _iter_rows_xlsx(path, keys, sheet_index, header_row, padded)
