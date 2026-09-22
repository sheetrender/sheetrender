from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import sys
import traceback
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from sheetrender import (
    RenderConfig,
    TemplateRenderError,
    compile_template,
    configure,
    dedupe_filenames,
    detect_group_candidates,
    get_config,
    group_context,
    grouped_render_units,
    iter_rows,
    merge_pdfs,
    parse_csv,
    parse_xlsx,
    render_compiled,
    render_context,
    render_filename,
    render_pdf,
    render_text,
    render_thumbnail,
    start_browser,
    stop_browser,
    zip_files,
)

_PAGE_SIZES = ("A3", "A4", "A5", "Letter", "Legal", "Tabloid")


class CliError(Exception):
    pass


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _margin_mm(value: str) -> float:
    normalized = value.strip().lower()
    if normalized.endswith("mm"):
        normalized = normalized[:-2].strip()
    try:
        parsed = float(normalized)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            "must be a millimeter value such as 12mm"
        ) from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return parsed


def _key_value(value: str) -> tuple[str, str]:
    if "=" not in value:
        raise argparse.ArgumentTypeError("must have the form key=value")
    key, item_value = value.split("=", 1)
    if not key:
        raise argparse.ArgumentTypeError("key must not be empty")
    return key, item_value


def _add_page_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--page-size",
        type=str.upper,
        choices=[size.upper() for size in _PAGE_SIZES],
        default="A4",
        help="PDF page size (default: A4)",
    )
    parser.add_argument(
        "--landscape", action="store_true", help="use landscape orientation"
    )
    parser.add_argument(
        "--margin",
        type=_margin_mm,
        default=15.0,
        metavar="MM",
        help="uniform page margin in millimeters (default: 15mm)",
    )


def _add_day_first(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--day-first",
        action="store_true",
        help="read ambiguous numeric dates such as 03/04/2026 as day/month",
    )


