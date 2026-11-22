# der-opt population model specification

**Version 1.0**, approved 2026-11-21 with the recommended choice on each decision, and
`add_devices` (section 3a, decision 12).
- v0.2: what each analysis needs, checked by `validate` (section 4a). Also covers premises
without load, series from data sources, background load, per-premise measure costs,
measures selected by type, and any premise setting as a column.
- v1.0: `add_devices` for adoption what-ifs (section 3a). It replaces
the parked customer settings draft ([CUSTOMER_SETTINGS_SPEC.md](CUSTOMER_SETTINGS_SPEC.md)).

## Purpose

A premise (one customer site behind one meter) is described today in three ways, each
able to do different things:

| | Single-home scenario | `houses:` entry | `premises.csv` row |
|---|---|---|---|
| Devices | any number, any type | any number, any type | at most one solar, battery and EV, as columns |
| Device behavior | in full | in full | one set per type for every premise (`device_params`) |
| DR, cost tests | yes | no | no |
| Coordination settings | no | yes | defaults only |

Fields are copied between the three by hand, so each new premise feature has to be added
in three places, and they drift. Devices are columns, so a second EV or a new device type
changes the file format. Each layer of defaults has its own merge logic. And one file
mixes the population, the network, the prices and what to run.

This spec gives der-opt **one model of a premise**:
1. **A population in tables** (section 1):
   - `premises.csv`: one row per premise;
   - `devices.csv`: one row per device;
   - time series keyed by premise and device.
2. **Device templates** (section 2): named parameter sets, each with its counterfactual
   policy and rules.
3. **One precedence rule for every setting** (section 3), with the source of every value
   recorded.
4. **Studies separate from populations** (section 4). A study says which population and
   network, the prices, the defaults and overrides, and what to run. A feeder study and a
   per-premise portfolio study (DR, screening, cost tests for each premise) both use the
   same population.
5. **One resolved premise inside der-opt** (section 5). Every way of describing a premise
   becomes it, and everything downstream uses it.

Single-home YAML and written-out premises stay, for small studies, and compile into the
same model (section 6).

## 1. The population: three tables

### `premises.csv`: one row per premise (one meter)

No column is required everywhere except `premise_id`: what a study needs depends on its
analyses (section 4a). The codes in "Needed for" are the analyses in that section.

| Column | Needed for | Meaning |
|---|---|---|
| `premise_id` | all | unique |
| `kind` | — | `premise` (default), or `background`: load through an element that isn't a customer, with no devices or bill (decision 8) |
| `element_id` | F, C | where it connects (network file) |
| `customer_class` | D | `residential` or `commercial` (5-in-10 or 10-in-10 settlement) |
| `tariff` | B | tariff file name |
| `export_program`, `baseline_territory` | B, when the tariff or program needs them | as in input spec v1.1 |
| `import_limit_kw`, `export_limit_kw` | C with `service_size` envelopes | meter limits |
| `participation`, `baseline_behavior`, `response`, `envelope_weight` | C | coordination settings ([COORDINATION_SPEC.md](COORDINATION_SPEC.md)) |
| any other premise setting, by name | — | e.g. `exclusive_import_export`, `income_tier`, `baseline_code`: a column named for a setting sets it (section 3, level 4) |
| any other column | — | a **tag** (`segment`, `program_cohort`, `weather_station`…): carried through, usable to select premises (section 3), reported |

A premise needs its **load** (section 1, time series) unless its `load` setting is
`none`, as for a standalone grid battery.

### `devices.csv`: one row per device

| Column | Required | Meaning |
|---|---|---|
| `premise_id` | yes | the premise it's behind |
| `device_id` | yes | unique within the premise (`battery`, `ev_1`, `ev_2`) |
| `type` | yes | `solar`, `battery`, `ev`, `thermostat`, `flexible_load` |
| `template` | no | a device template (section 2); without one, the type's built-in defaults |
| `kw`, `kwh` | per type | the size, in the type's usual terms (below) |
| `v2x` | EVs | `1` if it can discharge, else `0` |
| `installed_cost`, `annual_om` | T, for device-priced measures | the device's cost to the participant: upfront $ and $/year. A measure's participant costs are the sums over its devices (section 4a) |
| `charge_sources` | no | other devices at the same premise it may charge from, by `device_id`, `;`-separated (`solar` or `solar_1;solar_2`) |
| any device parameter | no | e.g. `degradation_cost_per_kwh`, `soc_departure_min_frac`: overrides the template for this device. An unknown column is an error naming the type's parameters |

