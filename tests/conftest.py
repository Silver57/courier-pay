"""Shared fixtures: synthetic frames in post-load shape, and real .xlsx bytes."""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import Any

import polars as pl
import pytest
import xlsxwriter

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from processing import REQUIRED_COLUMNS, SHEET_NAME  # noqa: E402

# Defaults mirror the real export: every cell is text, empties are "".
ROW_DEFAULTS: dict[str, str] = {
    "Waybill Number": "WB1",
    "Task Date": "2026-08-13",
    "Task Status": "Delivered",
    "Delivery Type": "TO_DOOR",
    "Courier Name": "Ana Silva QTK-LIS",
    "Zip Code": "1400-038",
    "Detailed address": "Rua A 1",
    "PUDO address": "",
}


def make_rows(*overrides: dict[str, Any]) -> pl.DataFrame:
    """Build a frame shaped exactly like load_epod() output."""
    rows = []
    for i, override in enumerate(overrides):
        row = dict(ROW_DEFAULTS)
        row["Waybill Number"] = f"WB{i + 1}"
        row.update(override)
        rows.append(row)
    return pl.DataFrame(
        rows, schema={name: pl.String for name in REQUIRED_COLUMNS}, orient="row"
    )


def make_workbook(
    header: list[str],
    data_rows: list[list[str]],
    *,
    sheet_name: str = SHEET_NAME,
    extra_sheets: tuple[str, ...] = ("Sheet1", "Sheet2"),
) -> bytes:
    """Build an in-memory .xlsx with the given header/rows on `sheet_name`."""
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    for name in extra_sheets:
        workbook.add_worksheet(name).write(0, 0, "pivot-ish noise")
    sheet = workbook.add_worksheet(sheet_name)
    for col, value in enumerate(header):
        sheet.write_string(0, col, value)
    for r, row in enumerate(data_rows, start=1):
        for c, value in enumerate(row):
            sheet.write_string(r, c, value)
    workbook.close()
    return buffer.getvalue()


@pytest.fixture
def realistic_workbook() -> bytes:
    """Workbook with duplicated headers and padding columns, like the real file."""
    header = [
        "Waybill Number",
        "LP No.",
        "Task Date",
        "Task Status",
        "Delivery Type",
        "Courier Name",
        "Contact Phone",
        "Contact Phone",  # duplicated, as in the real export
        "Zip Code",
        "Detailed address",
        "Seller Name",
        "PUDO address",
        "Seller Name",  # duplicated again, later position
    ]
    rows = [
        [
            "CNPRT001",
            "LP1",
            "2026-08-13",
            "Delivered",
            "TO_DOOR",
            "Diego Sol QTK-LIS",
            "first-phone",
            "second-phone",
            "1400-038",
            "Rua A 1",
            "first-seller",
            "",
            "second-seller",
        ],
        [
            "CNPRT002",
            "LP2",
            "2026-08-14",
            "Delivered",
            "PUDO",
            "  Gleiciane   Silva ",
            "first-phone",
            "second-phone",
            "2790-072",
            "Rua B 2",
            "first-seller",
            "Kiosk Central",
            "second-seller",
        ],
    ]
    return make_workbook(header, rows)
