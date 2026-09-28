import base64
import re
from datetime import UTC, date, datetime
from xml.etree import ElementTree

import pytest
import segno

from sheetrender.templating import date as format_date
from sheetrender.templating import get_env, money, money2, money_k, qr, render_row


@pytest.mark.parametrize("value", [
    date(2026, 9, 22), datetime(2026, 9, 22, 14, tzinfo=UTC),
    "2026-09-22", "2026-09-22T14:00:00Z", "2026-09-22T14:00:00+02:00",
    "2026-09-22 14:00", "09/22/2026", "22/09/2026", "22.09.2026",
    46287, 46287.5,
])
def test_dates(value):
    assert format_date(value) == "Sep 22, 2026"
    assert format_date(value, "%B %-d, %Y") == "September 22, 2026"


@pytest.mark.parametrize("value", [None, "", " ", "nonsense", "2026-02-30", "46287", "20260922",
                                         0, -1, 73416, float("inf"), float("nan"), True, {}])
def test_invalid_dates(value):
    assert format_date(value) == ""


def test_date_ambiguity_and_excel_epoch():
    assert format_date("01/02/2026", "%Y-%m-%d") == "2026-01-02"
    assert format_date("01.02.2026", "%Y-%m-%d") == "2026-01-02"
    assert format_date(1, "%Y-%m-%d") == "1900-01-01"
    assert format_date(59, "%Y-%m-%d") == format_date(60, "%Y-%m-%d") == "1900-02-28"
    assert format_date(61, "%Y-%m-%d") == "1900-03-01"
    assert format_date(73415, "%Y-%m-%d") == "2100-12-31"


@pytest.mark.parametrize("fmt", [None, 5, "%1000000000Y", "x" * 129])
def test_date_format_bounds(fmt):
    assert format_date("2026-09-22", fmt) == ""


@pytest.mark.parametrize("currency,prefix", [
    ("USD", "$"), ("EUR", "€"), ("GBP", "£"), ("JPY", "¥"),
    ("CHF", "CHF "), ("CAD", "CAD "), ("AUD", "AUD "), ("XYZ", "XYZ "),
])
def test_currencies(currency, prefix):
    assert money(1234, currency) == prefix + "1,234"
    assert money2(-1234.56, currency) == "-" + prefix + ("1,235" if currency == "JPY" else "1,234.56")
    assert money_k(-1234, currency) == "-" + prefix + "1K"
    assert money(-0.4, currency) == prefix + "0"
    assert money2(-0.004, currency) == prefix + ("0" if currency == "JPY" else "0.00")
    assert money_k(-0.4, currency) == prefix + "0"


def test_default_currency_and_keyword():
    assert money(1234) == "$1,234"
    assert money2(1234) == "$1,234.00"
    assert money_k(999999) == "$1M"
    assert render_row('{{ amount|money(currency="EUR") }}', {"amount": 1234}) == "€1,234"


@pytest.mark.parametrize("currency,prefix", [("JPY", "¥"), ("KRW", "KRW "), (" jpy ", "¥")])
def test_money2_zero_decimal_currencies(currency, prefix):
    assert money2(1234, currency) == prefix + "1,234"
    assert money2(1234.6, currency) == prefix + "1,235"
    assert money2(-0.4, currency) == prefix + "0"
    assert money2(-1234.6, currency) == "-" + prefix + "1,235"


@pytest.mark.parametrize("value", [
    "1899-12-31", "2101-01-01", "12/31/1899", "01.01.2101",
    "13.32.2024", "02/30/2024", "555-123-4567", "02134", "12345", 46287,
    date(1899, 12, 31), datetime(2101, 1, 1),
])
def test_ingest_date_detection_rejects_identifiers_and_implausible_dates(value):
    from sheetrender.templating import parse_date

    assert parse_date(value, excel_serial=False) is None


def test_date_filter_can_explicitly_format_years_outside_ingest_window():
    assert format_date("1899-12-31", "%Y-%m-%d") == "1899-12-31"
    assert format_date("2101-01-01", "%Y-%m-%d") == "2101-01-01"


