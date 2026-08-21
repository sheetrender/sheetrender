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
