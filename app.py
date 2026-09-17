"""courier-pay — internal Streamlit app.

Upload an EPOD task-list export, set the rate matrix, get per-courier pay and
ready-to-send text reports. Nothing is stored: state lives in the session only.
"""

from __future__ import annotations

from datetime import date
from typing import Any, Mapping

import polars as pl
import streamlit as st

import processing
import reports
from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_TO_DOOR,
    COURIER_TYPES,
    AlgarveDay,
    AlgarveRates,
    DayRates,
    EpodError,
    PreparedData,
    RateKey,
    TYPE_FT,
    TYPE_OUT,
)

st.set_page_config(page_title="courier-pay", page_icon="📦", layout="wide")

CATEGORY_LABELS: dict[str, str] = {
    CAT_TO_DOOR: "TO_DOOR",
    CAT_PUDO_FIRST: "PUDO first",
    CAT_PUDO_NEXT: "PUDO next",
}
FT_STATE_KEY = "ft_couriers"

MODE_STANDARD = "Standard"
MODE_ALGARVE = "Algarve"
ALGARVE_LIMIT_KEY = "algarve_daily_limit"
ALGARVE_UP_KEY = "rate_algarve_up_to_limit"
ALGARVE_ABOVE_KEY = "rate_algarve_above_limit"


def rate_key(ctype: str, cat: str) -> str:
    return f"rate_{ctype.lower()}_{cat.lower()}"


# Initial values for every input in either pay mode, keyed by widget key.
WIDGET_DEFAULTS: dict[str, float | int] = {
    **{
        rate_key(ctype, cat): 0.0
        for ctype in COURIER_TYPES
        for cat in CATEGORY_LABELS
    },
    ALGARVE_LIMIT_KEY: processing.ALGARVE_DAILY_LIMIT_DEFAULT,
    ALGARVE_UP_KEY: 0.0,
    ALGARVE_ABOVE_KEY: 0.0,
}

# Daily adjustments are remembered per mode for the session as
# {date: {column label: value}}, so they survive mode switches and re-uploads.
DAY_STORE_STANDARD = "day_adjust_standard"
DAY_STORE_ALGARVE = "day_adjust_algarve"
WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
PRICE_COLUMN = st.column_config.NumberColumn(
    min_value=0.0, step=0.01, format="%.2f", help="Blank = sidebar price"
)
# Standard: one column per rate, labelled like "FT PUDO first".
STANDARD_DAY_COLUMNS: dict[str, RateKey] = {
    f"{ctype} {label}": (ctype, cat)
    for ctype in COURIER_TYPES
    for cat, label in CATEGORY_LABELS.items()
}
ALGARVE_DAY_LIMIT = "Daily limit"
ALGARVE_DAY_UP = "Up to limit"
ALGARVE_DAY_ABOVE = "Above limit"


@st.cache_data(show_spinner="Parsing Excel file…")
def cached_prepare(file_bytes: bytes) -> PreparedData:
    """Parse + clean + classify. Cached on file content, so changing rates
    never re-parses the workbook."""
    return processing.prepare_from_bytes(file_bytes)


def keep_widget_state() -> None:
    """Seed input defaults and keep the values of inputs the other mode hides.

    Streamlit discards the state of widgets that are not rendered in a run;
    re-assigning them here means switching to Algarve and back keeps the
    standard rates and the FT roster. Widgets therefore take no value= (it
    would clash with state set through the Session State API).
    """
    for key, default in WIDGET_DEFAULTS.items():
        st.session_state[key] = st.session_state.get(key, default)
    if FT_STATE_KEY in st.session_state:
        st.session_state[FT_STATE_KEY] = st.session_state[FT_STATE_KEY]


def rates_sidebar() -> dict[RateKey, float]:
    """Six independent rate inputs (courier type x category), in EUR."""
    st.sidebar.header("Rates (EUR)")
    rates: dict[RateKey, float] = {}
    for ctype, heading in ((TYPE_FT, "Full-time (FT)"), (TYPE_OUT, "Outsourced (OUT)")):
        st.sidebar.subheader(heading)
        for cat, label in CATEGORY_LABELS.items():
            rates[(ctype, cat)] = st.sidebar.number_input(
                label,
                min_value=0.0,
                step=0.01,
                format="%.2f",
                key=rate_key(ctype, cat),
            )
    return rates