What `kw` and `kwh` mean for each type:

| `type` | `kw` | `kwh` | Time series it reads (section 1, below) |
|---|---|---|---|
| `solar` | inverter AC rating (a check on `pv_kw`) | — | `pv_kw` |
| `battery` | charge and discharge rating | usable capacity | — |
| `ev` | charger rating (and discharge, if `v2x`) | battery capacity | `ev_connected`, `ev_driving_kwh`, or a daily `pattern` from the template |
| `thermostat` | HVAC electric rating | — | `outdoor_temp_c` (new) |
| `flexible_load` | the most it can shed | — | `flexible_kw` (new): its baseline |

```csv
premise_id,device_id,type,template,kw,kwh,v2x,degradation_cost_per_kwh
home_01,solar,solar,,4.8,,,
home_01,ev,ev,commuter_charger,7.2,60,0,
home_02,ev_1,ev,commuter_charger,7.2,82,0,
home_02,ev_2,ev,commuter_v2x,11.5,75,1,0.03
home_06,battery,battery,battery_13kwh,5,13.5,,
home_06,ac,thermostat,central_ac_cooling,4,,,
```

### Time series

As in input spec v1.1 (long layout, ISO 8601 with offsets, average kW), with two
changes:
- **An optional `device_id` column.** A device's series (`pv_kw`, `ev_connected`…) come
  from rows with its `premise_id` and `device_id`. When a premise has one device of a
  type, `device_id` can be left blank and the series matched by type.
- **New columns:**
  - `outdoor_temp_c`, for thermostats, and needed only by premises that have one.
    Nothing else in der-opt reads temperature: load already reflects its weather.
    It can also be a shared file, keyed by weather station, with a `weather_station`
    tag in `premises.csv`, or a single value for a quick test;
  - `flexible_kw`, for flexible loads.
- **Load that a device models stays out of `load_kw`.** A premise with a `thermostat`
  device has its HVAC modeled by that device, so its `load_kw` must exclude the air
  conditioning (or heating). The same goes for a `flexible_load`'s baseline, which is
  `flexible_kw`, not part of `load_kw`. Otherwise the load is counted twice. `validate`
  can't detect this from the data, so it reminds you whenever a premise has either
  device.

**Series that don't come from these files** (a loadcast forecast, LBNL data, PVWatts,
the EV charging model's schedules) are given as a setting instead, with the data
sources scenarios use today:

```yaml
defaults:
  - where: {segment: existing}
    set: {premise: {load: {source: loadcast, path: ../data/loadcast/bank_a_2026_planning.nc, trace: poe10}}}
  - where: {type: solar}
    set: {device: {available_kw: {source: pvwatts, lat: 37.4, lon: -121.9, system_capacity_kw: 5}}}
```

A premise's or device's rows in the time series files are its table data (section 3,
level 4). So a study default data source fills in only where the files have nothing,
and an override replaces the file's series. `validate` reports which source each
premise's series came from.

These changes make it **input data spec v2.0**:
- `devices.csv` and the time series changes are new;
- premises gain `kind`, and lose their device columns.

## 2. Device templates

A template is a named set of parameters for one device type, in a library folder. It can
also carry the device's counterfactual policy and its coordination rules:

```yaml
# library/devices/commuter_charger.yaml
template: commuter_charger
type: ev
params:
  eta_charge: 0.92
  soc_max_frac: 0.9
  soc_init_frac: 0.9
  soc_departure_min_frac: 0.9
  degradation_cost_per_kwh: 0.0
  pattern: # used when the premise has no ev_connected series
    weekday: {depart: "07:30", arrive: "18:30", miles: 38}
    weekend: {depart: "10:00", arrive: "15:00", miles: 20}
    kwh_per_mile: 0.30
counterfactual: {type: rule_based_ev} # R1: what it does without optimization
rules: # how it responds by rule (response: rules), per mechanism
  dr_event: {action: pause}
  envelope: {action: limit}
```

- **Templates can extend templates** (`extends: commuter_charger.yaml`), like any block,
  so `commuter_v2x` can be `commuter_charger` with discharge.
- **Templates are found** in the folders the study lists (`population.templates`),
  default the library's `devices/` folder. A name in two folders is an error.
