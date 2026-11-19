# der-opt

A toolkit for modeling and optimizing distributed energy resources — solar, batteries,
smart thermostats, curtailable load, and V2X EVs — under resource-specific constraints.
Solves cost-minimizing dispatch for a premise (Pyomo + HiGHS), recovers dual/shadow
prices, and compares optimal dispatch against realistic rule-based baselines
(counterfactuals) to quantify the value of smart control.

## Setup

```bash
cd "/Users/seanearley/optimization tools"
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Optional extras: `.[pdf]` (re-parse PG&E NBT price-sheet PDFs), `.[validation]`
(PySAM bill cross-check), `.[lmp]` (wholesale LMP via `gridstatus`).

Check it works: `pytest -q` (should be all green).

## Running a scenario

```bash
der-opt run examples/scenarios/battery_solar_lbnl_nbt.yaml
der-opt run examples/scenarios/v2x_lbnl_nbt.yaml --duals -o out/v2x
```

It prints the optimal dispatch's cost broken down by source. If the scenario has a
`counterfactual` section, it also simulates that rule-based baseline and prints its
cost and the **value of optimization** (counterfactual cost minus optimal cost), plus
a note if a battery/EV ends the horizon with a different charge in the two runs (see
Modeling caveats — that difference isn't priced, so it tilts the comparison).

| Flag | Effect |
|---|---|
| `--duals` | print the dual/shadow-price families, marking exact LP duals vs. fix-and-resolve approximations |
| `-o DIR` | write everything printed as CSVs and charts (below) |
| `--no-counterfactual` | skip the scenario's counterfactual comparison |

Files written with `-o`:

| File | Contents |
|---|---|
| `device_dispatch.csv`, `grid_import.csv`, `grid_export.csv` | optimal dispatch, per period |
| `value_breakdown.csv` | $ by source, optimal and counterfactual side by side, with a total row |
| `dispatch.png`, `value_stack.png` | optimal dispatch and its value stack |
| `counterfactual_*.csv`, `counterfactual_dispatch.png` | the same for the counterfactual |
| `comparison.png` | optimal vs. counterfactual grid flows and value breakdown |
| `duals.csv`, `duals.png` | all dual families (long format), and a chart of the marginal value of energy at the meter (with `--duals`) |

State columns (`soc`, `temp`) are the value at the **start** of each period in both
the optimal and counterfactual CSVs.

The fastest way to a new scenario: **copy one of the files in `examples/scenarios/`
and edit it.** Both are verified to reproduce their Python-script counterparts
(`examples/04_*.py`, `examples/05_*.py`) exactly.

## Scenario file reference

```yaml
name: my_house                      # any label; prefixes constraint names in --duals output

time_grid:
  resolution_minutes: 60            # any resolution; LBNL, PVWatts, daily_tou need 60
  horizon_steps: 72                 # number of periods (72 hourly = 3 days)
  start: "2025-07-15T00:00:00"      # required for anything date/hour-of-day aware

devices: [...]                      # see Devices below; order matters for charge_sources
value_streams: [...]                # see Value streams below
external_load: ...                  # kW the premise must serve; see Data sources below

poi_limit_kw: 15.0                  # max import/export at the meter
exclusive_import_export: true       # see Modeling caveats -- turn on whenever export
                                    # price can exceed import price (NBT, LMP)

counterfactual:                     # optional; see Counterfactuals below
  policies:
    battery: {type: self_consumption}
