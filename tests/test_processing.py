"""Business-rule tests: FT detection, PUDO first/next, dedupe, hygiene."""

from __future__ import annotations

from conftest import make_rows

from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_TO_DOOR,
    REASON_DUPLICATE,
    REASON_MISSING,
    TYPE_FT,
    TYPE_OUT,
    apply_courier_type_overrides,
    courier_roster,
    prepare,
)


def categories(df, courier: str | None = None) -> list[str]:
    rows = df.payable
    if courier is not None:
        rows = rows.filter(rows["courier_norm"] == courier)
    return rows["category"].to_list()


# --- courier type -----------------------------------------------------------


def test_ft_detection_is_case_sensitive() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "FT Express"},
            {"Courier Name": "Joao FT Costa"},
            {"Courier Name": "Soft Delivery"},  # lowercase "ft" must not match
            {"Courier Name": "ft lowercase"},
            {"Courier Name": "Gleiciane Silva"},
        )
    )
    assert prepared.payable["courier_type"].to_list() == [
        TYPE_FT,
        TYPE_FT,
        TYPE_OUT,
        TYPE_OUT,
        TYPE_OUT,
    ]


def test_real_sample_names_are_all_outsourced() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "Ricardo LourinhoQTK-LIS"},
            {"Courier Name": "QTK-LIS Eduardo Santos "},
            {"Courier Name": "Antonio Perreira QTK-LIS "},
        )
    )
    assert set(prepared.payable["courier_type"].to_list()) == {TYPE_OUT}


# --- name normalization (whitespace only; depot tag is kept) ----------------


def test_names_are_stripped_and_collapsed_keeping_depot_tag() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "  Antonio   Perreira  QTK-LIS  "},
            {"Courier Name": "Antonio Perreira QTK-LIS"},
        )
    )
    names = prepared.payable["courier_norm"].to_list()
    assert names == ["Antonio Perreira QTK-LIS"] * 2  # both group together


def test_depot_tag_stripping_is_available_as_a_toggle() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ricardo LourinhoQTK-LIS"}), strip_depot_tag=True
    )
    assert prepared.payable["courier_norm"].to_list() == ["Ricardo Lourinho"]


# --- PUDO first / next ------------------------------------------------------


def test_one_pudo_first_per_courier_date_point_group() -> None:
    prepared = prepare(
        make_rows(
            *[
                {"Delivery Type": "PUDO", "PUDO address": "Kiosk A"}
                for _ in range(3)
            ]
        )
    )
    assert sorted(categories(prepared)) == [
        CAT_PUDO_FIRST,
        CAT_PUDO_NEXT,
        CAT_PUDO_NEXT,
    ]


def test_different_date_or_point_starts_a_new_group() -> None:
    prepared = prepare(
        make_rows(
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk A"},
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk A", "Task Date": "2026-08-14"},
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk B"},
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk A", "Courier Name": "Other"},
        )
    )
    assert categories(prepared) == [CAT_PUDO_FIRST] * 4


def test_pudo_falls_back_to_detailed_address() -> None:
    prepared = prepare(
        make_rows(
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": "Rua X"},
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": "Rua X"},
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": "Rua Y"},
        )
    )
    assert categories(prepared) == [CAT_PUDO_FIRST, CAT_PUDO_NEXT, CAT_PUDO_FIRST]
    assert prepared.payable["address_used"].to_list() == ["Rua X", "Rua X", "Rua Y"]


def test_to_door_row_does_not_steal_pudo_first() -> None:
    prepared = prepare(
        make_rows(
            {"Delivery Type": "TO_DOOR", "Detailed address": "Shared Address"},
            {"Delivery Type": "PUDO", "PUDO address": "Shared Address"},
            {"Delivery Type": "PUDO", "PUDO address": "Shared Address"},
        )
    )
    assert categories(prepared) == [CAT_TO_DOOR, CAT_PUDO_FIRST, CAT_PUDO_NEXT]


def test_pudo_rows_using_the_address_fallback_are_not_flagged() -> None:
    """An empty "PUDO address" is normal — only a total lack of address counts."""
    prepared = prepare(
        make_rows(
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": "Rua X"},
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk", "Detailed address": ""},
        )
    )
    assert prepared.stats.pudo_rows_without_address == 0


def test_addressless_pudo_rows_group_together_and_are_counted() -> None:
    prepared = prepare(
        make_rows(
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": ""},
            {"Delivery Type": "PUDO", "PUDO address": "", "Detailed address": ""},
        )
    )
    assert categories(prepared) == [CAT_PUDO_FIRST, CAT_PUDO_NEXT]
    assert prepared.stats.pudo_rows_without_address == 2


# --- manual courier type overrides ------------------------------------------


def test_courier_roster_lists_each_courier_once_with_counts() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "Ana Silva"},
            {"Courier Name": "Ana Silva"},
            {"Courier Name": "FT Rui"},
        )
    )
    roster = courier_roster(prepared.payable)
    assert roster.to_dicts() == [
        {"courier_norm": "Ana Silva", "courier_type": TYPE_OUT, "n_total": 2},
        {"courier_norm": "FT Rui", "courier_type": TYPE_FT, "n_total": 1},
    ]