- **Existing library device blocks** (`devices: [{type, name, params}]`, with one device)
  are accepted as templates too, named by their file, so the starter library works
  unchanged (decision 3).

## 3. One precedence rule

Every setting of every premise and device, from the meter limit to an EV's degradation
cost, is resolved the same way. The first item is the weakest:
1. **built-in defaults** for the device type or premise;
2. **the template** named on the device row;
3. **study defaults**, in the order written, each applying to the premises or devices it
   selects;
4. **the table row**: the premise's or device's own columns;
5. **study overrides**, by ID.

```yaml
defaults:
  - set: {premise: {poi_limit_kw: 20}} # everyone
  - where: {type: battery}
    set: {device: {degradation_cost_per_kwh: 0.015}}
  - where: {element_id: tx_a, segment: new_build}
    set: {premise: {participation: participant, response: rules}}
overrides:
  home_03: {premise: {participation: non_participant}}
  home_02/ev_2: {device: {soc_departure_min_frac: 0.8}}
```

- **`where`** selects by any premise column (element, class, tariff, tags) and, for
  devices, by `type` or `template`. No `where` means everyone.
- **A table row beats study defaults** (decision 2): the row is about that premise, the
  defaults are study-wide assumptions. To change a row's value for a study, use an
  override.
- **Mappings merge key by key** at every level, and `null` removes a setting, as in
  scenario composition.
- **Every resolved value records its source:**
  - `built-in`, `template:commuter_charger`, `defaults[2]`, `devices.csv:row 14`, or
    `override:home_02/ev_2`;
  - `der-opt validate --population DIR` writes `premises.csv` and
    `device_parameters.csv` (one row per premise, device and parameter, with value and
    source) before anything is solved;
  - every run writes the same files.

This replaces `house_defaults`, `device_params`, the counterfactual defaults by device
name, and the rules merge, with one mechanism.

**Selecting devices.** Wherever a setting names devices (a DR or cost-test measure,
`charge_sources`), a selector can stand in for a list of IDs, so it works across a
population:

```yaml
measure: {type: [battery, ev]} # or {template: [commuter_v2x]}, or {device_id: [battery]}
```

## 3a. Adding devices: `add_devices`

Precedence sets the parameters of devices that exist; it never adds one. Adoption
what-ifs ("what if 30% of homes on tx_a add a battery?") are a study step instead, so
the population files stay as they are and the what-if is recorded:

```yaml
add_devices:
  - where: {element_id: tx_a, kind: premise}
    share: 0.3 # of the selected premises that don't already have this device_id
    seed: 1
    device: {device_id: battery, type: battery, template: battery_13kwh, kw: 5, kwh: 13.5}
  - where: {segment: new_build}
    share: 0.8
    seed: 2
    device: {device_id: ev, type: ev, template: commuter_charger, kw: 7.2, kwh: 75}
```

- **Exact counts:** `round(share × eligible)` premises, chosen at random from `seed`.
  Each entry has its own random stream, so changing one entry's share or seed never
  changes which premises another entry picks. Raising a share keeps the premises it
  already chose and adds more.
- **Eligible premises:** those its `where` selects that don't already have a device with
  that `device_id`. Premises that already have one are counted, but left as they are.
- **An added device is like a row in `devices.csv`** (level 4 in section 3). Its source
  is `add_devices[0]`, and later defaults and overrides apply to it as to any device.
- **Its series:** an added device needs its type's series like any other. A template
  with a `pattern` covers an EV. Added solar needs a data source (PVWatts, or a shared
  production shape scaled by `kw`), given as a study default. `validate` reports any
  added device without its series.
- **Reported:** the premises each entry chose are in `device_parameters.csv` and the run
  record, so a what-if can be reproduced or audited.

## 4. Studies

A study file says what to do with a population:

