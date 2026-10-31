"""Pay calculation and report/download rendering."""

from __future__ import annotations

import io
import zipfile
from datetime import date

from conftest import make_rows

from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_REPEAT,
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
    format_rate,
    month_label,
    reports_zip,
    safe_filename,
    summary_csv,
    summary_table,
)

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


def test_format_eur() -> None:
    assert format_eur(0) == "0.00"
    assert format_eur(5) == "0.05"
    assert format_eur(1250) == "12.50"
    assert format_eur(100000) == "1000.00"


def test_format_rate() -> None:
    assert format_rate(100) == "1"
    assert format_rate(80) == "0.80"
    assert format_rate(125) == "1.25"


def test_month_label() -> None:
    assert month_label(date(2026, 6, 1), date(2026, 6, 30)) == "junho de 2026"
    assert month_label(date(2026, 6, 29), date(2026, 7, 2)) == "junho a julho de 2026"
    assert (
        month_label(date(2025, 12, 30), date(2026, 1, 2))
        == "dezembro de 2025 a janeiro de 2026"
    )


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
    assert (
        "Valor a Emitir no Recibo Verde e a Receber: 1.10€"
        in build_all_reports(after)["Ana Silva"]
    )


def test_sunday_deliveries_are_counted_but_not_paid() -> None:
    prepared = prepare(
        make_rows(
            {"Courier Name": "Ana Silva", "Task Date": "2026-08-15"},  # Saturday
            {"Courier Name": "Ana Silva", "Task Date": "2026-08-16"},  # Sunday
            {
                "Courier Name": "Ana Silva",
                "Task Date": "2026-08-16",
                "Delivery Type": "PUDO",
                "PUDO address": "Kiosk A",
            },
        )
    )
    result = calculate_pay(prepared.payable, RATES)
    row = result.per_courier.row(0, named=True)

    assert (row["n_to_door"], row["n_repeat"], row["n_pudo_first"], row["n_pudo_next"]) == (
        1, 0, 0, 0,
    )
    assert (row["n_sundays"], row["n_total"], row["total_cents"]) == (2, 3, 50)
    assert result.grand_total_cents == 50
    assert result.reconciled is True


# The June example the emails were specified from: 619 deliveries, 177 of them
# on Sundays, the other 442 at 1€.
JUNE_EXAMPLE_ROWS = (
    *[{"Courier Name": "Ana Silva", "Task Date": "2026-06-07"}] * 177,  # Sunday
    *[{"Courier Name": "Ana Silva", "Task Date": "2026-06-01"}] * 442,
)


def test_report_matches_expected_layout() -> None:
    rates = {**RATES, (TYPE_OUT, CAT_TO_DOOR): 1.00}
    result = calculate_pay(prepare(make_rows(*JUNE_EXAMPLE_ROWS)).payable, rates)
    # June's services, report sent in July.
    report = build_all_reports(result, today=date(2026, 7, 3))["Ana Silva"]

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
        "ENVIE SEMPRE O RECIBO VERDE E O IBAN NO MESMO EMAIL para financeiro@happydistrict.pt - todos os pagamentos são processados no 8o dia útil de cada mês\n"
        "\n"
        "\n"
        "Dados para emissão - HappyDistrict, Lda, NIF 517 480 662 Morada - Rua Eça de Queiroz, N 2 Lisboa.\n"
        "\n"
        "Informamos que caso exista alguma divergência no calculo das entregas deve responder a este email para financeiro@happydistrict.pt com o screenshot do histórico da sua app com o 1o e ultimo dia do mês seleccionados e a opção resumo. Essa ou quaisquer outras questões sobre pagamentos deverão ser colocadas em resposta em 1 email apenas e de forma concreta e objectiva. O envio de mais do que 1 email desnecessário fará com que o seu caso passe para os últimos da lista a serem processados.\n"
        "\n"
        "Obrigado\n"
        "\n"
        "\n"
        "Vítor Pereira\n"
    )


