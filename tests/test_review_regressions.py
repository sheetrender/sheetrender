"""Regression tests for bugs found in the 0.2.0 review, one group per bug."""
from __future__ import annotations

import pytest
from jinja2 import TemplateSyntaxError
from jinja2.exceptions import SecurityError

from sheetrender import (
    compile_template,
    dedupe_filenames,
    inject_watermark,
    iter_rows,
    parse_csv,
    render_filename,
    render_text,
    sanitize_columns,
    stamp_preview_watermark,
    strip_author_page_rules,
    validate_and_render,
)
from sheetrender.grouping import _GROUP_NAME_HINT
from sheetrender.render import _pdf_options
from sheetrender.templating import comma, money, money2, money_k


class TestSyntaxErrors:
    def test_compile_template_keeps_the_message_and_line(self):
        with pytest.raises(TemplateSyntaxError) as exc_info:
            compile_template("line one\n{% if %}")
        assert exc_info.value.lineno == 2
        assert str(exc_info.value).startswith("Template render error:")
        assert "__init__" not in str(exc_info.value)

    def test_validate_and_render_prefixes_once(self):
        with pytest.raises(TemplateSyntaxError) as exc_info:
            validate_and_render("{% for %}", {})
        assert str(exc_info.value).count("Template render error:") == 1


class TestDottedCapitalI:
    """ "İ".lower() is two characters, which used to shift every index after it."""

    def test_watermark_lands_before_the_closing_body_tag(self):
        html = "<html><body><p>İstanbul İzmir</p></BODY></html>"
        assert inject_watermark(html, "[WM]") == "<html><body><p>İstanbul İzmir</p>[WM]</BODY></html>"

    def test_page_rules_are_stripped_after_the_character(self):
        html = "<title>İİİİİİİİ</title><STYLE>@PAGE{margin:0} .a{color:red}</STYLE>"
        assert strip_author_page_rules(html) == "<title>İİİİİİİİ</title><STYLE> .a{color:red}</STYLE>"

    def test_nested_page_rule_and_unclosed_block(self):
        css = "<style>a{b:c}@page{@top-center{content:'x'}}d{e:f}</style>"
        assert strip_author_page_rules(css) == "<style>a{b:c}d{e:f}</style>"
        assert strip_author_page_rules("<style>@page{margin:0</style>") == "<style></style>"


class TestCsvCells:
    @pytest.fixture
    def rows(self, tmp_path):
        path = tmp_path / "data.csv"
        path.write_text(
            "zip,name,code,amount,qty\n"
            "02134,Nan,1_000,12.50,-3\n"
            "00501,Infinity,1e3\n",
            encoding="utf-8",
        )
        dataset = parse_csv(str(path))
        return dataset, list(iter_rows(str(path), dataset["columns"]))

    def test_only_plain_decimals_become_numbers(self, rows):
        _, (first, second) = rows
        assert first == {"zip": "02134", "name": "Nan", "code": "1_000", "amount": 12.5, "qty": -3}
        assert second["zip"] == "00501"
        assert second["name"] == "Infinity"
        assert second["code"] == "1e3"

    def test_short_rows_are_padded_so_every_key_exists(self, rows):
        dataset, (_, second) = rows
        assert second["amount"] is None and second["qty"] is None
        assert set(dataset["sample_rows"][1]) == {"zip", "name", "code", "amount", "qty"}

    def test_inferred_types_follow_the_converted_values(self, rows):
        dataset, _ = rows
        types = {column["key"]: column["inferred_type"] for column in dataset["columns"]}
        assert types == {"zip": "string", "name": "string", "code": "string", "amount": "number", "qty": "number"}


class TestColumnKeys:
    def test_a_literal_suffix_header_cannot_collide(self):
        keys = sanitize_columns(["a", "a", "a_2", "a"])
        assert keys == ["a", "a_2", "a_2_2", "a_3"]
        assert len(set(keys)) == len(keys)

    def test_jinja_constants_are_prefixed(self):
        assert sanitize_columns(["None", "True", "false"]) == ["col_none", "col_true", "col_false"]


class TestFilenames:
    def test_dedupe_ignores_case(self):
        assert dedupe_filenames(["Acme.pdf", "ACME.pdf", "acme (2).pdf"]) == [
            "Acme.pdf",
            "ACME (2).pdf",
            "acme (2) (2).pdf",
        ]

    def test_long_multibyte_names_fit_in_255_bytes(self):
        name = render_filename("{{ n }}", {"n": "請" * 200}, 0)
        assert name.endswith(".pdf")
        assert len(name.encode("utf-8")) <= 255 - len(" (9999)")

    def test_windows_device_name_with_inner_extension(self):
        assert render_filename("{{ n }}", {"n": "CON.txt"}, 0) == "row_1.pdf"
        assert render_filename("{{ n }}", {"n": "Contract.v2"}, 0) == "Contract.v2.pdf"


