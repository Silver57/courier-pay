# courier-pay

Internal Streamlit app that reads a courier tracking Excel export (EPOD task
list), calculates each courier's pay, and produces ready-to-send text reports.

Nothing is stored: there is no database, no email sending and no persistence.
Everything lives in the browser session, and the uploaded file only ever exists
in memory.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app.py
```

Then open the URL Streamlit prints (default <http://localhost:8501>).

## Input file

The app always reads the sheet named **`EPOD_TASK_LIST_V2`**; other sheets
(pivot tables etc.) are ignored, and a missing sheet is reported as an error
listing the sheets that *are* in the workbook.

Required columns (exact names — the loader errors and lists any that are
missing):

| Column | Used for |
| --- | --- |
| `Waybill Number` | unique delivery ID (deduplication) |
| `Task Date` | delivery date, text `YYYY-MM-DD` |
| `Task Status` | only `Delivered` is payable |
| `Delivery Type` | `TO_DOOR` or `PUDO` |
| `Courier Name` | who delivered it |
| `Zip Code` | reference |
| `Detailed address` | delivery address / PUDO fallback |
| `PUDO address` | PUDO point (empty for TO_DOOR) |

Real exports have ~90 columns, including **duplicated header names**
(`Contact Phone`, `Seller Name` each appear twice). The loader reads the header
row as data and resolves each required column to its first occurrence, so
duplicates are harmless; everything else is dropped immediately after load.
All cells are read as text, and empty cells (`""`) are treated as null.

## Business rules

**Courier name** is normalized by stripping and collapsing repeated whitespace,
and couriers are grouped by that normalized name. Depot tags such as `QTK-LIS`
are *kept* as part of the name. Removing them instead is a one-line change:
set `STRIP_DEPOT_TAG_DEFAULT = True` in `processing.py` (pattern:
`DEPOT_TAG_PATTERN`). Note that stripping tags also means an `FT` inside a tag
can no longer mark a courier as full-time.

**Courier type** — `FT` if the normalized name contains the uppercase
substring `FT`, otherwise `OUT`. Case-sensitive on purpose, so lowercase `ft`
inside a name (e.g. "Soft") does not match. Isolated in
`processing.courier_type_expr()`.

That rule only *seeds* the roster. The **Courier types** section in the app
lists every courier found in the file and lets you correct who is full-time —
add full-timers whose name carries no `FT`, or remove a false positive. The
selection is the source of truth for pay: everyone selected is `FT`, everyone
else is `OUT`. Corrections apply after parsing
(`processing.apply_courier_type_overrides()`), so they update totals and reports
instantly without re-reading the workbook, and they are remembered for the
session — re-uploading a corrected export keeps the same couriers marked.

**PUDO point identity** — the `PUDO address` when present, otherwise the
`Detailed address`. Isolated in `processing.pudo_point_expr()`.

**Categories** — every payable delivery is exactly one of:

- `TO_DOOR`
- `PUDO_FIRST` — within each group of (normalized courier, task date, PUDO
  point) exactly one PUDO delivery is the first. There is no ordering
  information in the file, so "first" simply means one row per group.
- `PUDO_NEXT` — the remaining PUDO deliveries in that group.

**Rates** — six independent values (courier type × category) set in the
sidebar, in EUR with 2 decimals. Pay per delivery is the matching rate; a
courier's total is the sum of their rows. All money is handled internally as
integer cents, so the reconciliation check (courier totals == grand total) is
exact.

## Data hygiene

Rows are excluded in this precedence order, and every exclusion is counted and
shown in the app (with a preview of the excluded rows):

1. **Duplicate waybill** — deduplicated keeping the first occurrence. Rows with
   no waybill number are never treated as duplicates of each other.
2. **Missing courier name, task date, or delivery type** (including dates that
   do not parse).
3. **Status other than `Delivered`** — broken down per status found.
4. **Delivery type other than `TO_DOOR` / `PUDO`** — broken down per type.

PUDO rows with no address at all are grouped together per courier and day
(1 first + the rest next) and the count is surfaced as a warning.

## Report format

One report per courier, plain text, ready to paste into an email:

```
Courier: Ana Silva (OUT)
Period: 2026-08-13 — 2026-08-14
------------------------------------------
TO_DOOR:     2 x 0.50 = 1.00
PUDO first:  1 x 1.25 = 1.25
PUDO next:   0 x 0.30 = 0.00
TOTAL: 2.25 EUR
------------------------------------------
```

The period spans the courier's first and last delivery date. Downloads: all
reports bundled as a `.zip` (one `.txt` per courier) and the results table as
`.csv`.

## Layout

| File | Contents |
| --- | --- |
| `app.py` | Streamlit UI only |
| `processing.py` | all Polars logic: load, clean, classify, calculate |
| `reports.py` | text report building and download payloads |
| `tests/` | pytest suite |

The parsed file is cached with `st.cache_data` keyed on the uploaded bytes, so
changing a rate recalculates without re-reading the workbook. Parsing and
classifying 500k rows takes well under a second, and a rate change recomputes
in ~10 ms.

## Tests

```bash
.venv/bin/python -m pytest tests -q
```
