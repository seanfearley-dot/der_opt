# der-opt scenario building blocks specification

**Version 1.0**, approved 2026-11-20 with the recommended choice on each decision (section 5).
Implemented in `der_opt.scenarios.compose`, `.validate` and `.templates`.

## Purpose

A full-year measure analysis needs a long scenario file: time grid, devices, value
streams, billing, a DR program and cost-test settings. Most of that repeats across
measures. This spec makes scenarios shorter, quicker to start and safer to run:

1. **Building blocks** (`extends`): shared pieces in small files, reused by many
   scenarios.
2. **Paths relative to the file that names them**, so a scenario runs from any folder.
3. **`der-opt new`**: a commented starting scenario for common analyses.
4. **`der-opt validate`**: every check that doesn't need a solve, in seconds.

## 1. Building blocks: `extends`

A scenario can start from one or more fragments:

```yaml
# scenarios/home_a_battery.yaml
extends:
  - ../library/time/year_2026.yaml
  - ../library/billing/e_elec_nbt_2026.yaml
  - ../library/programs/summer_peak_incentive.yaml
  - ../library/cost_tests/acc_2026_rates.yaml
  - ../library/devices/battery_13kwh.yaml

name: home_a_battery
external_load: {source: timeseries, path: ../data/timeseries.csv, premise_id: home_a, column: load_kw}
devices:
  - {type: solar, name: solar, params: {available_kw: {source: timeseries, path: ../data/timeseries.csv, premise_id: home_a, column: pv_kw}}}
```

- **A fragment** is any part of a scenario: one section or several. Fragments can
  themselves `extends` others. A cycle is an error.
- **Order:** fragments merge in the order listed, then the scenario's own content goes
  on top. Later always wins.
- **Merging:**
  - mappings merge key by key, at every depth;
  - `devices` merge **by `name`**: a later device with the same name replaces the
    earlier one, and new names are added (decision 2);
  - every other list replaces the earlier list whole;
  - a value of `null` removes an inherited key, for example `counterfactual: null`;
    with nothing inherited, it's passed to the schema as before.
- **Device order:** devices build after the devices they charge from
  (`charge_sources`), so a battery fragment can come before the scenario's own solar.
  They keep their listed order otherwise. A name charged from that isn't a device, or
  devices charging from each other, is an error.
- **Validation:** only the merged result is checked against the scenario schema.
  Fragments don't have to be complete on their own.
- **What's recorded:**
  - `scenario.resolved.yaml`: the fully merged scenario, written to the output folder;
  - `run_record.json`: a SHA-256 of every fragment, so a result can be traced to the
    exact building blocks used.

Suggested library layout (only a convention; nothing enforces it):

```
library/
  time/          year_2026.yaml
  billing/       e_elec_nbt_2026.yaml, e_tou_c_nem2_2026.yaml
  programs/      summer_peak_incentive.yaml, winter_test_events.yaml
  cost_tests/    acc_2026_rates.yaml          (scope, life, discount rates, avoided cost set)
  devices/       battery_13kwh.yaml, ev_level2.yaml
```

## 2. Paths

**Every relative path resolves from the folder of the file that contains it,** whether
that's a scenario or a fragment. Absolute paths are used as given. So
`library/billing/e_elec_nbt_2026.yaml` can name `../../tariffs/pge_e_elec_2026-06.yaml`,
and any scenario that extends it gets the right file, run from any folder.

Path-valued keys are fixed and listed here, so nothing is guessed:
- `path`, `csv_path`, `export_rates_csv`, `tariff`, `tariff_dir`, `urdb_json_path`;
- `scenario_dir`, `arrivals_csv`, `departures_csv`;
- `set` (an avoided cost set);
- `file` (an events file);
- `premises`, `network`, and each entry of `timeseries`;
- `extends` entries.

**Transition (decision 3):** today's scenarios name paths relative to the folder the
command runs from. For one release, if a relative path doesn't exist next to its file
but does exist from the current folder, der-opt uses it and warns, naming the file and
key to fix, and the path to write instead. After that release it's an error. The
shipped examples are rewritten to the new rule.

A path that doesn't exist either way is resolved from the file's folder, so the
missing-file error names where it looked. Values with `://` (URLs) and `TODO`
placeholders are left as written.