def algarve_sidebar() -> AlgarveRates:
    """Daily limit plus the two tier rates, in EUR. Same for FT and OUT."""
    st.sidebar.header("Algarve rates (EUR)")
    limit = int(
        st.sidebar.number_input(
            "Daily limit (deliveries per courier per day)",
            min_value=1,
            step=1,
            key=ALGARVE_LIMIT_KEY,
        )
    )
    up_to = st.sidebar.number_input(
        f"Up to {limit}/day",
        min_value=0.0,
        step=0.01,
        format="%.2f",
        key=ALGARVE_UP_KEY,
    )
    above = st.sidebar.number_input(
        f"Above {limit}/day",
        min_value=0.0,
        step=0.01,
        format="%.2f",
        key=ALGARVE_ABOVE_KEY,
    )
    return AlgarveRates(up_to_limit=up_to, above_limit=above, daily_limit=limit)


def resolve_full_time_couriers(
    prep: PreparedData,
) -> tuple[list[str], list[str], list[str]]:
    """Current FT roster, all courier names, and the auto-detected FT names.

    Read before the summary is rendered so corrected types are reflected in the
    same rerun as the edit.

    The "FT" name rule only seeds this roster; whatever is selected here wins,
    so full-timers whose names carry no "FT" can be added by hand. Selections
    are remembered for the session and survive re-uploads of the same couriers.
    """
    roster = processing.courier_roster(prep.payable)
    names = roster["courier_norm"].to_list()
    detected_ft = roster.filter(pl.col("courier_type") == TYPE_FT)[
        "courier_norm"
    ].to_list()

    if FT_STATE_KEY not in st.session_state:
        st.session_state[FT_STATE_KEY] = detected_ft
    # Drop names from a previously loaded file, otherwise the widget would be
    # handed a selection that is not in its options.
    st.session_state[FT_STATE_KEY] = [
        name for name in st.session_state[FT_STATE_KEY] if name in set(names)
    ]
    return st.session_state[FT_STATE_KEY], names, detected_ft


def show_courier_type_editor(names: list[str], detected_ft: list[str]) -> None:
    st.subheader("Courier types")
    st.caption(
        'Couriers whose name contains "FT" are pre-selected as full-time. '
        "Add or remove couriers here — pay, reports and totals update immediately."
    )
    st.multiselect(
        "Full-time (FT) couriers — everyone else is outsourced (OUT)",
        options=names,
        key=FT_STATE_KEY,
    )
    manual = set(st.session_state[FT_STATE_KEY]) ^ set(detected_ft)
    if manual:
        st.caption(f"{len(manual)} courier(s) changed from the automatic detection.")


def show_summary(prep: PreparedData, payable: pl.DataFrame, *, algarve: bool) -> None:
    stats = prep.stats
    period = processing.date_range(payable)

    cols = st.columns(4 if algarve else 5)
    cols[0].metric("Rows in file", f"{stats.total_rows:,}")
    cols[1].metric("Payable rows", f"{stats.payable_rows:,}")
    cols[2].metric(
        "Date range",
        f"{period[0]} → {period[1]}" if period else "—",
    )
    if algarve:
        # FT/OUT does not affect Algarve pay, so only the head count matters.
        cols[3].metric("Couriers", payable["courier_norm"].n_unique())
    else:
        counts = processing.courier_counts_by_type(
            payable.select("courier_norm", "courier_type").unique()
        )
        cols[3].metric("Couriers (FT)", counts[TYPE_FT])
        cols[4].metric("Couriers (OUT)", counts[TYPE_OUT])

    if stats.pudo_rows_without_address:
        st.warning(
            f"{stats.pudo_rows_without_address:,} PUDO rows have no address at all; "
            "they are grouped together per courier and day."
        )

    with st.expander(f"Excluded rows ({stats.excluded_rows:,})"):
        breakdown = [
            {"Reason": "Non-Delivered status", "Rows": stats.non_delivered_excluded},
            {"Reason": "Duplicate waybill", "Rows": stats.duplicates_removed},
            {
                "Reason": "Missing courier / date / delivery type",
                "Rows": stats.missing_fields_excluded,
            },
            {"Reason": "Unknown delivery type", "Rows": stats.bad_delivery_type_excluded},
        ]
        st.dataframe(pl.DataFrame(breakdown), hide_index=True, width="stretch")

        if stats.status_breakdown:
            st.caption("Statuses other than Delivered")
            st.dataframe(
                pl.DataFrame(
                    {
                        "Task Status": list(stats.status_breakdown),
                        "Rows": list(stats.status_breakdown.values()),
                    }
                ),
                hide_index=True,
            )
        if stats.delivery_type_breakdown:
            st.caption("Delivery types other than TO_DOOR / PUDO")
            st.dataframe(
                pl.DataFrame(
                    {
                        "Delivery Type": list(stats.delivery_type_breakdown),
                        "Rows": list(stats.delivery_type_breakdown.values()),
                    }
                ),
                hide_index=True,
            )
        if prep.excluded.height:
            st.caption("Preview of excluded rows (first 200)")
            st.dataframe(
                prep.excluded.select(
                    "exclusion_reason",
                    processing.COL_WAYBILL,
                    processing.COL_TASK_DATE,
                    processing.COL_STATUS,
                    processing.COL_DELIVERY_TYPE,
                    processing.COL_COURIER,
                ).head(200),
                hide_index=True,
                width="stretch",
            )


