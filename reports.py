"""Text report building and download payloads for courier-pay.

No Streamlit imports — everything returns plain strings or bytes.
"""

from __future__ import annotations

import io
import zipfile
from typing import Mapping

import polars as pl

from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_TO_DOOR,
    PayResult,
    RateKey,
)

REPORT_TEMPLATE = (
    "Courier: {name} ({ctype})\n"
    "Period: {d0} — {d1}\n"
    "------------------------------------------\n"
    "TO_DOOR:     {n_td} x {r_td} = {s_td}\n"
    "PUDO first:  {n_pf} x {r_pf} = {s_pf}\n"
    "PUDO next:   {n_pn} x {r_pn} = {s_pn}\n"
    "TOTAL: {total} EUR\n"
    "------------------------------------------\n"
)

_FILENAME_SAFE = set(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789 _.-"
)


def format_eur(cents: int) -> str:
    """Format integer cents as "12.50" (negative-safe, no float rounding)."""
    sign = "-" if cents < 0 else ""
    cents = abs(int(cents))
    return f"{sign}{cents // 100}.{cents % 100:02d}"


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


def build_all_reports(
    result: PayResult, rates: Mapping[RateKey, float]
) -> dict[str, str]:
    """Build the full text report for every courier, keyed by courier name."""
    periods = {
        row["courier_norm"]: row
        for row in build_periods(result.detail).iter_rows(named=True)
    }
    reports: dict[str, str] = {}
    for row in result.per_courier.iter_rows(named=True):
        name = row["courier_norm"]
        ctype = row["courier_type"]
        period = periods.get(name, {})
        rate_cents = {
            cat: round(rates[(ctype, cat)] * 100)
            for cat in (CAT_TO_DOOR, CAT_PUDO_FIRST, CAT_PUDO_NEXT)
        }
        counts = {
            CAT_TO_DOOR: row["n_to_door"],
            CAT_PUDO_FIRST: row["n_pudo_first"],
            CAT_PUDO_NEXT: row["n_pudo_next"],
        }
        reports[name] = REPORT_TEMPLATE.format(
            name=name,
            ctype=ctype,
            d0=period.get("d0", ""),
            d1=period.get("d1", ""),
            n_td=counts[CAT_TO_DOOR],
            r_td=format_eur(rate_cents[CAT_TO_DOOR]),
            s_td=format_eur(counts[CAT_TO_DOOR] * rate_cents[CAT_TO_DOOR]),
            n_pf=counts[CAT_PUDO_FIRST],
            r_pf=format_eur(rate_cents[CAT_PUDO_FIRST]),
            s_pf=format_eur(counts[CAT_PUDO_FIRST] * rate_cents[CAT_PUDO_FIRST]),
            n_pn=counts[CAT_PUDO_NEXT],
            r_pn=format_eur(rate_cents[CAT_PUDO_NEXT]),
            s_pn=format_eur(counts[CAT_PUDO_NEXT] * rate_cents[CAT_PUDO_NEXT]),
            total=format_eur(row["total_cents"]),
        )
    return reports


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
        pl.col("n_total").alias("Deliveries"),
        pl.col("total_cents")
        .map_elements(format_eur, return_dtype=pl.String)
        .alias("Total (EUR)"),
    )


def summary_csv(per_courier: pl.DataFrame) -> bytes:
    return summary_table(per_courier).write_csv().encode("utf-8")
