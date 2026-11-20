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
der-opt run examples/scenarios/feeder_ev_heat_wave.yaml -o out/feeder             # 20 houses, 2 transformers
der-opt run examples/scenarios/feeder_bank_planning_trace.yaml -o out/bank_trace  # bank 1-in-10 year + 40 new homes (.[forecast])
der-opt run examples/scenarios/battery_caiso_lmp.yaml                             # battery on CAISO LMP
der-opt run examples/scenarios/feeder_from_files.yaml                             # 6 homes from spec files
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

Every run ends with **checks** (feeder runs too, on every house in every run):

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
| `checks.csv` | every check finding: level, check, run, house, message |
| `run_record.json` | what produced the result: the scenario as resolved, a SHA-256 of every input file it names, the git commit (and whether there were uncommitted changes), Python and package versions, headline results, and the checks |
| `scenario.yaml` | a copy of the scenario file as run |

State columns (`soc`, `temp`) are the value at the **start** of each period in both
the optimal and counterfactual CSVs.

The fastest way to a new scenario: **copy one of the files in `examples/scenarios/`
and edit it.**

## Scenario file reference

```yaml
name: my_house                      # any label; prefixes constraint names in --duals output

time_grid:
  resolution_minutes: 60            # any resolution; LBNL, PVWatts, daily_tou need 60
  horizon_steps: 72                 # number of periods (72 hourly = 3 days)
  start: "2025-07-15T00:00:00"      # required for anything date/hour-of-day aware

devices: [...]                      # see Devices below; order matters for charge_sources
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
  tariff: examples/data/tariffs/pge_e_elec_2026-06.yaml
  program: {type: nbt, export_rates_csv: examples/data/pge_nbt_eec_2026.csv, acc_plus_per_kwh: 0.0088}
```

### Devices

Every device has `type`, a unique `name`, and `params`. Any array-valued param
(marked *array*) can be a literal list or a data source (see below).