@pytest.mark.parametrize("formatter", [money, money2, money_k])
@pytest.mark.parametrize("value", [None, "", "abc", float("nan"), float("inf")])
def test_currency_invalid(formatter, value):
    assert formatter(value, "EUR") == ""


def test_qr_svg_and_size():
    uri = qr('https://example.com/?a=1&b="hello"', 256)
    assert uri.startswith("data:image/svg+xml;base64,")
    svg = ElementTree.fromstring(base64.b64decode(uri.split(",", 1)[1]))
    assert svg.tag == "{http://www.w3.org/2000/svg}svg"
    assert float(svg.attrib["width"]) == pytest.approx(256)
    assert float(svg.attrib["height"]) == pytest.approx(256)
    # Segno groups the paths when a background and a non-unit scale coexist.
    # Check the dark modules, not just the white background's path.
    paths = svg.findall(".//{http://www.w3.org/2000/svg}path")
    dark_paths = [path for path in paths if path.get("stroke") in {"#000", "#000000", "black"}]
    assert len(dark_paths) == 1
    modules = sum(int(length) for length in re.findall(r"h(\d+)", dark_paths[0].attrib["d"]))
    expected = segno.make_qr('https://example.com/?a=1&b="hello"')
    assert modules == sum(sum(row) for row in expected.matrix)
    group = svg.find("{http://www.w3.org/2000/svg}g")
    assert group is not None
    scale = float(group.attrib["transform"].removeprefix("scale(").removesuffix(")"))
    assert scale * expected.symbol_size()[0] == pytest.approx(256)
    assert qr("hello") != qr("different")
    assert qr(0)
    assert qr("é")
    assert qr("x" * 1000)


@pytest.mark.parametrize("value", [None, "", " ", "x" * 1001])
def test_qr_empty_and_bound(value):
    assert qr(value) == ""


def test_qr_size_bounds():
    for size, expected in [(-1, 16), (10**9, 2048)]:
        svg = ElementTree.fromstring(base64.b64decode(qr("hello", size).split(",", 1)[1]))
        assert float(svg.attrib["width"]) == pytest.approx(expected)
    assert qr("hello", "bad") == ""


def test_registered_filters_remain_autoescaped():
    html = get_env().from_string('{{ x|date }} {{ y|money("EUR") }} <img src="{{ z|qr }}">')
    assert html.render(x="2026-09-22", y=1234, z="hello").startswith(
        'Sep 22, 2026 €1,234 <img src="data:image/svg+xml;base64,'
    )
    assert "&lt;" in render_row('{{ value|money(currency="<script>") }}', {"value": 1})


@pytest.mark.parametrize("formatter", [money, money2, money_k])
def test_currency_overflow_returns_empty(formatter):
    assert formatter(10**1000) == ""


def test_date_preserves_strict_undefined():
    from jinja2 import TemplateError

    with pytest.raises(TemplateError, match="Template render error:"):
        render_row("{{ unknown|date }}", {})


@pytest.mark.parametrize("separator", ["/", ".", "-"])
def test_day_first_flips_only_ambiguous_numeric_dates(separator):
    from sheetrender.templating import parse_date

    ambiguous = separator.join(["03", "04", "2026"])
    assert parse_date(ambiguous) == datetime(2026, 3, 4)
    assert parse_date(ambiguous, day_first=True) == datetime(2026, 4, 3)
    assert format_date(ambiguous, "%Y-%m-%d") == "2026-03-04"
    assert format_date(ambiguous, "%Y-%m-%d", day_first=True) == "2026-04-03"
    for unambiguous in (separator.join(["13", "04", "2026"]), separator.join(["04", "13", "2026"])):
        assert parse_date(unambiguous) == parse_date(unambiguous, day_first=True) == datetime(2026, 4, 13)


@pytest.mark.parametrize("value", [
    date(2026, 3, 4), datetime(2026, 3, 4, 14, tzinfo=UTC), "2026-03-04",
    "2026-03-04T14:00:00Z", 46085, 46085.5,
])
def test_day_first_leaves_iso_serials_and_date_objects_alone(value):
    assert format_date(value, day_first=True) == format_date(value) == "Mar 4, 2026"