```yaml
extends: [../library/time/year_2026.yaml]
name: tx_a_programs_2026
population:
  premises: ../data/population/premises.csv
  devices: ../data/population/devices.csv
  timeseries: [../data/population/timeseries.csv, ../data/weather/stations.csv]
  network: ../data/population/network.csv # needed for feeder analyses
  templates: [../library/devices]
tariffs:
  dir: ../data/tariffs
  programs: {nbt: {export_rates_csv: ../data/pge_nbt_eec_2026.csv, acc_plus_per_kwh: 0.0088}}
objective: bill # each premise's own optimization: bill, or a value_streams list
defaults: [...]
overrides: {...}
analyses:
  feeder: # coordinated, independent, counterfactual, and coordination runs
    coordination: {...} # COORDINATION_SPEC.md, unchanged
  portfolio: # each premise on its own
    dr: {...} # DR_CAPACITY_SPEC.md, per premise
    avoided_costs: {set: ../data/avoided_costs/acc_2026}
    cost_test: {...} # COST_TEST_SPEC.md, per premise
    select: {where: {participation: participant}} # optional: which premises
    measure: {type: [battery]} # the measure's devices at each premise (DR's R3, device-scope cost tests)
```

- **Analyses** (a study runs any of them):
  - `feeder`: needs a network, and covers every premise;
  - `portfolio`: each selected premise solved on its own, with DR, screening and the
    measure impact package per premise, and a summary table summing them (decision 5).
    Cost tests run per premise; an aggregated OpenBCA run is left for later.
- **Output:**
  - a folder per analysis;
  - for the portfolio, a subfolder per premise in today's single-home layout;
  - `run_record.json` hashes the population files and templates.
- **Composition:** `extends`, paths relative to each file, `validate` and `--pick` all
  work on study files as they do on scenarios today.

## 4a. What each analysis needs

Each analysis a study runs determines what the population must have. `der-opt validate`
checks exactly that for the study's analyses. It lists what's missing, by premise and
table row, before anything is solved. A column one analysis needs is never required by
a study that doesn't run that analysis.