def _add_hidden_debug(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--debug",
        action="store_true",
        default=argparse.SUPPRESS,
        help=argparse.SUPPRESS,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sheetrender",
        description="Render HTML templates with spreadsheet or JSON data.",
    )
    parser.add_argument(
        "--debug", action="store_true", help="show full tracebacks on errors"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    render_parser = subparsers.add_parser("render", help="render one PDF")
    render_parser.add_argument(
        "template", metavar="TEMPLATE.html", help="HTML template path"
    )
    render_parser.add_argument(
        "-o", "--output", required=True, metavar="FILE", help="output PDF"
    )
    render_parser.add_argument("--data", metavar="FILE", help="JSON object file")
    render_parser.add_argument(
        "--set",
        dest="values",
        action="append",
        type=_key_value,
        default=[],
        metavar="KEY=VALUE",
        help="add or override a string context value; may be repeated",
    )
    _add_page_arguments(render_parser)
    _add_day_first(render_parser)
    _add_hidden_debug(render_parser)

    batch_parser = subparsers.add_parser(
        "batch", help="render one PDF per row or group"
    )
    batch_parser.add_argument(
        "template", metavar="TEMPLATE.html", help="HTML template path"
    )
    batch_parser.add_argument(
        "data", metavar="DATA.(csv|xlsx)", help="CSV or XLSX data path"
    )
    batch_parser.add_argument(
        "-o", "--output", required=True, metavar="OUTDIR", help="output directory"
    )
    batch_parser.add_argument(
        "--filename", metavar="TEMPLATE", help="filename template"
    )
    grouping = batch_parser.add_mutually_exclusive_group()
    grouping.add_argument("--group-by", metavar="COLUMN", help="group rows by a column")
    grouping.add_argument(
        "--group", choices=("auto",), help="automatically choose a grouping column"
    )
    batch_parser.add_argument(
        "--merge", metavar="FILE", help="write all documents as one merged PDF"
    )
    batch_parser.add_argument(
        "--page-numbers",
        action="store_true",
        help="add page numbers to the merged PDF",
    )
    batch_parser.add_argument(
        "--zip", dest="zip_path", metavar="FILE", help="write a ZIP of rendered PDFs"
    )
    batch_parser.add_argument(
        "--watermark-html",
        metavar="HTML",
        help="inject a fixed HTML watermark into every document",
    )
    batch_parser.add_argument(
        "--concurrency",
        type=_positive_int,
        metavar="N",
        help=f"maximum concurrent renders (default: {RenderConfig().concurrency})",
    )
    _add_page_arguments(batch_parser)
    _add_day_first(batch_parser)
    _add_hidden_debug(batch_parser)

    thumbnail_parser = subparsers.add_parser(
        "thumbnail", help="render a first-page PNG"
    )
    thumbnail_parser.add_argument(
        "template", metavar="TEMPLATE.html", help="HTML template path"
    )
    thumbnail_parser.add_argument(
        "-o", "--output", required=True, metavar="FILE", help="output PNG"
    )
    thumbnail_parser.add_argument("--data", metavar="FILE", help="JSON object file")
    _add_page_arguments(thumbnail_parser)
    _add_day_first(thumbnail_parser)
    _add_hidden_debug(thumbnail_parser)

    inspect_parser = subparsers.add_parser(
        "inspect", help="inspect CSV or XLSX columns and samples"
    )
    inspect_parser.add_argument(
        "data", metavar="DATA.(csv|xlsx)", help="CSV or XLSX data path"
    )
    _add_hidden_debug(inspect_parser)

    return parser


def _page_settings(args: argparse.Namespace) -> dict[str, Any]:
    return {
        "page_size": args.page_size.lower(),
        "orientation": "landscape" if args.landscape else "portrait",
        "margins": {
            "top": args.margin,
            "right": args.margin,
            "bottom": args.margin,
            "left": args.margin,
        },
    }


def _read_text(path_value: str, description: str) -> str:
    path = Path(path_value)
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise CliError(f"Could not read {description} {path}: {exc}") from exc


def _read_json_object(path_value: str | None) -> dict[str, Any]:
    if path_value is None:
        return {}
    path = Path(path_value)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise CliError(
            f"Invalid JSON in {path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise CliError(f"Could not read JSON data {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise CliError(f"JSON data in {path} must be an object")
    return value


def _parse_dataset(path_value: str) -> dict[str, Any]:
    suffix = Path(path_value).suffix.lower()
    try:
        if suffix == ".csv":
            return parse_csv(path_value)
        if suffix == ".xlsx":
            return parse_xlsx(path_value)
    except (OSError, ValueError) as exc:
        raise CliError(f"Could not parse {path_value}: {exc}") from exc
    raise CliError(f"Unsupported data file {path_value}; expected .csv or .xlsx")


async def _render_command(args: argparse.Namespace) -> int:
    source = _read_text(args.template, "template")
    context = _read_json_object(args.data)
    context.update(args.values)
    html = render_compiled(
        compile_template(source, day_first=args.day_first), context
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        await start_browser()
        pdf = await render_pdf(html, _page_settings(args))
    finally:
        await stop_browser()

    await asyncio.to_thread(output.write_bytes, pdf)
    print(f"Rendered {output}")
    return 0


def _resolve_group_column(requested: str, columns: list[dict[str, Any]]) -> str:
    keys = {column["key"] for column in columns}
    if requested in keys:
        return requested
    original_matches = [
        column["key"] for column in columns if column.get("original") == requested
    ]
    if len(original_matches) == 1:
        return original_matches[0]
    available = ", ".join(column["key"] for column in columns)
    raise CliError(
        f"Unknown grouping column {requested!r}. Available columns: {available}"
    )


def _batch_contexts(
    args: argparse.Namespace,
    dataset: dict[str, Any],
) -> tuple[list[tuple[int, dict[str, Any]]], bool]:
    group_column = args.group_by
    if args.group == "auto":
        detection = detect_group_candidates(
            args.data, dataset["columns"], dataset["row_count"]
        )
        group_column = detection.get("recommended")
        if not group_column:
            raise CliError("Could not automatically detect a suitable grouping column")
        print(f"Auto group column: {group_column}")

    if group_column:
        group_column = _resolve_group_column(group_column, dataset["columns"])
        groups = grouped_render_units(
            args.data,
            dataset["columns"],
            None,
            {"group_by": [group_column]},
        )
        return [(group.index, group_context(group)) for group in groups], True

    rows = list(iter_rows(args.data, dataset["columns"]))
    return list(enumerate(rows)), False


def _check_filename_template(
    filename_template: str,
    units: list[tuple[int, dict[str, Any]]],
    *,
    day_first: bool = False,
) -> None:
    # render_filename falls back to row_N.pdf on any template error, which suits
    # a long-running service but would hide a typo like {{ custmer }} here.
    # Rendering the first document's name up front reports it instead.
    if not units:
        return
    try:
        render_text(filename_template, units[0][1], day_first=day_first)
    except Exception as exc:
        raise CliError(f"Invalid --filename template: {exc}") from exc


def _batch_filenames(
    units: list[tuple[int, dict[str, Any]]],
    filename_template: str | None,
    grouped: bool,
    *,
    day_first: bool = False,
) -> list[str]:
    if filename_template:
        _check_filename_template(filename_template, units, day_first=day_first)
    if filename_template or grouped:
        # With no template, a grouped document is named after its group key.
        names = [
            render_filename(
                filename_template, context, index, grouped=grouped, day_first=day_first
            )
            for index, context in units
        ]
    else:
        names = [f"row_{position + 1:04d}.pdf" for position in range(len(units))]
    return dedupe_filenames(names)


async def _render_batch(
    args: argparse.Namespace,
    template: Any,
    units: list[tuple[int, dict[str, Any]]],
    filenames: list[str],
    output_dir: Path,
) -> list[Path]:
    """Render every unit to output_dir and return the paths in unit order."""
    if args.concurrency is not None:
        configure(dataclasses.replace(get_config(), concurrency=args.concurrency))
    # Bounds how many rows hold rendered HTML in memory at once; the browser
    # side enforces the same number through its own gate.
    semaphore = asyncio.Semaphore(get_config().concurrency)
    rendered_paths: list[Path | None] = [None] * len(units)
    page_settings = _page_settings(args)

    try:
        await start_browser()
        async with render_context() as renderer:

            async def render_one(
                position: int,
                context: dict[str, Any],
                filename: str,
            ) -> None:
                async with semaphore:
                    html = render_compiled(template, context)
                    pdf = await renderer.render_pdf(
                        html,
                        page_settings,
                        watermark_html=args.watermark_html,
                    )
                    path = output_dir / filename
                    await asyncio.to_thread(path.write_bytes, pdf)
                    rendered_paths[position] = path
                    print(f"Rendered {filename}")

            async with asyncio.TaskGroup() as tasks:
                for position, ((_, context), filename) in enumerate(
                    zip(units, filenames, strict=True)
                ):
                    tasks.create_task(render_one(position, context, filename))
    finally:
        await stop_browser()

    return [path for path in rendered_paths if path is not None]


async def _write_merged(paths: list[Path], merged_path: Path, *, page_numbers: bool) -> None:
    merged_path.parent.mkdir(parents=True, exist_ok=True)
    merged = await asyncio.to_thread(
        merge_pdfs,
        [str(path) for path in paths],
        page_numbers=page_numbers,
    )
    await asyncio.to_thread(merged_path.write_bytes, merged)
    print(f"Merged {len(paths)} documents into {merged_path}")


async def _write_zip(paths: list[Path], zip_path: Path) -> None:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    archive = await asyncio.to_thread(
        zip_files,
        [(str(path), path.name) for path in paths],
    )
    await asyncio.to_thread(zip_path.write_bytes, archive)
    print(f"Archived {len(paths)} documents in {zip_path}")


async def _batch_command(args: argparse.Namespace) -> int:
    source = _read_text(args.template, "template")
    template = compile_template(source, day_first=args.day_first)
    dataset = _parse_dataset(args.data)
    units, grouped = _batch_contexts(args, dataset)
    filenames = _batch_filenames(
        units, args.filename, grouped, day_first=args.day_first
    )
    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    paths = await _render_batch(args, template, units, filenames, output_dir)
    if args.merge:
        await _write_merged(paths, Path(args.merge), page_numbers=args.page_numbers)
    if args.zip_path:
        await _write_zip(paths, Path(args.zip_path))

    print(f"Done: {len(paths)} documents in {output_dir}")
    return 0


async def _thumbnail_command(args: argparse.Namespace) -> int:
    source = _read_text(args.template, "template")
    context = _read_json_object(args.data)
    html = render_compiled(
        compile_template(source, day_first=args.day_first), context
    )
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)

    try:
        await start_browser()
        png = await render_thumbnail(html, _page_settings(args))
    finally:
        await stop_browser()

    await asyncio.to_thread(output.write_bytes, png)
    print(f"Rendered {output}")
    return 0


def _print_table(headers: tuple[str, ...], rows: list[tuple[str, ...]]) -> None:
    widths = [
        max([len(header), *(len(row[index]) for row in rows)])
        for index, header in enumerate(headers)
    ]
    print(
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers))
    )
    print("  ".join("-" * width for width in widths))
    for row in rows:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(row)))


