# der-opt

A toolkit for modeling and optimizing distributed energy resources — solar, batteries,
smart thermostats, curtailable load, and V2X EVs — under resource-specific constraints.
Solves cost-minimizing dispatch for a premise (Pyomo + HiGHS), recovers dual/shadow
prices, and compares optimal dispatch against realistic rule-based baselines
(counterfactuals) to quantify the value of smart control. Feeder scenarios put many
premises on a distribution network with transformer limits and price the congestion.

## Setup

Needs Python 3.11 or newer; developed and tested on 3.13. The Mac's built-in
`python3` is 3.9, too old, so make the environment with [uv](https://docs.astral.sh/uv/)
(installed here at `~/Library/Python/3.9/bin/uv`), which fetches Python itself:

```bash
cd "/Users/seanearley/optimization tools"
uv venv --python 3.13 .venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Optional extras: `.[pdf]` (re-parse PG&E NBT price-sheet PDFs), `.[validation]`
(PySAM bill cross-check), `.[forecast]` (read loadcast forecast files: xarray, netCDF4).

Check it works: `pytest -q` (should be all green).

### On Windows

Same steps in PowerShell (Python 3.11+ from python.org, 3.13 recommended; HiGHS, the
default solver, installs from pip with nothing extra):

```powershell
cd "C:\path\to\optimization tools"
py -3.13 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"
pytest -q
```

* If PowerShell won't run `Activate.ps1`, allow local scripts once:
  `Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`.
* Environment variables: `$env:NREL_API_KEY = "..."` instead of `export`, and
  `$env:MPLBACKEND = "Agg"` before running the example scripts.
* The repo has no remote. To move it with its history, run
  `git bundle create der-opt.bundle --all` here, then `git clone der-opt.bundle der-opt` there. Don't copy `.venv`:
  it holds this machine's builds.
* The examples that use the LBNL Phase 4 dataset or the EV charging model point at
  `/Users/seanearley/...`. Copy those datasets over and edit the paths; forward
  slashes work in YAML on Windows (`C:/Users/you/Documents/LBNL_Phase_4`) and avoid
  escaping backslashes. The feeder example and the tests need no external data.
* Close output CSVs in Excel before rerunning with the same `-o` folder: Windows
  won't let the run overwrite a file that's open.
* Scenario and tariff files are read as UTF-8 on every platform, so save them as
  UTF-8 (Notepad and VS Code do by default).

Tested on macOS with Python 3.13 (pandas 3.1, numpy 2.5, Pyomo 6.10, HiGHS 1.16), not
yet on Windows: run the tests there before trusting results.

## Running a scenario

```bash
der-opt run examples/scenarios/battery_solar_lbnl_nbt.yaml
der-opt run examples/scenarios/v2x_lbnl_nbt.yaml --duals -o out/v2x
der-opt run examples/scenarios/battery_solar_lbnl_nbt_annual.yaml -o out/annual   # ~1 min
der-opt run examples/scenarios/battery_solar_lbnl_nem2_annual.yaml -o out/nem2     # same house on NEM 2.0
der-opt run examples/scenarios/feeder_ev_heat_wave.yaml -o out/feeder             # a study: 20 homes, 2 transformers
der-opt run examples/scenarios/feeder_bank_planning_trace.yaml -o out/bank_trace  # bank 1-in-10 year + 40 new homes (.[forecast])
der-opt run examples/scenarios/battery_caiso_lmp.yaml                             # battery on CAISO LMP
der-opt run examples/scenarios/feeder_from_files.yaml                             # 6 homes from spec files
der-opt run examples/scenarios/feeder_coordination.yaml -o out/coordination       # price signal, envelopes, feeder DR
der-opt run examples/scenarios/dr_battery_annual.yaml -o out/dr                   # DR program, full year
der-opt run examples/scenarios/ev_commuter_annual.yaml -o out/ev                  # V2X EV from a daily pattern, full year
```

It prints the optimal dispatch's cost broken down by source. If the scenario has a
`counterfactual` section, it also simulates that rule-based baseline and prints its
cost and the **value of optimization** (counterfactual cost minus optimal cost).

If the scenario has a `billing` section, it then bills both runs the way the utility
would (see Billing below) and prints each bill, the **bill savings from optimization**,
and the export credit each run leaves unused. With the `nbt_bill` value stream the
objective *is* this bill (plus device costs), so "value of optimization" and "bill
savings" differ only by battery/EV wear. With `energy_arbitrage` they can disagree in
sign — see Modeling caveats.

Every run ends with **checks** (feeder runs too, on every premise in every run):

| Check | Level | Catches |
|---|---|---|
| energy balance | error | grid + devices don't add up to the load |
| meter limit, storage limits | error | import or export above its limit; charge outside its min/max |
| objective matches re-pricing | error | the solver's objective differs from re-pricing the same dispatch (e.g. a rate missing its x period length) |
| hard ratings hold | error | a feeder element over a hard rating in the coordinated run |
| import and export at once | warning | the same step both imports and exports, which a bill credits as real export |
| charge and discharge at once | warning | storage doing both in one step (pure loss) |
| end state | warning | runs ending with different stored energy, or a lone run ending below its start; that energy isn't valued |
| price units | warning | a $/kWh price above $5 or never above $0.002 (a $/MWh or cents slip) |
| load vs meter limit | warning | load the meter alone can't serve |
| tariff dates | warning | the run starts before the tariff took effect, or ends over a year after |
| input data | warning | CSV inputs with gaps (filled with the last value) or duplicate timestamps |

An error means the result is internally inconsistent: treat it as a bug, not a
finding. A warning means the result is consistent but could mislead.

| Flag | Effect |
|---|---|
| `--duals` | print the dual/shadow-price families, marking exact LP duals vs. fix-and-resolve approximations |
| `-o DIR` | write everything printed as CSVs and charts (below) |
| `--no-counterfactual` | skip the scenario's counterfactual comparison |
| `--no-validate` | skip the `der-opt validate` checks that run before solving |

Files written with `-o`:

| File | Contents |
|---|---|
| `device_dispatch.csv`, `grid_import.csv`, `grid_export.csv` | optimal dispatch, per period |
| `value_breakdown.csv` | $ by source, optimal and counterfactual side by side, with a total row |
| `dispatch.png`, `value_stack.png` | optimal dispatch and its value stack |
| `counterfactual_*.csv`, `counterfactual_dispatch.png` | the same for the counterfactual |
| `comparison.png` | optimal vs. counterfactual grid flows and value breakdown |
| `duals.csv`, `duals.png` | all dual families (long format), and a chart of the marginal value of energy at the meter (with `--duals`) |
| `bill_cycles.csv`, `counterfactual_bill_cycles.csv` | per billing cycle: kWh, charges by bucket, credits earned and applied, amount due, credit balances (with `billing`) |
| `checks.csv` | every check finding: level, check, message, run, premise |
| `summary.json` | the numbers the run prints, as data, for reports and scripts ([docs/REPORTS.md](docs/REPORTS.md)) |
| `run_record.json` | what produced the result: the scenario as resolved, a SHA-256 of every input file it names, the git commit (and whether there were uncommitted changes), Python and package versions, headline results, and the checks; with `extends`, a SHA-256 of every fragment |
| `scenario.yaml` | a copy of the scenario file as run |
| `scenario.resolved.yaml` | the scenario with everything it extends merged in and every path absolute |

State columns (`soc`, `temp`) are the value at the **start** of each period in both
the optimal and counterfactual CSVs.

### Starting a scenario, and checking it before a long solve

The quickest start is to pick blocks from a library:

```bash
der-opt blocks                                    # the library's blocks, by kind
der-opt new scenarios/home_a.yaml --pick --timeseries data/home_a.csv
```

`--pick` shows a menu for each kind of block (time grid, devices, prices, meter,
counterfactual, billing, DR program, add-ons, avoided costs, cost tests). It writes a
scenario that extends your picks, with solar and load from the time series file. It
ends by printing the same choice as a `--use` command, to repeat it without menus.
Picks that don't fit together are refused and nothing is written: a counterfactual for
a device you didn't pick, two batteries, the bill objective without billing, and so on.
Per-home values a block needs (a thermostat's outdoor temperature, a baseline
territory) are written into the scenario as `TODO`s. The library is `--library DIR`,
the one `der-opt setup library DIR` remembered, or the starter library.

Or start from a template:

```bash
der-opt new --list
der-opt new dr-battery scenarios/home_a.yaml --timeseries data/timeseries.csv \
    --tariff data/tariffs/pge_e_elec_2026-06.yaml --avoided-costs data/acc_2026 --library library
der-opt validate scenarios/home_a.yaml
```

`der-opt new` writes a commented starting scenario from a template: `dr-battery`,
`home-bill` (`--program nbt|nem2`), `feeder-from-files` (a study: `--premises`, `--devices`, `--network`) or
`lmp-battery` (`--price`). It fills in what the options give it, including the premise
when the time series file has only one and the run period from the time series (or
price) file, and writes `TODO` for the rest: incentives, costs, discount rates, a
baseline territory. With `--library DIR` the shared parts (time grid, devices,
prices or billing, meter limits, counterfactual, DR program, cost-test settings) go
into fragment files under `DIR` that the scenario extends. A block already in `DIR` is
used as it is, edits included, so a second scenario made with the same library shares
them. `--library examples/library` uses the starter library (below) and says which of
its blocks hold illustrative values.

`der-opt validate` runs every check that doesn't need a solve, in about a second:
what the scenario extends, the schema, `TODO`s left (naming the file each is in),
every file it names, `check-data` on its input-spec files, the avoided cost set
(including the measure's whole life), building the premise (data coverage, tariffs,
device names), billing's baseline territory, tariff dates, the DR event calendar, and
cost-test settings. It exits 1 on any error. **`der-opt run` runs the same checks
first** and stops before solving anything if one fails; `--no-validate` skips them.

### Building blocks: `extends`

```yaml
# scenarios/home_a.yaml
extends:
  - ../library/time/year_2026.yaml
  - ../library/devices/battery_13kwh.yaml
  - ../library/programs/summer_peak_incentive.yaml
name: home_a
devices:
  - {type: solar, name: solar, params: {available_kw: {source: timeseries, path: ../data/timeseries.csv, premise_id: home_a, column: pv_kw}}}
  - name: battery # the block's battery, with one setting changed
    params: {charge_sources: [solar]}
```

Fragments are any part of a scenario, and can extend others. They merge in order, then
the scenario's own content on top:
- mappings key by key;
- `devices` by `name`, a device with an inherited name merging into it setting by
  setting (one of another `type` replaces it), new names added;
- every other list replaced whole;
- `null` removes an inherited key (`counterfactual: null`,
  `soc_final_min_frac: null`).

A device can charge from one listed after it. Runs write `scenario.resolved.yaml`, the
merged scenario with every path absolute (it runs as it is), and `run_record.json`
records a SHA-256 of every fragment. Spec:
[docs/SCENARIO_COMPOSITION_SPEC.md](docs/SCENARIO_COMPOSITION_SPEC.md).

Paths written the old way (relative to the folder you run from) still work in this
release, with a warning naming the file, key and the path to write instead.

**The starter library**, [examples/library/](examples/library/README.md), has 32
blocks: time grids, batteries, a commuter EV (V2X or charge-only), a thermostat, a
flexible load, retail, demand-charge and LMP prices, the bill objective, meter limits,
counterfactuals, NBT and NEM2 billing, DR programs and test events, avoided costs, cost
tests, and study blocks (each premise's bill, device defaults). Every example is built from it, and the
tests run every block. Its `# illustrative` values are placeholders: copy the folder
and set your own.

Or copy one of the files in `examples/scenarios/` and edit it.

## Scenario file reference

Relative paths in a scenario (or a fragment it extends) are from **that file's
folder**, so a scenario runs from any folder. The examples' `../data/...` paths point
at `examples/data/`.

```yaml
extends: [../library/time/year_2026.yaml]  # optional: building blocks merged in first (below)
name: my_house                      # any label; prefixes constraint names in --duals output

time_grid:
  resolution_minutes: 60            # any resolution; LBNL, PVWatts, daily_tou need 60
  horizon_steps: 72                 # number of periods (72 hourly = 3 days)
  start: "2025-07-15T00:00:00"      # required for anything date/hour-of-day aware

devices: [...]                      # see Devices below
value_streams: [...]                # see Value streams below
external_load: ...                  # kW the premise must serve (omit for none); see Data sources below

poi_limit_kw: 15.0                  # meter limit, kW, both directions...
import_limit_kw: 15.0               # ...or set each: draw from the grid (service/panel rating)
export_limit_kw: 7.0                #     and send back (interconnection limit; 0 = non-export)
exclusive_import_export: true       # see Modeling caveats -- turn on whenever export
                                    # price can exceed import price (NBT, LMP)

counterfactual:                     # optional; see Counterfactuals below
  policies:
    battery: {type: self_consumption}

billing:                            # optional; see Billing below
  tariff: ../data/tariffs/pge_e_elec_2026-06.yaml
  program: {type: nbt, export_rates_csv: ../data/pge_nbt_eec_2026.csv, acc_plus_per_kwh: 0.0088}
```

### Devices

Every device has `type`, a unique `name`, and `params`. Any array-valued param
(marked *array*) can be a literal list or a data source (see below).

A device can also name a **template**: a file of parameters for one device type, with
its counterfactual policy and rules ([docs/POPULATION_MODEL_SPEC.md](docs/POPULATION_MODEL_SPEC.md)
section 2). Its own `params` go on top. Any one-device library block works as a template:

```yaml
devices:
  - {type: battery, name: battery, template: ../library/devices/battery_13kwh.yaml, params: {capacity_kwh: 10}}
```

Every setting is resolved by one precedence rule. From weakest to strongest:
1. built-in defaults;
2. the template;
3. defaults (a study's `defaults`);
4. the premise's or device's own value (its row in `premises.csv` or `devices.csv`, or
   what's written out);
5. a study's `overrides`.

A template's counterfactual policy gives the scenario a counterfactual run.

| `type` | Required params | Optional params (default) |
|---|---|---|
| `solar` | `available_kw` *array* | — |
| `battery` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw` | `eta_charge` (0.95), `eta_discharge` (0.95), `soc_min_frac` (0), `soc_max_frac` (1), `soc_init_frac` (0.5), `soc_final_min_frac`, `degradation_cost_per_kwh` (0), `charge_sources` |
| `ev_v2x` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw`, and either `pattern` (a daily pattern, below) or `connected` *array* (0/1 plug-in schedule) | same SOC/efficiency/degradation params as battery, plus `departure_period` (int or list; default: every step where `connected` goes from 1 to 0), `soc_departure_min_frac` (float or list), `charge_sources`, `driving_kwh` *array* (kWh used driving each period; without it SOC is frozen while away) |
| `thermostat` | `thermal_resistance_r`, `thermal_capacitance_c`, `ambient_temp_c` *array*, `comfort_min_c`, `comfort_max_c`, `cop`, `p_max_kw`, `initial_temp_c` | `mode` (`cooling` / `heating`) |
| `curtailable_load` | `baseline_load_kw` *array*, `block_size_kw`, `max_blocks` | — |

- **Meter limits.** `import_limit_kw` and `export_limit_kw` are separate because they
  usually differ: import is the service rating, export what the interconnection allows.
  `poi_limit_kw` sets both; a specific one overrides it. With an export limit, the
  optimizer and the rule-based counterfactual both curtail solar rather than exceed it,
  as an export-limited inverter does.
- **`charge_sources: [solar]`** — restricts a battery/EV to charging no faster than
  the named solar device(s) are generating at that moment (SGIP-style "no grid
  charging"). The named devices must be **listed earlier** in `devices`.
- **`departure_period`** — period index (0-based, from `start`) of each departure.
  For a multi-day run, list one per morning: `[9, 33, 57]` for 3 days at 9am.
  A single value only protects that one departure.

#### An EV's daily pattern

Instead of per-step `connected`, `departure_period` and `driving_kwh`, an EV can take a
daily pattern ([docs/EV_PATTERN_SPEC.md](docs/EV_PATTERN_SPEC.md)):

```yaml
- type: ev_v2x
  name: ev
  params:
    capacity_kwh: 75
    p_max_charge_kw: 7.2
    p_max_discharge_kw: 7.2
    soc_departure_min_frac: 0.8            # required at every departure
    pattern:
      weekday: {depart: "07:30", arrive: "18:30", miles: 38}
      weekend: {depart: "10:00", arrive: "15:00", miles: 20}   # or: weekend: home
      kwh_per_mile: 0.30                   # drawn from the battery
      holidays: weekend                    # PG&E holidays: weekend (default) or weekday
```

A trip runs from `depart` to the next `arrive` (the next day if `arrive` is earlier,
as for a night shift), and the day it departs picks the pattern. Its energy,
`miles × kwh_per_mile`, is spread over the steps the car is away. A step the car is
away for any part of counts as away, so on an hourly run 07:30 counts as 07:00 and
18:30 as 19:00. `validate` and `run` note it when that happens, and warn when the car
isn't home long enough to charge back a trip. `run` prints the departures, miles,
driving kWh and hours plugged in.

### Value streams

| `type` | Params |
|---|---|
| `nbt_bill` | none — optimizes the bill from the scenario's `billing` section (program `nbt`), with NBT's credit limits (see Billing). Use instead of `energy_arbitrage`, not with it |
| `nem2_bill` | none — the same for a `billing` section with program `nem2` |
| `energy_arbitrage` | `import_price`, `export_price` — each a price series (below). Values every exported kWh at its full export rate |
| `tariff_energy_charge` | `tariff: {urdb_json_path: ...}`, `direction` (`import`/`export`) — tiered TOU tariffs from OpenEI URDB JSON |
| `demand_charge` | `rate_per_kw` |
| `capacity_payment` | `event_periods`, `payment_per_kw_event`, `committed_kw` |

Price series (`import_price` / `export_price`):

```yaml
{type: constant, price: 0.25}
{type: daily_tou, hourly_pattern: [24 values, $/kWh by hour of day]}
{type: csv, path: prices.csv, column: price}          # first column = timestamps (see below)
{type: retail_tariff, path: ../data/tariffs/pge_e_elec_2026-06.yaml}  # total $/kWh of a billing tariff
{type: nbt_export, csv_path: ../data/pge_nbt_eec_2026.csv, adder_per_kwh: 0.0088}
    # real PG&E NBT export rate; adder_per_kwh = ACC Plus for the customer's application
    # year (residential: 2023 0.022, 2024 0.0176, 2025 0.0132, 2026 0.0088, 2027 0.0044);
    # include_produced: false drops the generation half, for CCA/DA customers
```

**CSV timestamps** are fitted to the run's steps: data finer than a step is averaged
over it (15-minute load in an hourly run), data coarser is held across it (an hourly
price in a 15-minute run), and steps with no data repeat the last value before them
with an `input data` warning saying how many. Timestamps must be on the scenario's
local clock with no time zone. The same applies to `external_load: {type: csv, ...}`.

**Wholesale prices (LMP)** come in as a `csv` price series. The CSV must have a
`timestamp` column on the scenario's local clock (no time zone), one row per step at
the scenario's resolution, and a $/kWh price column. `der-opt fetch-lmp` downloads one
CAISO pricing node from CAISO's public OASIS site (no account) and writes exactly that:

```bash
der-opt fetch-lmp TH_NP15_GEN-APND 2026-07-13 2026-07-20 -o data/lmp.csv          # day-ahead, hourly
der-opt fetch-lmp TH_NP15_GEN-APND 2026-07-13 2026-07-14 --market RTPD --resolution-minutes 15 -o data/lmp15.csv
```

```yaml
import_price: {type: csv, path: data/lmp.csv, column: lmp_per_kwh}
```

The end date is exclusive. Markets: `DAM` (day-ahead, hourly), `RTPD` (15-min), `RTM`
(5-min). Long ranges are split into several requests 5 s apart, as CAISO asks. For
another ISO, or prices you downloaded yourself, `der_opt.integrations.caiso_lmp.
lmp_price_table` does the same conversion on any table of interval times, nodes and
$/MWh prices: it divides by 1000, keeps one node, moves UTC to the local clock,
averages finer intervals down to the step, averages November's repeated hour, and
repeats the hour before March's skipped one. `examples/scenarios/battery_caiso_lmp.yaml`
is a 1 MW battery arbitraging a week of NP15 day-ahead prices.

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
{source: loadcast, path: bank_traces.nc, trace: poe10, var: net}   # needs .[forecast]
{source: loadcast, path: class_quantiles.nc, entity: res_nopv, quantile: 0.5, var: gross, per_customer: true}
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
- **loadcast** (`~/loadcast`, the load-forecasting package): profiles from its NetCDF output
  (`loadcast simulate-year` writes `bank_traces.nc` and `class_quantiles.nc`). The coupling is
  the file format only; der_opt never imports loadcast.
  - `trace`: one simulated weather year, by index or by annual peak: `poe10` (the 1-in-10 design
    year), `poe50`, `poe90`, `max`. A trace is a year that can happen, so use it for energy,
    storage and timing. `quantile` (e.g. 0.9) is each hour's own percentile across years: an
    envelope, not a year, so it overstates sustained stress.
  - `var`: `net` (what the grid sees; for a bank, after customers' PV and with losses and
    unmetered load), `gross` (before PV; use with a `solar` device, as with LBNL), `pv`.
  - `entity`: needed when the file holds several (classes); `per_customer: true` divides a
    class total by its customer count; `scale` multiplies.
  - Timestamps: loadcast stores UTC; the grid's local times are converted with the file's time
    zone (`tz` to override), and sub-hourly steps hold the hourly value. `align_year: true` maps
    the grid's dates onto the forecast's year (Feb 29 -> Feb 28).
  - To pick a trace and window: `python -c "from der_opt.integrations.load_forecast import
    describe_forecast; print(describe_forecast('<file>'))"` lists each trace's peak and when.

### Counterfactuals

`counterfactual.policies` maps a device name to the rule it follows in the baseline.
Devices you don't list run their passive default: solar generates everything
available, batteries/EVs/thermostats sit idle, curtailable load is fully served.

| `type` | Behavior | Fits | Overridable params |
|---|---|---|---|
| `self_consumption` | charge from solar surplus, discharge to cover load, ignore prices | `battery` (or `ev_v2x`) | `p_max_charge_kw`, `p_max_discharge_kw` |
| `rule_based_ev` | charge at full power from plug-in until full, never discharge | `ev_v2x` | `p_max_charge_kw`, `soc_max` (kWh) |
| `setpoint` | cool (or heat) just enough to hold the comfort limit, like a plain thermostat | `thermostat` | `offset_c` (moves the setpoint) |
| `no_flexibility` | idle | any | — |

Power limits default to the device's own. The counterfactual follows its rule only —
it doesn't enforce `departure_period`, `soc_final_min_frac`, or `charge_sources`
(those are optimizer constraints).

## Billing

`der_opt.billing` computes a bill from interval import/export the way PG&E bills it,
under the Net Billing Tariff (NBT, "NEM 3") or NEM 2.0. The rules come from PG&E's
tariff sheets, not from memory.

```yaml
billing:
  tariff: ../data/tariffs/pge_e_elec_2026-06.yaml
  income_tier: 3                  # base services charge tier (3 = not CARE/FERA)
  program: {type: nbt, export_rates_csv: ../data/pge_nbt_eec_2026.csv, acc_plus_per_kwh: 0.0088}
  # or, for NEM 2.0 on E-TOU-C:
  # tariff: ../data/tariffs/pge_e_tou_c_2026-06.yaml
  # baseline_territory: R         # the customer's baseline territory letter
  # baseline_code: basic          # or all_electric
  # program: {type: nem2, nsc_rate_per_kwh: 0.02781}
```

### NBT (Schedule NBT, Special Conditions 2 and 5; Schedule E-ELEC)

- **Imports** are billed on the retail tariff: per-kWh components by season and
  time-of-use period, plus a daily base services charge by income tier.
- **Exports** earn the hourly NBT export rate, which has a **generation** half and a
  **delivery** half. Generation credit only offsets generation kWh charges; delivery
  credit only offsets delivery kWh charges. Neither offsets non-bypassable charges
  (public purpose programs, nuclear decommissioning, competition transition, wildfire
  fund) or the base services charge. Imports and exports are never netted.
- **ACC Plus** (the early-adopter export bonus) is a separate credit that offsets any
  charge. It isn't in the export rate table — set it per customer.
- **Unused credit** rolls month to month. At the annual true-up, charges paid earlier
  in the year that later credits would have covered are credited back, and remaining
  excess credit **carries forward** to the next year (not forfeited, unless the
  customer leaves NBT).
- **Net Surplus Compensation**: if a year's export kWh exceed import kWh, the surplus
  is debited at PG&E's average NBT export rate and credited at the NSC rate. Both
  change monthly and aren't in the tariff sheet, so they're inputs
  (`nsc_rate_per_kwh`, `nsc_debit_rate_per_kwh`); without them the bill says NSC
  wasn't computed.
- **Billing cycles** default to calendar months; pass `cycle_start_dates` (meter-read
  dates) to match a real bill. A true-up runs only when the data spans exactly 12
  cycles; shorter runs report credit balances as of the end of the data. The base
  services charge is charged per calendar day that has data.
- **Holidays** (for the export rate's weekday vs. weekend/holiday split): PG&E's TOU
  holiday list from Schedule E-19, on observed dates. E-ELEC itself prices every day
  the same.

### NEM 2.0 (Schedule NEM2, Special Conditions 2, 5 and 9; Schedule E-TOU-C)

- **Netting by TOU period:** each billing cycle, imports and exports within the same
  TOU period (e.g. summer peak) are netted; net kWh are billed — or credited — at that
  period's rate, excluding non-bypassable charges. A peak export earns the peak rate.
- **Non-bypassable charges** are paid on each interval's imports net of that
  interval's exports; no export credit reduces them.
- **Baseline credit** (E-TOU-C: $0.0814/kWh on up to 6–20 kWh/day, by territory and
  season) applies to the cycle's net kWh. For a net *generator* it works the other
  way: the first baseline-allowance kWh of net generation are credited at the lower
  baseline rate.
- **Base services charge** is paid monthly and can't be offset by credits.
- **Energy settles at the true-up** after 12 cycles: a positive balance is owed, and a
  credit balance is **set to zero** (unlike NBT, it doesn't carry forward). If exports
  exceed imports for the year, the surplus kWh earn the NSC rate, which PG&E publishes
  monthly (`nsc_rate_per_kwh`; $0.02781 for November 2026 true-ups).
- **Paired storage:** NEM2 only credits renewable exports. A battery on a NEM2 system
  is normally set up as "No Grid Charging" (Special Condition 9) — model it with
  `charge_sources: [solar]`.
- Billing cycles and the true-up trigger work as for NBT.

### Tariff files

`examples/data/tariffs/` has E-ELEC and E-TOU-C (both effective 2026-06-01), itemized
component by component, each tagged `generation`, `delivery`, or `nbc` — the tag
decides what NBT export credit may offset, and which charges NEM2 bills on gross
imports. Each file also carries the sheet's published total rates, and the loader
refuses a file whose components don't add up to them, so a mistyped rate fails
loudly. A baseline credit is described by the component it changes (E-TOU-C's
Conservation Incentive Adjustment, over-baseline vs. baseline rate) and checked
against the sheet's published credit. To update for a new rate change, copy the
numbers from the current sheets (pages 2-3) and update `published_totals`.

### Optimizing the bill

The `nbt_bill` and `nem2_bill` value streams (`NBTBill` / `NEM2Bill` in
`der_opt.billing.objective`) put these rules into the optimization. Each one's cost is
the bill engine's net cost for the same dispatch, which the tests check to the cent,
so "value of optimization" and "bill savings" differ only by device wear.

- `nbt_bill` is a linear program: per billing cycle, credit applied to each bucket is
  capped by that bucket's charges and by the credit earned so far (or any time in the
  year, when a true-up runs). The meter-balance duals become the marginal *bill* value
  of a kWh.
- `nem2_bill` is linear except the baseline credit, whose value flips direction when a
  month goes from net consumer to net generator. That takes two yes/no variables per
  billing cycle (24 for a year), so it's a small MILP and needs both meter limits. It
  doesn't need `exclusive_import_export`: under NEM2 an import and an export in the
  same hour net at the same rate.

**What the two programs say about the same house.** The annual examples run one
household (LBNL hot-dry cluster, 13.5 kWh battery, solar that exports ~9,400 kWh more
than it imports in a year):

| | NBT bill | NEM2 bill |
|---|---|---|
| Self-consumption battery | $259 | $31 |
| Bill-optimal dispatch | $233 | $67 (battery nearly idle) |

Under NEM2 this house's leftover energy credit (~$3,200) is zeroed at true-up, so the
only bill value a battery can add is avoiding ~$0.012/kWh of non-bypassable charges.
Cycling costs more than that in wear, so the optimizer leaves the battery nearly idle
and comes out $79 ahead of self-consumption once wear is counted, despite a $37 higher
bill. A household that imports more than it exports would see a different answer.

**Not yet covered:** CCA/Direct Access customers (their generation charges and credits
come from the CCA), CARE/FERA discounts, multi-tier rates (e.g. E-1), medical baseline,
NEM2 load aggregation and multiple-tariff facilities, the California Climate Credit,
and demand charges.
None of this has been checked against a real bill yet — that's the real test.

## Input data: the spec format

Real premises come in as CSV files in one format, defined in
[docs/INPUT_DATA_SPEC.md](docs/INPUT_DATA_SPEC.md) (v2.0, approved). Produce that format
from your own pipeline; nothing else needs configuring per file. In short:

* **Time series:** `timestamp, premise_id, <values>`, one row per premise per interval.
  `timestamp` is the interval start in ISO 8601 with a UTC offset
  (`2026-07-13T00:00:00-07:00`). Values are average kW (`load_kw`, `pv_kw`,
  `import_kw`, `export_kw`), plus `ev_connected` (0/1), `ev_driving_kwh`, and
  `import_price_per_kwh` / `export_price_per_kwh` (prices go in a file without
  `premise_id`). An optional `device_id` column keys a device's own series, so two EVs
  at one premise each have theirs.
  - `outdoor_temp_c` feeds thermostats; a weather file can be keyed by
    `weather_station`, and a premise picks its station with a `weather_station` tag.
  - `flexible_kw` is a flexible load's baseline. Each premise is either **gross** (`load_kw`, plus `pv_kw` if it has
  solar) or **metered** (`import_kw`/`export_kw`), never both. A missing value is an
  empty cell, never 0. The interval is 5, 15, 30 or 60 minutes, with every interval
  present.
* **`premises.csv`:** one row per premise:
  - `element_id`, `tariff`, `export_program`, `customer_class`, `baseline_territory`,
    `import_limit_kw` and `export_limit_kw`;
  - `kind: background` for load that isn't a customer;
  - premise settings as columns (`participation`, `baseline_behavior`, `response`,
    `exclusive_import_export`, `load: none`…);
  - any other column as a tag.
* **`devices.csv` (2.0):** one row per device, with `premise_id`, `device_id`, `type`
  (`solar`, `battery`, `ev`, `thermostat`, `flexible_load`), an optional `template` by
  name, `kw` and `kwh` sizes, `v2x`, `installed_cost` and `annual_om`, `charge_sources`,
  and any device parameter by name. A study's `population` names it with `devices:`,
  and the folders to find templates in with `templates:` (default: your library's
  `devices/`).

  A 1.1 premises file with device columns is read for one release, with a warning.
  `der-opt convert premises premises.csv --timeseries timeseries.csv --out new/` writes
  the 2.0 pair, which builds identical premises.
* **`network.csv`:** one row per transformer or section: `element_id`, `parent_id`,
  `rating_kw`, `overload_cost_per_kwh`.

```bash
der-opt check-data data/timeseries.csv --premises data/premises.csv --network data/network.csv
der-opt check-data data/devices.csv --premises data/premises.csv
der-opt validate scenario.yaml --population out/population   # premises.csv and device_parameters.csv
```

`validate` on a feeder prints each element's premises and devices by type. With
`--population`, it writes two tables before anything is solved; every run with `-o`
writes them too:
- `premises.csv`: each premise's settings and technology mix;
- `device_parameters.csv`: every setting of every premise and device, with its value and
  where it came from (`devices.csv row 4`, `template:commuter`, `defaults[1]`,
  `override:home_a/battery`, `add_devices[0]`).

`check-data` names every rule a file breaks (exit code 1) and warns about suspect
data: empty cells, a day or more without consumption, 12 h unchanged, spikes, a home
over 100 kW. It also reports the period covered and each premise's kWh.

**Using the files.** For a single premise, any array or price can come from a file:

```yaml
external_load: {source: timeseries, path: data/timeseries.csv, premise_id: home_01, column: load_kw}
import_price: {type: timeseries, path: data/prices.csv, column: import_price_per_kwh}
```

For many premises, a study's `population` reads the files (see
[Studies](#studies-a-population-and-its-analyses)).
`examples/scenarios/feeder_from_files.yaml` runs the synthetic sample in
`examples/data/spec_sample/`:

```yaml
extends: [../library/time/2026-07-13T12_72x60min.yaml, ../library/study/homes_bill.yaml]
population:
  premises: ../data/spec_sample/premises.csv
  devices: ../data/spec_sample/devices.csv
  timeseries: [../data/spec_sample/timeseries.csv]   # a premise's columns may span several files
  network: ../data/spec_sample/network.csv           # instead of the feeder analysis's elements
tariffs:
  dir: ../data/tariffs                       # premises.csv `tariff` names a file here (default: der-opt's example tariffs)
  programs: {nbt: {export_rates_csv: ..., acc_plus_per_kwh: 0.0088}, nem2: {nsc_rate_per_kwh: 0.02781}}
defaults:                                    # homes_bill's come first: each home's own bill, and the counterfactuals
  - where: {type: battery}
    set: {device: {eta_charge: 0.96, degradation_cost_per_kwh: 0.015}}
analyses:
  feeder: {}
```

Sizes come from `devices.csv` and behavior from templates and `defaults` (built in:
batteries 95% efficient each way, starting and ending half full; EVs charged to 90% by
every departure, which is read off `ev_connected`). A metered premise can't have devices
added: its meter already includes them. On the local clock, November's repeated hour
is averaged into one and March's skipped hour interpolated.

## Demand response: events and capacity

A scenario with a `dr` section runs a demand response study for one measure, defined in
[docs/DR_CAPACITY_SPEC.md](docs/DR_CAPACITY_SPEC.md) (v1.2, approved). The template is
`examples/scenarios/dr_battery_annual.yaml`: a home battery in a summer program for a
full year, which runs in about 5 seconds.

```yaml
dr:
  customer_class: residential          # 5-in-10 settlement (commercial: 10-in-10)
  measure_devices: [battery]           # R3 removes these; default: all devices
  program:
    program_id: summer_peak
    season: [5, 6, 7, 8, 9, 10]        # counted capacity is reported for each month
    day_types: [weekday]
    window: "16:00-21:00"
    min_hours: 1
    max_hours: 3                       # also the length of shadow events
    max_events_per_month: 4
    max_consecutive_days: 2
    response: incentive                # default; or commitment (committed_kw), import_cap (cap_kw)
    settlement_basis: net              # or import: pay only for avoided grid import
    incentive_per_kwh: 1.00
    capacity_reference: all            # R2, R3, S, or all
  events:                              # one constructor:
    random: {events_per_month: {7: 3, 8: 3}, seed: 1, hours: 3, replicates: 1}
    # file: data/events.csv            # event_id, start, end, program_id, event_type (ISO 8601 with offset)
    # price: {series: {type: timeseries, ...}, events_per_year: 10, duration: variable, alpha: 0.8}
    # schedule: [{start: "17:00", hours: 3, days: weekdays, months: [7, 8]},
    #            {start: "16:00", hours: 1, nth_weekday: {n: 1, weekday: tue}, event_type: test}]
```

**Runs:** R1 is the counterfactual (the scenario's rule-based policies), R2 the
non-responding twin (the same optimization, never told about events), and R3 the home
without the measure. The responding run is the optimization with the program's
response; with `replicates`, there's one per randomized calendar.

**Two bases, always both:** every reduction is reported on a **net** basis (difference
in net load, so exports count) and an **import** basis (difference in grid import only,
each step floored at zero). Columns without a prefix are net; `import_` columns are the
import basis, and the printout shows each figure as net / import. What the incentive
pays for (and a commitment must deliver) follows the program's `settlement_basis`:
`net` (default) or `import`, where the measure stops at zero import and exporting earns
nothing.

**Reported:**
- **Per event (accounted):** reduction against R1, R2, R3 and the settlement baseline
  S, plus the incentive earned and same-day rebound.
- **Per month (counted):** the average reduction of a `max_hours` shadow event on every
  eligible day, from R2's device state that morning, in every season month whether
  events were called or not.
- **Slice-of-Day:** a 24-hour profile for each month.

`-o` writes `dr_events.csv`, `dr_event_performance.csv`, `dr_capacity.csv`,
`dr_capacity_slice_of_day.csv`, and `dr_net_load_kw.csv` (every run's net load).

**How each response is modeled:**
- **Payment basis:** the optimization is paid against an *expected* baseline (the
  settlement rule applied to R2), not its own. A perfect-foresight optimizer paid
  against its own baseline could raise it on purpose. The settlement actually earned is
  then computed from the responding run's own history and reported.
- **Incentive pricing:** the incentive is linear in each event step. The reported
  settlement floors each event's payment at zero.
- **Commitment and import cap:** shortfalls are allowed at `shortfall_penalty_per_kwh`
  (default $5), so the run never becomes infeasible.
- **Perfect foresight:** the optimizer sees every event in the year, so delivered
  response is an upper bound on what a controller with day-ahead notice would achieve.

**Checks:** event calendars that break the program's rules are rejected. Warnings
flag:
- a reduction larger than the devices could deliver;
- same-day rebound;
- settlement more than 25% away from the R2 load impact;
- shortfalls;
- accounted capacity more than 25% away from counted capacity.

**Open threads in the spec:** T1 (the day-of adjustment) and T2 (weekend and holiday
settlement) are provisional.

## Avoided costs and the measure impact package

How the pieces run, and in what order, is drawn in two sequence diagrams in
[docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): `der-opt run` with DR and cost tests, and
`der-opt openbca`.

**Avoided cost sets** ([docs/AVOIDED_COST_SPEC.md](docs/AVOIDED_COST_SPEC.md), v1.0) hold
an ACC vintage or your own curves in one format. Each set is a folder:
- `manifest.yaml`: source, dollars, years, and each component classed `energy` or
  `capacity`;
- `hourly.csv`: every hour of every year, ISO 8601 with offset;
- `capacity.csv`: $/kW-year with monthly shares.

`der-opt check-data <folder>` validates a set. `examples/data/avoided_costs/synthetic_2026`
is an illustrative set (2026-2028, escalating 2%/yr beyond).

**Screening values.** A scenario with `avoided_costs: {set: ...}` or a `cost_test`
section prints first-year screening values (`avoided_costs.csv` with `-o`):
- **Energy:** split into where the measure lowers load and where it raises it.
- **DR capacity:** for every reference, basis and measure, priced from `capacity.csv`
  only.
- **Full hourly:** every component, as a comparison figure only.
- **Emissions.**

They're for sweeps and quick comparisons, **not cost-test results**.

**The measure impact package** ([docs/COST_TEST_SPEC.md](docs/COST_TEST_SPEC.md), v1.0).
A `cost_test` section writes `measure_impacts/` with `-o`:
- `measure.yaml`: scope, life, costs and escalation;
- `energy_impact.csv`: every hour of the year in standard time;
- `capacity.csv`: the valued kW and every variant;
- `bills.csv`: without and with, if the scenario bills;
- `dr_incentives.csv`.

**Cost tests in OpenBCA.** First, once:

```bash
der-opt setup openbca          # clones OpenBCA to ~/.der-opt/openbca (or --dir PATH), pins the tested commit, installs it
```

Setup needs [uv](https://docs.astral.sh/uv/) and takes about a minute. It remembers the
location in `~/.der-opt/config.yaml`. `DER_OPT_OPENBCA` or `--openbca PATH` override it.
Then, for any scenario with a `cost_test` section, one command does the run and the cost
tests (about 40 s for the full-year example):

```bash
der-opt run examples/scenarios/dr_battery_annual.yaml --cost-tests            # output in out/dr_battery_annual
der-opt openbca out/dr_battery_annual                                          # or: rerun only the cost tests
```

This writes OpenBCA's input tables for each test into `out/dr/openbca/<test>/` (CSV,
readable). It runs OpenBCA's own core on them, through its table ("integrated") pathway,
and prints each test's OpenBCA totals and ratio. Next to them goes an SPM-convention
re-tally of the same values, where each stream counts by its sign, so load increases and
lost revenue are costs and a participant's incentive is a benefit. `cost_tests.csv` has
both. Once the tests are tallied, each test's folder keeps its settings, OpenBCA's result
CSVs and its log; OpenBCA's copy of the inputs and its database (about 36 MB a test) are
deleted unless you pass `--keep-openbca`.

Mapping, tested against OpenBCA commit 7e13b36:
- one configuration per test: PCT, RIM, `CA TRC`, `USI` (PAC), `USI + Non-USI` (SCT),
  each with its own discount rate;
- the measure and its load increases on separate rows;
- energy in MWh and avoided costs in $/MWh, because OpenBCA rounds avoided costs to 3
  decimals;
- capacity values with your monthly shares applied;
- bills as a custom stream (PCT benefit, RIM lost revenue).

**OpenBCA doesn't discount annual cost fields** (admin, O&M, the annual DR incentive).
That's its convention, kept by decision. Every export notes it, and prints how each
test's net benefits would change if those costs were discounted
([docs/COST_TEST_SPEC.md](docs/COST_TEST_SPEC.md) section 3). The `cost_test` section
needs `discount_rates: {utility, participant, societal}` for the export.

```yaml
cost_test:
  scope: control                       # control (vs R1) or device (vs R3)
  start_year: 2027
  measure_life_years: 10
  avoided_costs: {set: ../data/avoided_costs/synthetic_2026}
  capacity_value: {reference: R2, basis: import, measure: counted}
  participant_costs: {upfront: 300, annual_om: 0}
  program_costs: {admin_upfront: 75, admin_annual: 15}
```

## Studies: a population and its analyses

A study keeps the population (who the premises are and what they have) apart from the
analyses run on it ([docs/POPULATION_MODEL_SPEC.md](docs/POPULATION_MODEL_SPEC.md)).
`der-opt run` and `der-opt validate` recognize a study by its `population` or
`analyses` section. The feeder examples in `examples/scenarios/` are studies.

```yaml
name: two_homes
time_grid: {resolution_minutes: 60, horizon_steps: 8760, start: "2026-01-01T00:00:00"}
population:
  premises: data/premises.csv                # premise_id, element_id, customer_class, tariff, tags...
  devices: data/devices.csv                  # premise_id, device_id, type, template, kw, kwh, installed_cost...
  timeseries: [data/timeseries.csv]
  templates: [../library/devices]            # default: the library's devices/
tariffs: {dir: ../data/tariffs, programs: {nbt: {...}}}
objective: bill                              # or a list of value streams; each premise's own optimization
defaults:                                    # applied in order; a later one wins
  - set: {premise: {poi_limit_kw: 20}}       # no `where`: every premise
  - where: {type: battery}                   # device keys: type, template, device_id
    set: {device: {counterfactual: {type: self_consumption}}}
  - where: {type: battery, segment: late}    # premise columns and tags; a list means any of
    set: {device: {degradation_cost_per_kwh: 0.03}}
overrides:                                   # one premise or device; beats every default
  home_a/battery: {device: {installed_cost: 9000, eta_charge: 0.9}}
  home_b: {premise: {participant_upfront: 250}}
premises:                                    # written out, as well as (or instead of) the files
  - {name: shop, element: tx, tariff: pge_e_elec_2026-06, tags: {segment: retail}, external_load: {...}, devices: [...]}
  - {name: garage, element: tx, kind: background, external_load: {...}}  # fixed, not billed, never flexible
add_devices:                                 # adoption what-ifs: the files stay as they are
  - where: {element_id: tx, kind: premise}
    share: 0.3                               # of the selected premises without this device_id
    seed: 1
    device: {device_id: battery, type: battery, template: battery_13kwh, kw: 5, kwh: 13.5, installed_cost: 9500}
analyses:
  feeder:                                    # every premise on one network
    elements: [{name: tx, rating_kw: 12}]    # or population.network
    coordination: {envelopes: {}}
  portfolio:                                 # each selected premise on its own
    select: {segment: late}                  # default: every premise
    measure: {type: [battery]}               # the devices DR's R3 and the cost tests remove
    dr: {program: {...}, events: {...}}      # customer_class comes from each premise
    avoided_costs: {set: ../data/avoided_costs/synthetic_2026}
    cost_test: {...}                         # needs a run covering a whole calendar year
```

Precedence, lowest to highest: built-in defaults, the device's template, the study's
`defaults`, the population row (or the premise as written out), then `overrides`.
Library blocks that set `defaults` add up: a study that extends several gets every
block's entries in order, then its own. A device's `installed_cost` and
`annual_om` come from its row, a default or an override. In the portfolio, a premise's
participant cost is the cost test's base, plus its measure devices' costs, plus its own
`participant_upfront` / `participant_annual_om`. `validate` reports every problem by
premise before anything is solved: an override of a premise or device that isn't there,
a parameter a device doesn't have, a counterfactual that doesn't fit its device (a
`self_consumption` solar), DR without a premise's `customer_class`, and cost tests on
less than a year.

**`add_devices`** adds `round(share × eligible)` devices, at premises chosen at random
from `seed`. Eligible premises are those `where` selects that don't have a device with
that `device_id` and aren't metered (a meter already includes its devices). Each entry
has its own random order, so raising a share keeps the premises already chosen, and one
entry never changes another's. An added device is a `devices.csv` row (its source is
`add_devices[0]`), so defaults and overrides apply to it. It needs its series like any
device: a template with a `pattern` covers an EV. Added solar can take a shared shape
per kW of its `kw`, as a default:

```yaml
defaults:
  - where: {type: solar}  # rows with their own pv_kw series keep theirs
    set: {device: {available_kw: {source: timeseries, path: data/pv_shape.csv, premise_id: shape, column: pv_kw, per_kw: true}}}
```

`run` prints what each entry chose, and `run_record.json` lists the premises.

`der-opt run study.yaml -o out/` writes:
- `premises.csv` and `device_parameters.csv`: what every premise resolved to, and where
  each setting came from;
- `feeder/`: the feeder analysis's outputs (see [Feeders](#feeders-many-premises-on-a-network));
- `portfolio/<premise_id>/`: each premise's scenario.yaml and its outputs, as a
  single-home run writes them (report.txt holds what it printed);
- `portfolio/portfolio.csv`: one row per premise (costs, bills, DR incentive and counted
  kW, screening values, cost-test ratios and net benefits) and a total row (sums; ratios
  aren't summed);
- `run_record.json`.

A premise that fails in the portfolio is reported and the rest still run (exit code 1).
A one-premise study of the `dr_battery_annual` home gives that example's results exactly,
cost tests included.

**The old feeder form** (`houses`, `house_defaults`, `premise_data`,
`feeder.background_load`) and 1.1 premises files are read for one release, each with a
warning saying what to write instead. In a study:
- `houses` are `premises` (the same fields);
- `premise_data`'s files are `population`, and its tariff settings `tariffs`;
- `house_defaults.value_streams` is the `objective`, and its other settings a `defaults`
  entry with no `where`;
- `device_params.<type>` and counterfactual policies are `defaults` entries with
  `where: {type: ...}`;
- background load is a premise with `kind: background`.
Each old form of the examples resolves to the same premises as its study (the tests
check it).

## Feeders: many premises on a network

A study's `feeder` analysis is a multi-premise run. Every premise is an ordinary one
(devices, objective, bill, counterfactual), and the feeder is a tree of transformers and
line sections with kW ratings. `der-opt run` dispatches the same premises three ways
and prices each on the same terms:

| Run | What it is |
|---|---|
| coordinated | one optimization over every premise and the network: bills + wear + overload + deferral value |
| independent | each premise optimizing its own bill, blind to the network |
| counterfactual | the premises' counterfactual policies (e.g. EVs charging on plug-in) |

```yaml
extends: [../library/time/2026-07-13T12_72x60min.yaml, ../library/study/homes_bill.yaml]
population:                    # premises from input-spec files, and/or written out under premises:
  premises: ../data/heat_wave/premises.csv
  devices: ../data/heat_wave/devices.csv
  timeseries: [../data/heat_wave/timeseries.csv]
tariffs: {dir: ../data/tariffs, programs: {nbt: {...}}}
defaults:                      # after homes_bill's (objective: bill, and the counterfactual policies)
  - set: {premise: {poi_limit_kw: 30}}
analyses:
  feeder:
    elements:
      - {name: lateral, rating_kw: 200}
      - {name: tx_a, parent: lateral, rating_kw: 50, overload_cost_per_kwh: 1.0}
    value_streams:
      - {type: capacity_deferral, element: tx_a, value_per_kw: 40.0}
```

* **Elements:** `rating_kw` applies to load and backfeed. Without
  `overload_cost_per_kwh` it's a hard limit (a run is infeasible if uncontrollable load
  alone exceeds it); with one, flow may exceed it at that cost per kWh over.
* **`capacity_deferral`:** `value_per_kw` x (peak flow above `threshold_kw`, default 0).
  It takes an annual $/kW-year and applies it to the run's peak, so use it on a horizon
  that contains the element's annual peak.
* **Premises** come from the population's files (see Input data), or are written out
  under the study's `premises:`, with the same fields as a single-home scenario plus
  `element`. The example homes in `examples/data/heat_wave/` and
  `examples/data/new_subdivision/` are synthetic, written in that format. Each has:
  - base load, with morning and evening peaks and air conditioning that follows a
    heat-wave temperature;
  - rooftop solar, on some homes;
  - an EV that drives each day, on some homes;
  - a solar-only-charging battery that must end where it started, on some homes.

  The ranges are judgment calls for an inland California summer, not fitted to data.
* **Congestion prices** are the duals of each element's flow: the cost of one more kWh
  through it in each hour. They're exact while the model is an LP.

Output (`-o`, in `feeder/`): `premises.csv` (devices, sizes, each run's bill),
`device_parameters.csv`, `flows_kw.csv`, `loading.csv` (peak, % of rating, hours and kWh
over), `congestion_prices.csv`, `dispatch_<run>.csv` per premise, and
`feeder_loading.png`.

`feeder_ev_heat_wave.yaml`, three heat-wave days, transformer A with 7 EVs in 10
homes:

| | coordinated | independent | plug-in charging |
|---|---|---|---|
| Transformer A peak (50 kW rating) | 37.7 kW | 81.1 kW (5 h over) | 66.7 kW (6 h over) |
| Customer bills, 20 homes | $1,385 | $1,384 | $1,448 |

Each home optimizing its own bill is *worse* for the transformer than plug-in
charging: every EV waits for the same off-peak start at midnight. Coordination holds
the peak to the evening air-conditioning peak at a cost of $0.84 to two homes. The
congestion price on transformer A, $8.10/kWh over the eight peak hours, is the $40/kW
deferral value spread across the hours that set the peak.

Two modeling choices matter here:

* **Ties go to the earliest hour.** A TOU bill is indifferent among equally cheap
  hours, so the solver would return an arbitrary schedule. Every optimization here
  adds a negligible cost ($0.00001/kWh per hour into the run, left out of every
  reported cost) so that charging starts as soon as the cheapest window opens, as a
  "charge in off-peak" timer does. That is what produces the midnight spike in the
  independent run.
* **The coordinated run is a planner's optimum.** It minimizes everyone's costs
  together. It reports which premises pay more, but doesn't compensate them; that's for
  a settlement mechanism (not built yet).

`feeder_bank_planning_trace.yaml` puts the same machinery under a whole primary bank. The
bank's existing load is a background premise from loadcast's 1-in-10 design year (`trace:
poe10`, peak 1,990 kW on 4 Aug 2026 at 16:00). A new 40-home subdivision (80% EVs) joins
on two 75 kW transformers. Over 3-6 Aug:

| | coordinated | independent | plug-in charging |
|---|---|---|---|
| Bank peak (2,000 kW rating) | 2,069 kW (1 h over) | 2,086 kW (1 h over) | 2,102 kW (2 h over) |
| New transformer A peak (75 kW) | 105 kW | 207 kW | 167 kW |
| Customer bills, 40 new homes | $3,276 | $3,236 | $3,546 |

The bank's own 4 pm peak leaves the new homes little room, but what binds first is their
service transformers. Coordination costs 13 homes $40 in total for a $1,149 network saving.
The existing load is a background premise here (fixed, not billed), the new homes' synthetic weather is
not the trace's weather, and loadcast's bank load includes its losses while the new homes'
doesn't -- a sizing study, not a single consistent day.

Flow is a power balance only: no losses, voltage, or reactive power, so convert kVA
nameplates to kW yourself. Every premise shares one time grid.

### Coordination: price signals, operating envelopes, feeder DR

The coordinated run is the ideal: one optimizer, perfect foresight. A `coordination`
section adds runs with mechanisms a utility could use, alone or combined. Each run is
measured against `independent` (the feeder with no program) and the ideal. Spec:
[docs/COORDINATION_SPEC.md](docs/COORDINATION_SPEC.md); example:
`examples/scenarios/feeder_coordination.yaml`.

```yaml
defaults: # roles for everyone (built in: participant, own_bill, optimized)
  - set: {premise: {participation: participant, baseline_behavior: own_bill, response: optimized}}
premises: # or premises.csv columns: participation, baseline_behavior, response
  - name: home_02
    element: tx_1
    baseline_behavior: rule_based # without a program: own_bill or rule_based
    response: rules # with one: optimized, or rules
    rules: {dr_event: {ev: {action: discharge, to: export, keep_soc_frac: 0.5}}} # a V2X EV exports for events
    external_load: {...}
    devices: [...]
  - name: strip_mall # load that isn't a customer: fixed, not billed, never flexible
    element: tx_2
    kind: background
    external_load: {source: timeseries, path: ..., premise_id: strip_mall, column: load_kw}
analyses:
  feeder:
    elements: [...]
    coordination:
      price_signal: {source: coordinated} # or schedule (by element, month and hour), or file
      envelopes: {} # pro_rata_need shares of each element's headroom, a new envelope each hour
      dr_events:
        element: tx_1
        trigger: {above_pct: 80} # events where the forecast loading is over 80% of the rating
        program: {program_id: tx_1_peak, window: "15:00-22:00", max_hours: 4, response: incentive, incentive_per_kwh: 0.75}
      runs: # optional; without it, each mechanism alone, plus all combined
        envelopes: [envelopes]
        signal_and_envelopes: [price_signal, envelopes]
```

**Who takes part:**
- Non-participants behave the same in every run, and are fixed flows for everyone
  else, like background load.
- Participants follow `baseline_behavior` in `independent`, and respond to each run's
  mechanisms:
  - **optimized:** their own bill plus the mechanisms;
  - **by rule:** their baseline rules, overridden step by step without looking ahead.

**Rules** are actions with settings, by mechanism and device type (or device name):

| Device | Actions |
|---|---|
| EV | `pause`, `discharge` (V2X: `to: home_load` or `export`, `keep_soc_frac`), `limit` |
| Battery | `discharge`, with optional `precharge_hours` |
| Thermostat | `setback`, with optional precool; needs the `setpoint` counterfactual policy |
| Flexible load | `shed` |
| Solar | `curtail` |

When two rules apply at once, the one that draws less from the grid wins.

**The mechanisms:**
- **Price signal:** a $/kWh adder on net import, summed along the customer's path. It's
  charged on the bill (`paid: true`) and reported as signal charges.
- **Envelopes:** each participant's import (and optionally export) limit by step. It's
  built from each element's rating less a margin, less non-participants' and background
  flows, then:
  - allocated by `pro_rata_need` (default), `equal`, `max_min`, `service_size`,
    `weights`, `fixed` or `file`;
  - nested `min_along_path` (default) or `top_down`;
  - issued per step, per block of hours, or once (`static`).

  Limits are soft: going over is penalized and reported.
- **Feeder DR:** events triggered by the loading forecast of a "twin" without events.
  For DR alone, that's `independent`; combined, it's the other mechanisms in place.
  Participants below the element are settled against their own history, or the twin's
  load when the run is too short for the settlement rule's lookback.

**What's written:**
- `coordination.csv`: one row per run, with peaks, hours over the rating, network cost,
  bills by group, payments, and the network saving against the ideal's.
- `customers.csv`: one row per customer and run.
- `envelopes_<run>.csv`, `signal_<run>.csv`, `dr_calendar_<run>.csv`.

In the example, envelopes capture 87% of the ideal's network saving, and the three
together 95%. The evening price signal and the DR events, each alone, *add* $95 to
network cost: participants move charging to just after the window, which makes a new
peak on tx_1.

## Modeling caveats worth knowing

These all came up while building the examples — each produced a plausible-looking but
wrong answer before it was understood:

- **`energy_arbitrage` doesn't know NBT's credit limits.** It values each exported kWh
  at the full export rate, but on the bill the generation half of export credit can
  only offset generation charges. A household that exports more than it uses runs out
  of generation charges to offset, so extra exports just pile up unusable credit, while
  the charges it actually pays (delivery, non-bypassable, base services) only fall by
  importing less. In `battery_solar_lbnl_nbt_annual.yaml`, `energy_arbitrage` claims
  $395/yr of value over self-consumption, but its dispatch's **bill is $89 higher**.
  `nbt_bill` fixes this: same household, bill $233 vs $259 for self-consumption. Use
  `nbt_bill` whenever a scenario is billed on NBT.

- **`nbt_bill` values leftover credit at zero.** Credit still unused at the end of the
  run (carried forward at a true-up, or simply left over in a run shorter than a year)
  counts for nothing. That's right for a household that's a net exporter every year;
  for a run of a few days it understates exports late in the run, since in reality
  that credit would offset the rest of the month. It also needs
  `exclusive_import_export: true`: on some summer evenings the delivery export credit
  is higher than the delivery import rate, and without it the optimizer imports and
  exports in the same hour. That makes the solve a MILP (the annual example takes
  about a minute instead of ~4 s).

- **Terminal SOC and fair comparisons.** Energy left in a battery/EV at the end of the
  horizon isn't valued, so whichever dispatch ends with *less* stored energy looks
  better by roughly that energy's worth. `der-opt run` warns (`end state` check) when the optimal
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
- **Demand charges** in the optimizer use one peak over the whole horizon (not per
  billing month), and the optimizer's URDB **tiered tariffs** apply tiers per period
  (not monthly-cumulative). Fine for short horizons; inaccurate for multi-month runs.

## Python API

For anything the YAML doesn't cover — custom counterfactual policies, interval
heatmaps, multiple solves in a loop (sweeps, sensitivity runs) — script against the
Python API directly. `examples/03_full_mvp_with_duals.py` is the template: all five
device types, a URDB tariff, duals, and every chart type. The `der-opt` CLI's own code
(`src/der_opt/cli/main.py`, `src/der_opt/scenarios/`) shows how YAML scenarios map onto
the same objects.

Run it with `MPLBACKEND=Agg python examples/03_full_mvp_with_duals.py` (on Windows,
`$env:MPLBACKEND = "Agg"` first); charts land in
`examples/outputs/` (gitignored).

## Layout

```
src/der_opt/
  devices/       solar, battery, thermostat, curtailable_load, ev_v2x
  premise/       Premise (composition + POI balance), value streams, tariffs
  pricing/       price series, dual extraction, value breakdown
  simulation/    counterfactual control policies + simulator
  billing/       retail tariffs, PG&E holidays, bill engine (NBT, NEM2), bill objectives
  inputs/        input data spec 2.0: readers, validation, quality checks
  dr/            demand response: event calendars, settlement, responses, capacity
  avoided_costs/ avoided cost sets and first-year screening values
  cost_test/     the measure impact package for cost tests (OpenBCA)
  population/    the population model: resolved premises, precedence, templates, studies, portfolio
  integrations/  LBNL load, EV model, PVWatts, PG&E NBT export rates, CAISO LMP
  network/       feeder topology, multi-premise dispatch, congestion prices
  coordination/  participation, price signals, operating envelopes, rule-based responses, feeder DR
  scenarios/     YAML schema, loader, extends and paths, validate, templates (der-opt new), feeders and studies
  viz/           heatmap, dispatch, value stack, duals, comparison, feeder loading charts
  cli/           der-opt CLI
examples/        Python-API scripts, scenarios/ (YAML), library/ (scenario building blocks), data/
tests/           unit + golden-file integration tests
```