class TestSandboxAllocations:
    @pytest.mark.parametrize(
        "source",
        [
            "{{ 'x'.ljust(200000000) }}",
            "{{ 'x'.zfill(200000000) }}",
            "{{ '%200000000d' % 1 }}",
            "{{ '%*d' % (200000000, 1) }}",
            "{{ '%200000000d'|format(1) }}",
            "{{ '{:>200000000}'.format(1) }}",
            "{{ '{:>{w}}'.format(1, w=200000000) }}",
            "{{ 'x'|center(200000000) }}",
            "{{ 'x'|indent(200000000) }}",
            "{% set ns = namespace(s='x' * 1000) %}"
            "{% for i in range(24) %}{% set ns.s = ns.s + ns.s %}{% endfor %}{{ ns.s|length }}",
        ],
    )
    def test_refused(self, source):
        with pytest.raises(Exception) as exc_info:
            render_text(source, {})
        assert isinstance(exc_info.value.__cause__, SecurityError)

    def test_ordinary_formatting_still_works(self):
        source = (
            "{{ '%05.2f' % 3.14159 }}|{{ '%s: %d' % ('n', 5000000) }}|{{ '{:>6}'.format(5000000) }}|"
            "{{ 'Call 18005551234 %s' % 'now' }}|{{ 'ab'|center(6) }}|{{ 'a' + 'b' }}|{{ 'x\ny'|indent(2) }}"
        )
        assert render_text(source, {}) == "03.14|n: 5000000|5000000|Call 18005551234 now|  ab  |ab|x\n  y"


class TestMoneyFilters:
    def test_rounding_up_moves_to_the_next_suffix(self):
        assert money_k(999_999) == "$1M"
        assert money_k(999_499) == "$999K"
        assert money_k(999.6) == "$1K"
        assert money_k(-1_500_000) == "-$1.5M"

    def test_no_negative_zero(self):
        assert money(-0.4) == "$0"
        assert money2(-0.004) == "$0.00"
        assert comma(-0.4) == "0"
        assert money(-0.6) == "-$1"

    def test_non_finite_values_render_empty(self):
        assert money("nan") == "" and money_k(float("inf")) == "" and money2("-inf") == ""


def test_none_margin_falls_back_to_the_default():
    margins = _pdf_options({"margins": {"top": None, "left": 0}}).margins
    assert margins == {"top": "15mm", "right": "15mm", "bottom": "15mm", "left": "0mm"}


def test_preview_stamp_rejects_non_ascii_text_clearly():
    with pytest.raises(ValueError, match="must be ASCII"):
        stamp_preview_watermark(b"", text="APERÇU")


def test_group_name_hint_matches_whole_short_words_only():
    hinted = ["invoice_no", "orderid", "customer", "po", "po_number", "client_ref", "id", "bill_to"]
    not_hinted = ["postcode", "report_date", "paid", "valid", "preferred_name", "deposit", "description"]
    assert [key for key in hinted if not _GROUP_NAME_HINT.search(key)] == []
    assert [key for key in not_hinted if _GROUP_NAME_HINT.search(key)] == []


def test_unclosed_style_block_still_loses_its_page_rule():
    assert strip_author_page_rules("<style>@page{margin:0} p{color:red}") == "<style> p{color:red}"


def test_group_keys_treat_whole_floats_as_integers():
    from sheetrender import iter_groups

    groups = iter_groups(enumerate([{"inv": 1001}, {"inv": 1001.0}, {"inv": 1001.5}]), {"group_by": ["inv"]})
    assert [group.key for group in groups] == ["1001", "1001.5"]


def test_inspect_handles_a_file_with_no_columns(tmp_path, capsys):
    from sheetrender.cli import main

    path = tmp_path / "empty.csv"
    path.write_text("\n", encoding="utf-8")
    assert main(["inspect", str(path)]) == 0
    assert "Rows: 0" in capsys.readouterr().out


def test_browser_is_connected_reflects_the_launched_browser():
    from unittest.mock import MagicMock, patch

    from sheetrender import browser, browser_is_connected

    assert browser_is_connected() is False
    with patch.object(browser._state, "browser", MagicMock(is_connected=lambda: True)):
        assert browser_is_connected() is True
