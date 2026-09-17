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
missing). The Algarve export names three of them differently; either name is
accepted, whatever the pay mode, and the standard name wins if a file has both:

| Column | Algarve export | Used for |
| --- | --- | --- |
| `Waybill Number` | | unique delivery ID (deduplication) |
| `Task Date` | | delivery date, `YYYY-MM-DD` (text, or an Excel date cell) |
| `Task Status` | | only `Delivered` is payable |
| `Delivery Type` | | `TO_DOOR` or `PUDO` |
| `Courier Name` | `Driver` | who delivered it |
| `Zip Code` | `Destination Zip Code` | reference |
| `Detailed address` | `Address` | delivery address / PUDO fallback |
| `PUDO address` | | PUDO point (empty for TO_DOOR) |

Alternative names live in `processing.COLUMN_ALIASES`; add a line there if
another export renames a column.

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
courier's total is the sum of their rows.

**Sundays** — deliveries whose task date is a Sunday are already paid, in both
pay modes. They count toward the courier's total deliveries (the `Sundays`
column) but pay nothing and sit outside the categories and Algarve tiers.
Isolated in `processing.sunday_expr()`. All money is handled internally as
integer cents, so the reconciliation check (courier totals == grand total) is
exact.

### Algarve mode

The **Pay mode** switch at the top of the sidebar picks **Standard** (the rules
above) or **Algarve** for the whole uploaded file. Algarve pay uses three
sidebar inputs: a daily limit (default 70), the rate up to that limit, and the
rate above it.

- The count restarts for **each courier on each task date**.
- Tiers are marginal: a courier with 85 deliveries on one day is paid
  70 × the up-to rate + 15 × the above rate.
- Every payable delivery counts toward the limit, whatever its type (TO_DOOR,
  PUDO first and PUDO next alike). Rows excluded by the hygiene rules below
  (non-Delivered, duplicates, …) do not count.
- FT/OUT does not apply: every courier has the same two rates, and the courier
  types editor is hidden.

The file has no delivery order, so row order decides which deliveries of a day
fall above the limit. Totals are unaffected, since every delivery in a tier has
the same rate. Implemented in `processing.calculate_pay_algarve()`.

Rates and FT corrections entered in one mode are kept when you switch to the
other and back.

### Daily price adjustments

Normally every day is paid at the sidebar prices. Once a file is uploaded, the
**Daily price adjustments** table lists every day from the file's first to last
task date (with its delivery count), so specific days can be paid differently:

- **Standard** — one column per rate (`FT TO_DOOR` … `OUT PUDO next`).
- **Algarve** — `Daily limit`, `Up to limit` and `Above limit`, so a day can
  have its own limit as well as its own prices.

Enter values only on the days that differ. A blank cell on an adjusted day
keeps the sidebar value, and an adjustment applies to every courier that day.
Entering anything on a **Sunday** makes that Sunday a normal paid day (no longer
"já pagas"), with blank cells at the sidebar prices; untouched Sundays stay
already paid.

Reports need no special wording: the "pagas a X€" lines already group
deliveries by the price they were actually paid, so an adjusted day's
deliveries simply appear under their price. Adjustments are remembered per mode
for the session, keyed by date, so they survive mode switches and re-uploading
the same file. Implemented through the optional `day_rates` argument of
`processing.calculate_pay()` / `calculate_pay_algarve()`.

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

In Algarve mode the report shows the two tiers instead (the limit in the
labels follows the sidebar value):

```
Courier: Ana Silva
Mode: Algarve (70/day)
Period: 2026-08-13 — 2026-08-15
------------------------------------------
Up to 70/day: 180 x 1.00 = 180.00
Above 70/day: 15 x 0.80 = 12.00
TOTAL: 192.00 EUR
------------------------------------------
```

The period spans the courier's first and last delivery date. Downloads: all
reports bundled as a `.zip` (one `.txt` per courier) and the results table as
`.csv` (Algarve columns: Courier, Days, Up to N/day, Above N/day, Deliveries,
Total).

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
