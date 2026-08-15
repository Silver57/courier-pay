"""All Polars logic for courier-pay: load, clean, classify, calculate.

Pure functions only — no Streamlit imports. Everything heavy is expressed as
Polars expressions (group_by / window / hash passes) so it stays fast at
~500k rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Iterable, Mapping

import fastexcel
import polars as pl

SHEET_NAME = "EPOD_TASK_LIST_V2"

COL_WAYBILL = "Waybill Number"
COL_TASK_DATE = "Task Date"
COL_STATUS = "Task Status"
COL_DELIVERY_TYPE = "Delivery Type"
COL_COURIER = "Courier Name"
COL_ZIP = "Zip Code"
COL_ADDRESS = "Detailed address"
COL_PUDO_ADDRESS = "PUDO address"

REQUIRED_COLUMNS: tuple[str, ...] = (
    COL_WAYBILL,
    COL_TASK_DATE,
    COL_STATUS,
    COL_DELIVERY_TYPE,
    COL_COURIER,
    COL_ZIP,
    COL_ADDRESS,
    COL_PUDO_ADDRESS,
)

PAYABLE_STATUS = "Delivered"
DELIVERY_TO_DOOR = "TO_DOOR"
DELIVERY_PUDO = "PUDO"
VALID_DELIVERY_TYPES = (DELIVERY_TO_DOOR, DELIVERY_PUDO)

CAT_TO_DOOR = "TO_DOOR"
CAT_PUDO_FIRST = "PUDO_FIRST"
CAT_PUDO_NEXT = "PUDO_NEXT"
CATEGORIES: tuple[str, ...] = (CAT_TO_DOOR, CAT_PUDO_FIRST, CAT_PUDO_NEXT)

TYPE_FT = "FT"
TYPE_OUT = "OUT"
COURIER_TYPES: tuple[str, ...] = (TYPE_FT, TYPE_OUT)

# Depot tags like "QTK-LIS" appear at the start, end, or glued to courier
# names. Per current business decision they stay part of the name; flip the
# default (or pass strip_depot_tag=True) to remove them. Note: with stripping
# ON, an "FT" inside a tag can no longer mark a courier as full-time.
DEPOT_TAG_PATTERN = r"QTK-[A-Z]{3}"
STRIP_DEPOT_TAG_DEFAULT = False

RateKey = tuple[str, str]  # (courier_type, category)

REASON_DUPLICATE = "duplicate_waybill"
REASON_MISSING = "missing_courier_date_or_type"
_REASON_STATUS_PREFIX = "status:"
_REASON_DELIVERY_TYPE_PREFIX = "delivery_type:"


class EpodError(ValueError):
    """Base class for input-file problems surfaced to the user."""


class SheetNotFoundError(EpodError):
    def __init__(self, available: list[str]) -> None:
        super().__init__(
            f'Sheet "{SHEET_NAME}" was not found in the workbook. '
            f"Available sheets: {', '.join(available) or '(none)'}"
        )
        self.available = available


class MissingColumnsError(EpodError):
    def __init__(self, missing: list[str]) -> None:
        super().__init__(
            f'Sheet "{SHEET_NAME}" is missing required columns: '
            + ", ".join(f'"{name}"' for name in missing)
        )
        self.missing = missing


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def resolve_columns(raw_header: list[str | None]) -> dict[str, int]:
    """Map each required column name to its first 0-based sheet index.

    The header row is read as data, so duplicated header names (the real
    exports duplicate "Contact Phone" and "Seller Name") are tolerated by
    construction: the first occurrence wins.
    """
    first_index: dict[str, int] = {}
    for idx, cell in enumerate(raw_header):
        name = (cell or "").strip()
        if name:
            first_index.setdefault(name, idx)
    missing = [c for c in REQUIRED_COLUMNS if c not in first_index]
    if missing:
        raise MissingColumnsError(missing)
    return {c: first_index[c] for c in REQUIRED_COLUMNS}


def load_epod(file_bytes: bytes) -> pl.DataFrame:
    """Load the EPOD sheet from workbook bytes, keeping only needed columns.

    Two-pass read with header_row=None so column selection never depends on
    how fastexcel aliases duplicated headers; all cells are forced to string
    (empty cells arrive as "").
    """
    reader = fastexcel.read_excel(file_bytes)
    if SHEET_NAME not in reader.sheet_names:
        raise SheetNotFoundError(list(reader.sheet_names))

    header_df = reader.load_sheet(
        SHEET_NAME, header_row=None, n_rows=1, dtypes="string"
    ).to_polars()
    if header_df.height == 0:
        raise MissingColumnsError(list(REQUIRED_COLUMNS))
    col_map = resolve_columns(list(header_df.row(0)))

    by_index = sorted(col_map.items(), key=lambda kv: kv[1])
    indexes = [idx for _, idx in by_index]
    names = [name for name, _ in by_index]
    df = reader.load_sheet(
        SHEET_NAME,
        header_row=None,
        skip_rows=1,
        use_columns=indexes,
        dtypes="string",
    ).to_polars()
    df = df.rename(dict(zip(df.columns, names)))
    # skip_rows=1 assumes the header sits on the first physical row (true for
    # real exports); if blank leading rows ever shift it, drop the leaked
    # header row rather than paying someone for a delivery named "Waybill
    # Number".
    return df.filter(pl.col(COL_WAYBILL).ne_missing(COL_WAYBILL))


# --------------------------------------------------------------------------
# Cleaning and the three isolated business-rule helpers
# --------------------------------------------------------------------------


def clean(df: pl.DataFrame) -> pl.DataFrame:
    """Trim all text columns, turn ""/whitespace-only into null, parse dates.

    The original "Task Date" string column is kept so excluded-row previews
    can show what failed to parse.
    """
    return df.with_columns(
        pl.col(pl.String).str.strip_chars().replace("", None)
    ).with_columns(
        pl.col(COL_TASK_DATE).str.to_date("%Y-%m-%d", strict=False).alias("task_date")
    )


def normalized_courier_name_expr(
    *, strip_depot_tag: bool = STRIP_DEPOT_TAG_DEFAULT
) -> pl.Expr:
    """Normalized courier name: strip + collapse repeated whitespace.

    Depot-tag removal is a separable step, off by default.
    """
    expr = pl.col(COL_COURIER)
    if strip_depot_tag:
        expr = expr.str.replace_all(DEPOT_TAG_PATTERN, "")
    return (
        expr.str.replace_all(r"\s+", " ")
        .str.strip_chars()
        .replace("", None)
        .alias("courier_norm")
    )


def courier_type_expr() -> pl.Expr:
    """FT when the normalized name contains the uppercase substring "FT".

    Case-sensitive on purpose (must not match "ft" inside lowercase names).
    The real naming pattern may need adjusting — change only this function.
    """
    return (
        pl.when(pl.col("courier_norm").str.contains(TYPE_FT, literal=True))
        .then(pl.lit(TYPE_FT))
        .otherwise(pl.lit(TYPE_OUT))
        .alias("courier_type")
    )


def pudo_point_expr() -> pl.Expr:
    """PUDO point identity: "PUDO address" when present, else the delivery
    address. Valid because clean() already turned "" into null."""
    return pl.coalesce(COL_PUDO_ADDRESS, COL_ADDRESS).alias("pudo_point")


# --------------------------------------------------------------------------
# Hygiene + classification
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class CleaningStats:
    total_rows: int
    duplicates_removed: int
    missing_fields_excluded: int
    non_delivered_excluded: int
    status_breakdown: dict[str, int]
    bad_delivery_type_excluded: int
    delivery_type_breakdown: dict[str, int]
    pudo_rows_without_address: int
    payable_rows: int

    @property
    def excluded_rows(self) -> int:
        return self.total_rows - self.payable_rows


@dataclass(frozen=True)
class PreparedData:
    payable: pl.DataFrame
    excluded: pl.DataFrame
    stats: CleaningStats


def prepare(
    df: pl.DataFrame, *, strip_depot_tag: bool = STRIP_DEPOT_TAG_DEFAULT
) -> PreparedData:
    """Clean, apply hygiene rules and classify every payable delivery.

    Exclusion precedence: duplicate waybill > missing courier/date/type >
    non-Delivered status > unknown delivery type.
    """
    total_rows = df.height
    cleaned = (
        clean(df)
        .with_columns(normalized_courier_name_expr(strip_depot_tag=strip_depot_tag))
        .with_columns(courier_type_expr(), pudo_point_expr())
    )

    # Keep-first dedupe that never collapses null waybills (unique(subset=...)
    # would merge all null-waybill rows into one).
    is_dup = pl.col(COL_WAYBILL).is_not_null() & (
        pl.int_range(pl.len()).over(COL_WAYBILL) > 0
    )
    missing = (
        pl.col("courier_norm").is_null()
        | pl.col("task_date").is_null()
        | pl.col(COL_DELIVERY_TYPE).is_null()
    )
    flagged = cleaned.with_columns(
        pl.when(is_dup)
        .then(pl.lit(REASON_DUPLICATE))
        .when(missing)
        .then(pl.lit(REASON_MISSING))
        .when(pl.col(COL_STATUS).ne_missing(PAYABLE_STATUS))
        .then(
            pl.format(
                _REASON_STATUS_PREFIX + "{}", pl.col(COL_STATUS).fill_null("<null>")
            )
        )
        .when(~pl.col(COL_DELIVERY_TYPE).is_in(VALID_DELIVERY_TYPES))
        .then(pl.format(_REASON_DELIVERY_TYPE_PREFIX + "{}", pl.col(COL_DELIVERY_TYPE)))
        .otherwise(None)
        .alias("exclusion_reason")
    )
    excluded = flagged.filter(pl.col("exclusion_reason").is_not_null())
    payable = flagged.filter(pl.col("exclusion_reason").is_null()).drop(
        "exclusion_reason"
    )

    # Category: one PUDO delivery per (courier, date, PUDO point) group is
    # PUDO_FIRST, the rest PUDO_NEXT. No ordering exists — first row order
    # wins. Including is_pudo in the struct keeps a TO_DOOR row whose fallback
    # address coincides with a PUDO point from stealing first-distinct.
    is_pudo = (pl.col(COL_DELIVERY_TYPE) == DELIVERY_PUDO).alias("is_pudo")
    payable = payable.with_columns(is_pudo)
    first_of_group = pl.struct(
        "courier_norm", "task_date", "pudo_point", "is_pudo"
    ).is_first_distinct()
    payable = payable.with_columns(
        pl.when(~pl.col("is_pudo"))
        .then(pl.lit(CAT_TO_DOOR))
        .when(first_of_group)
        .then(pl.lit(CAT_PUDO_FIRST))
        .otherwise(pl.lit(CAT_PUDO_NEXT))
        .alias("category"),
        pl.when(pl.col("is_pudo"))
        .then(pl.col("pudo_point"))
        .otherwise(pl.col(COL_ADDRESS))
        .alias("address_used"),
    )

    reason_counts: dict[str, int] = dict(
        excluded.group_by("exclusion_reason").len().iter_rows()
    )
    status_breakdown = {
        k.removeprefix(_REASON_STATUS_PREFIX): v
        for k, v in reason_counts.items()
        if k.startswith(_REASON_STATUS_PREFIX)
    }
    delivery_type_breakdown = {
        k.removeprefix(_REASON_DELIVERY_TYPE_PREFIX): v
        for k, v in reason_counts.items()
        if k.startswith(_REASON_DELIVERY_TYPE_PREFIX)
    }
    stats = CleaningStats(
        total_rows=total_rows,
        duplicates_removed=reason_counts.get(REASON_DUPLICATE, 0),
        missing_fields_excluded=reason_counts.get(REASON_MISSING, 0),
        non_delivered_excluded=sum(status_breakdown.values()),
        status_breakdown=status_breakdown,
        bad_delivery_type_excluded=sum(delivery_type_breakdown.values()),
        delivery_type_breakdown=delivery_type_breakdown,
        # Only rows with no usable point at all: an empty "PUDO address" is
        # normal and falls back to the delivery address.
        pudo_rows_without_address=payable.filter(
            pl.col("is_pudo") & pl.col("pudo_point").is_null()
        ).height,
        payable_rows=payable.height,
    )
    return PreparedData(payable=payable, excluded=excluded, stats=stats)


def prepare_from_bytes(
    file_bytes: bytes, *, strip_depot_tag: bool = STRIP_DEPOT_TAG_DEFAULT
) -> PreparedData:
    """Load + clean + classify in one call (the cacheable unit)."""
    return prepare(load_epod(file_bytes), strip_depot_tag=strip_depot_tag)


# --------------------------------------------------------------------------
# Courier roster and manual type overrides
# --------------------------------------------------------------------------


def courier_roster(payable: pl.DataFrame) -> pl.DataFrame:
    """One row per courier: name, auto-detected type, delivery count."""
    return (
        payable.group_by("courier_norm")
        .agg(
            pl.col("courier_type").first().alias("courier_type"),
            pl.len().cast(pl.Int64).alias("n_total"),
        )
        .sort("courier_norm")
    )


def apply_courier_type_overrides(
    payable: pl.DataFrame, full_time_couriers: Iterable[str]
) -> pl.DataFrame:
    """Set courier_type from an explicit roster of full-time couriers.

    The FT-substring rule only seeds the initial roster; this list is the
    source of truth afterwards, so a courier whose name carries no "FT" can be
    marked full-time and a false positive can be marked back to outsourced.
    Applied after prepare() so corrections never re-parse the workbook.
    """
    names = list(dict.fromkeys(full_time_couriers))
    return payable.with_columns(
        pl.when(pl.col("courier_norm").is_in(names))
        .then(pl.lit(TYPE_FT))
        .otherwise(pl.lit(TYPE_OUT))
        .alias("courier_type")
    )


# --------------------------------------------------------------------------
# Pay calculation — integer cents end-to-end so reconciliation is exact
# --------------------------------------------------------------------------


def rates_frame(rates: Mapping[RateKey, float]) -> pl.DataFrame:
    """6-row (courier_type, category, rate_cents) frame for the pay join."""
    rows = [
        {
            "courier_type": ctype,
            "category": cat,
            "rate_cents": round(rates[(ctype, cat)] * 100),
        }
        for ctype in COURIER_TYPES
        for cat in CATEGORIES
    ]
    return pl.DataFrame(
        rows,
        schema={"courier_type": pl.String, "category": pl.String, "rate_cents": pl.Int64},
    )


@dataclass(frozen=True)
class PayResult:
    detail: pl.DataFrame  # payable rows + rate_cents
    per_courier: pl.DataFrame
    grand_total_cents: int
    reconciled: bool


def calculate_pay(payable: pl.DataFrame, rates: Mapping[RateKey, float]) -> PayResult:
    detail = payable.join(rates_frame(rates), on=["courier_type", "category"], how="left")
    per_courier = (
        detail.group_by("courier_norm", "courier_type")
        .agg(
            (pl.col("category") == CAT_TO_DOOR).sum().cast(pl.Int64).alias("n_to_door"),
            (pl.col("category") == CAT_PUDO_FIRST).sum().cast(pl.Int64).alias("n_pudo_first"),
            (pl.col("category") == CAT_PUDO_NEXT).sum().cast(pl.Int64).alias("n_pudo_next"),
            pl.len().cast(pl.Int64).alias("n_total"),
            pl.col("rate_cents").sum().alias("total_cents"),
        )
        .sort("courier_norm")
    )
    grand_total_cents = int(detail["rate_cents"].sum() or 0)
    courier_total_cents = int(per_courier["total_cents"].sum() or 0)
    return PayResult(
        detail=detail,
        per_courier=per_courier,
        grand_total_cents=grand_total_cents,
        reconciled=courier_total_cents == grand_total_cents,
    )


# --------------------------------------------------------------------------
# Small summary helpers
# --------------------------------------------------------------------------


def date_range(payable: pl.DataFrame) -> tuple[date, date] | None:
    if payable.height == 0:
        return None
    return payable["task_date"].min(), payable["task_date"].max()


def courier_counts_by_type(per_courier: pl.DataFrame) -> dict[str, int]:
    counts = dict(per_courier.group_by("courier_type").len().iter_rows())
    return {ctype: counts.get(ctype, 0) for ctype in COURIER_TYPES}
