"""Loader tests: sheet/column validation, duplicate headers, text preservation."""

from __future__ import annotations

import io
from datetime import datetime

import polars as pl
import pytest
import xlsxwriter
from conftest import make_workbook

from processing import (
    MissingColumnsError,
    REQUIRED_COLUMNS,
    SHEET_NAME,
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


# --- Algarve export: same data under different header names -------------------

# Header of the real Algarve export, verbatim (duplicates included).
ALGARVE_HEADER = (
    "Waybill Number|LP No.|Task Date|Plan Station|Route Number|Task Status|"
    "Order Source|Delivery Type|Driver|DSP Name|Stop Number|Sorting Bag Number|"
    "Destination Country|Destination Region|Destination City|Destination Zip Code|"
    "Address|Receiver to Latitude|Receiver to Longitude|Actual delivery Latitude|"
    "Actual delivery  Longitude|Delivery Gap Distance|Contact Name|Contact Phone|"
    "Contact Phone|Contact Email|Creation time|Inbound Time|Outbound time|"
    "Start Delivery Time|Delivery Time|Sign-in method|Sign Detail|Signature|"
    "Signed photos|Sign Name|Sign POD|Delivery Failure Time|Task Fail Reason|"
    "Delivery Fail Detail|Call Times|Latest Call Time|Gap Distance Warning|"
    "Locker/PUDO ID|Task Code|Route Scheme Name|Task Level|DSP Action Time|"
    "DSP Action|DSP Operator Name|Has New Task|OCR POD Fail Times|"
    "Dispatch Fail Reason|Return Remark|Bad Customer Tag|Bad ZipCode Tag|"
    "Physical Exchange Point|Delivery Mode|PUDO address|"
    "PUDO reroute failure - Precheck reason|SDSA Failure Reason(OOH)|Weight (g)|"
    "Long|Width|Height|Eligible Mailbox|Gap Distance Type|Call Duration (min)|"
    "Call Fail Reason|Call Type|OCR POD Fail Type|Seller Name|Point Code|"
    "Market Place Name|Station Group|PIN Code|hasPinCode|sellerInterception|"
    "sellerInterceptionStatus|Seller Name|Relate Zone|originalPlanTaskDate|"
    "Arriving at the wrong hub|Wrong hub name|hasCommercialAreaTag|"
    "podCheckManualResult|POD invalid reason|podCheckTimeManual|Last POD Operator"
).split("|")


def algarve_row(**values: str) -> list[str]:
    row = [f"noise-{i}" for i in range(len(ALGARVE_HEADER))]
    for name, value in values.items():
        row[ALGARVE_HEADER.index(name)] = value
    return row


def test_algarve_export_loads_under_canonical_names() -> None:
    data = make_workbook(
        ALGARVE_HEADER,
        [
            algarve_row(**{
                "Waybill Number": "PT001",
                "Task Date": "2026-08-13",
                "Task Status": "Delivered",
                "Delivery Type": "PUDO",
                "Driver": "Rui Faro",
                "DSP Name": "Some DSP Lda",
                "Destination Zip Code": "8000-001",
                "Address": "Rua do Algarve 1",
                "PUDO address": "Kiosk Faro",
            })
        ],
    )
    df = load_epod(data)

    assert set(df.columns) == set(REQUIRED_COLUMNS)
    row = df.row(0, named=True)
    assert row["Courier Name"] == "Rui Faro"  # Driver, not DSP Name
    assert row["Zip Code"] == "8000-001"
    assert row["Detailed address"] == "Rua do Algarve 1"
    assert row["PUDO address"] == "Kiosk Faro"


def test_canonical_header_wins_over_its_alias() -> None:
    header = [*REQUIRED_COLUMNS, "Driver", "Address"]
    resolved = resolve_columns(header)
    assert resolved["Courier Name"] == REQUIRED_COLUMNS.index("Courier Name")
    assert resolved["Detailed address"] == REQUIRED_COLUMNS.index("Detailed address")


def test_missing_column_error_names_the_accepted_alternatives() -> None:
    header = [c for c in REQUIRED_COLUMNS if c != "Courier Name"]
    with pytest.raises(MissingColumnsError) as excinfo:
        load_epod(make_workbook(header, []))
    assert excinfo.value.missing == ["Courier Name"]
    assert '"Courier Name" or "Driver"' in str(excinfo.value)


def test_real_excel_date_cells_parse_like_text_dates() -> None:
    buffer = io.BytesIO()
    workbook = xlsxwriter.Workbook(buffer, {"in_memory": True})
    sheet = workbook.add_worksheet(SHEET_NAME)
    date_format = workbook.add_format({"num_format": "yyyy-mm-dd"})
    for col, name in enumerate(REQUIRED_COLUMNS):
        sheet.write_string(0, col, name)
    values = ["WB1", "", "Delivered", "TO_DOOR", "Ana", "1400-038", "Rua A", ""]
    for col, value in enumerate(values):
        sheet.write_string(1, col, value)
    sheet.write_datetime(1, 1, datetime(2026, 8, 13, 14, 5), date_format)
    workbook.close()

    cleaned = clean(load_epod(buffer.getvalue()))
    assert str(cleaned["task_date"][0]) == "2026-08-13"


def test_unrecognised_date_text_still_fails_to_parse() -> None:
    header = list(REQUIRED_COLUMNS)
    rows = [
        ["WB1", "13/08/2026", "Delivered", "TO_DOOR", "Ana", "1400-038", "Rua A", ""],
        ["WB2", "2026-08-13x", "Delivered", "TO_DOOR", "Ana", "1400-038", "Rua A", ""],
    ]
    cleaned = clean(load_epod(make_workbook(header, rows)))
    assert cleaned["task_date"].to_list() == [None, None]
