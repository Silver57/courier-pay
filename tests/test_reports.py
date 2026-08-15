"""Pay calculation and report/download rendering."""

from __future__ import annotations

import io
import zipfile

from conftest import make_rows

from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_TO_DOOR,
    TYPE_FT,
    TYPE_OUT,
    apply_courier_type_overrides,
    calculate_pay,
    prepare,
)
from reports import (
    build_all_reports,
    format_eur,
    reports_zip,
    safe_filename,
    summary_csv,
)

RATES = {
    (TYPE_FT, CAT_TO_DOOR): 1.10,
    (TYPE_FT, CAT_PUDO_FIRST): 2.20,
    (TYPE_FT, CAT_PUDO_NEXT): 0.55,
    (TYPE_OUT, CAT_TO_DOOR): 0.50,
    (TYPE_OUT, CAT_PUDO_FIRST): 1.25,
    (TYPE_OUT, CAT_PUDO_NEXT): 0.30,
}


def test_format_eur() -> None:
    assert format_eur(0) == "0.00"
    assert format_eur(5) == "0.05"
    assert format_eur(1250) == "12.50"
    assert format_eur(100000) == "1000.00"


def test_safe_filename() -> None:
    assert safe_filename("Antonio Perreira QTK-LIS") == "Antonio Perreira QTK-LIS"
    assert safe_filename("Ana/Maria: courier") == "Ana_Maria_ courier"
    assert safe_filename("///") == "courier"


def test_pay_totals_and_reconciliation() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "OUT One"},
            {"Courier Name": "OUT One"},
            {
                "Courier Name": "OUT One",
                "Delivery Type": "PUDO",
                "PUDO address": "Kiosk A",
            },
            {
                "Courier Name": "OUT One",
                "Delivery Type": "PUDO",
                "PUDO address": "Kiosk A",
            },
            {"Courier Name": "FT Two"},
        )
    )
    result = calculate_pay(prepared.payable, RATES)

    # OUT One: 2 x 0.50 + 1 x 1.25 + 1 x 0.30 = 2.55 ; FT Two: 1 x 1.10
    totals = dict(
        zip(
            result.per_courier["courier_norm"].to_list(),
            result.per_courier["total_cents"].to_list(),
        )
    )
    assert totals == {"OUT One": 255, "FT Two": 110}
    assert result.grand_total_cents == 365
    assert result.reconciled is True


def test_rate_change_only_affects_matching_rows() -> None:
    prepared = prepare(make_rows({"Courier Name": "OUT One"}))
    base = calculate_pay(prepared.payable, RATES).grand_total_cents
    bumped = calculate_pay(
        prepared.payable, {**RATES, (TYPE_OUT, CAT_TO_DOOR): 1.00}
    ).grand_total_cents
    assert (base, bumped) == (50, 100)


def test_marking_a_courier_ft_repays_at_ft_rates() -> None:
    prepared = prepare(make_rows({"Courier Name": "Ana Silva"}))
    before = calculate_pay(prepared.payable, RATES)
    assert before.grand_total_cents == 50  # OUT TO_DOOR

    payable = apply_courier_type_overrides(prepared.payable, ["Ana Silva"])
    after = calculate_pay(payable, RATES)
    assert after.grand_total_cents == 110  # FT TO_DOOR
    assert after.reconciled is True
    assert build_all_reports(after, RATES)["Ana Silva"].startswith(
        "Courier: Ana Silva (FT)"
    )


def test_report_matches_expected_layout() -> None:
    prepared = prepare(
        make_rows(
            {
                "Courier Name": "Ana Silva",
                "Waybill Number": "WB-B",
                "Detailed address": "Rua B",
                "Task Date": "2026-08-14",
            },
            {
                "Courier Name": "Ana Silva",
                "Waybill Number": "WB-A",
                "Detailed address": "Rua A",
                "Task Date": "2026-08-13",
            },
            {
                "Courier Name": "Ana Silva",
                "Waybill Number": "WB-P",
                "Delivery Type": "PUDO",
                "PUDO address": "Kiosk Z",
                "Task Date": "2026-08-13",
            },
        )
    )
    result = calculate_pay(prepared.payable, RATES)
    report = build_all_reports(result, RATES)["Ana Silva"]

    assert report == (
        "Courier: Ana Silva (OUT)\n"
        "Period: 2026-08-13 — 2026-08-14\n"
        "------------------------------------------\n"
        "TO_DOOR:     2 x 0.50 = 1.00\n"
        "PUDO first:  1 x 1.25 = 1.25\n"
        "PUDO next:   0 x 0.30 = 0.00\n"
        "TOTAL: 2.25 EUR\n"
        "------------------------------------------\n"
    )


def test_zip_contains_one_txt_per_courier() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ana Silva"}, {"Courier Name": "Rui Costa"})
    )
    result = calculate_pay(prepared.payable, RATES)
    all_reports = build_all_reports(result, RATES)

    with zipfile.ZipFile(io.BytesIO(reports_zip(all_reports))) as archive:
        assert sorted(archive.namelist()) == ["Ana Silva.txt", "Rui Costa.txt"]
        assert archive.read("Ana Silva.txt").decode().startswith(
            "Courier: Ana Silva (OUT)"
        )


def test_summary_csv_has_header_and_one_row_per_courier() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ana Silva"}, {"Courier Name": "Rui Costa"})
    )
    result = calculate_pay(prepared.payable, RATES)
    lines = summary_csv(result.per_courier).decode().strip().splitlines()
    assert lines[0] == (
        "Courier,Type,TO_DOOR,PUDO first,PUDO next,Deliveries,Total (EUR)"
    )
    assert lines[1] == "Ana Silva,OUT,1,0,0,1,0.50"
    assert len(lines) == 3
