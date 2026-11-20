# der-opt measure impact export specification (for cost tests)

**Version 1.0 — approved 2026-11-20.** Not yet implemented. Change it only by a new
version, approved the same way. (Drafts 0.1-0.3 led here; section 3 holds the OpenBCA
fit check.)

## Division of labor

| | der-opt | OpenBCA |
|---|---|---|
| Measure impacts: hourly energy, DR capacity, bills, DR incentives | **computes** | receives |
| Measure and program costs, life, net-to-gross, tax credits | passes through from the scenario | receives |
| Avoided costs | passes the chosen avoided-cost set through | receives, applies |
| Allocation of each stream to each test, cost treatment (incl. tax credits) | — | **does** |
| Lifetime projection, discounting, test results (PCT, RIM, TRC, PAC, SCT, JSTs) | — | **does** |

der-opt doesn't compute cost tests. That keeps a single source of truth for test
results: OpenBCA's allocation and discounting, configured in OpenBCA.

The fit check (section 3) found that OpenBCA can carry everything der-opt supplies. Bills
go in as a custom stream. The one gap is how it classifies negative streams (decision 2).

## 1. The measure

One premise, one full analysis year. `scope` (a per-run setting) picks the "without"
case for every impact:

| `scope` | "With" | "Without" |
|---|---|---|
| `control` | the responding run | **R1**: same devices, today's rule-based control |
| `device` | the responding run | **R3**: no measure devices |

## 2. The impact package

Every run with a `cost_test` section writes a `measure_impacts/` folder. Its format is
der-opt's own and independent of OpenBCA, so it can be reused and checked, and it's
what the OpenBCA export is built from:

| File | Contents |
|---|---|
| `measure.yaml` | measure id, scope, `start_year`, `measure_life_years`, `net_to_gross`, `impact_degradation`, `bill_escalation`, `cost_escalation`; participant costs (upfront, annual O&M, replacements), program costs (admin upfront and annual, upfront incentive), tax credits; the avoided-cost set's `set_id`; the source run's record fingerprint |
| `energy_impact.csv` | every hour of the analysis year (ISO 8601 with offset): `load_reduction_kwh` ("without" minus "with" net load), split into `decrease_kwh` (≥ 0, the measure lowers load) and `increase_kwh` (≥ 0, the measure raises load: rebound, charging). The split keeps increases as **increased supply costs**, not netted (approved). |
| `capacity.csv` | month: the DR capacity kW used for valuation (default **R2**, import basis, counted), plus every other reference, basis and measure for reference |
| `bills.csv` | the analysis year's bill "without" and "with" by billing cycle, their difference, and `bill_escalation` (later years escalate it; approved) |
| `dr_incentives.csv` | the analysis year's DR incentive earned at settlement, by event and by month |

The capacity value's reference defaults to R2 (approved). Its basis and measure default
to import and counted, as drafted in v0.1. Each can be set:
`capacity_value: {reference: R2, basis: import, measure: counted}`.

## 3. The OpenBCA export

### What the fit check found (C1, OpenBCA commit 7e13b36, 2026-11-19)

The demo runs from the command line in about 20 seconds.

| Question | Finding |
|---|---|
| 1. Energy impact as an 8760 shape | **Yes.** An `ELECTRIC` load shape, hourly, in kWh (savings can be negative). OpenBCA has **no daylight saving and no leap days**: shapes and avoided costs must be in standard time, with every year aligned to the shape's calendar. |
| 2. DR capacity | One `coincident_peak_savings_kw` per measure, x each capacity avoided cost's $/kW-year by year, with a peak line-loss factor. **No monthly allocation.** |
| 3. Bills and incentives for PCT and RIM | **Incentives: yes.** `utility_incentive_annual_dollar_per_year` gets a factor per test: a benefit in PCT, a cost in RIM and USI, a transfer in TRC. **Bills: no.** OpenBCA has no bill concept. Its four "custom" annual value streams could carry them. |
| 4. Cost treatment | **One per run**, set in the configuration. Its factor table covers `USI + Non-USI` (TRC-like), `USI` (UCT/PAC), `CA TRC`, **`PCT` and `RIM`**, the last two undocumented. Tax incentives: −1 (a cost reduction) in TRC-like tests and PCT, 0 in RIM. The discount rate is also **one per run**. |
| 5. Avoided costs | **Yes.** `avoided_costs_ts`: hourly values by year, plus annual values for capacity. `value_stream_groups` classifies each avoided cost (energy vs capacity pathway, include in test or not). |