def _inspect_command(args: argparse.Namespace) -> int:
    dataset = _parse_dataset(args.data)
    if Path(args.data).suffix.lower() == ".xlsx":
        print(f"Sheet: {dataset['sheet_name']}")
    print(f"Rows: {dataset['row_count']}")
    print("Columns:")
    _print_table(
        ("Original", "Key", "Type"),
        [
            (
                str(column["original"]),
                str(column["key"]),
                str(column["inferred_type"]),
            )
            for column in dataset["columns"]
        ],
    )
    print("Sample rows:")
    if dataset["sample_rows"]:
        for index, row in enumerate(dataset["sample_rows"], start=1):
            rendered = json.dumps(row, ensure_ascii=False, default=str)
            print(f"{index}: {rendered}")
    else:
        print("(none)")
    return 0


async def _main_async(args: argparse.Namespace) -> int:
    if args.command == "render":
        return await _render_command(args)
    if args.command == "batch":
        return await _batch_command(args)
    if args.command == "thumbnail":
        return await _thumbnail_command(args)
    if args.command == "inspect":
        return _inspect_command(args)
    raise CliError(f"Unknown command: {args.command}")


def _exception_tree(exc: BaseException):
    yield exc
    if isinstance(exc, BaseExceptionGroup):
        for nested in exc.exceptions:
            yield from _exception_tree(nested)


