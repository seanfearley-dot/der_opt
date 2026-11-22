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
  category and part), and `cost_tests` (each test's OpenBCA and SPM-convention
  benefits, costs, ratio and net benefits) once they've run.
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

## 2. `report.html` (next)

One page per output folder that opens without a server, rebuilt by
`der-opt report OUT` without solving again.

## 3. `der-opt ui` (after)

A local web app built around studies: a population browser, a study editor with live
validation, a run queue, and the reports.
