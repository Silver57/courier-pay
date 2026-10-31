"""Daily price adjustments: specific days paid at their own prices."""

from __future__ import annotations

from datetime import date

from conftest import make_rows

from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_REPEAT,
    CAT_TO_DOOR,
    TYPE_FT,
    TYPE_OUT,
    AlgarveDay,
    AlgarveRates,
    PayResult,
    calculate_pay,
    calculate_pay_algarve,
    prepare,
)
from reports import build_all_reports, build_all_reports_algarve

RATES = {
    (TYPE_FT, CAT_TO_DOOR): 1.10,
    (TYPE_FT, CAT_REPEAT): 0.40,
    (TYPE_FT, CAT_PUDO_FIRST): 2.20,
    (TYPE_FT, CAT_PUDO_NEXT): 0.55,
    (TYPE_OUT, CAT_TO_DOOR): 0.50,
    (TYPE_OUT, CAT_REPEAT): 0.20,
    (TYPE_OUT, CAT_PUDO_FIRST): 1.25,
    (TYPE_OUT, CAT_PUDO_NEXT): 0.30,
}
ALGARVE = AlgarveRates(up_to_limit=1.00, above_limit=0.80)  # limit 70

THU, FRI = date(2026, 8, 13), date(2026, 8, 14)
SUN, NEXT_SUN = date(2026, 8, 16), date(2026, 8, 23)


def deliveries(
    n: int, day: date, courier: str = "Ana Silva", **fields: str
) -> list[dict[str, str]]:
    return [{"Courier Name": courier, "Task Date": day.isoformat(), **fields}] * n


def totals(result: PayResult) -> dict[str, int]:
    return dict(
        zip(
            result.per_courier["courier_norm"].to_list(),
            result.per_courier["total_cents"].to_list(),
        )
    )


# --- standard mode ------------------------------------------------------------


def test_adjusted_day_uses_its_prices_and_other_days_keep_normal_rates() -> None:
    payable = prepare(make_rows(*deliveries(3, THU), *deliveries(2, FRI))).payable
    result = calculate_pay(payable, RATES, {FRI: {(TYPE_OUT, CAT_TO_DOOR): 0.70}})

    # 3 x 0.50 + 2 x 0.70
    assert totals(result) == {"Ana Silva": 290}
    assert result.reconciled is True
    report = build_all_reports(result)["Ana Silva"]
    assert (
        "Restantes entregas: 5\n"
        "- 2 pagas a 0.70€\n"
        "- 3 pagas a 0.50€\n"
        "\n"
        "Valor a Emitir no Recibo Verde e a Receber: 2.90€\n"
    ) in report


def test_prices_left_out_of_an_adjusted_day_use_normal_rates() -> None:
    payable = prepare(
        make_rows(
            *deliveries(1, FRI),
            *deliveries(1, FRI, **{"Delivery Type": "PUDO", "PUDO address": "Kiosk A"}),
            *deliveries(1, FRI, courier="Joao FT Costa"),
        )
    ).payable
    result = calculate_pay(payable, RATES, {FRI: {(TYPE_OUT, CAT_PUDO_FIRST): 2.00}})

    assert totals(result) == {"Ana Silva": 50 + 200, "Joao FT Costa": 110}
    assert result.detail["day_adjusted"].to_list() == [True, True, True]


def test_adjusted_prices_apply_per_courier_type() -> None:
    payable = prepare(
        make_rows(*deliveries(1, THU), *deliveries(1, THU, courier="Joao FT Costa"))
    ).payable
    result = calculate_pay(payable, RATES, {THU: {(TYPE_FT, CAT_TO_DOOR): 1.50}})
    assert totals(result) == {"Ana Silva": 50, "Joao FT Costa": 150}