def _missing_chromium(exc: BaseException) -> bool:
    for item in _exception_tree(exc):
        message = str(item).lower()
        missing_executable = "executable" in message and any(
            phrase in message
            for phrase in ("doesn't exist", "does not exist", "not found")
        )
        missing_browser = "browser" in message and "not found" in message
        if "chromium" in message and (missing_executable or missing_browser):
            return True
    return False


def _template_error(exc: BaseException) -> BaseException | None:
    return next(
        (
            item
            for item in _exception_tree(exc)
            if isinstance(item, TemplateRenderError)
            or str(item).startswith("Template render error:")
        ),
        None,
    )


def _first_leaf(exc: BaseException) -> BaseException:
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        return _first_leaf(exc.exceptions[0])
    return exc


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "batch" and args.page_numbers and not args.merge:
        parser.error("--page-numbers only applies to the merged PDF; add --merge FILE")
    try:
        return asyncio.run(_main_async(args))
    except KeyboardInterrupt:
        print("Interrupted", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - this is the CLI's traceback boundary
        if args.debug:
            traceback.print_exc()
        template_error = _template_error(exc)
        if template_error is not None:
            if not args.debug:
                print(str(template_error).replace("\n", " "), file=sys.stderr)
            return 2
        if _missing_chromium(exc):
            print(
                "Chromium is not installed for Playwright. Run: playwright install chromium",
                file=sys.stderr,
            )
            return 3
        if not args.debug:
            message = str(_first_leaf(exc)).replace("\n", " ")
            print(f"Error: {message}", file=sys.stderr)
        return 2 if isinstance(exc, CliError) else 1


if __name__ == "__main__":
    raise SystemExit(main())
