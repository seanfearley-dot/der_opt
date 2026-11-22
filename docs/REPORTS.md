# der-opt reports and the local app

**Status:** a working design note, not an approved spec. The user chose to start
building on the recommended options (2026-11-21):
- the reader is the analyst running der-opt (not outside readers, for now);
- interactive charts (Plotly), with one shared copy of its script per output tree;
- OpenBCA's working files deleted by default (`--keep-openbca` keeps them);
- order: summaries, then report pages, then the local app. `der-opt compare` isn't
  planned.

## 1. `summary.json` (built)

Every output folder gets a `summary.json`: the numbers the run prints, as data.
Reports and the app read it, never the printed text. The CSVs keep their names.

Common fields:

| Field | What it holds |
|---|---|
| `summary_version` | `1.0`; changes when a shape changes in a way readers must know |
| `kind` | `scenario` (a single home), `feeder` or `study` |
| `name`, `created` | the scenario's name; when it was written (UTC) |
| `time_grid` | `start`, `end`, `resolution_minutes`, `steps` |
| `checks` | error and warning counts, and every warning and error finding |
| `files` | every other file in the folder |

By kind:
- **`scenario`:** `status`, `devices`, `ev_patterns`, `costs` (optimal and
  counterfactual by value stream, with totals, and `value_of_optimization`),
  `counterfactual_policies`, `bills` (each run's bill summary), `dr` (the DR summary and
  capacity by month), `screening` (the avoided cost set, the reference, and values by
  category and part), and `cost_tests` (each test's benefits, costs, ratio and net
  benefits, as OpenBCA reports them) once they've run.
- **`feeder`:** `premises`, `elements`, `mix` (premises and devices by element),
  `background`, `runs` (total, customer bills, degradation and network costs per run),
  `loading` (per run and element: peak, its time, % of rating, hours and kWh over),
  `congestion` (per element: the highest price, binding hours, $/kW over the run),
  `coordinated_vs_independent` (who pays more, and the network saving) and
  `coordination` (each mechanism run's row of coordination.csv, its mechanisms and
  events).
- **`study`:** `premises`, `population` (premises with each device type, by element, by
  origin), `add_devices` (what each entry chose), `analyses`, `feeder` (its folder) and
  `portfolio` (each premise's folder, measure and note, whether it failed, the table's
  path and its total row).

## 1a. OpenBCA's working files (built)

A cost-test run writes OpenBCA a copy of its inputs for each of the five tests. The
avoided-cost series alone is about 27 MB, plus OpenBCA's database (8 MB), so about
180 MB a premise. After the tests run and are tallied, each test's folder keeps
`test.json` and OpenBCA's result CSVs and log; the rest is deleted unless `run` gets
`--keep-openbca`. `export_openbca` on the run's `measure_impacts/` writes the inputs
again.

## 2. `report.html` (built)

One page per output folder, written at the end of every `run -o` and rebuilt by
`der-opt report OUT` (`--open` opens it) without solving again. It's built from the
folder's `summary.json` and CSVs.
- **Charts** are Plotly, interactive: zoom, hover, legend entries to hide. plotly.js
  (4.8 MB) sits once in the output tree's `_assets/`; a study puts it at its root, so
  its feeder and portfolio pages share it. A page opens without a server, as long as
  `_assets/` stays beside it (or above it, for a study's runs).
- **Long runs** open on the week with the highest import (or loading), with
  1-day, 1-week, 1-month and whole-run buttons and a range slider. A page's time
  charts zoom together: narrowing one to a day narrows every other to the same day.
  Hovering shows every series' value at that time, with its unit. A time axis is sent
  once per page, not per trace, so a year of hourly dispatch is about 0.4 MB.
- **Tables** sort by any column, and long ones have a filter box. Very long ones are
  folded under a summary line.
- **Light and dark** follow the system setting.

What each page shows:
- **Single home:** headline cards (optimal and counterfactual cost, value of
  optimization, bills, DR incentive, TRC and RIM ratios, leading the panel); the value breakdown; the meter
  and each device's power and stored energy (counterfactual dotted); where optimization
  moved load, by hour and day; energy by month; DR capacity by month and the events;
  screening values; cost tests (OpenBCA's results); bills; checks; every setting
  with its source.
- **Feeder:** cards (costs by run, network saving, who pays more); premises by element;
  costs by run; peak flow by element and run, and each element's flow over time with
  its rating; congestion prices; each premise's bill change under coordination and its
  bills by run; the coordination runs against the ideal; checks; settings.
- **Study:** cards (premises, device types, analyses); the feeder analysis's headline
  figures, linked to its page; the portfolio (a bar per premise for a chosen measure,
  and the table, each premise linked to its page); the population by element, the
  premises table, and what `add_devices` chose; checks; settings. Each run's page links
  back to the study's.

## 3. `der-opt ui` (built)

```bash
der-opt ui                 # the current folder; opens http://localhost:8700/
der-opt ui path/to/project --port 8710 --no-open
```

A local web app on the project's own files. There's no database: what it shows is
read from the files each time, and what it saves is the YAML you'd edit by hand.
- **Scenarios and runs:** every scenario and study file in the project (any YAML with a
  name and a scenario's or study's sections, including one built wholly from blocks),
  and every run folder under `out/`, each opening its report.
- **A file:**
  - **Edit:** the YAML as written, saved with ⌘S / Ctrl+S, then checked;
  - **Check:** `der-opt validate`'s findings, what it extends, and what `add_devices`
    chose;
  - **Population:** the premises table and every setting with its source, plus a
    selector preview: type a `where` (as in `defaults` or `add_devices`) and see which
    premises or devices it picks;
  - **Merged:** the file with its blocks merged in and paths resolved;
  - **Run:** queue it, with the output folder and `run`'s options.
- **Study settings** (a study's tab): its own `defaults`, `overrides` and
  `add_devices`, each entry as rows of a setting and its value, with setting names
  suggested (premise settings, device parameters, premises and devices to override,
  templates). A value is YAML (`0.9`, `[solar]`, `{type: self_consumption}`). Each
  `where` shows what it picks as you type. Entries can be added, removed and reordered.
  Saving checks every entry first (a value that isn't YAML, an add_devices entry
  without its device_id) and writes nothing if one is wrong; then it rewrites only
  those three sections, as the examples write them, and checks the study. Comments
  above the sections stay; comments inside them don't. Defaults inherited from the
  blocks the study extends are listed, read-only.
- **New:** a file from a template (`der-opt new`'s templates, with only that template's
  fields, and the project's CSVs, tariffs and avoided cost sets suggested), or a
  single-home scenario from library blocks, picked by kind, with a live check that the
  picks fit together. It never overwrites a file, and opens the new one on its Check
  tab.
- **Run queue:** runs go one at a time, as `der-opt run` in a subprocess. Each one's
  log streams in, and when it's done its report opens from the same server. The queue
  lasts while the app runs; the output folders stay.

Safety: it listens on 127.0.0.1 only and answers only requests naming localhost. Every
change (save, run, cancel, new file) carries a token from the page, so another site
open in the browser can't drive it. Every path must be inside the project folder. It's
for one person on their own machine; sharing it on a network would need sign-in, which
isn't built.
