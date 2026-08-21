from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass
from typing import Iterable

from sheetrender.sheets import iter_rows


RESERVED_CONTEXT_KEYS = {"items", "item_count", "group_key"}

_GROUP_NAME_HINT = re.compile(
    r"invoice|order|po|quote|statement|receipt|bill|customer|client|account|ref|number|no$|id$",
    re.IGNORECASE,
)


@dataclass
class Group:
    index: int
    key: str
    key_values: tuple[str, ...]
    member_indexes: list[int]
    rows: list[dict]
    first_row: dict | None = None

    def __post_init__(self) -> None:
        # Keep direct construction backwards-compatible while iter_groups
        # captures this before any line-item sorting can change rows[0].
        if self.first_row is None and self.rows:
            self.first_row = self.rows[0]


@dataclass
class ScanResult:
    groups: list[Group]
    n_groups: int
    min_group_size: int
    max_group_size: int
    avg_group_size: float
    doc_columns: list[str]
    item_columns: list[str]
    sample_group: dict | None
    largest_group_size: int


def _normalize_key_value(value) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _item_sort_key(value) -> tuple[int, float, str]:
    # Separate numeric and text buckets so Python never compares unlike types;
    # the final bucket also keeps missing spreadsheet cells at the end.
    if value is None:
        return (2, 0.0, "")
    try:
        return (0, float(value), "")
    except (TypeError, ValueError):
        return (1, 0.0, str(value))


def iter_groups(
    rows: Iterable[tuple[int, dict]],
    group_config: dict,
) -> list[Group]:
    group_by = group_config.get("group_by") or []
    if not group_by:
        raise ValueError("group_by must contain at least one column")

    groups_by_values: dict[tuple[str, ...], Group] = {}
    for member_index, row in rows:
        key_values = tuple(_normalize_key_value(row.get(key)) for key in group_by)
        group = groups_by_values.get(key_values)
        if group is None:
            # Dict insertion order makes the ordinal a property of the first
            # appearance, even when later members are non-contiguous.
            group = Group(
                index=len(groups_by_values),
                key=" – ".join(key_values),
                key_values=key_values,
                member_indexes=[],
                rows=[],
                first_row=row,
            )
            groups_by_values[key_values] = group
        group.member_indexes.append(member_index)
        group.rows.append(row)

    groups = list(groups_by_values.values())
    sort_items_by = group_config.get("sort_items_by")
    if sort_items_by:
        for group in groups:
            # Sort the row and source index as one unit so retries can still
            # identify the exact sheet members after item ordering changes.
            members = list(zip(group.member_indexes, group.rows, strict=True))
            members.sort(key=lambda member: _item_sort_key(member[1].get(sort_items_by)))
            group.member_indexes = [member_index for member_index, _ in members]
            group.rows = [row for _, row in members]
    return groups


def group_context(group: Group) -> dict:
    # Keeping every first-row key at the top level makes legacy templates total
    # under StrictUndefined while grouped templates gain the repeating rows.
    return {
        **(group.first_row or {}),
        "items": group.rows,
        "item_count": len(group.rows),
        "group_key": group.key,
    }


def group_columns_missing(columns: list[dict], group_config: dict) -> list[str]:
    """Return configured grouping/sort keys absent from a dataset schema."""
    available = {column.get("key") for column in columns}
    referenced = list(group_config.get("group_by") or [])
    sort_items_by = group_config.get("sort_items_by")
    if sort_items_by:
        referenced.append(sort_items_by)
    return list(dict.fromkeys(key for key in referenced if key not in available))


def grouped_render_units(
    dataset_path: str,
    columns: list[dict],
    selected_indexes: set[int] | None,
    group_config: dict,
) -> list[Group]:
    # Selection stays in dataset-index space. Every later consumer uses this
    # same pass so an immutable file and snapshot always reproduce ordinals.
    selected_rows = (
        (index, row)
        for index, row in enumerate(iter_rows(dataset_path, columns))
        if selected_indexes is None or index in selected_indexes
    )
    return iter_groups(selected_rows, group_config)


def _constant_within_group(group: Group, key: str) -> bool:
    first_value = _normalize_key_value((group.first_row or {}).get(key))
    return all(_normalize_key_value(row.get(key)) == first_value for row in group.rows)


def scan_groups(
    dataset_path: str,
    columns: list[dict],
    group_config: dict,
) -> ScanResult:
    # iter_groups consumes this iterator exactly once; groups must remain in
    # memory anyway because global equality can join distant sheet rows.
    groups = iter_groups(enumerate(iter_rows(dataset_path, columns)), group_config)
    sizes = [len(group.rows) for group in groups]
    n_groups = len(groups)
    min_group_size = min(sizes, default=0)
    max_group_size = max(sizes, default=0)
    avg_group_size = sum(sizes) / n_groups if n_groups else 0.0

    column_keys = [column["key"] for column in columns]
    doc_columns = [
        key
        for key in column_keys
        if all(_constant_within_group(group, key) for group in groups)
    ]
    doc_column_set = set(doc_columns)
    item_columns = [key for key in column_keys if key not in doc_column_set]

    sample_group = None
    if groups:
        # A median-sized group is more useful to a prompt than either a
        # singleton or an outlier invoice with hundreds of items.
        representative = sorted(groups, key=lambda group: (len(group.rows), group.index))[
            n_groups // 2
        ]
        sample_group = group_context(representative)
        sample_group["items"] = representative.rows[:20]

    return ScanResult(
        groups=groups,
        n_groups=n_groups,
        min_group_size=min_group_size,
        max_group_size=max_group_size,
        avg_group_size=avg_group_size,
        doc_columns=doc_columns,
        item_columns=item_columns,
        sample_group=sample_group,
        largest_group_size=max_group_size,
    )


def detect_group_candidates(
    dataset_path: str,
    columns: list[dict],
    row_count: int,
) -> dict:
    counters = {column["key"]: Counter() for column in columns}
    blank_counts = {column["key"]: 0 for column in columns}

    # Counting every column in this one pass stays bounded by the upload cell
    # cap and avoids rescanning a remote-sheet snapshot per candidate.
    observed_rows = 0
    for row in iter_rows(dataset_path, columns):
        observed_rows += 1
        for column in columns:
            key = column["key"]
            value = _normalize_key_value(row.get(key))
            counters[key][value] += 1
            if not value:
                blank_counts[key] += 1

    n_rows = observed_rows
    candidates = []
    recommendation_choices: list[tuple[float, int, str]] = []
    for position, column in enumerate(columns):
        key = column["key"]
        counts = counters[key]
        n_distinct = len(counts)
        if n_rows == 0:
            continue
        compression = n_rows / n_distinct if n_distinct else 0.0
        if not (
            1 < n_distinct < n_rows
            and compression >= 1.3
            and blank_counts[key] / n_rows <= 0.5
        ):
            continue

        sizes = list(counts.values())
        name_hint = bool(_GROUP_NAME_HINT.search(key))
        candidates.append(
            {
                "column": key,
                "original": str(column.get("original", key)),
                "n_groups": n_distinct,
                "min_group_size": min(sizes),
                "max_group_size": max(sizes),
                "avg_group_size": sum(sizes) / n_distinct,
                "name_hint": name_hint,
            }
        )

        near_boolean = n_distinct <= 3 and n_rows > 30
        if name_hint and 1.5 <= compression <= 100 and not near_boolean:
            recommendation_choices.append((compression, -position, key))

    recommended = None
    if recommendation_choices:
        recommended = max(recommendation_choices)[2]

    return {
        "row_count": int(row_count),
        "candidates": candidates,
        "recommended": recommended,
    }
