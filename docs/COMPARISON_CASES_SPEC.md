# der-opt comparison cases specification

**v1.2, 2026-11-22.** v1.2 replaces `no_measure` with `without`, the cost tests' comparison home
built device by device (cost test spec v1.3, section 1a). v1.0 was approved 2026-11-22 with the recommended answer to each
decision in section 8; v1.1 makes one A/B choice drive the whole report page, with
every difference shown as A − B (section 5). Built as written, except where a note
below says otherwise.

## Purpose

A single-home run answers one question today: what optimization is worth against the
rule-based counterfactual. An analyst also wants to know what the solar is worth, what
the battery is worth even under simple control, and what DR participation adds. Each
of those is a comparison between two versions of the same home over the same window.

This spec adds a fixed set of **cases** to every single-home run, each priced the same
way, and lets the report compare **any two** of them. The cost tests (TRC, RIM and the
others) stay as they are: they measure one thing, the measure the `cost_test` section
defines.

## 1. The cases

Each case is the same home, load and weather over the same window. Cases differ only
in which devices are present and how they're run.

| Case | Devices | How they run | Today |
|---|---|---|---|
| `no_devices` | none: the home's load alone | — | new |
| `without` | as the cost tests' `without` says, device by device (v1.2; was `no_measure`) | removed, rule-based, on a TOU timer, events-only or backup-only | new |
| `rule_based` | all | the `counterfactual` policies | R1, "counterfactual" |
| `optimized` | all | optimized, no DR events | R2, "optimal" |
| `responding` | all | optimized, responding to DR events | DR's responding run |

- **`without` (v1.2)** is the cost tests' comparison home, each device as
  `cost_test.without` says (cost test spec v1.3, section 1a). With no cost test, every
  device takes its type's default and a battery is removed. In `scenarios/new_home.yaml`
  the library's cost-test block compares the battery against self-consumption, so
  `without` is the home with its solar and a self-consuming battery.
- **A case appears only when the run can make it:**
  - `rule_based` needs a `counterfactual` section;
  - `responding` needs a `dr` section;
  - `without` needs at least one device.
- **Each case gets a label from its devices,** so the page reads "No devices",
  "Battery on self-consumption" (the comparison home), "Battery, rule-based",
  "Optimized" and "Optimized + DR". The names in the
  table are the ones used in files and settings.
- **Removing a device removes everything it does.** Removing an EV also removes its
  charging load, so the case without it is a home without the car. The page says so
  under that case's label.
- **Charging sources must stay.** A device whose `charge_sources` names a removed
  device stops the case with an error, as R3 does today. For example, a battery that
  charges only from solar can't stay when the solar goes.

## 2. How each case is priced

- **The same value streams and bill as the run.** Each case gets its total cost by
  value stream and its bill under the run's own tariff and program.
- **Degradation counts; smoothing doesn't.** Degradation is part of each case's
  cost, and the smoothing cost stays out of every reported number, as now.
- **The DR incentive belongs to `responding` alone.** It's earned at settlement and
  appears there as its own line.
- **A home without solar on an NBT bill.** It exports nothing, so the NBT program
  has nothing to credit, and its bill is the tariff's import charges. Whether that home
  would really be on E-ELEC is decision 1.
- **Stored energy at the end isn't valued.** This is the same caveat as today, and
  the `end state` check warns when any two cases end with different stored energy, not
  only optimal and counterfactual.

## 3. Running them

| Case | Where it comes from | Extra solve |
|---|---|---|
| `optimized` | the main solve | no |
| `rule_based` | the counterfactual simulation | no |
| `responding` | the DR study | no |
| `without` | a simulation of its rule-based and timer devices, plus one solve when a device is events-only or backup-only | only then |
| `no_devices` | the meter reading the load | no |

- **`no_devices` isn't solved.** With no devices there's nothing to decide: the meter
  imports the load. (As built; the draft had a solve here. A solve could only add
  simultaneous import and export where the meter allows it, which no real meter does.)
- **`without` is the cost tests' "without"** (v1.2): the impact package and screening
  values use it, so it's built once. (v1.0's `no_measure` reused DR's R3; R3 stays as
  it is for DR capacity.)
- **Cases are on by default** in every single-home run (decision 2). A scenario can
  choose which to run:

```yaml
cases:
  include: [no_devices, without, rule_based, optimized, responding]  # default: every case the run can make
  compare: {without: rule_based, with: optimized}   # the page's opening pair; default in section 5
  no_devices: {billing: ../library/billing/pge_e_tou_c_2026.yaml}   # optional, per case (decision 1); this block doesn't exist yet
```

- **A per-case `billing` override** prices that case on another tariff, for
  example to show a home without solar on the rate it would have chosen. A case on a
  different tariff carries the tariff's name in its label, so it's never mistaken for
  a same-tariff comparison.

## 4. What's written

**`summary.json` gains `cases` and `case_compare`.** `summary_version` goes to `1.1`.
It only adds fields, so a 1.0 reader still works. `case_compare` holds the opening
pair and why, the cost tests' pair, and the quick buttons. For each case:
- `label`, `description`, `devices`, `policies` (for `rule_based`), `tariff` and
  `program`;
- `costs`: each value stream and the total, as `costs.optimal` is now;
- `bill`: the bill summary, as in `bills`;
- `energy`: import kWh, export kWh, peak import kW and when, and self-consumed solar
  kWh where there's solar;
- `dr_incentive` (`responding` only);
- `by_month`: bill (the amount due of the cycles starting that month), import, export
  and peak.

The existing `costs`, `bills` and `value_of_optimization` fields stay, so nothing that
reads them breaks.

