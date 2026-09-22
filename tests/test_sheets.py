from pathlib import Path

from sheetrender.sheets import parse_xlsx

# The backend suite read this workbook from the repo's examples/ directory. It
# travels with the tests here instead, so the package's suite is self-contained.
XLSX = Path(__file__).parent / "data" / "performance-reports.xlsx"


def test_parse_xlsx():
    result = parse_xlsx(str(XLSX))
    assert len(result["columns"]) == 16
    keys = [c["key"] for c in result["columns"]]
    assert "employee_name" in keys
    assert "performance_pct" in keys
    assert "bonus_eligible" in keys
    assert 90 <= result["row_count"] <= 110
    assert isinstance(result["sample_rows"][0]["sales_target"], (int, float))


def test_xlsx_dates_are_detected_and_json_serializable(tmp_path):
    import json
    from datetime import date, datetime

    from openpyxl import Workbook

    from sheetrender.sheets import iter_rows

    path = tmp_path / "dates.xlsx"
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(["Native", "Timestamp", "ISO", "Amount", "Serial", "Boolean"])
    sheet.append([date(2026, 9, 22), datetime(2026, 9, 22, 14, 30), "2026-09-22", 12.5, 46287, True])
    workbook.save(path)
    result = parse_xlsx(str(path))
    assert [column["inferred_type"] for column in result["columns"]] == [
        "date", "date", "date", "number", "number", "string",
    ]
    rows = list(iter_rows(str(path), result["columns"]))
    assert result["sample_rows"] == rows
    assert rows[0]["native"].startswith("2026-09-22")
    assert rows[0]["timestamp"] == "2026-09-22T14:30:00"
    assert rows[0]["iso"] == "2026-09-22"
    json.dumps(result)


def test_csv_dates_use_first_populated_cell_including_after_sample_window(tmp_path):
    from sheetrender.sheets import iter_rows, parse_csv

    path = tmp_path / "dates.csv"
    path.write_text(
        "ISO,US,European,Late,Code,Number,Mixed,Invalid\n"
        "2026-09-22,09/22/2026,22.09.2026,,02134,46287,hello,2026-02-30\n"
        + "2026-09-23,,,,,,,,\n" * 5
        + "2026-09-24,,,2026-09-25,46287,7,2026-09-22,none\n"
    )
    result = parse_csv(str(path))
    by_key = {column["key"]: column["inferred_type"] for column in result["columns"]}
    assert by_key == {
        "iso": "date", "us": "date", "european": "date", "late": "date",
        "code": "string", "number": "number", "mixed": "string", "invalid": "string",
    }
    assert result["row_count"] == 7
    first = next(iter_rows(str(path), result["columns"]))
    assert first["us"] == "09/22/2026"
    assert first["code"] == "02134"
    assert first["number"] == 46287


def test_date_ingest_keeps_the_cell_cap(tmp_path):
    import pytest

    from sheetrender.sheets import TooManyCellsError, parse_csv

    path = tmp_path / "dates.csv"
    path.write_text("Issued\n2026-09-22\n2026-09-23\n")
    with pytest.raises(TooManyCellsError):
        parse_csv(str(path), max_cells=2)
