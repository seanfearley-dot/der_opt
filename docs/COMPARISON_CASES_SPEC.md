# der-opt comparison cases specification

**v1.0, approved 2026-11-22**, with the recommended answer to each decision in
section 8. Built as written, except where a note below says otherwise.

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
| `no_measure` | every device except the measure's | optimized | DR's R3 |
| `rule_based` | all | the `counterfactual` policies | R1, "counterfactual" |
| `optimized` | all | optimized, no DR events | R2, "optimal" |
| `responding` | all | optimized, responding to DR events | DR's responding run |

- **The measure's devices** come from `cost_test.measure_devices`, then
  `dr.measure_devices`, then all devices. These are the same rules R3 and the cost
  tests use now. In `scenarios/new_home.yaml` the measure is the battery, so
  `no_measure` is the home with solar only.
- **A case appears only when the run can make it:**
  - `rule_based` needs a `counterfactual` section;
  - `responding` needs a `dr` section;
  - `no_measure` is left out when the measure is every device, because it would equal
    `no_devices`.
- **Each case gets a label from its devices,** so the page reads "No devices", "Solar
  only", "Battery, rule-based", "Optimized" and "Optimized + DR". The names in the
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
| `no_measure` | DR's R3, when the DR study ran with the same measure devices; otherwise a new solve | only then |
| `no_devices` | the meter reading the load | no |

- **`no_devices` isn't solved.** With no devices there's nothing to decide: the meter
  imports the load. (As built; the draft had a solve here. A solve could only add
  simultaneous import and export where the meter allows it, which no real meter does.)
- **`no_measure` reuses DR's R3** when the DR study removed the same devices, and the
  cost tests reuse `no_measure` for `scope: device`, so the measure is solved without
  once at most. With a solar-only remainder there's little to optimize: solar can at
  most be curtailed.
- **Cases are on by default** in every single-home run (decision 2). A scenario can
  choose which to run:

```yaml
cases:
  include: [no_devices, no_measure, rule_based, optimized, responding]  # default: every case the run can make
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

A year of hourly net load for five cases is about 0.4 MB. The existing
`counterfactual_*` and DR files stay as they are.

**The printed output** gains a cases table after the bills: one row per case with its
bill, total cost, import, export and peak.

## 5. The report

**Top panel:**
- **TRC and RIM lead the panel, as now.** They belong to the cost tests' measure,
  so they stay fixed.
- **Below them, a comparison selector:** "Compare `[without ▾]` with `[with ▾]`".
  Its cards show what moved from the first case to the second:
  - bill savings;
  - total cost savings, including degradation;
  - import kWh;
  - export kWh;
  - peak import kW;
  - the DR incentive, when `responding` is one of the two.
- **Quick buttons for the usual questions,** each setting both dropdowns. A button
  only appears when its cases exist.

| Button | Without | With |
|---|---|---|
| Solar | `no_devices` | `no_measure` |
| The battery, rule-based | `no_measure` | `rule_based` |
| Optimization | `rule_based` | `optimized` |
| DR participation | `optimized` | `responding` |
| Everything | `no_devices` | `responding` |

  The button labels come from the case labels. With an EV as the measure, the second
  button reads "The EV, rule-based".
- **The page opens on the cost tests' own pair** (decision 4): `rule_based` →
  `responding` for `scope: control`, and `no_measure` → `responding` for
  `scope: device`. The cards under TRC and RIM then describe the same comparison the
  ratios do, and the page says so. Without cost tests it opens on `rule_based` →
  `optimized`, which is today's "value of optimization". `cases.compare` overrides
  either.

**A Compare section** follows the value breakdown and uses the same selector:
- the bill by month, both cases side by side, with the difference;
- each value stream for both cases, as the value breakdown shows optimal and
  counterfactual now;
- net load at the meter for both cases, on the page's linked time axis, so zooming to
  a day shows that day here too;
- the average day by month: net load of each case, and the difference.

Everything is computed in the page from the embedded cases, so changing the pair
needs no server and no re-run. The value breakdown and dispatch sections keep showing
optimal against counterfactual, as now.

## 6. Studies

- **A study's portfolio** already runs each premise as a single-home scenario, so
  every premise page gets its own cases and Compare section.
- **On the study page** (decision 3):
  - the portfolio table gains each case's bill;
  - the selector picks a pair for the whole study;
  - the portfolio's bar per premise shows that pair's bill savings.
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