| Code | Analysis | Each premise needs | Each device needs | Study needs |
|---|---|---|---|---|
| **O** | dispatch: optimal and counterfactual runs, value of optimization, duals (every analysis builds on it) | load, from the files or a data source, or `load: none` | its type's series (`pv_kw`; EV plug-in and driving, or a `pattern`; `outdoor_temp_c`; `flexible_kw`) | time grid, `objective` |
| **B** | billing (`objective: bill`, or bills reported) | `tariff`; `export_program` for export credit; `baseline_territory` for a tariff with a baseline credit | — | `tariffs.dir`, program settings |
| **W** | wholesale or other price streams (LMP, demand charges, capacity payments) | — | — | the value streams and their price files |
| **D** | DR (portfolio) | `customer_class` | — | `dr` program and events, `measure`; a full year for settlement history (shorter runs fall back to the twin's load) |
| **S** | screening values | — | — | `avoided_costs.set` covering the run |
| **T** | cost tests | measure costs: device `installed_cost` and `annual_om`, or premise settings `participant_upfront`, `participant_annual_om` | — | `cost_test` (scope, life, discount rates, program costs), the avoided cost set covering the life, OpenBCA for running them |
| **F** | feeder runs (coordinated, independent, counterfactual) | `element_id`; background premises need only `element_id` and load | — | a network (`network.csv` or elements) |
| **C** | coordination | the settings each mechanism uses: limits for `service_size`, `envelope_weight` for `weights`; participants below a `dr_events` element | rules come from templates or defaults | `coordination` |

- **Counterfactual policies** (part of O) come from templates or defaults. A device
  without one runs passive, and `validate` notes it.
- **Shared series** (prices, weather by station) are files the study names. A tag
  (`weather_station`, `lmp_node`) selects each premise's series through defaults.
- **Measure costs:** a device-priced measure (decision 10) sums its devices'
  `installed_cost` and `annual_om`. Premise settings add to, or replace, those for costs
  that aren't any device's (an audit, a controls kit).

## 5. The resolved premise

Every input becomes the same internal structure, and nothing downstream reads YAML
fields or table columns directly:

```
ResolvedPremise
  premise_id, kind (premise or background), element_id, customer_class, tags
  load: gross (load_kw series), metered (import/export series), a data source, or none
  limits: import_limit_kw, export_limit_kw, exclusive_import_export
  billing: tariff, program, territory, income tier  |  objective: bill or value streams
  coordination: participation, baseline_behavior, response
  measure: devices, and participant costs (for DR and cost tests)
  devices: [ResolvedDevice(device_id, type, template, params, series, costs, counterfactual, rules)]
  sources: {setting -> where its value came from}
```

The engine works on lists of these:
- building the optimization;
- the counterfactual simulator;
- billing;
- the DR study;
- screening values and the impact package;
- the feeder study and coordination.

Today, DR and cost tests take a single-home scenario and the feeder takes houses; both
move onto resolved premises, which is what makes the portfolio analysis possible.

## 6. Compatibility and migration

Nothing that runs today stops running. The old forms compile into the new model:

| Today | Becomes |
|---|---|
| a single-home scenario (top-level `devices`, `external_load`…) | a population of one premise, written out, and a study with a portfolio analysis |
| `houses:` | premises written out in YAML (`premises:` in the study, any number of devices each) |
| `premise_data` with v1.1 `premises.csv` device columns | read as device rows (`pv_kw_ac` → solar, `battery_kwh`/`battery_kw` → battery, `ev_*` → ev), with a warning. `der-opt convert premises` writes the `devices.csv` |
| `house_defaults` | a study `defaults` entry with no `where` |
| `feeder.background_load` | `premises.csv` rows with `kind: background`, or written-out background premises |
| `dr.measure_devices`, `cost_test.measure_devices` (device names) | `measure`, by `device_id` |
| `cost_test.participant_costs` | premise settings `participant_upfront`, `participant_annual_om` (as a study default) |
| `house_defaults.device_params.<type>` | a `defaults` entry with `where: {type: …}` |
| counterfactual policies by device name | the same, as a `defaults` entry; templates carry their own |
| `houses.csv` output | `premises.csv` and `device_parameters.csv` |

- **Old names** (`houses`, `house_defaults`, `premise_data`) are accepted for one release,
  each with a warning saying what to write instead (decision 6).
- **The examples** move to the new form in the same change, and the tests check that
  each old form and its new equivalent resolve to the same premises.
- **"House" is retired** for "premise" throughout, in files, output and messages.

## 7. Not in this version

- **Scale in solving:** a full year for hundreds of coordinated premises. That's the
  decomposition work. This spec makes the population scale, so a 2,000-row table is no
  harder to write or check than a 6-row one, but the solve is unchanged.
- **Aggregated cost tests** across a portfolio, as one OpenBCA measure.
- **Premise-to-premise relations** (a shared battery, an aggregator's fleet as one
  resource).

## 8. Decisions

Approved 2026-11-21: the recommended choice on each.

1. **Device size columns:**
   - **`kw` and `kwh` with a meaning per type, plus any parameter by name
     (recommended);**
   - only the parameters' own names (`capacity_kwh`, `p_max_charge_kw`…). That's exact,
     but wider and harder to fill in.
2. **Precedence:**
   - **built-in, template, study defaults, table row, overrides (recommended);**
   - study defaults above table rows, so a study can change any row without listing
     overrides. But then the data no longer wins over assumptions.
3. **Templates:**
   - **one device per file, with parameters, counterfactual and rules, extending each
     other; existing one-device library blocks accepted (recommended);**
   - a single templates file per library.
4. **Studies and populations:**
   - **separate files, with single-home YAML and written-out premises kept as
     conveniences that compile into the same model (recommended);**
   - only the new form, with old scenarios converted once.
5. **Portfolio analysis:**
   - **in this spec: per-premise DR, screening and cost tests, with a summed summary
     (recommended);**
   - a later spec, keeping DR and cost tests single-premise for now.
6. **Migration:**
   - **old names and v1.1 premises files read for one release, with warnings, and
     `der-opt convert premises` (recommended);**
   - convert everything at once.
7. **New time series columns:**
   - **`device_id`, `outdoor_temp_c` (with shared weather-station files) and
     `flexible_kw` (recommended);**
   - leave thermostats and flexible loads to written-out premises for now.
8. **Background load:**
   - **rows in `premises.csv` with `kind: background`, using the same time series and data
     sources (recommended);**
   - a separate `background.csv`;
   - keep it in the study YAML only.
9. **Series from data sources:**
   - **a `load` or series setting, resolved by the same precedence, so file rows (level 4)
     beat study defaults and overrides beat both (recommended);**
   - an error when a premise has both file rows and a data source.
10. **Cost-test inputs per premise:**
    - **device `installed_cost` and `annual_om` summed over the measure, with premise
      settings for costs that aren't any device's (recommended);**
    - premise settings only.
11. **What's needed:**
    - **per analysis, as in section 4a, checked by `validate` for the analyses the
      study runs (recommended);**
    - a fixed set of required columns.
12. **Adoption what-ifs:** `add_devices` in the study, with exact counts and a random
    stream per entry (section 3a). Approved.
