# der-opt input data specification

**Version 1.1 — approved 2026-11-19** (1.1 adds `customer_class` to premises, for DR
settlement baselines; see docs/DR_CAPACITY_SPEC.md). `der-opt check-data` validates files against it,
and scenarios read these files directly (see the README's Input data section). Change
it only by a new version, approved the same way.

## Purpose

This is the format to produce from your own data pipeline so files load into der-opt
without per-file configuration. It replaces format guessing (utility exports, ad hoc
CSVs) with one contract. A file that passes `der-opt check-data` will load; one that
fails gets a message naming the rule it broke.

## Scope

The spec covers three kinds of file:

| File | What it holds | Needed for |
|---|---|---|
| **Time series** | anything that varies by interval: load, solar, prices, EV plug-in and driving | every run |
| **Premises** | one row per premise: where it connects, its rate, its devices | feeder runs, or many-premise runs |
| **Network** | one row per transformer or line section | feeder runs |

Device behavior (efficiencies, charge limits, degradation cost) and run settings stay in
the scenario YAML. These files hold data, not settings.

## Rules for every file

1. **Format:** CSV, UTF-8, comma-separated, with one header row first. No blank lines,
   no notes above the header, no totals rows.
2. **Names:** lower-case `snake_case` column names, exactly as listed below. Columns not
   listed here are allowed and ignored, so you can carry your own fields.
3. **Numbers:** plain decimals (`1.25`), no thousands separators, no units in cells.
4. **Missing values:** an empty cell. Never `0`, `NA`, `-999` or a dropped row. Zero
   means zero.

## 1. Time series files

### Layout: long, one row per premise per interval

```csv
timestamp,premise_id,load_kw,pv_kw
2026-07-13T00:00:00-07:00,house_01,0.82,0
2026-07-13T00:15:00-07:00,house_01,0.79,0
2026-07-13T00:00:00-07:00,house_02,1.31,0
```

### Columns

| Column | Required | Meaning |
|---|---|---|
| `timestamp` | yes | **start** of the interval, ISO 8601 **with UTC offset**: `2026-07-13T00:00:00-07:00` (or `Z` for UTC) |
| `premise_id` | yes, except in shared files (below) | matches `premise_id` in the premises file; letters, digits, `_` and `-` only |
| one or more value columns | yes | from the table below |

### Value columns

The unit is part of the name. All power is **average kW over the interval**, never kWh.
Values are never negative; direction is in the name.

| Column | Unit | Meaning |
|---|---|---|
| `load_kw` | kW | **gross** consumption: everything the premise uses, with no solar subtracted |
| `pv_kw` | kW | solar production; if `pv_kw` is given, it is what the solar device can produce |
| `import_kw` | kW | metered energy from the grid (use with `export_kw` when gross load isn't known) |
| `export_kw` | kW | metered energy to the grid |
| `ev_connected` | 0 or 1 | 1 while the EV is plugged in for the whole interval |
| `ev_driving_kwh` | kWh | energy the EV uses driving during the interval. The one column in kWh, because it is energy drawn, not a power level |
| `import_price_per_kwh` | $/kWh | price to buy |
| `export_price_per_kwh` | $/kWh | price paid for export |

**Load comes in one of two forms per premise, never both:**
- **Gross (preferred):** `load_kw`, plus `pv_kw` if the premise has solar. The model can
  then dispatch solar, batteries and EVs.
- **Metered:** `import_kw` and `export_kw`. These already include whatever solar and
  storage the premise had, so a scenario can't add those devices to the premise on top.

### Shared series

Prices, and anything else that is the same for every premise, go in a file with no
`premise_id` column. For example `prices.csv` holds `timestamp, import_price_per_kwh,
export_price_per_kwh`.

### Time rules

- **Interval:** 5, 15, 30 or 60 minutes, the same throughout a file. Every interval in
  the covered period has a row, including both daylight-saving days. November's
  repeated hour appears twice, with different offsets (`01:00-07:00` and `01:00-08:00`).
  March's skipped hour doesn't appear.
- **Coverage:** a file must cover the whole run period of any scenario that uses it.
- **Order:** sorted by `premise_id`, then `timestamp`. Each `premise_id` + `timestamp`
  appears once.
- **Resolution vs the run:** a file may be finer than the run's step. It's then averaged
  over each step: 15-minute load in an hourly run. Load is never made finer than the
  file. A price may be held across finer steps.

## 2. Premises file

One row per premise. `premises.csv`:

| Column | Required | Meaning |
|---|---|---|
| `premise_id` | yes | unique |
| `element_id` | feeder runs | the transformer or section it's served from (network file) |
| `customer_class` | for DR settlement | `residential` or `commercial` (sets 5-in-10 or 10-in-10) |
| `tariff` | for billing | tariff file name, e.g. `pge_e_elec_2026-06` |
| `export_program` | for billing | `nbt`, `nem2` or empty |
| `baseline_territory` | if the tariff has a baseline | PG&E letter, e.g. `R` |
| `pv_kw_ac` | if solar | inverter rating; with `pv_kw` time series, used only as a check |
| `battery_kwh`, `battery_kw` | if a battery | usable capacity and power rating |
| `ev_battery_kwh`, `ev_charger_kw` | if an EV | needs `ev_connected` and `ev_driving_kwh` time series |
| `ev_v2x` | if an EV | `1` if it can discharge to the home or grid, else `0` |
| `import_limit_kw` | no | most the premise can draw from the grid (service or panel rating) |
| `export_limit_kw` | no | most it can send back (interconnection limit; `0` = non-export) |

## 3. Network file

One row per transformer or line section. `network.csv`:

| Column | Required | Meaning |
|---|---|---|
| `element_id` | yes | unique |
| `parent_id` | no | the element upstream; empty for the root |
| `rating_kw` | no | thermal limit in kW, both directions (convert kVA with power factor before writing) |
| `overload_cost_per_kwh` | no | empty = hard limit; a number = soft limit at that cost |

## Validation

`der-opt check-data <file>` checks a file against this spec and reports, at three levels:

- **Rejected (the file won't load):**
  - a missing required column, a bad timestamp or missing offset;
  - an interval that isn't uniform, a duplicate row, a negative value;
  - a premise with both gross and metered columns;
  - an unknown `premise_id` or `element_id`;
  - a network cycle.
- **Warnings (it loads, but look):**
  - empty cells, and how many;
  - 24 h or more with no consumption;
  - 12 h or more of an unchanged value;
  - a spike over 10x the 99th percentile;
  - a value that looks like the wrong unit, e.g. a home over 100 kW;
  - `pv_kw` above `pv_kw_ac`.
- **Report:** the period covered, the interval, the premises, and total and peak kWh
  per premise.

The run checks then treat any empty cells the run actually uses as gaps: the value
before them is repeated, with an `input data` warning.

## Decisions (approved with version 1.0)

Each was approved as recommended; the spec above follows them.

1. **Timestamps: with UTC offset (recommended), or local clock without one.** An offset
   makes every row unambiguous, including November's repeated hour, and every tool
   writes it (pandas: `ts.isoformat()`; SQL: `to_char(... 'YYYY-MM-DD"T"HH24:MI:SSOF')`).
   A local clock is easier to read but can't tell the two 1 am hours apart.
2. **Values: average kW (recommended), or kWh per interval.** kW reads the same at any
   interval and is what the model uses. kWh is what meters record, so it's slightly
   easier to export but easy to mix up when intervals differ.
3. **Layout: long (recommended), or wide (one column per premise).** Long handles any
   number of premises and several quantities in one file, and comes straight out of a
   database. Wide is easier to eyeball in Excel, but needs one file per quantity.
4. **The two ingestion paths I just built:** retire the PG&E Download My Data reader and
   the per-file config (recommended), or keep them alongside the spec. Retiring them
   leaves one documented way in. The repair logic (gaps, daylight saving, unit
   conversion) moves into `check-data` and the spec reader.