**New files:**

| File | Contents |
|---|---|
| `cases.csv` | one row per case and billing cycle: bill (amount due), import kWh, export kWh, peak kW; and one whole-run row per case with its bill, DR incentive and cost by value stream (as built: a bill stream nets over the run, so its cost doesn't split by cycle) |
| `cases_net_load_kw.csv` | one column per case: net load at the meter (import − export), every step |
| `cases_device_dispatch.csv` | each case's device power (`case.device.net_power`, kW) and stored energy (`case.device.soc`, kWh), every step (v1.1: so the dispatch charts can follow A and B) |

A year of hourly net load for five cases is about 0.4 MB. The existing
`counterfactual_*` and DR files stay as they are.

**The printed output** gains a cases table after the bills: one row per case with its
bill, total cost, import, export and peak.

## 5. The report

**One A/B choice drives the page** (v1.1, 2026-11-22). Two selectors in v1.0, one
in the summary and one in a Compare section, read as two separate choices, and the
value breakdown and dispatch still showed optimal against counterfactual. Now:

- **A bar pinned at the top of the page**, above every section, stays in view while
  scrolling: **A** `[case ▾]` **B** `[case ▾]`, the quick buttons, and one line saying
  what a sign means.
- **Every difference is A − B, stated once,** in the bar: positive means B is lower
  than A. Cards show the signed difference with A's and B's values beneath; chart
  series and the bill table's column are called "difference". A and B have their own
  colors (gray and blue) everywhere, with A dotted on time charts.
- **The quick buttons** set both A and B:

| Button | A | B |
|---|---|---|
| The measure | `without` | the cost tests' "with" (`responding`, or `optimized`) |
| Optimization | `rule_based` | `optimized` |
| DR participation | `optimized` | `responding` |
| Everything | `no_devices` | `responding` |

  A button only appears when its cases exist. (v1.2 replaced v1.0's "Solar" and "The
  battery, rule-based" buttons with "The measure".)
- **The page opens on the cost tests' own pair** (decision 4): `rule_based` →
  `responding` for `scope: control`, `no_measure` → `responding` for `scope: device`;
  without cost tests, `rule_based` → `optimized`. `cases.compare` overrides it.

**What follows A and B:**
- **Summary cards:** bill, total cost (with degradation), import, export, and mean net
  load during the DR events (peak import when there's no DR), each the difference with
  both values beneath; and the DR incentive a case earned, as
  its own card (not a difference, and not in the costs).
- **Value breakdown:** each value stream for A and B, and the difference.
- **Dispatch:** the meter (the home's load, A, B; A − B in the legend) and each
  device's power and stored energy (B solid, A dotted) on the linked time axis; the
  average day in each month. (v1.1 dropped the day-by-hour heatmap: it added little.)
- **Energy by month:** import and export, A and B.
- **Bills:** amount due by billing cycle for A and B, and the difference; and the bill
  summary as a table with A, B and difference columns.

**The page's order:** summary, dispatch, energy by month, bills, demand response, cost
tests (screening values folded beneath), value breakdown, checks, settings.

**What doesn't, and says so:** TRC and RIM, the DR section, screening values and the
cost tests describe fixed comparisons (the cost tests' pair, or the DR study's R1, R2,
R3 and S). Each carries a line beginning "Not set by A and B", naming what it compares.

A folder written before cases has no cases, and its page shows optimal against
counterfactual, as before. Everything is computed in the page from the embedded cases
(`cases_net_load_kw.csv`, `cases_device_dispatch.csv` and `summary.json`), so changing
the pair needs no server and no re-run.

## 6. Studies

- **A study's portfolio** already runs each premise as a single-home scenario, so
  every premise page gets its own cases and A/B bar.
- **On the study page** (decision 3):
  - the portfolio table gains each case's bill;
  - one A/B choice for the whole study;
  - a bar per premise of its bill, A − B (total cost where there's no bill), largest
    first, with the total and the number of premises lower in B.
- **Feeder runs aren't cases.** Coordinated, independent and counterfactual on a
  network compare control strategies on shared limits, a different question, and they
  stay as they are.

## 7. What it doesn't do

- **No cost tests for other pairs.** A TRC or RIM for "the solar" would need its own
  OpenBCA run and its own costs. The cost tests stay on the one measure the
  `cost_test` section defines.
- **No tariff comparisons by default.** The same home under NEM2 and NBT is a
  per-case `billing` override at most, not a new case.
- **No comparison across scenarios.** Comparing two output folders, such as two
  smoothing costs, stays out, as the reports note already says (`der-opt compare`
  isn't planned).

## 8. Decisions (approved 2026-11-22)

1. **The tariff for the cases without solar.** Recommended: **the run's own
   tariff** by default, so the comparison shows only what the devices change, plus the
   per-case `billing` override for the rate that home would really choose. A home
   without solar exports nothing, so the NBT program makes no difference to it. The
   alternative is a standard non-solar rate (E-TOU-C) by default, which needs that
   tariff added to the library and mixes a tariff change into "what the solar is
   worth".
2. **On by default, or opt-in.** Recommended: **on**. At most one or two small extra
   solves, and the Compare section is always there. The alternative is opt-in with
   `cases:`.
3. **Studies.** Recommended: each case's bill in the portfolio table and a
   study-wide pair selector, as in section 6. The alternative is premise pages only,
   with no study-level comparison.
4. **The opening pair.** Recommended: **the cost tests' own pair**, so the cards under
   TRC and RIM agree with the ratios. The alternative is always opening on
   `rule_based` → `optimized`.
