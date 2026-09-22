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
