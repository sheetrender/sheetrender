"""What survived extraction from the backend's group-detection API tests.

The source file drove a FastAPI route: ownership checks, rate limiting, DB
session mocks, upload replacement clearing stale config. None of that is in
this package. The one thing under the route that is the package's own is the
shape of what detect_group_candidates hands back — the route only renames
nothing and passes it through — so the response-shape assertion is kept here,
against the function directly.
"""
from __future__ import annotations

from pathlib import Path

from sheetrender.grouping import detect_group_candidates
from sheetrender.sheets import parse_csv


def _csv(tmp_path: Path, content: str) -> tuple[str, list[dict]]:
    path = tmp_path / "invoices.csv"
    path.write_text(content, encoding="utf-8")
    return str(path), parse_csv(str(path))["columns"]


def test_detect_candidate_response_shape(tmp_path):
    path, columns = _csv(
        tmp_path, "Invoice Number,Item\nA,one\nA,two\nB,three\nB,four\n"
    )

    result = detect_group_candidates(path, columns, 4)

    assert result["row_count"] == 4
    assert result["recommended"] == "invoice_number"
    candidate = result["candidates"][0]
    assert set(candidate) == {
        "column",
        "original",
        "n_groups",
        "min_group_size",
        "max_group_size",
        "avg_group_size",
        "name_hint",
    }
    assert candidate == {
        "column": "invoice_number",
        "original": "Invoice Number",
        "n_groups": 2,
        "min_group_size": 2,
        "max_group_size": 2,
        "avg_group_size": 2.0,
        "name_hint": True,
    }