Two things in how OpenBCA scores a run matter here:
- **Only four categories count as costs:** admin, utility incentive, measure cost and tax
  incentive. Everything else is a benefit, so **negative streams are netted into
  benefits**. That covers increased supply costs, bill increases, and RIM's lost
  revenue. Net benefits are unaffected, but **benefit-cost ratios differ from the SPM's
  convention**.
- **Possible upstream bug:** the tax-incentive rows list `USI + Non-USI` twice (factors 0
  and −1) and have no `USI` row. The effect looks correct (0 contributes nothing), but
  it's worth reporting to the maintainers.

### Proposed mapping

- **One OpenBCA run per test.** der-opt writes five configurations (PCT, RIM, TRC, PAC,
  SCT). Each has its own cost treatment, discount rate, and which streams are included.
- **Energy:** the hourly impact as a dimensioned `ELECTRIC` load shape. It's moved to
  standard time with February 29 dropped, and every avoided-cost year is realigned to
  the shape's calendar (decision 6 of the avoided-cost spec, done in the export).
- **DR capacity:** der-opt applies the monthly shares itself. For each capacity
  component, the export gives one annual value per year: price x sum over months of
  (share x the month's kW). The measure's `coincident_peak_savings_kw` is set to 1 and
  the line loss to 0, because values are already at the meter. That makes the capacity
  values specific to the measure: one avoided-cost subset per measure.
- **Bills:** a custom annual stream, "participant bill savings", in the first year. Its
  per-year values are the escalation index (1.00, 1.03, ...), so OpenBCA escalates it.
  It's included in PCT (as is) and RIM (as lost revenue), and excluded elsewhere.
- **DR incentive:** `utility_incentive_annual_dollar_per_year`. OpenBCA's factors handle
  it per test.
- **Costs:** `incremental_cost_*` for measure costs, `admin_cost_*` for admin, and
  `host_customer_tax_incentive_upfront_dollar` for tax credits.
- **Results:** OpenBCA's own output. der-opt also re-tallies OpenBCA's per-stream values
  (`final_value_calculations_ts`) with **negative streams counted as costs**, to give
  SPM-convention benefit-cost ratios next to OpenBCA's. The values all come from
  OpenBCA; only the classification differs.

## 4. Inputs

```yaml
cost_test:
  scope: control                  # control | device (per run)
  start_year: 2027
  measure_life_years: 10
  avoided_costs: {set: avoided_costs/acc_2024_pge_cz12}
  capacity_value: {reference: R2, basis: import, measure: counted}
  net_to_gross: 1.0
  impact_degradation: 0.01        # per year
  bill_escalation: 0.03           # per year
  cost_escalation: 0.025
  participant_costs: {upfront: 500, annual_om: 0, replacements: []}
  program_costs: {admin_upfront: 100, admin_annual: 20, upfront_incentive: 0}
  tax_credits: 0
```

Discount rates and test definitions live in OpenBCA.

## 5. What der-opt still reports itself

Only **first-year impacts**: kWh lowered and raised, DR capacity kW, bill change and
incentives. These come with first-year **screening values** from the avoided-cost
spec's section 3, labeled as screening values, not test results. They're useful for
sweeps and quick comparisons without a round trip through OpenBCA (decision 1).

## Decisions (approved with version 1.0)

- **Scope:** a per-run setting, `control` (against R1) or `device` (against R3).
- **Capacity value:** R2 / import / counted by default, configurable.
- **Tests:** OpenBCA allocates, discounts and runs them. der-opt computes no cost tests.
  It writes one OpenBCA configuration per test (PCT, RIM, TRC, PAC, SCT), filling
  OpenBCA's Excel templates.
- **Tax credits:** OpenBCA's cost-treatment setting decides their treatment.
- **Later-year bills:** escalated, carried as a custom stream.
- **Negative streams:** OpenBCA's values and ratios. der-opt also re-tallies OpenBCA's
  per-stream output with negative streams (load increases, bill increases, lost revenue)
  counted as costs, giving SPM-convention ratios alongside.
- **Impact degradation:** 0 for now.
- **First-year screening values:** der-opt keeps them, labeled as screening, not test
  results.

## Threads to pull

- **C1. OpenBCA fit check: done** (section 3). Its findings become checks in the
  export. Recheck the mapping when OpenBCA changes (pinned to commit 7e13b36).
- **C2. SCT externalities.** Which societal values OpenBCA should apply beyond the ACC.