def test_day_first_env_and_render_helpers():
    from sheetrender.filenames import render_filename
    from sheetrender.templating import (
        compile_template,
        render_text,
        validate_and_render,
    )

    row = {"d": "03/04/2026"}
    html = '{{ d|date }} / {{ d|date("%d %B %Y") }}'
    assert get_env().from_string(html).render(**row) == "Mar 4, 2026 / 04 March 2026"
    assert get_env(day_first=True).from_string(html).render(**row) == "Apr 3, 2026 / 03 April 2026"
    assert render_row(html, row) == "Mar 4, 2026 / 04 March 2026"
    assert render_row(html, row, day_first=True) == "Apr 3, 2026 / 03 April 2026"
    assert validate_and_render(html, row, day_first=True) == "Apr 3, 2026 / 03 April 2026"
    assert compile_template(html, day_first=True).render(**row) == "Apr 3, 2026 / 03 April 2026"
    assert render_text("{{ d|date('%Y-%m-%d') }}", row) == "2026-03-04"
    assert render_text("{{ d|date('%Y-%m-%d') }}", row, day_first=True) == "2026-04-03"
    assert render_filename("inv {{ d|date('%Y-%m-%d') }}", row, 0) == "inv 2026-03-04.pdf"
    assert render_filename("inv {{ d|date('%Y-%m-%d') }}", row, 0, day_first=True) == "inv 2026-04-03.pdf"


def test_cli_day_first_flag_reaches_filenames():
    from sheetrender.cli import _batch_filenames, _build_parser

    assert _build_parser().parse_args(["render", "t.html", "-o", "o.pdf"]).day_first is False
    args = _build_parser().parse_args(["batch", "t.html", "d.csv", "-o", "out", "--day-first"])
    assert args.day_first is True
    units = [(0, {"d": "03/04/2026"})]
    assert _batch_filenames(units, "{{ d|date('%Y-%m-%d') }}", False) == ["2026-03-04.pdf"]
    assert _batch_filenames(units, "{{ d|date('%Y-%m-%d') }}", False, day_first=True) == ["2026-04-03.pdf"]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("$1,234.50", "$1,234.50"),
        ("1,234.5", "$1,234.50"),
        ("(45.00)", "-$45.00"),
        ("-$1,234.50", "-$1,234.50"),
        ("$ 12", "$12.00"),
        ("1.234,50 €", "$1,234.50"),
        ("99,90", "$99.90"),
        ("1,23,456.00", "$123,456.00"),
        ("12.50", "$12.50"),
    ],
)
def test_number_filters_read_numbers_as_a_csv_export_writes_them(value, expected):
    """Excel and Google Sheets CSV exports write the displayed text, so a
    currency column arrives as "$1,234.50" — which money2 used to print as
    nothing at all, leaving the amount blank on every document."""
    env = get_env()
    assert env.filters["money2"](value) == expected
    assert env.filters["comma2"](value) == expected.replace("$", "")


@pytest.mark.parametrize("value", ["1,2,3", "12.5%", "USD", "1.2.3,4,5", "$", "()", "1,234,56"])
def test_number_filters_leave_text_that_is_not_a_number_blank(value):
    assert get_env().filters["money2"](value) == ""


def test_pct_and_comma_accept_displayed_text():
    env = get_env()
    assert env.filters["pct"]("89%") == "89%"
    assert env.filters["pct"](" 89 % ") == "89%"
    assert env.filters["comma"]("12,000") == "12,000"
    assert env.filters["money_k"]("$1,250,000") == "$1.2M"


def test_sumcol_reads_decimal_commas_instead_of_dropping_them():
    sumcol = get_env().filters["sumcol"]
    # "1.234,50" used to lose its comma and total as 1.2345.
    items = [{"a": "1.234,50"}, {"a": "99,90"}, {"a": "$1,000.00"}, {"a": "(0.40)"}]
    assert sumcol(items, "a") == 2334.0


