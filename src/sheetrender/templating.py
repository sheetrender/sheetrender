from __future__ import annotations

import base64
import io
import math
import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import date as Date
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from functools import wraps

import segno
from jinja2 import (
    StrictUndefined,
    Template,
    TemplateError,
    TemplateSyntaxError,
    Undefined,
    select_autoescape,
)
from jinja2 import filters as _jinja_filters
from jinja2.exceptions import SecurityError
from jinja2.sandbox import SandboxedEnvironment

# Ceilings for single expressions that can allocate far more than the template
# text suggests. Any size check a caller runs on the rendered output comes after
# the expression has already been evaluated, so `{{ 'x' * 10**9 }}`,
# `'x'.ljust(10**9)` or `10**10**8` would exhaust memory or CPU before it ever
# fired. The limits are generous for any real document.
MAX_REPEAT_LENGTH = 1_000_000
MAX_POWER_EXPONENT = 10_000
MAX_POWER_BASE_BITS = 4_096

ERROR_PREFIX = "Template render error:"

# str methods whose only argument is an output width. Templates get the same
# result from the bounded `center` filter or `format`, so they are refused
# outright rather than checked.
_PADDING_METHODS = frozenset({"ljust", "rjust", "center", "zfill", "expandtabs"})

_DIGIT_RUN_RE = re.compile(r"[0-9]+")
# Captures width and precision: "%-8.3f" gives ("8", "3"). Either may be "*",
# which takes the number from the arguments instead.
_PERCENT_SPEC_RE = re.compile(r"%[-+ #0]*([0-9]+|\*)?(?:\.([0-9]+|\*))?")
_BRACE_FIELD_RE = re.compile(r"\{[^{}]*\}")
# A replacement field inside a format spec, as in "{:>{width}}".
_NESTED_FIELD_RE = re.compile(r":[^{}]*\{")


def _to_number(x) -> float | None:
    """float(x) for the formatting filters, or None if x is not a finite number."""
    try:
        v = float(x)
    except (TypeError, ValueError, OverflowError):
        return None
    return v if math.isfinite(v) else None


def _sign(v: float, digits: int) -> str:
    # Decided on the rounded value so -0.4 prints as "$0", not "-$0".
    return "-" if round(v, digits) < 0 else ""


def _currency_prefix(currency) -> str:
    code = str(currency or "USD").strip().upper()
    return {"USD": "$", "EUR": "€", "GBP": "£", "JPY": "¥"}.get(code, code + " ")


def money_k(x, currency="USD"):
    v = _to_number(x)
    if v is None:
        return ""
    prefix = f"{_sign(v, 0)}{_currency_prefix(currency)}"
    v = abs(v)
    # Thresholds sit half a unit below the round number so a value that rounds
    # up to it moves to the next suffix: 999,999 is "$1M", not "$1000K".
    if v >= 999_500:
        s = f"{v / 1_000_000:.1f}".removesuffix(".0")
        return f"{prefix}{s}M"
    if v >= 999.5:
        return f"{prefix}{round(v / 1000)}K"
    return f"{prefix}{round(v)}"


def money(x, currency="USD"):
    v = _to_number(x)
    if v is None:
        return ""
    return f"{_sign(v, 0)}{_currency_prefix(currency)}{abs(v):,.0f}"


def money2(x, currency="USD"):
    v = _to_number(x)
    if v is None:
        return ""
    digits = 0 if str(currency).strip().upper() in {"JPY", "KRW"} else 2
    return f"{_sign(v, digits)}{_currency_prefix(currency)}{abs(v):,.{digits}f}"


def parse_date(value, *, excel_serial: bool = True) -> Date | None:
    """Parse dates without locale guessing; ambiguous numeric dates are month-first.

    Excel's 1900 system is supported from serial 1 through 73415 (2100-12-31).
    Serial 60 shares 1900-02-28 with 59, matching Excel readers' leap-day fix.
    Numeric strings stay text so identifiers do not become dates at ingest.
    Ingest detection (excel_serial=False) accepts only years 1900..2100.
    """
    if isinstance(value, Undefined):
        value = str(value)
    if isinstance(value, Date):
        return value if excel_serial or 1900 <= value.year <= 2100 else None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        if excel_serial and 1 <= value < 73416:
            return datetime(1899, 12, 30) + timedelta(days=value + (value < 60))
        return None
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        return None
    value = value.strip()
    if value.isdecimal():
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed if excel_serial or 1900 <= parsed.year <= 2100 else None
    except ValueError:
        pass
    for separator in ("/", ".", "-"):
        for order in ("%m{sep}%d{sep}%Y", "%d{sep}%m{sep}%Y"):
            try:
                parsed = datetime.strptime(value, order.format(sep=separator))
                return parsed if excel_serial or 1900 <= parsed.year <= 2100 else None
            except ValueError:
                pass
    return None


