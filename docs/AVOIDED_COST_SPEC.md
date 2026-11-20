# der-opt avoided cost specification

**Version 1.0 — approved 2026-11-20.** Not yet implemented. Change it only by a new
version, approved the same way.

## Purpose

Avoided costs turn a measure's impacts into dollars:

- **Energy attribute:** the hourly change in net load against the counterfactual (R1),
  over all 8760 hours, valued at hourly avoided costs.
- **Capacity attribute:** the measure's DR capacity in kW (docs/DR_CAPACITY_SPEC.md,
  counted and accounted, per reference and basis), valued at capacity prices.

The spec defines one stored format that holds either a **CPUC Avoided Cost Calculator
(ACC) vintage** or **your own curves**. Swapping between them is a file change, not a
code change. It also defines which components count as energy and which as capacity,
so capacity is never valued twice. Discounting, measure life and the cost tests
themselves belong to the cost-test export spec, which comes next.

## 1. An avoided cost set

A set is one folder:

```
avoided_costs/acc_2024_pge_cz12/
  manifest.yaml
  hourly.csv          # every hour of every year in `years`, one file
  capacity.csv
```

A scenario points at the folder: `avoided_costs: {set: avoided_costs/acc_2024_pge_cz12}`.

### manifest.yaml

```yaml
set_id: acc_2024_pge_cz12
source: CPUC ACC 2024 (v1b), PG&E, climate zone 12   # or "internal: <description>"
location: {utility: PGE, climate_zone: "12"}
dollars: {basis: nominal, base_year: 2024}             # or real, with base_year
at: meter                                            # values already include losses to the customer meter
years: [2026, 2050]                                  # hourly.csv covers every hour of these years
components:                                          # every column in the hourly files, classified
  energy:              {class: energy}
  ancillary_services:  {class: energy}
  cap_and_trade:       {class: energy}
  ghg_adder:           {class: energy}
  ghg_rebalancing:     {class: energy}
  generation_capacity: {class: capacity, capacity_component: generation}
  transmission:        {class: capacity, capacity_component: transmission}
  distribution:        {class: capacity, capacity_component: distribution}
emissions: marginal_emissions_tco2_per_mwh           # optional non-dollar column
extend: null                                         # see decision 5
```

Every component has a class:
- **`energy`:** values the energy attribute.
- **`capacity`:** the hourly share of a capacity value. It's used only for the "full
  hourly" comparison (section 3), never added to DR capacity.

**DR capacity is valued separately.** It's priced only from `capacity.csv` ($/kW-year
and monthly shares), never from the hourly capacity-class components. The two
calculations share no inputs, and their results are reported in separate columns.

### Hourly file (`hourly.csv`)

One file for the whole set. It follows the input data spec's rules: CSV, UTF-8, one
header row, and `timestamp` as the interval start in ISO 8601 with a UTC offset.

| Column | Meaning |
|---|---|
| `timestamp` | every hour of every year in `years`, in order, including both daylight-saving days (8760 rows a year, 8784 in a leap year) |
| one column per component | $/kWh at the meter, named as in the manifest |
| emissions column (optional) | tCO2 per MWh |

### capacity.csv

Capacity prices for the DR capacity attribute, with their allocation across months:

| Column | Meaning |
|---|---|
| `year` | the year |
| `capacity_component` | `generation`, `transmission` or `distribution` |
| `price_per_kw_year` | $/kW-year |
| `m1` ... `m12` | the share of the year's value each month carries; each row sums to 1 |

The monthly shares say which months capacity is worth having in. For an ACC vintage,
they come from the ACC's hourly allocation of that component, summed by month. For your
own curves, you set them.

## 2. Validation

`der-opt check-data <set folder>` checks a set and reports what it covers. It rejects:
- missing columns, or a component without a class;
- hourly rows that don't cover every hour of every year in `years` exactly once;
- allocation shares that don't sum to 1;
- negative capacity prices.

It warns about hourly values outside a plausible range (below -$1 or above $10 per kWh),
which usually signals a $/MWh slip.

## 3. What der-opt computes

For the analysis year (the run's year), in each set's dollars:

| Result | How |
|---|---|
| **Energy value** | sum over hours of (R1 net load - measure's net load) x the energy-class components, by component |
| **DR capacity value** | from `capacity.csv` only: for each capacity component, price_per_kw_year x sum over months (share x the month's DR capacity kW), for each DR reference (R2, R3, S) and basis (net, import), counted and accounted |
| **Full hourly comparison** | the same hourly sum using every component, capacity included. Reported for reference only, and never added to the capacity value |
| **Emissions** | sum over hours of the change in net load x marginal emissions, in tCO2, if the set has it |

All of these are written to `avoided_costs.csv`, by component, with the set's
`set_id`. They're also recorded in the run record.

The energy attribute is valued on the **net** basis: energy exported to the grid
displaces generation just as avoided consumption does. The capacity attribute carries
whichever DR basis and reference you choose.

## 4. Getting the ACC into this format

The CPUC distributes the ACC as large spreadsheets, and their layout changes between
vintages. An importer (`der-opt import-acc`) is built per vintage, against the actual
files, and writes a set in this format: hourly components, the classification above,
and monthly capacity shares. It can't be written until there's a real ACC file to
build it against (thread A1).

## Decisions (approved with version 1.0)

1. **Hourly data:** one file (`hourly.csv`) for every year, components as columns.
2. **Timestamps:** ISO 8601 with offset, as in the input spec. The importer converts the
   ACC's hour-of-year once.
3. **Classification:** energy, ancillary services and GHG components as energy;
   generation, transmission and distribution capacity as capacity. Overridable per
   set. DR capacity is valued separately, from `capacity.csv` only.
4. **Capacity allocation:** monthly shares.
5. **Years outside the set:** an error, unless the manifest declares `extend`.
6. **Mapping the analysis year onto other years:** by month, day and hour, dropping or
   repeating February 29 as needed.

## Threads to pull

- **A1. ACC importer.** Needs the actual ACC vintage files you use (and which utility
  and climate zone) to build `import-acc` against.