def day_table_editor(
    payable: pl.DataFrame,
    store_key: str,
    columns: Mapping[str, tuple[pl.DataType, Any]],
) -> dict[date, dict[str, Any]]:
    """Calendar table: one row per day of the file's date range, with an
    editable (dtype, column_config) column per adjustable value.

    Returns {date: {label: value}} for days with at least one value entered.
    The editor key carries the date range, so edits typed against one file
    never shift onto the rows of another file with the same number of days.
    """
    period = processing.date_range(payable)
    assert period is not None  # callers only get here with payable rows
    days: list[date] = pl.date_range(*period, eager=True).to_list()
    counts = dict(payable.group_by("task_date").len().iter_rows())
    store: dict[date, dict[str, Any]] = st.session_state.setdefault(store_key, {})

    base = pl.DataFrame(
        [
            pl.Series("Date", days, dtype=pl.Date),
            pl.Series("Day", [WEEKDAYS[d.weekday()] for d in days]),
            pl.Series("Deliveries", [counts.get(d, 0) for d in days], dtype=pl.Int64),
            *(
                pl.Series(label, [store.get(d, {}).get(label) for d in days], dtype=dtype)
                for label, (dtype, _) in columns.items()
            ),
        ]
    )
    edited = st.data_editor(
        base,
        key=f"{store_key}_{period[0]}_{period[1]}",
        num_rows="fixed",
        hide_index=True,
        disabled=["Date", "Day", "Deliveries"],
        column_config={label: config for label, (_, config) in columns.items()},
        # Tall enough to show a whole month without scrolling.
        height=35 * (min(len(days), 31) + 1) + 3,
    )

    # Rows stay in order (fixed row count), so pair them with `days` rather
    # than reading "Date" back: the editor returns it as a datetime.
    adjusted: dict[date, dict[str, Any]] = {}
    for day, row in zip(days, edited.iter_rows(named=True)):
        values = {
            label: row[label]
            for label in columns
            if row[label] is not None and row[label] == row[label]  # skip NaN
        }
        if values:
            adjusted[day] = values
    for day in days:
        store.pop(day, None)
    store.update(adjusted)

    if adjusted:
        listed = ", ".join(f"{d} ({WEEKDAYS[d.weekday()]})" for d in sorted(adjusted))
        st.caption(f"{len(adjusted)} day(s) adjusted: {listed}")
    else:
        st.caption("No days adjusted — every day uses the sidebar prices.")
    return adjusted


def show_day_adjustments_intro() -> None:
    st.subheader("Daily price adjustments")
    st.caption(
        "Every day is paid at the sidebar prices. Enter prices only on days that "
        "differ; blank cells keep the sidebar price. Sundays are already paid "
        "(\"já pagas\") unless you enter a value on that Sunday — the whole "
        "Sunday is then paid here, with any blank cells at the sidebar price."
    )


def standard_day_rates(payable: pl.DataFrame) -> DayRates:
    show_day_adjustments_intro()
    columns = {label: (pl.Float64, PRICE_COLUMN) for label in STANDARD_DAY_COLUMNS}
    adjusted = day_table_editor(payable, DAY_STORE_STANDARD, columns)
    return {
        day: {STANDARD_DAY_COLUMNS[label]: value for label, value in values.items()}
        for day, values in adjusted.items()
    }