def date_needs_confirmation(value) -> bool:
    """Dotted or unpadded numeric strings need another date cell at ingest."""
    if not isinstance(value, str):
        return False
    value = value.strip()
    return bool(
        re.fullmatch(r"\d{1,2}\.\d{1,2}\.\d{4}", value)
        or re.fullmatch(r"(?:\d[/\-]\d{1,2}|\d{1,2}[/\-]\d)[/\-]\d{4}", value)
    )


def date(value, fmt="%b %-d, %Y") -> str:
    """Format a date, returning empty text for blank or unparseable input."""
    try:
        parsed = parse_date(value)
        # strftime widths allocate before the sandbox can check the result.
        if parsed is None or not isinstance(fmt, str) or len(fmt) > 128:
            return ""
        if any(int(width) > 128 for width in re.findall(r"%[-_0^#]*([0-9]+)", fmt)):
            return ""
        return parsed.strftime(fmt)
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def qr(value, size=128) -> str:
    """SVG data URI, with a pixel size clamped to 16..2048 and a 1000-char cap.

    Oversized input returns empty text, never a code for a truncated value.
    """
    if value is None:
        return ""
    text = str(value)
    if not text.strip() or len(text) > 1000:
        return ""
    try:
        size = max(16, min(2048, int(size)))
        code = segno.make_qr(text)
        output = io.BytesIO()
        code.save(output, kind="svg", scale=size / code.symbol_size()[0], light="white")
        return "data:image/svg+xml;base64," + base64.b64encode(output.getvalue()).decode("ascii")
    except (TypeError, ValueError, OverflowError):
        return ""


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
    v = _to_number(x)
    if v is None:
        return ""
    return f"{_sign(v, 0)}{abs(v):,.0f}"


def comma2(x):
    # money2 without the "$": the amount half of a non-USD price, so the
    # template supplies its own currency symbol next to it.
    v = _to_number(x)
    if v is None:
        return ""
    return f"{_sign(v, 2)}{abs(v):,.2f}"


def pct(x):
    v = _to_number(x)
    if v is None:
        return ""
    return f"{round(v)}%"


def bar_width(x):
    v = _to_number(x)
    if v is None:
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


class BoundedSandboxedEnvironment(SandboxedEnvironment):
    """SandboxedEnvironment that refuses runaway allocations at evaluation time.

    Covers the single expressions that turn a short template into a huge value:
    `*`, `**`, `+`, `%`-formatting, `str.format` widths and the padding methods.
    Growth spread over a loop (`~` concatenation, `join`) is not bounded here;
    callers rendering hostile templates still need a process memory limit.
    """

    intercepted_binops = frozenset({"*", "**", "+", "%"})

    def call_binop(self, context, operator, left, right):
        if operator == "*":
            _check_repeat(left, right)
        elif operator == "**":
            _check_power(left, right)
        elif operator == "+":
            _check_concat(left, right)
        elif operator == "%":
            _check_percent_format(left, right)
        return super().call_binop(context, operator, left, right)

    def is_safe_attribute(self, obj, attr, value):
        if isinstance(obj, str) and attr in _PADDING_METHODS:
            return False
        return super().is_safe_attribute(obj, attr, value)

    # Jinja sandboxes str.format through one of two hooks depending on its
    # version: format_string up to 3.1.4, wrap_str_format from 3.1.5.
    def format_string(self, s, args, kwargs, format_func=None):
        _check_brace_format(s, _format_values(args, kwargs))
        return super().format_string(s, args, kwargs, format_func)

    def wrap_str_format(self, value):
        sandboxed = super().wrap_str_format(value)
        if sandboxed is None:
            return None

        @wraps(sandboxed)
        def bounded(*args, **kwargs):
            _check_brace_format(value.__self__, _format_values(args, kwargs))
            return sandboxed(*args, **kwargs)

        return bounded


def _sized_length(value) -> int | None:
    if isinstance(value, (str, bytes, list, tuple)):
        return len(value)
    return None


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _check_repeat(left, right) -> None:
    for seq, count in ((left, right), (right, left)):
        length = _sized_length(seq)
        if length is None or not _is_int(count):
            continue
        if count > 0 and length * count > MAX_REPEAT_LENGTH:
            raise SecurityError(
                f"repeating a value {count} times would exceed {MAX_REPEAT_LENGTH} characters"
            )


def _check_power(base, exponent) -> None:
    if not _is_int(exponent):
        return
    if abs(exponent) > MAX_POWER_EXPONENT:
        raise SecurityError(f"exponent {exponent} exceeds {MAX_POWER_EXPONENT}")
    if _is_int(base) and base.bit_length() > MAX_POWER_BASE_BITS:
        raise SecurityError("power base is too large")


def _check_concat(left, right) -> None:
    left_length = _sized_length(left)
    right_length = _sized_length(right)
    if left_length is None or right_length is None:
        return
    if left_length + right_length > MAX_REPEAT_LENGTH:
        raise SecurityError(f"joining these values would exceed {MAX_REPEAT_LENGTH} characters")