def test_sunday_stays_already_paid_unless_adjusted() -> None:
    payable = prepare(make_rows(*deliveries(2, SUN), *deliveries(2, NEXT_SUN))).payable
    result = calculate_pay(payable, RATES, {NEXT_SUN: {(TYPE_OUT, CAT_TO_DOOR): 0.90}})

    row = result.per_courier.row(0, named=True)
    assert (row["n_to_door"], row["n_sundays"], row["n_total"]) == (2, 2, 4)
    assert row["total_cents"] == 180
    report = build_all_reports(result)["Ana Silva"]
    assert "Domingos: 2 já pagas.\nRestantes entregas: 2 pagas a 0.90€\n" in report


def test_adjusting_a_sunday_pays_left_out_prices_at_normal_rates() -> None:
    payable = prepare(make_rows(*deliveries(1, SUN))).payable
    result = calculate_pay(payable, RATES, {SUN: {(TYPE_OUT, CAT_PUDO_FIRST): 2.00}})

    row = result.per_courier.row(0, named=True)
    assert (row["n_to_door"], row["n_sundays"], row["total_cents"]) == (1, 0, 50)


def test_adjustments_for_dates_outside_the_file_change_nothing() -> None:
    payable = prepare(make_rows(*deliveries(3, THU))).payable
    plain = calculate_pay(payable, RATES)
    adjusted = calculate_pay(
        payable, RATES, {date(2026, 9, 1): {(TYPE_OUT, CAT_TO_DOOR): 9.99}}
    )
    assert totals(adjusted) == totals(plain) == {"Ana Silva": 150}
    assert adjusted.detail["day_adjusted"].to_list() == [False] * 3


# --- Algarve mode -------------------------------------------------------------


def split(result: PayResult) -> dict[str, tuple[int, int, int, int]]:
    """courier -> (up to limit, above limit, already-paid Sundays, cents)."""
    return {
        row["courier_norm"]: (
            row["n_up_to_limit"],
            row["n_above_limit"],
            row["n_sundays"],
            row["total_cents"],
        )
        for row in result.per_courier.iter_rows(named=True)
    }


def test_algarve_adjusted_day_has_its_own_limit_and_prices() -> None:
    payable = prepare(make_rows(*deliveries(85, THU), *deliveries(85, FRI))).payable
    result = calculate_pay_algarve(
        payable,
        ALGARVE,
        {FRI: AlgarveDay(up_to_limit=1.50, above_limit=1.00, daily_limit=50)},
    )

    # THU: 70 x 1.00 + 15 x 0.80 = 82.00 ; FRI: 50 x 1.50 + 35 x 1.00 = 110.00
    assert split(result) == {"Ana Silva": (120, 50, 0, 19200)}
    assert result.reconciled is True
    report = build_all_reports_algarve(result)["Ana Silva"]
    assert (
        "Restantes entregas: 170\n"
        "- 50 pagas a 1.50€\n"
        "- 105 pagas a 1€\n"
        "- 15 pagas a 0.80€\n"
    ) in report


def test_algarve_values_left_out_of_a_day_keep_sidebar_values() -> None:
    payable = prepare(make_rows(*deliveries(85, THU), *deliveries(85, FRI))).payable
    result = calculate_pay_algarve(
        payable,
        ALGARVE,
        {THU: AlgarveDay(daily_limit=80), FRI: AlgarveDay(above_limit=0.50)},
    )

    # THU: 80 x 1.00 + 5 x 0.80 = 84.00 ; FRI: 70 x 1.00 + 15 x 0.50 = 77.50
    assert split(result) == {"Ana Silva": (150, 20, 0, 16150)}


def test_algarve_adjusted_sunday_is_paid_in_tiers() -> None:
    payable = prepare(make_rows(*deliveries(80, SUN), *deliveries(5, NEXT_SUN))).payable
    result = calculate_pay_algarve(payable, ALGARVE, {SUN: AlgarveDay(up_to_limit=2.00)})

    # SUN adjusted: 70 x 2.00 + 10 x 0.80 ; NEXT_SUN stays already paid
    assert split(result) == {"Ana Silva": (70, 10, 5, 14800)}
    assert result.reconciled is True