```

### Devices

Every device has `type`, a unique `name`, and `params`. Any array-valued param
(marked *array*) can be a literal list or a data source (see below).

| `type` | Required params | Optional params (default) |
|---|---|---|
| `solar` | `available_kw` *array* | — |
| `battery` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw` | `eta_charge` (0.95), `eta_discharge` (0.95), `soc_min_frac` (0), `soc_max_frac` (1), `soc_init_frac` (0.5), `soc_final_min_frac`, `degradation_cost_per_kwh` (0), `charge_sources` |
| `ev_v2x` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw`, `connected` *array* (0/1 plug-in schedule) | same SOC/efficiency/degradation params as battery, plus `departure_period` (int or list), `soc_departure_min_frac` (float or list), `charge_sources` |
| `thermostat` | `thermal_resistance_r`, `thermal_capacitance_c`, `ambient_temp_c` *array*, `comfort_min_c`, `comfort_max_c`, `cop`, `p_max_kw`, `initial_temp_c` | `mode` (`cooling` / `heating`) |
| `curtailable_load` | `baseline_load_kw` *array*, `block_size_kw`, `max_blocks` | — |

- **`charge_sources: [solar]`** — restricts a battery/EV to charging no faster than
  the named solar device(s) are generating at that moment (SGIP-style "no grid
  charging"). The named devices must be **listed earlier** in `devices`.
- **`departure_period`** — period index (0-based, from `start`) of each departure.
  For a multi-day run, list one per morning: `[9, 33, 57]` for 3 days at 9am.
  A single value only protects that one departure.

### Value streams

| `type` | Params |
|---|---|
| `energy_arbitrage` | `import_price`, `export_price` — each a price series (below) |
| `tariff_energy_charge` | `tariff: {urdb_json_path: ...}`, `direction` (`import`/`export`) — tiered TOU tariffs from OpenEI URDB JSON |
| `demand_charge` | `rate_per_kw` |
| `capacity_payment` | `event_periods`, `payment_per_kw_event`, `committed_kw` |

Price series (`import_price` / `export_price`):

```yaml
{type: constant, price: 0.25}
{type: daily_tou, hourly_pattern: [24 values, $/kWh by hour of day]}
{type: csv, path: prices.csv, column: price}          # first column = timestamps
{type: nbt_export, csv_path: examples/data/pge_nbt_eec_2026.csv}   # real PG&E NEM3/NBT
                                                       # add include_produced: false for CCA/DA customers
```

### Data sources

`external_load` and any *array* device param accept:

```yaml
[1.2, 1.3, ...]                                   # literal, length = horizon_steps
{type: csv, path: load.csv, column: load_kw}      # timestamp-indexed, resampled to the grid

