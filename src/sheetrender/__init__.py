"""Spreadsheet + HTML template in, a PDF per row out."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("sheetrender")
except PackageNotFoundError:  # running from a source tree without install
    __version__ = "0.0.0"

from .column_keys import sanitize_columns
from .config import DEFAULT_EGRESS_HOSTS, RenderConfig, configure, get_config
from .filenames import dedupe_filenames, render_filename
from .grouping import (
    RESERVED_CONTEXT_KEYS,
    Group,
    ScanResult,
    detect_group_candidates,
    group_columns_missing,
    group_context,
    grouped_render_units,
    iter_groups,
    scan_groups,
)
from .html_sanitize import ALLOWED_EGRESS_HOSTS, sanitize_render_html
from .render import (
    BatchRenderer,
    PriorityGate,
    apply_pdf_metadata,
    browser_is_connected,
    inject_preview_watermark,
    inject_watermark,
    merge_pdfs,
    render_context,
    render_pdf,
    render_thumbnail,
    rendered_page_count,
    stamp_pdf_page_numbers,
    stamp_preview_watermark,
    start_browser,
    stop_browser,
    strip_author_page_rules,
    zip_files,
)
from .sheets import DEFAULT_MAX_CELLS, iter_rows, parse_csv, parse_xlsx
from .templating import (
    TemplateRenderError,
    compile_template,
    get_env,
    render_compiled,
    render_row,
    render_text,
    validate_and_render,
)

__all__ = [
    "__version__",
    "RenderConfig",
    "DEFAULT_EGRESS_HOSTS",
    "configure",
    "get_config",
    "start_browser",
    "stop_browser",
    "browser_is_connected",
    "render_pdf",
    "render_context",
    "BatchRenderer",
    "render_thumbnail",
    "rendered_page_count",
    "merge_pdfs",
    "zip_files",
    "stamp_pdf_page_numbers",
    "stamp_preview_watermark",
    "apply_pdf_metadata",
    "strip_author_page_rules",
    "inject_watermark",
    "inject_preview_watermark",
    "PriorityGate",
    "compile_template",
    "render_compiled",
    "render_row",
    "validate_and_render",
    "render_text",
    "TemplateRenderError",
    "get_env",
    "Group",
    "ScanResult",
    "iter_groups",
    "group_context",
    "group_columns_missing",
    "grouped_render_units",
    "scan_groups",
    "detect_group_candidates",
    "RESERVED_CONTEXT_KEYS",
    "render_filename",
    "dedupe_filenames",
    "sanitize_render_html",
    "ALLOWED_EGRESS_HOSTS",
    "parse_csv",
    "parse_xlsx",
    "iter_rows",
    "DEFAULT_MAX_CELLS",
    "sanitize_columns",
]
