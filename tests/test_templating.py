import pytest
from jinja2 import TemplateError

from sheetrender.grouping import Group, group_context
from sheetrender.templating import (
    TemplateRenderError,
    compile_template,
    get_env,
    render_compiled,
    validate_and_render,
)


def test_money_k():
    env = get_env()
    assert env.filters["money_k"](234000) == "$234K"


def test_money2():
    money2 = get_env().filters["money2"]
    assert money2(1234.5) == "$1,234.50"
    assert money2(None) == ""
    assert money2("12.3") == "$12.30"
    assert money2(-12.3) == "-$12.30"
    assert get_env().filters["money"](-12.3) == "-$12"
    assert get_env().filters["money_k"](-1234) == "-$1K"


def test_comma2_formats_two_decimals_without_symbol():
    comma2 = get_env().filters["comma2"]
    assert comma2(1234.5) == "1,234.50"
    assert comma2(-1234.5) == "-1,234.50"
    assert comma2("12.3") == "12.30"
    assert comma2(None) == ""
    assert comma2("not a number") == ""


def test_sumcol_skips_missing_and_non_numeric_cells():
    sumcol = get_env().filters["sumcol"]
    items = [
        {"amount": 2},
        {"amount": None},
        {"amount": "3.5"},
        {"amount": ""},
        {"amount": "not a number"},
        {},
    ]

    assert sumcol(items, "amount") == 5.5
    assert sumcol([], "amount") == 0.0


def test_sumcol_parses_currency_and_ignores_nested_junk():
    sumcol = get_env().filters["sumcol"]
    items = [
        {"amount": "$1,234.56"},
        {"amount": "€ 20"},
        {"amount": "£(500)"},
        {"amount": "(500)"},
        ["nested", "junk"],
        "junk",
        None,
    ]

    assert sumcol(items, "amount") == 254.56
    assert sumcol({"amount": 10}, "amount") == 0.0
    assert sumcol(None, "amount") == 0.0


def test_sumcol_and_money2_render_inside_loop_template():
    html = """{% for item in items %}{{ item.name }}{% endfor %}
    <strong>{{ items | sumcol('amount') | money2 }}</strong>"""

    rendered = validate_and_render(
        html,
        {"items": [{"name": "Pen", "amount": "2.25"}, {"name": "Tape", "amount": None}]},
    )

    assert "PenTape" in rendered
    assert "$2.25" in rendered


def test_filter_precedence_runtime_error_is_a_repairable_template_error():
    context = group_context(
        Group(
            index=0,
            key="INV-1",
            key_values=("INV-1",),
            member_indexes=[0, 1],
            rows=[{"amount": 10}, {"amount": 5}],
        )
    )

    with pytest.raises(TemplateError, match="Template render error:.*multiply"):
        validate_and_render(
            "{{ (items | sumcol('amount')) * 1.08 | round(2) | money2 }}",
            context,
        )


def test_filter_precedence_runtime_error_survives_the_precompiled_path():
    """Ported from a batch-runner test that asserted this failed one row only.

    That runner lived in the app, but the property it depended on is the
    package's: a template compiled once for a batch and rendered per row raises
    the same prefixed TemplateRenderError that validate_and_render does — and
    prefixes it exactly once, so a caller can report the message verbatim.
    """
    context = {
        "items": [{"amount": 10}, {"amount": 5}],
        "item_count": 2,
        "group_key": "INV-1",
    }
    template = compile_template(
        "{{ (items | sumcol('amount')) * 1.08 | round(2) | money2 }}"
    )

    with pytest.raises(TemplateRenderError) as exc_info:
        render_compiled(template, context)

    message = str(exc_info.value)
    assert message.startswith("Template render error:")
    assert message.count("Template render error:") == 1
    assert "multiply" in message


def test_pct():
    env = get_env()
    assert env.filters["pct"](89) == "89%"


def test_bar_width_clamp():
    env = get_env()
    assert env.filters["bar_width"](130) == 100.0


def test_sign_class_negative():
    env = get_env()
    assert env.filters["sign_class"](90, 100) == "negative"


def test_yesno_class_no():
    env = get_env()
    assert env.filters["yesno_class"]("No") == "no"


def test_strict_undefined_raises():
    with pytest.raises(Exception):
        validate_and_render("{{ missing_var }}", {})


def test_syntax_error_raises():
    with pytest.raises(TemplateError) as exc_info:
        validate_and_render("{% for %}", {})
    assert str(exc_info.value).count("Template render error:") == 1