def algarve_day_rates(payable: pl.DataFrame) -> dict[date, AlgarveDay]:
    show_day_adjustments_intro()
    columns = {
        ALGARVE_DAY_LIMIT: (
            pl.Int64,
            st.column_config.NumberColumn(
                min_value=1, step=1, format="%d", help="Blank = sidebar limit"
            ),
        ),
        ALGARVE_DAY_UP: (pl.Float64, PRICE_COLUMN),
        ALGARVE_DAY_ABOVE: (pl.Float64, PRICE_COLUMN),
    }
    adjusted = day_table_editor(payable, DAY_STORE_ALGARVE, columns)
    return {
        day: AlgarveDay(
            up_to_limit=values.get(ALGARVE_DAY_UP),
            above_limit=values.get(ALGARVE_DAY_ABOVE),
            daily_limit=(
                int(values[ALGARVE_DAY_LIMIT]) if ALGARVE_DAY_LIMIT in values else None
            ),
        )
        for day, values in adjusted.items()
    }


def show_results(result: processing.PayResult, table: pl.DataFrame) -> None:
    """Results table (either mode) with a TOTAL row: counts summed, other
    text columns blank, and the reconciled grand total."""
    st.subheader("Results")
    st.caption("Sunday deliveries are already paid: they are counted but add nothing to the totals.")
    totals: dict[str, str | int] = {}
    for name, dtype in table.schema.items():
        if name == "Total (EUR)":
            totals[name] = reports.format_eur(result.grand_total_cents)
        elif dtype.is_integer():
            totals[name] = int(table[name].sum() or 0)
        else:
            totals[name] = ""
    totals[table.columns[0]] = "TOTAL"
    grand = pl.DataFrame([totals], schema=table.schema)
    st.dataframe(pl.concat([table, grand]), hide_index=True, width="stretch")

    left, right = st.columns(2)
    left.metric("Grand total", f"{reports.format_eur(result.grand_total_cents)} EUR")
    if result.reconciled:
        right.success("Reconciliation OK — courier totals match the grand total.")
    else:
        right.error("Reconciliation FAILED — courier totals do not match the grand total.")


def main() -> None:
    st.title("📦 courier-pay")
    st.caption(
        "Upload an EPOD task-list export, set the rates, download the reports. "
        "Nothing is saved — everything lives in this session only."
    )

    keep_widget_state()
    mode = st.sidebar.radio(
        "Pay mode", (MODE_STANDARD, MODE_ALGARVE), horizontal=True, key="pay_mode"
    )
    rates = algarve_sidebar() if mode == MODE_ALGARVE else rates_sidebar()
    algarve = isinstance(rates, AlgarveRates)

    uploaded = st.file_uploader(
        "EPOD task list export (.xlsx)", type="xlsx", key="epod_file"
    )
    if uploaded is None:
        st.info("Upload an .xlsx export to begin.")
        return

    try:
        prep = cached_prepare(uploaded.getvalue())
    except EpodError as exc:
        st.error(str(exc))
        return
    except Exception as exc:  # never crash on a bad file
        st.error(f"Could not read this file: {exc}")
        return

    if prep.payable.height == 0:
        show_summary(prep, prep.payable, algarve=algarve)
        st.warning("No payable deliveries found in this file.")
        return

    if isinstance(rates, AlgarveRates):
        show_summary(prep, prep.payable, algarve=True)
        day_rates = algarve_day_rates(prep.payable)
        result = processing.calculate_pay_algarve(prep.payable, rates, day_rates)
        table = reports.summary_table_algarve(result.per_courier, rates.daily_limit)
        all_reports = reports.build_all_reports_algarve(result)
    else:
        ft_names, all_names, detected_ft = resolve_full_time_couriers(prep)
        payable = processing.apply_courier_type_overrides(prep.payable, ft_names)

        show_summary(prep, payable, algarve=False)
        show_courier_type_editor(all_names, detected_ft)
        day_rates = standard_day_rates(payable)

        result = processing.calculate_pay(payable, rates, day_rates)
        table = reports.summary_table(result.per_courier)
        all_reports = reports.build_all_reports(result)

    show_results(result, table)

    st.subheader("Courier reports")
    names = list(all_reports)
    selected = st.selectbox("Preview report for", names, key="report_courier")
    if selected:
        st.code(all_reports[selected], language=None)

    left, right = st.columns(2)
    left.download_button(
        "Download all reports (.zip)",
        data=reports.reports_zip(all_reports),
        file_name="courier_reports.zip",
        mime="application/zip",
        key="dl_zip",
    )
    right.download_button(
        "Download summary (.csv)",
        data=reports.summary_csv(table),
        file_name="courier_pay_summary.csv",
        mime="text/csv",
        key="dl_csv",
    )


if __name__ == "__main__":
    main()
