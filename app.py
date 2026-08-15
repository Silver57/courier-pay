"""courier-pay — internal Streamlit app.

Upload an EPOD task-list export, set the rate matrix, get per-courier pay and
ready-to-send text reports. Nothing is stored: state lives in the session only.
"""

from __future__ import annotations

import polars as pl
import streamlit as st

import processing
import reports
from processing import (
    CAT_PUDO_FIRST,
    CAT_PUDO_NEXT,
    CAT_TO_DOOR,
    COURIER_TYPES,
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
DEFAULT_RATES: dict[RateKey, float] = {
    (ctype, cat): 0.0 for ctype in COURIER_TYPES for cat in CATEGORY_LABELS
}
FT_STATE_KEY = "ft_couriers"


@st.cache_data(show_spinner="Parsing Excel file…")
def cached_prepare(file_bytes: bytes) -> PreparedData:
    """Parse + clean + classify. Cached on file content, so changing rates
    never re-parses the workbook."""
    return processing.prepare_from_bytes(file_bytes)


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
                value=DEFAULT_RATES[(ctype, cat)],
                step=0.01,
                format="%.2f",
                key=f"rate_{ctype.lower()}_{cat.lower()}",
            )
    return rates


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


def show_summary(prep: PreparedData, payable: pl.DataFrame) -> None:
    stats = prep.stats
    period = processing.date_range(payable)
    counts = processing.courier_counts_by_type(
        payable.select("courier_norm", "courier_type").unique()
    )

    cols = st.columns(5)
    cols[0].metric("Rows in file", f"{stats.total_rows:,}")
    cols[1].metric("Payable rows", f"{stats.payable_rows:,}")
    cols[2].metric(
        "Date range",
        f"{period[0]} → {period[1]}" if period else "—",
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


def show_results(result: processing.PayResult) -> None:
    st.subheader("Results")
    table = reports.summary_table(result.per_courier)
    grand = pl.DataFrame(
        [
            {
                "Courier": "TOTAL",
                "Type": "",
                "TO_DOOR": int(result.per_courier["n_to_door"].sum() or 0),
                "PUDO first": int(result.per_courier["n_pudo_first"].sum() or 0),
                "PUDO next": int(result.per_courier["n_pudo_next"].sum() or 0),
                "Deliveries": int(result.per_courier["n_total"].sum() or 0),
                "Total (EUR)": reports.format_eur(result.grand_total_cents),
            }
        ],
        schema=table.schema,
    )
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

    rates = rates_sidebar()
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
        show_summary(prep, prep.payable)
        st.warning("No payable deliveries found in this file.")
        return

    ft_names, all_names, detected_ft = resolve_full_time_couriers(prep)
    payable = processing.apply_courier_type_overrides(prep.payable, ft_names)

    show_summary(prep, payable)
    show_courier_type_editor(all_names, detected_ft)

    result = processing.calculate_pay(payable, rates)
    show_results(result)

    st.subheader("Courier reports")
    all_reports = reports.build_all_reports(result, rates)
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
        data=reports.summary_csv(result.per_courier),
        file_name="courier_pay_summary.csv",
        mime="text/csv",
        key="dl_csv",
    )


if __name__ == "__main__":
    main()