`premise_data.tariff_dir` used to default to `examples/data/tariffs` from the current
folder. It now defaults to the tariffs that ship in der-opt's `examples/data/tariffs`,
wherever the command runs.

## 3. `der-opt new`

```bash
der-opt new dr-battery scenarios/home_a.yaml --timeseries data/timeseries.csv --premise home_a
der-opt new --list
```

Writes a commented starting scenario from a template, filled in from the options given.
Anything it can't fill becomes a clearly marked `TODO`, which `validate` reports as an
error until it's replaced. Templates:

| Template | Writes |
|---|---|
| `dr-battery` | one home, full year, battery with a DR program and a `cost_test` section |
| `home-bill` | one home, full year, bill-optimized solar and battery (NBT or NEM2) |
| `feeder-from-files` | houses and network from `premises.csv`, `network.csv` and time series |
| `lmp-battery` | a standalone battery on a wholesale price |

With `--library DIR`, the shared parts go into fragment files under `DIR` (time grid,
battery, prices or billing, DR program, cost-test settings, feeder house defaults), and
the scenario extends them. A fragment that already exists is used as it is, never
overwritten, so the next scenario made with the same library shares it. Without
`--library`, the scenario is written as one file. The scenario file itself is only
overwritten with `--force`.

What `new` fills in:
- **Paths**, relative to the file they're written in when both are in one project
  folder; absolute when they only share the root or home folder.
- **The premise**, when the time series file has only one.
- **The run period**: the time series file's span (or, for `lmp-battery`, the price
  file's), named as a year when it is one; `--year` for a whole calendar year instead.
- **The feeder's capacity-deferral element**: the network file's root.
- **A baseline territory line**, as `TODO`, when the tariff has a baseline credit or
  the program is NEM2.

`TODO` is written for choices only you can make: the DR incentive, participant
costs, discount rates, the deferral value, a missing file.

## 4. `der-opt validate`

```bash
der-opt validate scenarios/home_a.yaml
```

Runs every check that doesn't need a solve, in seconds, and prints them in the same
format as a run's checks. It exits 1 on any error. A check that fails stops the ones
that depend on it (a schema error stops the file checks; a missing file stops the
build). It checks:

- **The scenario:**
  - `extends` resolves, with no cycles, and the merged result fits the schema;
  - no `TODO` placeholders are left.
- **Files and data:**
  - every named file exists;
  - input-spec files pass `check-data`, and their time series cover the run period;
  - avoided cost sets pass, and cover the measure's life;
  - tariffs load, the baseline territory is set where the tariff needs it, and tariff
    dates fit the run.
- **Devices and DR:**
  - `charge_sources` and `measure_devices` name real devices;
  - each DR program's calendar builds and passes the program's rules.
- **Cost tests:**
  - `cost_test` has discount rates;
  - OpenBCA is set up (a warning, not an error, since it's only needed with
    `--cost-tests`).

**`der-opt run` validates first (decision 5),** so a typo stops a run in seconds, before
a one-minute annual solve, and reuses the premise validation built. `--no-validate`
skips it. Before a run, two things `validate` reports are left out: `check-data`'s
data-quality warnings (flat runs, outliers; its errors still stop the run) and, unless
the run has `--cost-tests`, whether OpenBCA is set up. With `--cost-tests`, a missing
OpenBCA or missing discount rates are errors.

## 5. Decisions

Approved 2026-11-20: the recommended choice on each.

1. **Relative paths resolve from:**
   - **the file that contains them (recommended):** a scenario or fragment works from
     any folder, and libraries can be shared;
   - the folder the command runs from (today's behavior);
   - a project root marked by a file.
2. **Merging lists:**
   - **devices by name, every other list replaced (recommended):** a scenario can add a
     battery fragment to its own solar device;
   - every list replaced whole: simpler, but devices can't be combined from fragments;
   - lists appended: devices and value streams could be duplicated by accident.
3. **Old-style paths:**
   - **a one-release fallback with a warning naming the file and key (recommended);**
   - errors right away: cleaner, but every existing scenario breaks at once.
4. **Templates:** the four in section 3 (recommended), or a different set.
5. **`run` validates first (recommended),** with `--no-validate` to skip it; or
   `validate` only when you call it.
