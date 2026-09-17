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

# Some exports name the same data differently (the Algarve export has "Driver",
# "Destination Zip Code" and "Address"). Accepted header names per required
# column, in preference order; the loader renames whichever it finds to the
# canonical name, so everything after loading is shared.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    COL_COURIER: (COL_COURIER, "Driver"),
    COL_ZIP: (COL_ZIP, "Destination Zip Code"),
    COL_ADDRESS: (COL_ADDRESS, "Address"),
}

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
# Standard-mode prices for specific days: {date: {RateKey: EUR}}. Keys left out
# of a day use the normal rate; any date present counts as an adjusted day.
DayRates = Mapping[date, Mapping[RateKey, float]]

# Algarve mode: per courier per day, the first N deliveries are paid one rate
# and every delivery after that another. Categories and FT/OUT do not apply.
ALGARVE_DAILY_LIMIT_DEFAULT = 70

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
            + ", ".join(
                " or ".join(f'"{name}"' for name in COLUMN_ALIASES.get(c, (c,)))
                for c in missing
            )
        )
        self.missing = missing  # canonical names


# --------------------------------------------------------------------------
# Loading
# --------------------------------------------------------------------------


def resolve_columns(raw_header: list[str | None]) -> dict[str, int]:
    """Map each required (canonical) column name to its 0-based sheet index.

    The header row is read as data, so duplicated header names (the real
    exports duplicate "Contact Phone" and "Seller Name") are tolerated by
    construction: the first occurrence wins. A column may be found under any
    of its COLUMN_ALIASES, earlier aliases preferred.
    """
    first_index: dict[str, int] = {}
    for idx, cell in enumerate(raw_header):
        name = (cell or "").strip()
        if name:
            first_index.setdefault(name, idx)
    resolved: dict[str, int] = {}
    missing: list[str] = []
    for column in REQUIRED_COLUMNS:
        found = next(
            (a for a in COLUMN_ALIASES.get(column, (column,)) if a in first_index),
            None,
        )
        if found is None:
            missing.append(column)
        else:
            resolved[column] = first_index[found]
    if missing:
        raise MissingColumnsError(missing)
    return resolved


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

    Dates are read from a leading YYYY-MM-DD: text cells hold just that, while
    real Excel date cells arrive as "YYYY-MM-DD HH:MM:SS". The original "Task
    Date" string column is kept so excluded-row previews can show what failed
    to parse.
    """
    return df.with_columns(
        pl.col(pl.String).str.strip_chars().replace("", None)
    ).with_columns(
        pl.col(COL_TASK_DATE)
        .str.extract(r"^(\d{4}-\d{2}-\d{2})(?:[ T]|$)")
        .str.to_date("%Y-%m-%d", strict=False)
        .alias("task_date")
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


def sunday_expr() -> pl.Expr:
    """Sunday deliveries are paid separately, so they are counted and
    reported but carry no pay in either mode (ISO weekday 7) — unless that
    Sunday has daily price adjustments, see already_paid_expr()."""
    return (pl.col("task_date").dt.weekday() == 7).alias("is_sunday")


def already_paid_expr() -> pl.Expr:
    """Sunday deliveries are already paid unless their day was adjusted,
    which turns that Sunday into a normal paid day. Needs "day_adjusted"."""
    return (pl.col("is_sunday") & ~pl.col("day_adjusted")).alias("already_paid")


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
    payable = payable.with_columns(is_pudo, sunday_expr())
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


def day_rates_frame(
    rates: Mapping[RateKey, float], day_rates: DayRates
) -> pl.DataFrame:
    """(task_date, courier_type, category, day_rate_cents) for adjusted days,
    with prices a day leaves out filled from the normal rates."""
    rows = [
        {
            "task_date": day,
            "courier_type": ctype,
            "category": cat,
            "day_rate_cents": round(prices.get((ctype, cat), rates[(ctype, cat)]) * 100),
        }
        for day, prices in day_rates.items()
        for ctype in COURIER_TYPES
        for cat in CATEGORIES
    ]
    return pl.DataFrame(
        rows,
        schema={
            "task_date": pl.Date,
            "courier_type": pl.String,
            "category": pl.String,
            "day_rate_cents": pl.Int64,
        },
    )


@dataclass(frozen=True)
class PayResult:
    # payable rows + rate_cents, day_adjusted, already_paid
    detail: pl.DataFrame
    per_courier: pl.DataFrame
    grand_total_cents: int
    reconciled: bool


def calculate_pay(
    payable: pl.DataFrame,
    rates: Mapping[RateKey, float],
    day_rates: DayRates | None = None,
) -> PayResult:
    """Adjusted days use their own prices; every other day the normal rates.

    Category counts cover paid deliveries only (already-paid Sundays sit
    outside them), so n_to_door + n_pudo_first + n_pudo_next + n_sundays ==
    n_total.
    """
    detail = (
        payable.join(
            rates_frame(rates),
            on=["courier_type", "category"],
            how="left",
            maintain_order="left",
        )
        .join(
            day_rates_frame(rates, day_rates or {}),
            on=["task_date", "courier_type", "category"],
            how="left",
            maintain_order="left",
        )
        .with_columns(pl.col("day_rate_cents").is_not_null().alias("day_adjusted"))
        .with_columns(already_paid_expr())
        .with_columns(
            pl.when(pl.col("already_paid"))
            .then(pl.lit(0, dtype=pl.Int64))
            .otherwise(pl.coalesce("day_rate_cents", "rate_cents"))
            .alias("rate_cents")
        )
        .drop("day_rate_cents")
    )
    paid = ~pl.col("already_paid")
    per_courier = (
        detail.group_by("courier_norm", "courier_type")
        .agg(
            ((pl.col("category") == CAT_TO_DOOR) & paid).sum().cast(pl.Int64).alias("n_to_door"),
            ((pl.col("category") == CAT_PUDO_FIRST) & paid).sum().cast(pl.Int64).alias("n_pudo_first"),
            ((pl.col("category") == CAT_PUDO_NEXT) & paid).sum().cast(pl.Int64).alias("n_pudo_next"),
            pl.col("already_paid").sum().cast(pl.Int64).alias("n_sundays"),
            pl.len().cast(pl.Int64).alias("n_total"),
            pl.col("rate_cents").sum().alias("total_cents"),
        )
        .sort("courier_norm")
    )
    return _pay_result(detail, per_courier)


@dataclass(frozen=True)
class AlgarveRates:
    up_to_limit: float  # EUR per delivery, first daily_limit of the day
    above_limit: float  # EUR per delivery beyond daily_limit
    daily_limit: int = ALGARVE_DAILY_LIMIT_DEFAULT


@dataclass(frozen=True)
class AlgarveDay:
    """Algarve overrides for one day; None keeps the normal value."""

    up_to_limit: float | None = None
    above_limit: float | None = None
    daily_limit: int | None = None


def algarve_days_frame(
    rates: AlgarveRates, day_rates: Mapping[date, AlgarveDay]
) -> pl.DataFrame:
    """(task_date, day_limit, day_up_cents, day_above_cents) for adjusted
    days, with values a day leaves out filled from the normal rates."""
    rows = [
        {
            "task_date": day,
            "day_limit": rates.daily_limit if adj.daily_limit is None else adj.daily_limit,
            "day_up_cents": round(
                (rates.up_to_limit if adj.up_to_limit is None else adj.up_to_limit) * 100
            ),
            "day_above_cents": round(
                (rates.above_limit if adj.above_limit is None else adj.above_limit) * 100
            ),
        }
        for day, adj in day_rates.items()
    ]
    return pl.DataFrame(
        rows,
        schema={
            "task_date": pl.Date,
            "day_limit": pl.Int64,
            "day_up_cents": pl.Int64,
            "day_above_cents": pl.Int64,
        },
    )


def calculate_pay_algarve(
    payable: pl.DataFrame,
    rates: AlgarveRates,
    day_rates: Mapping[date, AlgarveDay] | None = None,
) -> PayResult:
    """Marginal daily tiers: the count resets per (courier, task date).

    Every payable delivery counts regardless of category or courier type. No
    ordering exists, so row order decides which rows land above the limit —
    totals are unaffected since all rows in a tier share one rate. Adjusted
    days use their own limit and prices.

    Already-paid Sunday deliveries sit in neither tier (see already_paid_expr),
    so n_up_to_limit + n_above_limit + n_sundays == n_total.
    """
    position = pl.int_range(pl.len()).over("courier_norm", "task_date")
    paid = ~pl.col("already_paid")
    detail = (
        payable.join(
            algarve_days_frame(rates, day_rates or {}),
            on="task_date",
            how="left",
            maintain_order="left",
        )
        .with_columns(pl.col("day_limit").is_not_null().alias("day_adjusted"))
        .with_columns(already_paid_expr())
        .with_columns(
            (paid & (position >= pl.col("day_limit").fill_null(rates.daily_limit)))
            .alias("above_limit")
        )
        .with_columns(
            pl.when(pl.col("already_paid"))
            .then(pl.lit(0, dtype=pl.Int64))
            .when(pl.col("above_limit"))
            .then(pl.col("day_above_cents").fill_null(round(rates.above_limit * 100)))
            .otherwise(pl.col("day_up_cents").fill_null(round(rates.up_to_limit * 100)))
            .cast(pl.Int64)
            .alias("rate_cents")
        )
        .drop("day_limit", "day_up_cents", "day_above_cents")
    )
    per_courier = (
        detail.group_by("courier_norm")
        .agg(
            pl.col("task_date").n_unique().cast(pl.Int64).alias("n_days"),
            (paid & ~pl.col("above_limit")).sum().cast(pl.Int64).alias("n_up_to_limit"),
            pl.col("above_limit").sum().cast(pl.Int64).alias("n_above_limit"),
            pl.col("already_paid").sum().cast(pl.Int64).alias("n_sundays"),
            pl.len().cast(pl.Int64).alias("n_total"),
            pl.col("rate_cents").sum().alias("total_cents"),
        )
        .sort("courier_norm")
    )
    return _pay_result(detail, per_courier)


def _pay_result(detail: pl.DataFrame, per_courier: pl.DataFrame) -> PayResult:
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