@pytest.mark.parametrize("value", [
    # Google Sheets import (FORMATTED_STRING) and Sheets' own date-time format.
    "9/22/2026 15:59:00", "9/22/2026 3:59 PM", "09/22/2026 15:59",
    # Google Forms "Timestamp" column as its CSV download writes it.
    "2026/09/22 3:59:00 PM GMT+1", "2026/09/22 15:59:00",
    "22.09.2026 15:59", "2026-09-22T15:59:00Z",
])
def test_date_reads_a_timestamp_with_its_clock_time(value):
    """A Forms/Sheets timestamp used to format as empty text, so a certificate
    template's `timestamp | date(...)` left every award date blank."""
    assert format_date(value) == "Sep 22, 2026"
    assert format_date(value, "%H:%M") == "15:59"


@pytest.mark.parametrize("value", [
    "September 22, 2026", "Sep 22, 2026", "22 September 2026", "22 Sep 2026",
    "Tuesday, September 22, 2026", "22-Sep-2026", "22-Sep-26", "2026/09/22",
])
def test_date_reads_month_names_and_year_first_slashes(value):
    assert format_date(value, "%Y-%m-%d") == "2026-09-22"


@pytest.mark.parametrize("value", [
    "9/22/2026 25:00", "9/22/2026 13:00 PM", "9/22/2026 12:61", "Meeting 10:30",
    "Septembre 22, 2026", "Sep 2026", "2026/22/09",
])
def test_date_still_rejects_broken_timestamps(value):
    assert format_date(value) == ""


def test_ingest_types_a_sheets_timestamp_column_as_a_date(tmp_path):
    from sheetrender.sheets import parse_csv

    path = tmp_path / "responses.csv"
    path.write_text("Timestamp,Name\n9/22/2026 15:59:00,Ann\n9/23/2026 9:01:10,Bob\n")
    types = [c["inferred_type"] for c in parse_csv(str(path))["columns"]]
    assert types == ["date", "string"]