def test_undetected_full_timer_can_be_marked_ft() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ana Silva"}, {"Courier Name": "Rui Costa"})
    )
    assert set(prepared.payable["courier_type"].to_list()) == {TYPE_OUT}

    overridden = apply_courier_type_overrides(prepared.payable, ["Ana Silva"])
    types = dict(
        zip(
            overridden["courier_norm"].to_list(),
            overridden["courier_type"].to_list(),
        )
    )
    assert types == {"Ana Silva": TYPE_FT, "Rui Costa": TYPE_OUT}


def test_false_positive_can_be_marked_back_to_out() -> None:
    prepared = prepare(make_rows({"Courier Name": "FT Rui"}))
    assert prepared.payable["courier_type"].to_list() == [TYPE_FT]

    overridden = apply_courier_type_overrides(prepared.payable, [])
    assert overridden["courier_type"].to_list() == [TYPE_OUT]


def test_overrides_leave_other_columns_untouched() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "Ana Silva", "Delivery Type": "PUDO", "PUDO address": "K"},
            {"Courier Name": "Ana Silva", "Delivery Type": "PUDO", "PUDO address": "K"},
        )
    )
    overridden = apply_courier_type_overrides(prepared.payable, ["Ana Silva"])
    assert overridden["category"].to_list() == [CAT_PUDO_FIRST, CAT_PUDO_NEXT]
    assert overridden.drop("courier_type").equals(prepared.payable.drop("courier_type"))


def test_unknown_override_names_are_ignored() -> None:
    prepared = prepare(make_rows({"Courier Name": "Ana Silva"}))
    overridden = apply_courier_type_overrides(
        prepared.payable, ["Someone Not In This File"]
    )
    assert overridden["courier_type"].to_list() == [TYPE_OUT]


# --- hygiene ----------------------------------------------------------------


def test_dedupe_keeps_first_occurrence_and_counts_drops() -> None:
    prepared = prepare(
        make_rows(
            {"Waybill Number": "DUP", "Detailed address": "kept"},
            {"Waybill Number": "DUP", "Detailed address": "dropped"},
            {"Waybill Number": "DUP", "Detailed address": "dropped too"},
            {"Waybill Number": "UNIQUE"},
        )
    )
    assert prepared.stats.duplicates_removed == 2
    assert prepared.payable["Detailed address"].to_list() == ["kept", "Rua A 1"]
    assert prepared.excluded["exclusion_reason"].to_list() == [REASON_DUPLICATE] * 2


def test_null_waybills_are_not_collapsed_as_duplicates() -> None:
    prepared = prepare(
        make_rows({"Waybill Number": ""}, {"Waybill Number": ""})
    )
    assert prepared.stats.duplicates_removed == 0
    assert prepared.stats.payable_rows == 2


def test_rows_missing_courier_or_date_or_type_are_excluded() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": ""},
            {"Task Date": ""},
            {"Task Date": "not-a-date"},
            {"Delivery Type": ""},
            {},
        )
    )
    assert prepared.stats.missing_fields_excluded == 4
    assert prepared.stats.payable_rows == 1
    assert set(prepared.excluded["exclusion_reason"].to_list()) == {REASON_MISSING}


def test_non_delivered_statuses_are_broken_down() -> None:
    prepared = prepare(
        make_rows(
            {"Task Status": "Delivered"},
            {"Task Status": "Failed"},
            {"Task Status": "Failed"},
            {"Task Status": "In Transit"},
            {"Task Status": ""},
        )
    )
    assert prepared.stats.non_delivered_excluded == 4
    assert prepared.stats.status_breakdown == {
        "Failed": 2,
        "In Transit": 1,
        "<null>": 1,
    }
    assert prepared.stats.payable_rows == 1


def test_unknown_delivery_types_are_excluded_and_reported() -> None:
    prepared = prepare(
        make_rows(
            {"Delivery Type": "TO_DOOR"},
            {"Delivery Type": "PUDO", "PUDO address": "Kiosk"},
            {"Delivery Type": "LOCKER"},
            {"Delivery Type": "PICKUP"},
        )
    )
    assert prepared.stats.bad_delivery_type_excluded == 2
    assert prepared.stats.delivery_type_breakdown == {"LOCKER": 1, "PICKUP": 1}
    assert prepared.stats.payable_rows == 2


def test_stats_add_up() -> None:
    prepared = prepare(
        make_rows(
            {},
            {"Task Status": "Failed"},
            {"Waybill Number": "WB1"},  # duplicate of the first row
            {"Courier Name": ""},
            {"Delivery Type": "LOCKER"},
        )
    )
    stats = prepared.stats
    assert stats.total_rows == 5
    assert stats.payable_rows == 1
    assert stats.excluded_rows == 4
    assert prepared.excluded.height == 4
