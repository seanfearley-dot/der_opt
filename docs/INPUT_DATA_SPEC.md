# der-opt input data specification

**Version 2.0 — approved 2026-11-21** with the population model
([POPULATION_MODEL_SPEC.md](POPULATION_MODEL_SPEC.md)). 2.0 adds a devices file, premise
settings and tags, and per-device time series (section 0); everything else is as in 1.1
(1.1 added `customer_class` to premises, for DR settlement baselines; see
docs/DR_CAPACITY_SPEC.md). `der-opt check-data` validates files against it,
and scenarios read these files directly (see the README's Input data section). Change
it only by a new version, approved the same way.

## 0. What 2.0 changes

- **A devices file, `devices.csv`, one row per device** (section 2a). It replaces the
  premises file's device columns (`pv_kw_ac`, `battery_kwh`, `battery_kw`,
  `ev_battery_kwh`, `ev_charger_kw`, `ev_v2x`).
  - v1.1 premises files with those columns still load, with a note. `der-opt convert
    premises` writes the two 2.0 files.
  - A premises file can't have both those columns and a devices file.
- **The premises file gains:**
  - `kind`: `premise` (the default), or `background` for load through an element that
    isn't a customer, with no devices or bill;
  - premise settings as columns, by name: `participation`, `baseline_behavior`,
    `response`, `envelope_weight`, `exclusive_import_export` (`0`/`1`), `income_tier`,
    `baseline_code`, `poi_limit_kw`, and `load` (`none` for a premise with no load);
  - **tags:** any other column, carried through and usable to select premises.
- **Time series gain:**
  - an optional `device_id` key column;
  - `outdoor_temp_c` (°C, may be negative), for thermostats;
  - `flexible_kw` (kW), a flexible load's baseline.

  A shared weather file can be keyed by a `weather_station` column instead of
  `premise_id`, and a premise picks its station with a `weather_station` tag.

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

## 2a. Devices file (2.0)

One row per device. `devices.csv`:

| Column | Required | Meaning |
|---|---|---|
| `premise_id` | yes | the premise it's behind (in the premises file) |
| `device_id` | yes | unique within the premise; letters, digits, `_` and `-` |
| `type` | yes | `solar`, `battery`, `ev`, `thermostat` or `flexible_load` |
| `template` | no | a device template's name (POPULATION_MODEL_SPEC.md section 2) |
| `kw` | per type | `solar`: inverter AC rating (a check on `pv_kw`); `battery`: charge and discharge rating; `ev`: charger rating; `thermostat`: HVAC electric rating; `flexible_load`: the most it can shed |
| `kwh` | per type | `battery`, `ev`: capacity |
| `v2x` | no | EVs: `1` if it can discharge (at `kw`), else `0` |
| `installed_cost`, `annual_om` | no | the device's cost to the participant: upfront $, and $/year |
| `charge_sources` | no | devices at the same premise it may charge from, by `device_id`, `;`-separated |
| any device parameter | no | by its name (`degradation_cost_per_kwh`, `soc_departure_min_frac`…); a plain number. An unknown column is rejected, naming the type's parameters |

```csv
premise_id,device_id,type,template,kw,kwh,v2x
home_01,solar,solar,,4.8,,
home_01,ev,ev,commuter_charger,7.2,60,0
home_06,battery,battery,battery_13kwh,5,13.5,
```

Each device reads its series from the time series files:
- solar: `pv_kw`;
- EV: `ev_connected` and `ev_driving_kwh`, or its template's daily `pattern`;
- thermostat: `outdoor_temp_c`;
- flexible load: `flexible_kw`.

Rows match on `premise_id` and `device_id`, or on `premise_id` alone when the premise
has one device of that type.

## 3. Network file

One row per transformer or line section. `network.csv`:

| Column | Required | Meaning |
|---|---|---|
| `element_id` | yes | unique |
| `parent_id` | no | the element upstream; empty for the root |
| `rating_kw` | no | thermal limit in kW, both directions (convert kVA with power factor before writing) |
| `overload_cost_per_kwh` | no | empty = hard limit; a number = soft limit at that cost (with an emergency rating: the cost between normal and emergency) |
| `emergency_rating_kw` | no | the emergency rating, above `rating_kw` (coordination spec v1.1, section 1b) |
| `violation_cost_per_kwh` | no | the cost above the emergency rating; empty = a hard wall there |
| `emergency_kwh_per_day` | no | at most this many kWh above normal each local day |

## Validation

`der-opt check-data <file>` checks a file against this spec and reports, at three levels:

- **Rejected (the file won't load):**
  - a missing required column, a bad timestamp or missing offset;
  - in a devices file: a repeated `premise_id` + `device_id`, an unknown `type`, or a
    column that isn't any device type's parameter;
  - a premise setting with a value it can't take, such as `response: rules` without
    `baseline_behavior: rule_based`;
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