def _check_widths(literal_widths, values, *, values_are_widths: bool) -> None:
    """Refuse a format whose width or precision could exceed the ceiling.

    A width is either a literal number inside a format spec or, when the spec
    asks for it (`%*d`, `{:>{width}}`), an integer argument.
    """
    widths = [int(width) for width in literal_widths]
    if values_are_widths:
        widths += [abs(value) for value in values if _is_int(value)]
    if any(width > MAX_REPEAT_LENGTH for width in widths):
        raise SecurityError(f"format width exceeds {MAX_REPEAT_LENGTH} characters")


def _check_percent_format(format_string, values) -> None:
    if not isinstance(format_string, str):
        return
    specs = _PERCENT_SPEC_RE.findall(format_string)
    literal_widths = [part for spec in specs for part in spec if part.isdigit()]
    if isinstance(values, dict):
        values = tuple(values.values())
    elif not isinstance(values, tuple):
        values = (values,)
    _check_widths(literal_widths, values, values_are_widths="*" in format_string)


def _format_values(args, kwargs) -> list:
    """Every value passed to format() or format_map(), mappings flattened."""
    values = list(kwargs.values())
    for arg in args:
        values.extend(arg.values() if isinstance(arg, Mapping) else (arg,))
    return values


def _check_brace_format(format_string, values) -> None:
    if not isinstance(format_string, str):
        return
    fields = _BRACE_FIELD_RE.findall(format_string)
    literal_widths = [run for field in fields for run in _DIGIT_RUN_RE.findall(field)]
    nested = _NESTED_FIELD_RE.search(format_string) is not None
    _check_widths(literal_widths, values, values_are_widths=nested)


def _bounded_center(value, width=80):
    _check_widths([], (width,), values_are_widths=True)
    return _jinja_filters.do_center(value, width)


def _bounded_indent(s, width=4, first=False, blank=False):
    # indent adds `width` to every line, so the growth is lines x width.
    lines = str(s).count("\n") + 1
    added = (width if _is_int(width) else len(str(width))) * lines
    if added > MAX_REPEAT_LENGTH:
        raise SecurityError(f"indenting would add more than {MAX_REPEAT_LENGTH} characters")
    return _jinja_filters.do_indent(s, width, first, blank)


def _bounded_format(value, *args, **kwargs):
    _check_percent_format(str(value), kwargs or args)
    return _jinja_filters.do_format(value, *args, **kwargs)


def get_env(*, autoescape: bool = True) -> SandboxedEnvironment:
    env = BoundedSandboxedEnvironment(
        autoescape=select_autoescape(default=True) if autoescape else False,
        undefined=StrictUndefined,
    )
    env.filters.update(
        {
            "money_k": money_k,
            "money": money,
            "money2": money2,
            "date": date,
            "qr": qr,
            "sumcol": sumcol,
            "comma": comma,
            "comma2": comma2,
            "pct": pct,
            "bar_width": bar_width,
            "sign_class": sign_class,
            "yesno_class": yesno_class,
            "center": _bounded_center,
            "indent": _bounded_indent,
            "format": _bounded_format,
        }
    )
    return env


class TemplateRenderError(TemplateError):
    """A non-syntax failure raised while evaluating a template."""


@contextmanager
def _prefixed_errors() -> Iterator[None]:
    """Re-raise any failure as a TemplateError whose message starts with ERROR_PREFIX.

    The guards nest (validate_and_render wraps render_compiled, which batch jobs
    also call directly), so an error that already carries the prefix passes
    through untouched instead of gaining a second one.
    """
    try:
        yield
    except Exception as exc:
        if isinstance(exc, TemplateError) and str(exc).startswith(ERROR_PREFIX):
            raise
        if isinstance(exc, TemplateSyntaxError):
            # Same type, so callers can still read .lineno; str() appends the line.
            raise TemplateSyntaxError(
                f"{ERROR_PREFIX} {exc.message}", exc.lineno, exc.name, exc.filename
            ) from exc
        raise TemplateRenderError(f"{ERROR_PREFIX} {exc}") from exc


def compile_template(html: str) -> Template:
    """Compile a template once so batch jobs don't re-parse it per row."""
    with _prefixed_errors():
        return get_env().from_string(html)


def render_compiled(template: Template, row: dict) -> str:
    with _prefixed_errors():
        return template.render(**row)


def validate_and_render(html: str, row: dict) -> str:
    return render_compiled(compile_template(html), row)


# Older name for validate_and_render, kept for existing callers.
render_row = validate_and_render


def render_text(template_str: str, row: dict) -> str:
    """Render a plain-text template (filename patterns and other non-HTML
    strings) without autoescaping — entity-escaped output would corrupt them."""
    with _prefixed_errors():
        return get_env(autoescape=False).from_string(template_str).render(**row)