# (input, sumcol under 0.4.0, sumcol now, money2 under 0.4.0, money2 now).
# The 0.4.0 columns were produced by the released wheel. sumcol keeps every
# total 0.4.0 gave, except the forms marked below, where 0.4.0 read a decimal
# comma or dot grouping as something else (1.234,5 as 1.2345) or skipped a
# currency symbol it did not know (¥). money2 (all the number filters share
# its parser) only ever gains a value where 0.4.0 printed nothing.
_NAN, _INF = float("nan"), float("inf")
NUMBER_TABLE = [
    ("$1e3", 1000.0, 1000.0, "", ""),
    ("(1e3)", -1000.0, -1000.0, "", ""),
    ("$12.", 12.0, 12.0, "", ""),
    ("(12.)", -12.0, -12.0, "", ""),
    ("$1.5E3", 1500.0, 1500.0, "", ""),
    ("1,234.", 1234.0, 1234.0, "", ""),
    ("$1,234.", 1234.0, 1234.0, "", ""),
    ("(1,234.50)", -1234.5, -1234.5, "", "-$1,234.50"),
    ("1,2,3", 123.0, 123.0, "", ""),
    ("$nan", _NAN, _NAN, "", ""),
    ("$1e400", _INF, _INF, "", ""),
    ("12.", 12.0, 12.0, "$12.00", "$12.00"),
    ("1e3", 1000.0, 1000.0, "$1,000.00", "$1,000.00"),
    ("-1e3", -1000.0, -1000.0, "-$1,000.00", "-$1,000.00"),
    ("$-5", -5.0, -5.0, "", "-$5.00"),
    ("-$5", -5.0, -5.0, "", "-$5.00"),
    ("$ 5", 5.0, 5.0, "", "$5.00"),
    ("( 5 )", -5.0, -5.0, "", "-$5.00"),
    ("(5)", -5.0, -5.0, "", "-$5.00"),
    ("(-5)", 5.0, 5.0, "", "$5.00"),
    ("()", 0.0, 0.0, "", ""),
    ("--5", 0.0, 0.0, "", ""),
    ("+5", 5.0, 5.0, "$5.00", "$5.00"),
    ("$+5", 5.0, 5.0, "", "$5.00"),
    ("1,234", 1234.0, 1234.0, "", "$1,234.00"),
    ("1,234.5", 1234.5, 1234.5, "", "$1,234.50"),
    ("$1,234.50", 1234.5, 1234.5, "", "$1,234.50"),
    ("12,345,678", 12345678.0, 12345678.0, "", "$12,345,678.00"),
    ("12,34,567", 1234567.0, 1234567.0, "", "$1,234,567.00"),
    ("nan", _NAN, _NAN, "", ""),
    ("inf", _INF, _INF, "", ""),
    ("1e400", _INF, _INF, "", ""),
    ("-0", 0.0, 0.0, "$0.00", "$0.00"),
    ("0x10", 0.0, 0.0, "", ""),
    ("1_000", 1000.0, 1000.0, "$1,000.00", "$1,000.00"),
    ("١٢٣", 123.0, 123.0, "$123.00", "$123.00"),
    ("12.5%", 0.0, 0.0, "", ""),
    ("$.5", 0.5, 0.5, "", "$0.50"),
    (".5", 0.5, 0.5, "$0.50", "$0.50"),
    ("5.", 5.0, 5.0, "$5.00", "$5.00"),
    ("1..2", 0.0, 0.0, "", ""),
    ("$", 0.0, 0.0, "", ""),
    ("-", 0.0, 0.0, "", ""),
    ("( )", 0.0, 0.0, "", ""),
    ("1.234", 1.234, 1.234, "$1.23", "$1.23"),
    ("0.1", 0.1, 0.1, "$0.10", "$0.10"),
    ("  7  ", 7.0, 7.0, "$7.00", "$7.00"),
    ("£3", 3.0, 3.0, "", "$3.00"),
    ("3€", 3.0, 3.0, "", "$3.00"),
    ("3 €", 3.0, 3.0, "", "$3.00"),
    ("$3.00-", 0.0, 0.0, "", ""),
    ("1.5e-3", 0.0015, 0.0015, "$0.00", "$0.00"),
    ("∞", 0.0, 0.0, "", ""),
    ("TRUE", 0.0, 0.0, "", ""),
    (True, 0.0, 0.0, "$1.00", "$1.00"),
    (None, 0.0, 0.0, "", ""),
    ("", 0.0, 0.0, "", ""),
    (5, 5.0, 5.0, "$5.00", "$5.00"),
    (-2, -2.0, -2.0, "-$2.00", "-$2.00"),
    # Deliberate changes: a decimal comma or dot grouping, and ¥.
    ("1.234,5", 1.2345, 1234.5, "", "$1,234.50"),
    ("€1.234,50", 1.2345, 1234.5, "", "$1,234.50"),
    ("1 234,50", 123450.0, 1234.5, "", "$1,234.50"),
    ("1,23", 123.0, 1.23, "", "$1.23"),
    ("12,5", 125.0, 12.5, "", "$12.50"),
    ("1.234.567", 0.0, 1234567.0, "", "$1,234,567.00"),
    ("¥3", 0.0, 3.0, "", "$3.00"),
]


def _same_number(a, b):
    return (a != a and b != b) or a == b


@pytest.mark.parametrize(("value", "old_sum", "new_sum", "old_money", "new_money"), NUMBER_TABLE)
def test_number_parsing_against_0_4_0(value, old_sum, new_sum, old_money, new_money):
    """"$1e3", "(1e3)", "$12." and "(12.)" totalled under 0.4.0's
    strip-then-Decimal reading and fell to 0 under the stricter parser: a
    silent zero in an invoice total. sumcol falls back to 0.4.0's reading
    wherever the new parser declines."""
    from sheetrender.templating import sumcol

    assert _same_number(sumcol([{"a": value}], "a"), new_sum)
    assert money2(value) == new_money
    if old_money:
        assert new_money == old_money


def test_number_table_changes_only_the_deliberate_forms():
    deliberate = NUMBER_TABLE[NUMBER_TABLE.index(("1.234,5", 1.2345, 1234.5, "", "$1,234.50")):]
    for row in NUMBER_TABLE:
        if row not in deliberate:
            assert _same_number(row[1], row[2]), row
