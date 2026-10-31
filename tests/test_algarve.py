"""Algarve mode: marginal daily tiers per courier, reset every day."""

from __future__ import annotations

import io
import zipfile
from datetime import date

from conftest import make_rows

from processing import (
    AlgarveDay,
    AlgarveRates,
    PayResult,
    calculate_pay_algarve,
    prepare,
)
from reports import (
    build_all_reports_algarve,
    reports_zip,
    summary_csv,
    summary_table_algarve,
)

RATES = AlgarveRates(up_to_limit=1.00, above_limit=0.80)  # limit defaults to 70


def pay(*rows: dict[str, str], rates: AlgarveRates = RATES) -> PayResult:
    return calculate_pay_algarve(prepare(make_rows(*rows)).payable, rates)


def split(result: PayResult) -> dict[str, tuple[int, int, int]]:
    """courier -> (deliveries up to limit, deliveries above limit, cents)."""
    return {
        row["courier_norm"]: (
            row["n_up_to_limit"],
            row["n_above_limit"],
            row["total_cents"],
        )
        for row in result.per_courier.iter_rows(named=True)
    }


# --- tiers --------------------------------------------------------------------


def test_deliveries_beyond_the_limit_get_the_second_rate() -> None:
    result = pay(*[{"Courier Name": "Ana Silva"}] * 85)

    # 70 x 1.00 + 15 x 0.80 = 82.00
    assert split(result) == {"Ana Silva": (70, 15, 8200)}
    assert result.grand_total_cents == 8200
    assert result.reconciled is True


def test_exactly_the_limit_has_nothing_above() -> None:
    result = pay(*[{"Courier Name": "Ana Silva"}] * 70)
    assert split(result) == {"Ana Silva": (70, 0, 7000)}


def test_daily_limit_is_configurable() -> None:
    rates = AlgarveRates(up_to_limit=1.00, above_limit=0.80, daily_limit=2)
    result = pay(*[{"Courier Name": "Ana Silva"}] * 5, rates=rates)
    assert split(result) == {"Ana Silva": (2, 3, 440)}


def test_count_resets_every_day() -> None:
    result = pay(
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-08-13"}] * 75,
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-08-14"}] * 60,
    )
    # Without the reset, 135 deliveries would put 65 above the limit.
    assert split(result) == {"Ana Silva": (130, 5, 13400)}
    assert result.per_courier["n_days"].to_list() == [2]


def test_count_is_per_courier() -> None:
    result = pay(
        *[{"Courier Name": "Ana Silva"}] * 50,
        *[{"Courier Name": "Rui Costa"}] * 50,
    )
    assert split(result) == {
        "Ana Silva": (50, 0, 5000),
        "Rui Costa": (50, 0, 5000),
    }


def test_categories_and_courier_type_do_not_matter() -> None:
    def mixed_day(courier: str) -> list[dict[str, str]]:
        return [
            *[{"Courier Name": courier}] * 40,
            *[
                {"Courier Name": courier, "Delivery Type": "PUDO", "PUDO address": "Kiosk A"}
            ]
            * 20,
            *[
                {"Courier Name": courier, "Delivery Type": "PUDO", "PUDO address": "Kiosk B"}
            ]
            * 15,
        ]

    result = pay(*mixed_day("Joao FT Costa"), *mixed_day("Rui Costa"))

    # 75 deliveries each, FT and OUT alike: 70 x 1.00 + 5 x 0.80 = 74.00
    assert split(result) == {
        "Joao FT Costa": (70, 5, 7400),
        "Rui Costa": (70, 5, 7400),
    }
    per_courier = result.per_courier
    assert (
        per_courier["n_up_to_limit"] + per_courier["n_above_limit"]
    ).to_list() == per_courier["n_total"].to_list()
    assert result.reconciled is True


def test_repeat_packages_are_paid_flat_and_sit_outside_the_tiers() -> None:
    rates = AlgarveRates(up_to_limit=1.00, above_limit=0.80, daily_limit=3, repeat=0.25)
    result = pay(
        *[{"Courier Name": "Ana Silva", "Detailed address": "Rua A 1"}] * 3,
        {"Courier Name": "Ana Silva", "Detailed address": "Rua B 2"},
        {"Courier Name": "Ana Silva", "Detailed address": "Rua C 3"},
        rates=rates,
    )
    # Rua A: 1 TO_DOOR + 2 repeats. Tiered rows are Rua A (1st), Rua B, Rua C:
    # exactly the limit, so nothing is above it. 3 x 1.00 + 2 x 0.25 = 3.50
    assert split(result) == {"Ana Silva": (3, 0, 350)}
    row = result.per_courier.row(0, named=True)
    assert (row["n_repeat"], row["n_total"]) == (2, 5)
    assert result.reconciled is True


