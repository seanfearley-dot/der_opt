# Starter library: scenario building blocks

Blocks a scenario can `extend` ([docs/SCENARIO_COMPOSITION_SPEC.md](../../docs/SCENARIO_COMPOSITION_SPEC.md)).
Every example in `../scenarios/` is built from them, and the tests run every block, so a
block that stops working fails a test.

**Values marked `# illustrative` are placeholders**, not sourced numbers: an incentive,
costs, discount rates, a flat export price, degradation. For your own work, copy this
folder and set them:

```bash
cp -r examples/library my_library
der-opt new dr-battery scenarios/home_a.yaml --timeseries ... --library my_library
```

`der-opt new --library DIR` uses a block already in `DIR` as it is, and writes only the
ones that aren't there. Pointed at this library, it reuses these blocks and notes which
ones have illustrative values.

## Using a block

A scenario lists blocks under `extends`, then adds what's its own. Data that differs by
home (load, solar, temperature) stays in the scenario:

```yaml
extends:
  - ../library/time/year_2026.yaml
  - ../library/devices/battery_13kwh.yaml
  - ../library/value_streams/pge_e_elec_2026-06_import.yaml
  - ../library/meter/panel_20kw.yaml
  - ../library/counterfactual/battery_self_consumption.yaml
name: home_a
devices:
  - {type: solar, name: solar, params: {available_kw: {source: timeseries, path: ../data/home_a.csv, column: pv_kw}}}
  - name: battery # change one setting of the block's battery; null removes one
    params: {charge_sources: [solar], soc_final_min_frac: null}
external_load: {source: timeseries, path: ../data/home_a.csv, column: load_kw}
```

What merges and what replaces:
- **Merges:** mappings key by key, and devices by name, setting by setting.
- **Replaces:** other lists, whole. So `value_streams` blocks don't combine; use one
  (`pge_e_elec_2026-06_import_demand_charge.yaml` restates the energy prices for that
  reason).
- **Blocks that add to a block:** a few are meant to come after another in the list,
  and say so (`programs/monthly_test_events.yaml`).

## The blocks

| Folder | Block | What it is |
|---|---|---|
| `time/` | `year_2026` | 2026, hourly (8,760 steps) |
| | `2026-07-13_168x60min` | the week of the CAISO LMP file |
| | `2026-07-13T12_72x60min` | three July days, noon to noon (feeder examples) |
| | `2026-08-03T12_72x60min` | three days around the loadcast bank's design-year peak |
| | `2025-07-15_168x60min`, `2025-07-15_72x60min` | a week and three days in July 2025 (the LBNL examples) |
| `devices/` | `battery_13kwh` | 13.5 kWh / 5 kW home battery, back to half charge at the end |
| | `battery_1mw_4mwh` | 1 MW / 4 MWh grid battery |
| | `ev_commuter_v2x` | 75 kWh V2X EV on a daily commuting pattern (38 mi weekdays, 20 mi weekends) |
| | `ev_commuter_charge_only` | the same EV without V2X (extends `ev_commuter_v2x`) |
| | `thermostat_cooling` | central AC on a smart thermostat, 20–25 °C; **the scenario supplies `ambient_temp_c`** |
| | `curtailable_load` | a load served in 0.75 kW blocks (up to 2 off); **the scenario supplies `baseline_load_kw`** |
| `value_streams/` | `pge_e_elec_2026-06_import` | E-ELEC import, flat $0.05/kWh export |
| | `pge_e_elec_2026-06_import_demand_charge` | the same, plus an illustrative $15/kW demand charge |
| | `caiso_lmp_np15_dam_2026-07-13` | buy and sell at CAISO NP15 day-ahead |
| | `bill` | optimize the bill in the billing section (NBT or NEM2) |
| `meter/` | `panel_15kw`, `panel_20kw` | meter limit, both directions |
| | `interconnection_1mw` | 1 MW limit for a grid battery |
| | `non_export` | nothing back to the grid |
| | `no_simultaneous_import_export` | a single meter: no import and export in the same step (needed when export can pay more than import; on/off variables only in those steps) |
| `counterfactual/` | `battery_self_consumption` | R1: the battery self-consumes |
| | `ev_tou_timer` | R1: the EV charges on a TOU timer, from the start of the cheapest period |
| | `ev_plug_and_charge` | R1: the EV charges at full power on plug-in |
| `billing/` | `pge_e_elec_2026-06_nbt` | E-ELEC with NBT export credits |
| | `pge_e_tou_c_2026-06_nem2` | E-TOU-C with NEM 2.0; **the scenario supplies `baseline_territory`** |
| `programs/` | `summer_peak_incentive` | May–Oct weekday 4–9 pm DR, paid per kWh, randomized events |
| | `summer_peak_commitment` | the same with a 3 kW firm commitment (extends `summer_peak_incentive`) |
| | `monthly_test_events` | add after a program: year-round, a 2-hour test event the first Tuesday of each month |
| `avoided_costs/` | `synthetic_2026` | first-year screening values from the synthetic set |
| `cost_tests/` | `synthetic_2026` | cost-test settings on the synthetic set, illustrative costs and rates; the battery compared against self-consumption |
| `study/` | `homes_bill` | a study's objective (each premise's own bill) and counterfactuals (EVs on a TOU timer, self-consuming batteries) |
| | `device_params` | `defaults` for a study's batteries and EVs (sizes come from `devices.csv`) |

Study blocks set `objective` and `defaults`. A study that extends several gets every
block's `defaults`, in order, then its own (a later entry wins).

A block with a `TODO` needs that value from the scenario. `der-opt validate` reports any
`TODO` left, naming the block it's in.

Not here, because they belong to one study rather than many: network ratings and
deferral values, synthetic premise mixes (both specific to a feeder), and capacity
payments (`capacity_payment` needs event step numbers for one run; DR programs cover
the same ground).
