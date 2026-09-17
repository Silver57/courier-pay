"""Text report building and download payloads for courier-pay.

No Streamlit imports — everything returns plain strings or bytes.
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

import polars as pl

from processing import PayResult


@dataclass(frozen=True)
class Issuer:
    """Company the courier issues the Recibo Verde to."""

    email: str  # where the receipt and IBAN must be sent
    details: str  # the "Dados para emissão" line, verbatim


ISSUER_STANDARD = Issuer(
    email="financeiro@happydistrict.pt",
    details=(
        "Dados para emissão - HappyDistrict, Lda, NIF 517 480 662 "
        "Morada - Rua Eça de Queiroz, N 2 Lisboa."
    ),
)
ISSUER_ALGARVE = Issuer(
    email="financeiro@prismalda.pt",
    details=(
        "Dados para emissão – Prisma Citadino, Lda, NIF 519 411 889 "
        "Morada – Rua Vasco da Gama, 1 Famões"
    ),
)

REPORT_TEMPLATE = (
    "Saudações,\n"
    "\n"
    "Junto enviamos os dados para emissão de Recibo Verde dos serviços "
    "prestados em {month}.\n"
    "\n"
    "Mês- {current_month}\n"
    "\n"
    "Total de entregas: {n_total}\n"
    "Domingos: {n_sundays} já pagas.\n"
    "{paid_lines}\n"
    "\n"
    "Valor a Emitir no Recibo Verde e a Receber: {total}€\n"
    "\n"
    "ENVIE SEMPRE O RECIBO VERDE E O IBAN NO MESMO EMAIL para {issuer_email} "
    "- todos os pagamentos são processados no 8o dia útil de cada mês\n"
    "\n"
    "\n"
    "{issuer_details}\n"
    "\n"
    "Informamos que caso exista alguma divergência no calculo das entregas deve "
    "responder a este email para financeiro@happydistrict.pt com o screenshot do "
    "histórico da sua app com o 1o e ultimo dia do mês seleccionados e a opção "
    "resumo. Essa ou quaisquer outras questões sobre pagamentos deverão ser "
    "colocadas em resposta em 1 email apenas e de forma concreta e objectiva. "
    "O envio de mais do que 1 email desnecessário fará com que o seu caso passe "
    "para os últimos da lista a serem processados.\n"
    "\n"
    "Obrigado\n"
    "\n"
    "\n"
    "Vítor Pereira\n"
)

PT_MONTHS: tuple[str, ...] = (
    "janeiro",
    "fevereiro",
    "março",
    "abril",
    "maio",
    "junho",
    "julho",
    "agosto",
    "setembro",
    "outubro",
    "novembro",
    "dezembro",
)

_FILENAME_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-"
)


def format_eur(cents: int) -> str:
    """Format integer cents as "12.50" (negative-safe, no float rounding)."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


def format_rate(cents: int) -> str:
    """Per-delivery rate: whole euros without decimals ("1"), else "0.80"."""
    return format_eur(cents).removesuffix(".00")


def month_label(d0: date, d1: date) -> str:
    """Portuguese month of the period: "junho de 2026", or a range such as
    "junho a julho de 2026" when deliveries cross a month boundary."""
    end = f"{PT_MONTHS[d1.month - 1]} de {d1.year}"
    if (d0.year, d0.month) == (d1.year, d1.month):
        return end
    start = PT_MONTHS[d0.month - 1]
    if d0.year != d1.year:
        start = f"{start} de {d0.year}"
    return f"{start} a {end}"


def paid_lines(n_paid: int, counts_by_rate: Mapping[int, int]) -> str:
    """The "Restantes entregas" line: one line when every paid delivery shares
    a rate, otherwise the total followed by one line per rate (highest first)."""
    groups = sorted(
        ((cents, n) for cents, n in counts_by_rate.items() if n), reverse=True
    )
    if len(groups) == 1:
        cents, n = groups[0]
        return f"Restantes entregas: {n} pagas a {format_rate(cents)}€"
    lines = [f"Restantes entregas: {n_paid}"]
    lines += [f"- {n} pagas a {format_rate(cents)}€" for cents, n in groups]
    return "\n".join(lines)


def safe_filename(name: str) -> str:
    """Courier name -> filesystem-safe stem for the per-courier .txt."""
    cleaned = "".join(ch if ch in _FILENAME_SAFE else "_" for ch in name)
    while "__" in cleaned:
        cleaned = cleaned.replace("__", "_")
    return cleaned.strip(" ._") or "courier"


