from sheetrender.sanitize import sanitize_column


def test_employee_name():
    assert sanitize_column("Employee Name") == "employee_name"


def test_performance_pct():
    assert sanitize_column("Performance %") == "performance_pct"


def test_client_retention_pct():
    assert sanitize_column("Client Retention %") == "client_retention_pct"


def test_report_key():
    assert sanitize_column("Report Key") == "report_key"


def test_collision_dedupe():
    from sheetrender.sanitize import sanitize_columns

    cols = ["Foo Bar", "foo_bar"]
    result = sanitize_columns(cols)
    assert result[0] == "foo_bar"
    assert result[1] == "foo_bar_2"
