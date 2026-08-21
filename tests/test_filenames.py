from sheetrender.filenames import dedupe_filenames, render_filename


def test_render_filename_without_template_uses_row_number():
    assert render_filename(None, {}, 0) == "row_1.pdf"


def test_render_filename_plain_template():
    assert render_filename("Invoice", {}, 0) == "Invoice.pdf"


def test_render_filename_substitutes_columns():
    assert render_filename("Invoice {{ invoice_id }}", {"invoice_id": 42}, 0) == "Invoice 42.pdf"


def test_render_filename_missing_column_falls_back():
    assert render_filename("Invoice {{ missing }}", {}, 2) == "row_3.pdf"


def test_render_filename_sanitizes_separators_reserved_chars_and_whitespace():
    assert render_filename('  ACME/West\\Q1<>:"|?*\nReport  ', {}, 0) == "ACMEWestQ1Report.pdf"


def test_render_filename_does_not_html_escape_values():
    assert render_filename("{{ company }}", {"company": "Smith & Sons <VIP>"}, 0) == "Smith & Sons VIP.pdf"


def test_render_filename_windows_reserved_stem_falls_back():
    assert render_filename("{{ code }}", {"code": "CON"}, 4) == "row_5.pdf"
    assert render_filename("{{ code }}", {"code": "lpt1"}, 0) == "row_1.pdf"


def test_render_filename_appends_pdf_once_case_insensitively():
    assert render_filename("Report.PDF", {}, 0) == "Report.PDF"


def test_render_filename_caps_stem_at_150_characters():
    result = render_filename("x" * 200, {}, 0)
    assert result == f"{'x' * 150}.pdf"


def test_dedupe_filenames_preserves_order_and_appends_suffixes():
    assert dedupe_filenames(["Invoice.pdf", "Invoice.pdf", "Other.pdf", "Invoice.pdf"]) == [
        "Invoice.pdf",
        "Invoice (2).pdf",
        "Other.pdf",
        "Invoice (3).pdf",
    ]