def build_periods(detail: pl.DataFrame) -> pl.DataFrame:
    """One row per courier with the first and last delivery date."""
    return detail.group_by("courier_norm").agg(
        pl.col("task_date").min().alias("d0"),
        pl.col("task_date").max().alias("d1"),
    )


def _render_report(
    row: Mapping[str, Any],
    period: Mapping[str, Any],
    counts_by_rate: Mapping[int, int],
    issuer: Issuer,
    today: date,
) -> str:
    return REPORT_TEMPLATE.format(
        # The services sentence names the delivery period; the "Mês-" line is
        # the month the report is sent in.
        month=month_label(period["d0"], period["d1"]),
        current_month=month_label(today, today),
        n_total=row["n_total"],
        n_sundays=row["n_sundays"],
        paid_lines=paid_lines(row["n_total"] - row["n_sundays"], counts_by_rate),
        total=format_eur(row["total_cents"]),
        issuer_email=issuer.email,
        issuer_details=issuer.details,
    )


def paid_counts_by_rate(detail: pl.DataFrame) -> dict[str, dict[int, int]]:
    """courier -> {rate_cents: deliveries} over paid deliveries, taken from
    the rate each row was actually paid, so adjusted days fold in naturally."""
    counts: dict[str, dict[int, int]] = {}
    grouped = (
        detail.filter(~pl.col("already_paid"))
        .group_by("courier_norm", "rate_cents")
        .len()
    )
    for name, cents, n in grouped.iter_rows():
        counts.setdefault(name, {})[cents] = n
    return counts


def _build_reports(
    result: PayResult, issuer: Issuer, today: date | None
) -> dict[str, str]:
    today = today or date.today()
    periods = {
        row["courier_norm"]: row
        for row in build_periods(result.detail).iter_rows(named=True)
    }
    counts = paid_counts_by_rate(result.detail)
    reports: dict[str, str] = {}
    for row in result.per_courier.iter_rows(named=True):
        name = row["courier_norm"]
        reports[name] = _render_report(
            row, periods[name], counts.get(name, {}), issuer, today
        )
    return reports


def build_all_reports(result: PayResult, *, today: date | None = None) -> dict[str, str]:
    """Build the full text report for every courier, keyed by courier name.

    `today` sets the "Mês-" line (defaults to the current date)."""
    return _build_reports(result, ISSUER_STANDARD, today)


def build_all_reports_algarve(
    result: PayResult, *, today: date | None = None
) -> dict[str, str]:
    """Algarve-mode text report for every courier, keyed by courier name.

    `today` sets the "Mês-" line (defaults to the current date)."""
    return _build_reports(result, ISSUER_ALGARVE, today)


def reports_zip(reports: Mapping[str, str]) -> bytes:
    """Bundle one .txt per courier into an in-memory zip."""
    buffer = io.BytesIO()
    used: set[str] = set()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in reports.items():
            stem = safe_filename(name)
            candidate, n = stem, 1
            while candidate in used:
                n += 1
                candidate = f"{stem}_{n}"
            used.add(candidate)
            archive.writestr(f"{candidate}.txt", text)
    return buffer.getvalue()


def summary_table(per_courier: pl.DataFrame) -> pl.DataFrame:
    """Results table with friendly headers and EUR-formatted totals."""
    return per_courier.select(
        pl.col("courier_norm").alias("Courier"),
        pl.col("courier_type").alias("Type"),
        pl.col("n_to_door").alias("TO_DOOR"),
        pl.col("n_pudo_first").alias("PUDO first"),
        pl.col("n_pudo_next").alias("PUDO next"),
        pl.col("n_sundays").alias("Sundays"),
        pl.col("n_total").alias("Deliveries"),
        pl.col("total_cents")
        .map_elements(format_eur, return_dtype=pl.String)
        .alias("Total (EUR)"),
    )


def summary_table_algarve(per_courier: pl.DataFrame, daily_limit: int) -> pl.DataFrame:
    """Algarve-mode results table with friendly headers and EUR totals."""
    return per_courier.select(
        pl.col("courier_norm").alias("Courier"),
        pl.col("n_days").alias("Days"),
        pl.col("n_up_to_limit").alias(f"Up to {daily_limit}/day"),
        pl.col("n_above_limit").alias(f"Above {daily_limit}/day"),
        pl.col("n_sundays").alias("Sundays"),
        pl.col("n_total").alias("Deliveries"),
        pl.col("total_cents")
        .map_elements(format_eur, return_dtype=pl.String)
        .alias("Total (EUR)"),
    )


def summary_csv(table: pl.DataFrame) -> bytes:
    """CSV of an already-formatted results table (either mode)."""
    return table.write_csv().encode("utf-8")
