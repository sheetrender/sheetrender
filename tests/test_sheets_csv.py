import csv
import os
import tempfile

from sheetrender.sheets import iter_rows, parse_csv


def write_temp_csv(rows: list, headers: list) -> str:
    f = tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False, newline="")
    writer = csv.writer(f)
    writer.writerow(headers)
    writer.writerows(rows)
    f.close()
    return f.name


def test_columns_and_keys():
    path = write_temp_csv(
        [["Alice", "42000", "3.5"], ["Bob", "55000", "4.1"]],
        ["Employee Name", "Salary", "Score"],
    )
    try:
        result = parse_csv(path)
        assert result["sheet_name"] == "csv"
        cols = result["columns"]
        assert cols[0]["original"] == "Employee Name"
        assert cols[0]["key"] == "employee_name"
        assert cols[1]["original"] == "Salary"
        assert cols[1]["key"] == "salary"
        assert cols[2]["original"] == "Score"
        assert cols[2]["key"] == "score"
    finally:
        os.unlink(path)


def test_inferred_types():
    path = write_temp_csv(
        [["Alice", "42000", "hello"]],
        ["Name", "Amount", "Note"],
    )
    try:
        result = parse_csv(path)
        cols = result["columns"]
        assert cols[0]["inferred_type"] == "string"
        assert cols[1]["inferred_type"] == "number"
        assert cols[2]["inferred_type"] == "string"
    finally:
        os.unlink(path)


def test_row_count_and_sample():
    path = write_temp_csv(
        [["Alice", "1"], ["Bob", "2"], ["", ""]],
        ["Name", "Val"],
    )
    try:
        result = parse_csv(path)
        assert result["row_count"] == 2
        assert len(result["sample_rows"]) == 2
    finally:
        os.unlink(path)


def test_numeric_conversion_in_iter_rows():
    path = write_temp_csv(
        [["Alice", "42000", "3.5"]],
        ["Name", "Salary", "Score"],
    )
    try:
        result = parse_csv(path)
        rows = list(iter_rows(path, result["columns"]))
        assert rows[0]["salary"] == 42000
        assert isinstance(rows[0]["salary"], int)
        assert rows[0]["score"] == 3.5
        assert isinstance(rows[0]["score"], float)
        assert rows[0]["name"] == "Alice"
    finally:
        os.unlink(path)


def test_sample_rows_numeric():
    path = write_temp_csv(
        [["Alice", "42000"]],
        ["Name", "Salary"],
    )
    try:
        result = parse_csv(path)
        assert result["sample_rows"][0]["salary"] == 42000
    finally:
        os.unlink(path)
