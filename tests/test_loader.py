"""Loader tests: sheet/column validation, duplicate headers, text preservation."""

from __future__ import annotations

import polars as pl
import pytest
from conftest import make_workbook

from processing import (
    MissingColumnsError,
    REQUIRED_COLUMNS,
    SheetNotFoundError,
    clean,
    load_epod,
    resolve_columns,
)


def test_missing_sheet_lists_available_sheets() -> None:
    data = make_workbook(list(REQUIRED_COLUMNS), [], sheet_name="Some Other Sheet")
    with pytest.raises(SheetNotFoundError) as excinfo:
        load_epod(data)
    message = str(excinfo.value)
    assert "EPOD_TASK_LIST_V2" in message
    assert "Some Other Sheet" in message


def test_missing_columns_lists_all_of_them() -> None:
    header = [c for c in REQUIRED_COLUMNS if c not in ("Zip Code", "PUDO address")]
    data = make_workbook(header, [])
    with pytest.raises(MissingColumnsError) as excinfo:
        load_epod(data)
    assert set(excinfo.value.missing) == {"Zip Code", "PUDO address"}
    assert "Zip Code" in str(excinfo.value)
    assert "PUDO address" in str(excinfo.value)


def test_resolve_columns_first_occurrence_wins() -> None:
    header = ["Contact Phone", *REQUIRED_COLUMNS, "Contact Phone", "Courier Name"]
    resolved = resolve_columns(header)
    # "Courier Name" appears at index 5 (inside REQUIRED_COLUMNS) and again at
    # the end; the first one must win.
    assert resolved["Courier Name"] == 1 + REQUIRED_COLUMNS.index("Courier Name")
    assert set(resolved) == set(REQUIRED_COLUMNS)


def test_duplicate_headers_are_tolerated(realistic_workbook: bytes) -> None:
    df = load_epod(realistic_workbook)
    assert df.columns == sorted(
        REQUIRED_COLUMNS,
        key=lambda c: [
            "Waybill Number",
            "Task Date",
            "Task Status",
            "Delivery Type",
            "Courier Name",
            "Zip Code",
            "Detailed address",
            "PUDO address",
        ].index(c),
    )
    assert df.height == 2
    assert df["Waybill Number"].to_list() == ["CNPRT001", "CNPRT002"]


def test_values_stay_text_and_empties_become_null(realistic_workbook: bytes) -> None:
    df = load_epod(realistic_workbook)
    assert df.schema["Zip Code"] == pl.String
    assert df["Zip Code"].to_list() == ["1400-038", "2790-072"]
    assert df["PUDO address"].to_list() == ["", "Kiosk Central"]

    cleaned = clean(df)
    assert cleaned["PUDO address"].to_list() == [None, "Kiosk Central"]
    assert str(cleaned["task_date"][0]) == "2026-08-13"


def test_whitespace_only_cell_becomes_null() -> None:
    header = list(REQUIRED_COLUMNS)
    row = ["WB1", "2026-08-13", "Delivered", "TO_DOOR", "   ", "1400-038", "Rua A", ""]
    cleaned = clean(load_epod(make_workbook(header, [row])))
    assert cleaned["Courier Name"][0] is None
