"""Render-boundary HTML sanitization.

Every template passes through here before rendering, so it cannot execute
script, embed frames, or reference non-allowlisted remote resources. The
second layer — the Playwright egress allowlist in render.py — catches anything
markup sanitization can't see, such as CSS url()/@import fetches.
"""
from __future__ import annotations

from urllib.parse import urlparse

import nh3

from sheetrender.config import DEFAULT_EGRESS_HOSTS

# The default egress allowlist permits Google Fonts because templates commonly
# use them; nothing else is allowed by default. Same object as
# config.DEFAULT_EGRESS_HOSTS, under the name this module has always exported.
ALLOWED_EGRESS_HOSTS = DEFAULT_EGRESS_HOSTS

_ALLOWED_TAGS = {
    # document shell
    "html", "head", "body", "title", "meta", "style", "link",
    # sections & text
    "header", "footer", "main", "section", "article", "aside", "nav", "address",
    "div", "p", "span", "h1", "h2", "h3", "h4", "h5", "h6",
    "blockquote", "pre", "code", "hr", "br", "wbr",
    "strong", "em", "b", "i", "u", "s", "small", "sub", "sup", "mark",
    "abbr", "cite", "q", "dfn", "kbd", "samp", "var", "time", "figure", "figcaption",
    # lists
    "ul", "ol", "li", "dl", "dt", "dd",
    # tables
    "table", "caption", "colgroup", "col", "thead", "tbody", "tfoot", "tr", "th", "td",
    # media (data: URIs / allowlisted hosts only, enforced below)
    "img", "a",
    # inline svg for charts and decorative marks
    "svg", "g", "path", "rect", "circle", "ellipse", "line", "polyline", "polygon",
    "text", "tspan", "defs", "linearGradient", "radialGradient", "stop", "clipPath", "use",
}

_GLOBAL_ATTRIBUTES = {
    "style", "class", "id", "dir", "lang", "title", "role",
    "align", "valign", "width", "height",
}

_SVG_PRESENTATION_ATTRIBUTES = {
    "d", "viewBox", "preserveAspectRatio", "xmlns", "fill", "stroke", "stroke-width",
    "stroke-linecap", "stroke-linejoin", "stroke-dasharray", "stroke-dashoffset",
    "fill-opacity", "stroke-opacity", "opacity", "transform", "x", "y", "x1", "y1",
    "x2", "y2", "cx", "cy", "r", "rx", "ry", "points", "offset", "stop-color",
    "stop-opacity", "gradientUnits", "gradientTransform", "text-anchor",
    "dominant-baseline", "font-size", "font-family", "font-weight", "clip-path",
}

_ALLOWED_ATTRIBUTES = {
    "*": _GLOBAL_ATTRIBUTES | _SVG_PRESENTATION_ATTRIBUTES,
    "meta": {"charset", "name", "content"},
    "link": {"href", "rel", "type", "media", "crossorigin"},
    "img": {"src", "alt"},
    "a": {"href"},
    "table": {"cellpadding", "cellspacing", "border"},
    "th": {"colspan", "rowspan", "scope"},
    "td": {"colspan", "rowspan"},
    "col": {"span"},
    "use": {"href"},
}


def _allowed_url(value: str, allowed_egress_hosts: frozenset[str]) -> bool:
    lowered = value.strip().lower()
    if lowered.startswith("data:") or lowered.startswith("#"):
        return True
    if lowered.startswith(("http://", "https://", "//")):
        host = (urlparse(value.strip()).hostname or "").lower()
        return host in allowed_egress_hosts
    # Relative URLs would resolve against about:blank — nothing to fetch, but
    # there's also no legitimate use in a self-contained template.
    return False


def _attribute_filter(
    tag: str,
    attribute: str,
    value: str,
    allowed_egress_hosts: frozenset[str],
) -> str | None:
    if attribute in {"src", "href"}:
        return value if _allowed_url(value, allowed_egress_hosts) else None
    return value


def sanitize_render_html(
    html: str, *, allowed_egress_hosts: frozenset[str] | None = None
) -> str:
    """Sanitize a fully rendered document just before it reaches Chromium."""
    if allowed_egress_hosts is None:
        allowed_egress_hosts = ALLOWED_EGRESS_HOSTS
    cleaned = nh3.clean(
        html,
        tags=_ALLOWED_TAGS,
        attributes=_ALLOWED_ATTRIBUTES,
        attribute_filter=lambda tag, attribute, value: _attribute_filter(
            tag, attribute, value, allowed_egress_hosts
        ),
        url_schemes={"http", "https", "data"},
        link_rel=None,
        # script content is dropped entirely; style must stay (it's allowlisted
        # as a tag above, so it may not also appear in clean_content_tags).
        clean_content_tags={"script"},
    )
    # nh3 drops the doctype; without it Chromium renders in quirks mode and
    # layout shifts subtly.
    return f"<!DOCTYPE html>\n{cleaned}"