def test_repeat_rate_defaults_to_zero_and_can_be_adjusted_per_day() -> None:
    rows = [{"Courier Name": "Ana Silva", "Detailed address": "Rua A 1"}] * 2
    assert split(pay(*rows)) == {"Ana Silva": (1, 0, 100)}

    payable = prepare(make_rows(*rows)).payable
    adjusted = calculate_pay_algarve(
        payable, RATES, {date(2026, 8, 13): AlgarveDay(repeat=0.30)}
    )
    assert split(adjusted) == {"Ana Silva": (1, 0, 130)}


def test_only_payable_deliveries_count_toward_the_limit() -> None:
    result = pay(
        *[{"Courier Name": "Ana Silva"}] * 70,
        *[{"Courier Name": "Ana Silva", "Task Status": "Failed"}] * 5,
        {"Courier Name": "Ana Silva"},
    )
    assert split(result) == {"Ana Silva": (70, 1, 7080)}


# --- reports and downloads ----------------------------------------------------


def test_sunday_deliveries_sit_outside_both_tiers() -> None:
    result = pay(
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-08-15"}] * 75,  # Saturday
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-08-16"}] * 80,  # Sunday
    )
    assert split(result) == {"Ana Silva": (70, 5, 7400)}
    row = result.per_courier.row(0, named=True)
    assert (row["n_sundays"], row["n_total"], row["n_days"]) == (80, 155, 2)
    assert result.grand_total_cents == 7400
    assert result.reconciled is True


def test_algarve_report_matches_expected_layout() -> None:
    # The June example: 619 deliveries, 177 on a Sunday. Both tiers at 1€
    # collapse into the single "pagas a 1€" line.
    rates = AlgarveRates(up_to_limit=1.00, above_limit=1.00)
    result = pay(
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-06-07"}] * 177,
        *[{"Courier Name": "Ana Silva", "Task Date": "2026-06-01"}] * 442,
        rates=rates,
    )
    report = build_all_reports_algarve(result, today=date(2026, 7, 3))["Ana Silva"]

    assert report == (
        "Saudações,\n"
        "\n"
        "Junto enviamos os dados para emissão de Recibo Verde dos serviços prestados em junho de 2026.\n"
        "\n"
        "Mês- julho de 2026\n"
        "\n"
        "Total de entregas: 619\n"
        "Domingos: 177 já pagas.\n"
        "Restantes entregas: 442 pagas a 1€\n"
        "\n"
        "Valor a Emitir no Recibo Verde e a Receber: 442.00€\n"
        "\n"
        "ENVIE SEMPRE O RECIBO VERDE E O IBAN NO MESMO EMAIL para financeiro@prismalda.pt - todos os pagamentos são processados no 8o dia útil de cada mês\n"
        "\n"
        "\n"
        "Dados para emissão – Prisma Citadino, Lda, NIF 519 411 889 Morada – Rua Vasco da Gama, 1 Famões\n"
        "\n"
        "Informamos que caso exista alguma divergência no calculo das entregas deve responder a este email para financeiro@happydistrict.pt com o screenshot do histórico da sua app com o 1o e ultimo dia do mês seleccionados e a opção resumo. Essa ou quaisquer outras questões sobre pagamentos deverão ser colocadas em resposta em 1 email apenas e de forma concreta e objectiva. O envio de mais do que 1 email desnecessário fará com que o seu caso passe para os últimos da lista a serem processados.\n"
        "\n"
        "Obrigado\n"
        "\n"
        "\n"
        "Vítor Pereira\n"
    )


def test_algarve_report_lists_both_tier_rates() -> None:
    result = pay(*[{"Courier Name": "Ana Silva"}] * 85)
    report = build_all_reports_algarve(result)["Ana Silva"]
    assert (
        "Restantes entregas: 85\n"
        "- 70 pagas a 1€\n"
        "- 15 pagas a 0.80€\n"
        "\n"
        "Valor a Emitir no Recibo Verde e a Receber: 82.00€\n"
    ) in report


def test_algarve_summary_csv_and_zip() -> None:
    result = pay(
        *[{"Courier Name": "Ana Silva"}] * 85,
        {"Courier Name": "Rui Costa"},
    )

    table = summary_table_algarve(result.per_courier, RATES.daily_limit)
    lines = summary_csv(table).decode().strip().splitlines()
    assert lines == [
        "Courier,Days,Up to 70/day,Above 70/day,Repeat,Sundays,Deliveries,Total (EUR)",
        "Ana Silva,1,70,15,0,0,85,82.00",
        "Rui Costa,1,1,0,0,0,1,1.00",
    ]

    all_reports = build_all_reports_algarve(result)
    with zipfile.ZipFile(io.BytesIO(reports_zip(all_reports))) as archive:
        assert sorted(archive.namelist()) == ["Ana Silva.txt", "Rui Costa.txt"]
        text = archive.read("Rui Costa.txt").decode()
        assert text.startswith("Saudações,\n")
        assert "para financeiro@prismalda.pt" in text
