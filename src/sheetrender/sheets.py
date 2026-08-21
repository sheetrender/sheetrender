from __future__ import annotations

import csv as _csv
from collections.abc import Iterator
from typing import Any

from openpyxl import load_workbook

from sheetrender.sanitize import sanitize_columns


class TooManyCellsError(ValueError):
    pass


DEFAULT_MAX_CELLS = 2_000_000

MAX_XLSX_CELL_CHARACTERS = 32_767


class CellTooLongError(ValueError):
    pass


def _too_many_cells_message(max_cells: int) -> str:
    return (
        f"This file has too many populated cells (max {max_cells:,}). "
        "Split it into smaller sheets and try again."
    )


def _is_empty_row(values: tuple[Any, ...]) -> bool:
    return all(v is None for v in values)


def _infer_type(values: list[Any]) -> str:
    for value in values:
        if value is None:
            continue
        return "number" if isinstance(value, int | float) and not isinstance(value, bool) else "string"
    return "string"


def parse_xlsx(path: str, max_cells: int | None = None) -> dict[str, Any]:
    if max_cells is None:
        max_cells = DEFAULT_MAX_CELLS
    wb = load_workbook(path, read_only=True, data_only=True)
    try:
        ws = wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header_row = next(rows, None)
        if header_row is None:
            raise ValueError("Workbook has no header row")

        populated_cells = sum(1 for h in header_row if h is not None)
        if populated_cells > max_cells:
            raise TooManyCellsError(_too_many_cells_message(max_cells))

        originals = ["" if h is None else str(h) for h in header_row]
        keys = sanitize_columns(originals)
        non_empty_by_column: list[list[Any]] = [[] for _ in keys]
        sample_rows: list[dict[str, Any]] = []
        row_count = 0
        # Populated cells, header included, so sparse files aren't penalized
        # for their width.

        for row in rows:
            values = tuple(row[: len(keys)])
            if _is_empty_row(values):
                continue
            row_count += 1
            populated_cells += sum(1 for v in values if v is not None)
            if populated_cells > max_cells:
                raise TooManyCellsError(_too_many_cells_message(max_cells))
            item = {key: value for key, value in zip(keys, values, strict=False)}
            for index, value in enumerate(values):
                if value is not None and index < len(non_empty_by_column):
                    non_empty_by_column[index].append(value)
            if len(sample_rows) < 5:
                sample_rows.append(item)

        columns = [
            {"original": original, "key": key, "inferred_type": _infer_type(non_empty_by_column[index])}
            for index, (original, key) in enumerate(zip(originals, keys, strict=False))
        ]
        return {
            "sheet_name": ws.title,
            "columns": columns,
            "row_count": row_count,
            "sample_rows": sample_rows,
        }
    finally:
        wb.close()


def parse_csv(path: str, max_cells: int | None = None) -> dict[str, Any]:
    if max_cells is None:
        max_cells = DEFAULT_MAX_CELLS
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.reader(f)
        header_row = next(reader, None)
        if header_row is None:
            raise ValueError("CSV has no header row")

        populated_cells = sum(1 for h in header_row if h.strip())
        if populated_cells > max_cells:
            raise TooManyCellsError(_too_many_cells_message(max_cells))

        originals = [h.strip() for h in header_row]
        keys = sanitize_columns(originals)
        non_empty_by_column: list[list[Any]] = [[] for _ in keys]
        sample_rows: list[dict[str, Any]] = []
        row_count = 0
        # Populated cells, header included, so sparse files aren't penalized
        # for their width.

        for row in reader:
            values = row[: len(keys)]
            if all(cell.strip() == "" for cell in values):
                continue
            row_count += 1
            populated_cells += sum(1 for cell in values if cell.strip())
            if populated_cells > max_cells:
                raise TooManyCellsError(_too_many_cells_message(max_cells))
            converted = []
            for cell in values:
                cell = cell.strip()
                if cell == "":
                    converted.append(None)
                else:
                    try:
                        converted.append(int(cell))
                    except ValueError:
                        try:
                            converted.append(float(cell))
                        except ValueError:
                            converted.append(cell)
            item = {key: val for key, val in zip(keys, converted, strict=False)}
            for index, val in enumerate(converted):
                if val is not None and index < len(non_empty_by_column):
                    non_empty_by_column[index].append(val)
            if len(sample_rows) < 5:
                sample_rows.append(item)

        columns = [
            {"original": original, "key": key, "inferred_type": _infer_type(non_empty_by_column[i])}
            for i, (original, key) in enumerate(zip(originals, keys, strict=False))
        ]
        return {"sheet_name": "csv", "columns": columns, "row_count": row_count, "sample_rows": sample_rows}


def _iter_rows_csv(path: str, columns: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    keys = [c["key"] for c in columns]
    with open(path, newline="", encoding="utf-8-sig") as f:
        reader = _csv.reader(f)
        next(reader, None)
        for row in reader:
            values = row[: len(keys)]
            if all(cell.strip() == "" for cell in values):
                continue
            converted = []
            for cell in values:
                cell = cell.strip()
                if cell == "":
                    converted.append(None)
                else:
                    try:
                        converted.append(int(cell))
                    except ValueError:
                        try:
                            converted.append(float(cell))
                        except ValueError:
                            converted.append(cell)
            yield {key: val for key, val in zip(keys, converted, strict=False)}


def iter_rows(path: str, columns: list[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    if path.lower().endswith(".csv"):
        yield from _iter_rows_csv(path, columns)
        return
    wb = load_workbook(path, read_only=True, data_only=True)
    ws = wb.worksheets[0]
    keys = [c["key"] for c in columns]
    try:
        rows = ws.iter_rows(values_only=True)
        next(rows, None)
        for row in rows:
            values = tuple(row[: len(keys)])
            if _is_empty_row(values):
                continue
            yield {key: value for key, value in zip(keys, values, strict=False)}
    finally:
        wb.close()
