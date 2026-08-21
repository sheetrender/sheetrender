from pathlib import Path

import pytest

from sheetrender.grouping import (
    detect_group_candidates,
    group_columns_missing,
    group_context,
    grouped_render_units,
    iter_groups,
    scan_groups,
)
from sheetrender.sheets import parse_csv


def _csv(tmp_path: Path, content: str) -> tuple[str, list[dict]]:
    path = tmp_path / "grouping.csv"
    path.write_text(content, encoding="utf-8")
    parsed = parse_csv(str(path))
    return str(path), parsed["columns"]


def test_non_contiguous_groups_unify_in_first_appearance_order():
    rows = [
        (0, {"invoice": "A", "item": "first"}),
        (1, {"invoice": "B", "item": "middle"}),
        (2, {"invoice": "A", "item": "last"}),
    ]

    groups = iter_groups(rows, {"group_by": ["invoice"]})

    assert [(group.index, group.key) for group in groups] == [(0, "A"), (1, "B")]
    assert groups[0].member_indexes == [0, 2]
    assert [row["item"] for row in groups[0].rows] == ["first", "last"]


def test_key_values_are_normalized_before_grouping():
    rows = [
        (0, {"key": None}),
        (1, {"key": "   "}),
        (2, {"key": 42}),
        (3, {"key": "42"}),
    ]

    groups = iter_groups(rows, {"group_by": ["key"]})

    assert [group.key_values for group in groups] == [("",), ("42",)]
    assert [group.member_indexes for group in groups] == [[0, 1], [2, 3]]


def test_multi_key_display_joins_values_with_en_dash():
    groups = iter_groups(
        [(0, {"account": "North", "invoice": " 001 "})],
        {"group_by": ["account", "invoice"]},
    )

    assert groups[0].key_values == ("North", "001")
    assert groups[0].key == "North – 001"


def test_sort_items_by_is_numeric_and_keeps_none_last():
    rows = [
        (4, {"invoice": "A", "position": 10}),
        (7, {"invoice": "A", "position": None}),
        (9, {"invoice": "A", "position": "2"}),
    ]

    group = iter_groups(
        rows,
        {"group_by": ["invoice"], "sort_items_by": "position"},
    )[0]

    assert [row["position"] for row in group.rows] == ["2", 10, None]
    assert group.member_indexes == [9, 4, 7]


def test_sort_items_by_preserves_first_appearance_row_for_document_context():
    first = {"invoice": "A", "position": 10, "recipient": "first@example.com"}
    group = iter_groups(
        [
            (4, first),
            (7, {"invoice": "A", "position": 2, "recipient": "sorted@example.com"}),
        ],
        {"group_by": ["invoice"], "sort_items_by": "position"},
    )[0]

    assert group.first_row is first
    assert [row["position"] for row in group.rows] == [2, 10]
    assert group_context(group)["recipient"] == "first@example.com"


def test_group_columns_missing_checks_group_and_sort_columns_in_order():
    columns = [{"key": "invoice"}, {"key": "item"}]

    assert group_columns_missing(
        columns,
        {
            "enabled": True,
            "group_by": ["customer", "invoice", "customer"],
            "sort_items_by": "position",
        },
    ) == ["customer", "position"]
    assert group_columns_missing(
        columns, {"enabled": True, "group_by": ["invoice"], "sort_items_by": "item"}
    ) == []


def test_group_context_keeps_first_row_columns_and_complete_items():
    groups = iter_groups(
        [
            (0, {"invoice": "A", "customer": "Acme", "amount": 2}),
            (1, {"invoice": "A", "customer": "Acme", "amount": 3}),
        ],
        {"group_by": ["invoice"]},
    )

    context = group_context(groups[0])

    assert context["invoice"] == "A"
    assert context["customer"] == "Acme"
    assert context["amount"] == 2
    assert context["items"] == groups[0].rows
    assert context["item_count"] == 2
    assert context["group_key"] == "A"


def test_grouped_render_units_is_deterministic_for_selected_rows(tmp_path):
    path, columns = _csv(
        tmp_path,
        "invoice,item\nA,Pen\nB,Tape\nA,Paper\nC,Clip\n",
    )
    config = {"group_by": ["invoice"]}

    first = grouped_render_units(path, columns, {0, 2, 3}, config)
    second = grouped_render_units(path, columns, {0, 2, 3}, config)

    assert first == second
    assert [(group.index, group.key, group.member_indexes) for group in first] == [
        (0, "A", [0, 2]),
        (1, "C", [3]),
    ]


def test_scan_groups_classifies_columns_and_selects_median_sample(tmp_path):
    path, columns = _csv(
        tmp_path,
        "invoice,customer,item,amount\n"
        "A,Acme,Pen,2\n"
        "A,Acme,Paper,3\n"
        "B,Beta,Pen,4\n"
        "C,Core,Binder,5\n"
        "C,Core,Pen,6\n"
        "C,Core,Clip,7\n",
    )

    result = scan_groups(path, columns, {"group_by": ["invoice"]})

    assert result.n_groups == 3
    assert result.min_group_size == 1
    assert result.max_group_size == 3
    assert result.avg_group_size == pytest.approx(2.0)
    assert result.doc_columns == ["invoice", "customer"]
    assert result.item_columns == ["item", "amount"]
    assert result.sample_group["group_key"] == "A"
    assert result.sample_group["item_count"] == 2
    assert len(result.sample_group["items"]) == 2
    assert result.largest_group_size == 3


def test_detect_candidates_filters_bad_columns_and_recommends_invoice(tmp_path):
    lines = ["invoice_number,unique_id,constant,mostly_blank,status"]
    for index in range(40):
        mostly_blank = "" if index < 25 else f"batch-{index % 5}"
        lines.append(
            f"INV-{index // 5},{index},same,{mostly_blank},{'open' if index % 2 else 'closed'}"
        )
    path, columns = _csv(tmp_path, "\n".join(lines) + "\n")

    result = detect_group_candidates(path, columns, 40)

    candidates = {candidate["column"]: candidate for candidate in result["candidates"]}
    assert "unique_id" not in candidates
    assert "constant" not in candidates
    assert "mostly_blank" not in candidates
    assert candidates["status"]["n_groups"] == 2
    assert candidates["invoice_number"]["name_hint"] is True
    assert result["recommended"] == "invoice_number"


def test_near_boolean_candidate_is_never_recommended(tmp_path):
    lines = ["status,unique"]
    for index in range(40):
        lines.append(f"{'yes' if index % 2 else 'no'},{index}")
    path, columns = _csv(tmp_path, "\n".join(lines) + "\n")

    result = detect_group_candidates(path, columns, 40)

    assert [candidate["column"] for candidate in result["candidates"]] == ["status"]
    assert result["recommended"] is None


def test_detect_candidates_returns_none_for_degenerate_columns(tmp_path):
    path, columns = _csv(
        tmp_path,
        "unique,constant\n1,x\n2,x\n3,x\n4,x\n",
    )

    result = detect_group_candidates(path, columns, 4)

    assert result == {"row_count": 4, "candidates": [], "recommended": None}