| `type` | Required params | Optional params (default) |
|---|---|---|
| `solar` | `available_kw` *array* | — |
| `battery` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw` | `eta_charge` (0.95), `eta_discharge` (0.95), `soc_min_frac` (0), `soc_max_frac` (1), `soc_init_frac` (0.5), `soc_final_min_frac`, `degradation_cost_per_kwh` (0), `charge_sources` |
| `ev_v2x` | `capacity_kwh`, `p_max_charge_kw`, `p_max_discharge_kw`, `connected` *array* (0/1 plug-in schedule) | same SOC/efficiency/degradation params as battery, plus `departure_period` (int or list), `soc_departure_min_frac` (float or list), `charge_sources`, `driving_kwh` *array* (kWh used driving each period; without it SOC is frozen while away) |
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
{type: retail_tariff, path: examples/data/tariffs/pge_e_elec_2026-06.yaml}  # total $/kWh of a billing tariff
{type: nbt_export, csv_path: examples/data/pge_nbt_eec_2026.csv, adder_per_kwh: 0.0088}
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
  tariff: examples/data/tariffs/pge_e_elec_2026-06.yaml
  income_tier: 3                  # base services charge tier (3 = not CARE/FERA)
  program: {type: nbt, export_rates_csv: examples/data/pge_nbt_eec_2026.csv, acc_plus_per_kwh: 0.0088}
  # or, for NEM 2.0 on E-TOU-C:
  # tariff: examples/data/tariffs/pge_e_tou_c_2026-06.yaml
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
[docs/INPUT_DATA_SPEC.md](docs/INPUT_DATA_SPEC.md) (v1.1, approved). Produce that format
from your own pipeline; nothing else needs configuring per file. In short:

* **Time series:** `timestamp, premise_id, <values>`, one row per premise per interval.
  `timestamp` is the interval start in ISO 8601 with a UTC offset
  (`2026-07-13T00:00:00-07:00`). Values are average kW (`load_kw`, `pv_kw`,
  `import_kw`, `export_kw`), plus `ev_connected` (0/1), `ev_driving_kwh`, and
  `import_price_per_kwh` / `export_price_per_kwh` (prices go in a file without
  `premise_id`). Each premise is either **gross** (`load_kw`, plus `pv_kw` if it has
  solar) or **metered** (`import_kw`/`export_kw`), never both. A missing value is an
  empty cell, never 0. The interval is 5, 15, 30 or 60 minutes, with every interval
  present.
* **`premises.csv`:** one row per premise: `element_id`, `tariff`, `export_program`,
  `customer_class`, `baseline_territory`, solar/battery/EV sizes, `import_limit_kw`,
  `export_limit_kw`.
* **`network.csv`:** one row per transformer or section: `element_id`, `parent_id`,
  `rating_kw`, `overload_cost_per_kwh`.

```bash
der-opt check-data data/timeseries.csv --premises data/premises.csv --network data/network.csv
```

`check-data` names every rule a file breaks (exit code 1) and warns about suspect
data: empty cells, a day or more without consumption, 12 h unchanged, spikes, a home
over 100 kW. It also reports the period covered and each premise's kWh.

**Using the files.** For a single premise, any array or price can come from a file:

```yaml
external_load: {source: timeseries, path: data/timeseries.csv, premise_id: home_01, column: load_kw}
import_price: {type: timeseries, path: data/prices.csv, column: import_price_per_kwh}
```

For a feeder, `premise_data` builds every house from the files.
`examples/scenarios/feeder_from_files.yaml` runs the synthetic sample in
`examples/data/spec_sample/`:

```yaml
premise_data:
  premises: data/premises.csv
  timeseries: [data/timeseries.csv]          # a premise's columns may span several files
  network: data/network.csv                  # instead of feeder.elements
  tariff_dir: examples/data/tariffs          # premises.csv `tariff` names a file here
  programs: {nbt: {export_rates_csv: ..., acc_plus_per_kwh: 0.0088}, nem2: {nsc_rate_per_kwh: 0.02781}}
house_defaults:
  value_streams: [{type: bill}]              # each home's own bill, nbt_bill or nem2_bill by its program
  device_params: {battery: {eta_charge: 0.96, degradation_cost_per_kwh: 0.015}, ev: {...}}
```

Sizes come from `premises.csv` and behavior from `device_params` (defaults: batteries
95% efficient each way, starting and ending half full; EVs charged to 90% by every
departure, which is read off `ev_connected`). A metered premise can't have devices
added: its meter already includes them. On the local clock, November's repeated hour
is averaged into one and March's skipped hour interpolated.

## Feeders: many houses on a network

A scenario with a `feeder` section is a multi-house run. Every house is an ordinary
premise (devices, value streams, bill, counterfactual), and the feeder is a tree of
transformers and line sections with kW ratings. `der-opt run` dispatches the same
houses three ways and prices each on the same terms:

| Run | What it is |
|---|---|
| coordinated | one optimization over every house and the network: bills + wear + overload + deferral value |
| independent | each house optimizing its own bill, blind to the network |
| counterfactual | the rule-based policies in `house_defaults.counterfactual` (e.g. EVs charging on plug-in) |

```yaml
time_grid: {resolution_minutes: 60, horizon_steps: 72, start: "2026-07-13T12:00:00"}
feeder:
  elements:
    - {name: lateral, rating_kw: 200}
    - {name: tx_a, parent: lateral, rating_kw: 50, overload_cost_per_kwh: 1.0}
  value_streams:
    - {type: capacity_deferral, element: tx_a, value_per_kw: 40.0}
synthetic_houses:              # generated houses (no data needed), or list them under houses:
  - {element: tx_a, count: 10, seed: 1, ev_share: 0.7, solar_share: 0.4, battery_share: 0.5}