def test_report_lists_each_rate_when_they_differ() -> None:
    prepared = prepare(
        make_rows(
            *[{"Courier Name": "Ana Silva"}] * 3,
            {"Courier Name": "Ana Silva", "Delivery Type": "PUDO", "PUDO address": "Kiosk A"},
            {"Courier Name": "Ana Silva", "Delivery Type": "PUDO", "PUDO address": "Kiosk A"},
            {"Courier Name": "Ana Silva", "Task Date": "2026-08-16"},  # Sunday
        )
    )
    result = calculate_pay(prepared.payable, RATES)
    report = build_all_reports(result)["Ana Silva"]

    # 3 x 0.50 + 1 x 1.25 + 1 x 0.30 = 3.05
    assert (
        "Total de entregas: 6\n"
        "Domingos: 1 já pagas.\n"
        "Restantes entregas: 5\n"
        "- 1 pagas a 1.25€\n"
        "- 3 pagas a 0.50€\n"
        "- 1 pagas a 0.30€\n"
        "\n"
        "Valor a Emitir no Recibo Verde e a Receber: 3.05€\n"
    ) in report
    assert "prestados em agosto de 2026." in report


def test_mes_line_is_the_current_month_not_the_delivery_period() -> None:
    result = calculate_pay(prepare(make_rows(*JUNE_EXAMPLE_ROWS)).payable, RATES)

    sent_in_january = build_all_reports(result, today=date(2027, 1, 10))["Ana Silva"]
    assert "prestados em junho de 2026.\n\nMês- janeiro de 2027\n" in sent_in_january

    by_default = build_all_reports(result)["Ana Silva"]
    assert f"Mês- {month_label(date.today(), date.today())}\n" in by_default


def test_zip_contains_one_txt_per_courier() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ana Silva"}, {"Courier Name": "Rui Costa"})
    )
    result = calculate_pay(prepared.payable, RATES)
    all_reports = build_all_reports(result)

    with zipfile.ZipFile(io.BytesIO(reports_zip(all_reports))) as archive:
        assert sorted(archive.namelist()) == ["Ana Silva.txt", "Rui Costa.txt"]
        assert archive.read("Ana Silva.txt").decode().startswith("Saudações,\n")


def test_summary_csv_has_header_and_one_row_per_courier() -> None:
    prepared = prepare(
        make_rows({"Courier Name": "Ana Silva"}, {"Courier Name": "Rui Costa"})
    )
    result = calculate_pay(prepared.payable, RATES)
    table = summary_table(result.per_courier)
    lines = summary_csv(table).decode().strip().splitlines()
    assert lines[0] == (
        "Courier,Type,TO_DOOR,Repeat,PUDO first,PUDO next,Sundays,Deliveries,Total (EUR)"
    )
    assert lines[1] == "Ana Silva,OUT,1,0,0,0,0,1,0.50"
    assert len(lines) == 3


def test_repeat_packages_are_paid_the_repeat_rate_of_the_courier_type() -> None:
    prepared = prepare(
        make_rows(
            *[{"Courier Name": "OUT One", "Detailed address": "Rua A 1"}] * 3,
            *[{"Courier Name": "FT Two", "Detailed address": "Rua A 1"}] * 2,
        )
    )
    result = calculate_pay(prepared.payable, RATES)

    # OUT One: 1 x 0.50 + 2 x 0.20 = 0.90 ; FT Two: 1 x 1.10 + 1 x 0.40 = 1.50
    rows = {r["courier_norm"]: r for r in result.per_courier.iter_rows(named=True)}
    assert (rows["OUT One"]["n_to_door"], rows["OUT One"]["n_repeat"]) == (1, 2)
    assert (rows["OUT One"]["total_cents"], rows["FT Two"]["total_cents"]) == (90, 150)
    assert result.reconciled is True

    report = build_all_reports(result)["OUT One"]
    assert (
        "Restantes entregas: 3\n"
        "- 1 pagas a 0.50€\n"
        "- 2 pagas a 0.20€\n"
    ) in report
