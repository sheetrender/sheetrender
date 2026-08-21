from __future__ import annotations

from decimal import Decimal, InvalidOperation

from jinja2 import StrictUndefined, Template, TemplateError, select_autoescape
from jinja2.sandbox import SandboxedEnvironment


def money_k(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    prefix = "-$" if v < 0 else "$"
    v = abs(v)
    if v >= 1_000_000:
        m = v / 1_000_000
        s = f"{m:.1f}"
        if s.endswith(".0"):
            s = s[:-2]
        return f"{prefix}{s}M"
    if v >= 1000:
        return f"{prefix}{round(v / 1000)}K"
    return f"{prefix}{round(v)}"


def money(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return f"{'-$' if v < 0 else '$'}{abs(v):,.0f}"


def money2(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return f"{'-$' if v < 0 else '$'}{abs(v):,.2f}"


def sumcol(items, key):
    if not isinstance(items, list):
        return 0.0
    total = Decimal("0")
    for item in items:
        if not isinstance(item, dict):
            continue
        value = item.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        negative = False
        if isinstance(value, str):
            value = value.strip().translate(
                str.maketrans("", "", "$€£, \t\r\n\u00a0\u202f")
            )
            if len(value) > 1 and value.startswith("(") and value.endswith(")"):
                negative = True
                value = value[1:-1]
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, TypeError, ValueError):
            continue
        total += -parsed if negative else parsed
    return float(total)


def comma(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return f"{v:,.0f}"


def comma2(x):
    # money2 without the "$": the amount half of a non-USD price, so the
    # template supplies its own currency symbol next to it.
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return f"{v:,.2f}"


def pct(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return ""
    return f"{round(v)}%"


def bar_width(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(100.0, v))


def sign_class(actual, target):
    try:
        return "positive" if float(actual) >= float(target) else "negative"
    except (TypeError, ValueError):
        return ""


def yesno_class(v):
    if v is None:
        return "no"
    return "" if str(v).strip().lower() in {"yes", "true", "1"} else "no"


def get_env(*, autoescape: bool = True) -> SandboxedEnvironment:
    env = SandboxedEnvironment(
        autoescape=select_autoescape(default=True) if autoescape else False,
        undefined=StrictUndefined,
    )
    env.filters.update(
        {
            "money_k": money_k,
            "money": money,
            "money2": money2,
            "sumcol": sumcol,
            "comma": comma,
            "comma2": comma2,
            "pct": pct,
            "bar_width": bar_width,
            "sign_class": sign_class,
            "yesno_class": yesno_class,
        }
    )
    return env


def compile_template(html: str) -> Template:
    """Compile a template once so batch jobs don't re-parse it per row."""
    try:
        return get_env().from_string(html)
    except TemplateError as exc:
        raise type(exc)(f"Template render error: {exc}") from exc


class TemplateRenderError(TemplateError):
    """A non-syntax failure raised while evaluating a template."""


def _wrapped_render_error(exc: Exception) -> TemplateError:
    # validate_and_render deliberately guards both compilation and rendering,
    # while render_compiled also has to be safe when batch jobs call it
    # directly. Keep the nested guard from adding the same prefix twice.
    if isinstance(exc, TemplateRenderError) or (
        isinstance(exc, TemplateError) and str(exc).startswith("Template render error:")
    ):
        return exc
    return TemplateRenderError(f"Template render error: {exc}")


def render_compiled(template: Template, row: dict) -> str:
    try:
        return template.render(**row)
    except Exception as exc:
        wrapped = _wrapped_render_error(exc)
        if wrapped is exc:
            raise
        raise wrapped from exc


def validate_and_render(html: str, row: dict) -> str:
    try:
        return render_compiled(compile_template(html), row)
    except Exception as exc:
        wrapped = _wrapped_render_error(exc)
        if wrapped is exc:
            raise
        raise wrapped from exc


def render_row(html: str, row: dict) -> str:
    return validate_and_render(html, row)


def render_text(template_str: str, row: dict) -> str:
    """Render a plain-text template (filenames, email subjects/bodies) without
    HTML autoescaping — entity-escaped output would corrupt non-HTML strings."""
    try:
        return get_env(autoescape=False).from_string(template_str).render(**row)
    except Exception as exc:
        wrapped = _wrapped_render_error(exc)
        if wrapped is exc:
            raise
        raise wrapped from exc