house_defaults:                # applied to every house that doesn't set its own
  value_streams: [{type: nbt_bill}]
  poi_limit_kw: 30
  counterfactual: {policies: {ev: {type: rule_based_ev}, battery: {type: self_consumption}}}
billing: {...}                 # shared by every house
```

* **Elements:** `rating_kw` applies to load and backfeed. Without
  `overload_cost_per_kwh` it's a hard limit (a run is infeasible if uncontrollable load
  alone exceeds it); with one, flow may exceed it at that cost per kWh over.
* **`capacity_deferral`:** `value_per_kw` x (peak flow above `threshold_kw`, default 0).
  It takes an annual $/kW-year and applies it to the run's peak, so use it on a horizon
  that contains the element's annual peak.
* **Houses** under `houses:` take the same fields as a one-house scenario plus
  `element`. **`synthetic_houses`** generates them (`der_opt.network.synthetic`): base
  load, morning and evening occupancy bumps, air conditioning that follows a heat-wave
  temperature with a few hours' lag, and, by share, rooftop solar, an EV that drives
  each day and must be back at 90% by its departure, and a solar-only-charging battery
  that must end where it started. The ranges are judgment calls for an inland
  California summer, not fitted to data.
* **Congestion prices** are the duals of each element's flow: the cost of one more kWh
  through it in each hour. They're exact while the model is an LP.

Output (`-o`): `houses.csv` (devices, the generated draws, each run's bill),
`flows_kw.csv`, `loading.csv` (peak, % of rating, hours and kWh over),
`congestion_prices.csv`, `dispatch_<run>.csv` per house, and `feeder_loading.png`.

`feeder_ev_heat_wave.yaml`, three heat-wave days, transformer A with 7 EVs in 10
homes:

| | coordinated | independent | plug-in charging |
|---|---|---|---|
| Transformer A peak (50 kW rating) | 37.7 kW | 81.1 kW (6 h over) | 68.0 kW (8 h over) |
| Customer bills, 20 homes | $1,398 | $1,397 | $1,468 |

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
  together. It reports which houses pay more, but doesn't compensate them; that's for
  a settlement mechanism (not built yet).

`feeder_bank_planning_trace.yaml` puts the same machinery under a whole primary bank: the
bank's existing load is one device-less house whose `external_load` is loadcast's 1-in-10
design year (`trace: poe10`, peak 1,990 kW on 4 Aug 2026 at 16:00), and a new 40-home
subdivision (80% EVs) joins on two 75 kW transformers. Over 3-6 Aug:

| | coordinated | independent | plug-in charging |
|---|---|---|---|
| Bank peak (2,000 kW rating) | 2,069 kW (1 h over) | 2,086 kW (1 h over) | 2,102 kW (2 h over) |
| New transformer A peak (75 kW) | 105 kW | 209 kW | 167 kW |
| Customer bills, 40 new homes | $3,330 | $3,290 | $3,631 |

The bank's own 4 pm peak leaves the new homes little room, but what binds first is their
service transformers. Coordination costs 13 homes $40 in total for a $1,282 network saving.
The existing load is fixed here (no controllable devices), the new homes' synthetic weather is
not the trace's weather, and loadcast's bank load includes its losses while the new homes'
doesn't -- a sizing study, not a single consistent day.

Flow is a power balance only: no losses, voltage, or reactive power, so convert kVA
nameplates to kW yourself. Every house shares one time grid.

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
  inputs/        input data spec v1.1: readers, validation, quality checks
  integrations/  LBNL load, EV model, PVWatts, PG&E NBT export rates, CAISO LMP
  network/       feeder topology, multi-premise dispatch, congestion prices, synthetic houses
  scenarios/     YAML schema, loader, data-source resolvers, feeder scenarios
  viz/           heatmap, dispatch, value stack, duals, comparison, feeder loading charts
  cli/           der-opt CLI
examples/        Python-API scripts, scenarios/ (YAML), data/ (NBT rates, sample tariff)
tests/           unit + golden-file integration tests
```