{source: lbnl_net_load,   scenario_dir: ..., cluster_name: ..., start_hour_ending: 4681}
{source: lbnl_gross_load, scenario_dir: ..., cluster_name: ..., start_hour_ending: 4681}
{source: lbnl_pv,         scenario_dir: ..., cluster_name: ..., start_hour_ending: 4681}
{source: ev_connected_schedule, arrivals_csv: ..., departures_csv: ..., percentile: 50}
{source: pvwatts, lat: 37.77, lon: -122.42, system_capacity_kw: 6.0, start_hour: 4680}
```

- **LBNL** (`~/Documents/LBNL_Phase_4`): per-customer profiles from a cluster in one
  scenario folder. Use **`lbnl_net_load`** when there's no `solar` device, and
  **`lbnl_gross_load` + `lbnl_pv`** when solar is its own device (net load already
  subtracts PV — using it alongside a solar device double-counts). `start_hour_ending`
  is 1-indexed hour of the year (4681 = July 15, midnight). Hourly grids only. To browse
  clusters: `python -c "from der_opt.integrations.lbnl_load import list_clusters; print(list_clusters('<scenario_dir>', sector='res'))"`.
- **EV schedule** (`~/claude runs/ev-daily-charging-model`): plug-in window from the
  project's empirical PSRC arrival/departure data (median ≈ home 5pm–9am).
- **PVWatts**: live NREL API. Set `NREL_API_KEY` in your environment; otherwise it
  uses NREL's rate-limited `DEMO_KEY`.

### Counterfactuals

`counterfactual.policies` maps a device name to the rule it follows in the baseline.
Devices you don't list run their passive default: solar generates everything
available, batteries/EVs/thermostats sit idle, curtailable load is fully served.

| `type` | Behavior | Fits | Overridable params |
|---|---|---|---|
| `self_consumption` | charge from solar surplus, discharge to cover load, ignore prices | `battery` (or `ev_v2x`) | `p_max_charge_kw`, `p_max_discharge_kw` |
| `rule_based_ev` | charge at full power from plug-in until full, never discharge | `ev_v2x` | `p_max_charge_kw`, `soc_max` (kWh) |
| `no_flexibility` | idle | any | — |

Power limits default to the device's own. The counterfactual follows its rule only —
it doesn't enforce `departure_period`, `soc_final_min_frac`, or `charge_sources`
(those are optimizer constraints).

## Modeling caveats worth knowing

These all came up while building the examples — each produced a plausible-looking but
wrong answer before it was understood:

- **Terminal SOC and fair comparisons.** Energy left in a battery/EV at the end of the
  horizon isn't valued, so whichever dispatch ends with *less* stored energy looks
  better by roughly that energy's worth. `der-opt run` prints a note when the optimal
  and counterfactual end states differ. Both shipped examples trigger it: the optimal
  battery drains to 0 kWh vs. 3.8 kWh for self-consumption, and the optimal EV ends at
  60 kWh vs. 75 kWh for rule-based charging — so their reported value of optimization
  is somewhat overstated. Longer horizons shrink the effect relative to the total.
- **`exclusive_import_export`.** By default import and export are only linked by their
  difference, so if export price ever exceeds import price the optimizer can import
  and export simultaneously for free money. Real NBT export rates spike above typical
  import rates on summer evenings — turn this on for NBT/LMP export scenarios. (It
  adds a binary per period, so duals become fix-and-resolve approximations.)
- **EV customer expectations.** Use a realistic departure target (~0.8), not the
  statistical daily-need minimum (~0.1) — the latter lets the optimizer run the car
  near-empty for export revenue. `integrations/ev_profile.charging_device_params()`
  returns both as `customer_target_soc_frac` and `technical_min_departure_frac`.
- **Optimal ≠ what deployed hardware does.** The optimizer will e.g. wait for the
  midday NBT export-price trough to charge; commercial BMS products charge greedily
  as soon as solar exceeds load. That gap is the value of smart control — the
  counterfactual policies in `der_opt.simulation` model the greedy behavior.
- **Demand charges** use one peak over the whole horizon (not per billing month), and
  **tiered tariffs** apply tiers per period (not monthly-cumulative). Fine for short
  horizons; inaccurate for multi-month runs.

## Python API

For anything the YAML doesn't cover — custom counterfactual policies, interval
heatmaps, multiple solves in a loop (sweeps, sensitivity runs) — script against the
Python API directly. The `examples/` scripts are the templates:

| Script | Shows |
|---|---|
| `03_full_mvp_with_duals.py` | all five device types, URDB tariff, duals, all chart types |
| `04_battery_solar_lbnl_reference.py` | battery + solar on real LBNL data and NBT export, vs. self-consumption |
| `05_v2x_lbnl_reference.py` | multi-day V2X on real LBNL + EV-model data, vs. dumb charging |

Run any with `MPLBACKEND=Agg python examples/<script>.py`; charts land in
`examples/outputs/` (gitignored).

## Layout

```
src/der_opt/
  devices/       solar, battery, thermostat, curtailable_load, ev_v2x
  premise/       Premise (composition + POI balance), value streams, tariffs
  pricing/       price series, dual extraction, value breakdown
  simulation/    counterfactual control policies + simulator
  integrations/  LBNL load, EV model, PVWatts, PG&E NBT export rates
  scenarios/     YAML schema, loader, data-source resolvers
  viz/           heatmap, dispatch, value stack, duals, comparison charts
  cli/           der-opt CLI
examples/        Python-API scripts, scenarios/ (YAML), data/ (NBT rates, sample tariff)
tests/           unit + golden-file integration tests
```
