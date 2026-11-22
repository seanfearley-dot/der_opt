# der-opt per-customer coordination settings specification

**PARKED (2026-11-21): superseded by [POPULATION_MODEL_SPEC.md](POPULATION_MODEL_SPEC.md),**
which covers per-premise settings, device parameters and the technology mix in one model.
Kept for its reasoning; nothing here will be built as written.

Version 0.2 — DRAFT (not approved).
v0.2: synthetic houses were removed from der-opt, so their section is gone.

## Purpose

The coordination spec ([COORDINATION_SPEC.md](COORDINATION_SPEC.md)) gives each customer
coordination settings:
- `participation`;
- `baseline_behavior`;
- `response`;
- `rules`;
- `envelope_weight` (for the `weights` envelope method).

Today a customer can only get its own settings if it's written out under `houses:`.
Customers built from `premises.csv` all get `house_defaults`.
This spec lets every customer have its own settings, however it was built:
1. **`premises.csv` columns** for the settings that fit in a cell (section 1). This
   changes the input data spec from v1.1 to v1.2.
2. **`house_overrides`** in the feeder scenario, by house name, for any house, rules
   included (section 2).

It also makes each customer's **technology mix** visible the same way for every source,
before a solve (section 4).

A premise is one row of `premises.csv`, one meter: these settings are per meter, the
"connection point" unit envelopes are allocated to.

## 1. `premises.csv` columns (input data spec v1.2)

New optional columns. A blank cell means `house_defaults`.

| Column | Values | Meaning |
|---|---|---|
| `participation` | `participant`, `non_participant` | in the program or not |
| `baseline_behavior` | `own_bill`, `rule_based` | what its devices do with no program |
| `response` | `optimized`, `rules` | how a participant responds to the mechanisms |
| `envelope_weight` | a number ≥ 0 | its weight for the `weights` envelope method |

```csv
premise_id,element_id,tariff,export_program,...,participation,baseline_behavior,response
home_01,tx_1,pge_e_elec_2026-06,nbt,...,,,
home_02,tx_1,pge_e_elec_2026-06,nbt,...,,rule_based,rules
home_03,tx_1,pge_e_tou_c_2026-06,nem2,...,non_participant,rule_based,
```

`check-data` checks the values. It also reports a row with `response: rules` and
`baseline_behavior: own_bill` as an error, since rules keep the baseline rules, so they
need a rule-based baseline. Files without these columns are unchanged: v1.1 files are
valid v1.2 files.

Rules stay out of the file: they're structured (mechanism → device → action and
settings), and a CSV cell would hide that. They go in `house_overrides`, or in
`house_defaults` for everyone.

## 2. `house_overrides`

A mapping in the feeder scenario, keyed by house name, for a house from any source:
written out under `houses:` or from `premises.csv`.

```yaml
house_overrides:
  home_03: {participation: non_participant}
  home_06:
    response: rules
    baseline_behavior: rule_based
    rules: {dr_event: {battery: {action: discharge, to: export}}}
  tx_a_h04: {envelope_weight: 2}
```

- **What an override can set:** the coordination settings only: `participation`,
  `baseline_behavior`, `response`, `rules`, `envelope_weight`. A house's devices,
  tariff and load still come from where it was built (decision 2).
- **Rules** merge key by key over `house_defaults.rules`, as on a written-out house.
- **`validate`** reports, as errors:
  - an override naming a house that doesn't exist (with the closest names);
  - a setting outside the list above;
  - the same mistakes in values and rules it already catches on written-out houses.
- **It composes like the rest of a scenario:** a library block can hold overrides, and
  mappings merge, so several blocks can each add some.

## 3. Precedence and what's reported

For each setting, the first of these that sets it:
1. `house_overrides`;
2. the house's own source: its `houses:` entry or its `premises.csv` cell;
3. `house_defaults`;
4. the built-in defaults: `participant`, `own_bill`, `optimized`.

`houses.csv` (written with `-o`) gets a column for each setting, and a `settings_from`
column naming the source of anything not from the defaults (e.g.
`participation: override; baseline_behavior: premises.csv`). That way a large feeder's
assignments can be checked at a glance.

## 4. Each customer's technology mix

Today `houses.csv` lists device *names*, plus size columns from `premises.csv`
(`pv_kw_ac`, `battery_kwh`...), and no sizes at all for written-out houses. The run summary counts solar, batteries and EVs by name, and misses
thermostats and flexible loads. This section replaces that with one table read from
the devices as built, so it's right whatever the source.

**Columns, one row per house:**

| Column | From the built device |
|---|---|
| `solar_kw` | peak available output over the run |
| `battery_kwh`, `battery_kw` | usable capacity, larger of charge and discharge rating |
| `ev_kwh`, `ev_charger_kw`, `ev_v2x` | capacity, charge rating, and whether it can discharge |
| `ev_miles_per_day` | from its daily pattern, if it has one (weekday, weekend) |
| `thermostat_kw` | HVAC electric rating |
| `flexible_kw` | the most a flexible load can shed (block size × blocks) |
| `n_<type>` | only when a house has more than one device of a type; sizes are then totals |

- **Also in the table:**
  - the house's element, data source (gross or metered) and tariff;
  - its coordination settings with `settings_from` (section 3);
  - each run's bill, after these columns.
- **Written** as `houses.csv` with `-o`, as now.
- **Shown before a solve:**
  - `der-opt validate` on a feeder prints the mix by element and by group, using
    device types, not names. For example:
    `tx_1: 3 houses (participants, optimized: 2), 2 solar 10.4 kW, 3 EV (1 V2X), 1 battery 13.5 kWh`;
  - `der-opt validate --houses FILE` writes the table without running anything;
  - the run summary uses the same counts.

## 5. Decisions for you

Each has a recommendation, which the draft assumes.

1. **Where per-customer settings can come from:**
   - **`premises.csv` columns for the four scalar settings, and `house_overrides` for
     any house, rules included (recommended);**
   - `house_overrides` only, with no new columns. This keeps the input spec at v1.1,
     but a large customer list needs a long YAML mapping.
2. **What `house_overrides` can change:**
   - **only the coordination settings (recommended).** Devices, tariffs and load stay
     with their source, so a premise's devices are always what `premises.csv` says;
   - any house setting (limits, billing, device parameters). It's more powerful, but
     two places would then describe one premise.
3. **Precedence:** **override, then the house's own source, then `house_defaults`
   (recommended).**
4. **Technology mix:**
   - **one `houses.csv` layout for every source, read from the built devices, with
     `validate` printing the mix and `--houses` writing it before a solve
     (recommended);**
   - keep today's source-specific columns, adding only device types.
