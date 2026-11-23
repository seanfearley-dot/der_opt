# der-opt measure impact export specification (for cost tests)

**Version 1.3 — approved 2026-11-22.** Implemented: the impact package and the OpenBCA
export (`der_opt.cost_test`, `der-opt openbca`). Change it only by a new
version, approved the same way. (1.3 compares the measure against a home built device
by device, section 1, retiring `scope`; 1.2 drops der-opt's re-tally of OpenBCA's streams and
reports OpenBCA's results alone, its own ratio included; 1.1 switched the export from
Excel templates to OpenBCA's input tables; 1.0 approved 2026-11-20; drafts 0.1-0.3 led
to 1.0.)

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

One premise, one full analysis year. "With" is the responding run when there's a DR
program, otherwise the optimized run (`cost_test.with` picks). "Without" is the
comparison home, built device by device (v1.3, below; until 1.3 a `scope` picked R1 or
R3 for the whole home).

### 1a. What each device is compared against (v1.3)

**Why.** `scope: control` compares against R1, every device under its rule-based
control. That's one behavior for the whole home, and often the wrong one: a battery
program's comparison may be no battery, a thermostat's is a thermostat that doesn't
pre-cool, and an event program's EV is one on a TOU charging timer.

**The comparison home is built device by device.** Each device has a mode:

| Mode | The device in the comparison home | Devices |
|---|---|---|
| `removed` | gone, with everything it does (an EV takes its charging load with it) | battery, EV |
| `rule_based` | its `counterfactual` policy: thermostat `setpoint` (holds the top of the comfort band, no pre-cooling), battery `self_consumption` | thermostat, battery, EV |
| `tou_timer` | an EV that starts charging at full power when the tariff's cheapest period begins (or at a set time) and stops when full; it never discharges | EV |
| `events_only` | the TOU-timer EV that also responds to DR events: shifts charging out of an event, or discharges into it with V2X, and recharges after | EV |
| `backup_only` | a battery held full for backup that discharges only into DR events, then recharges | battery |
| `unchanged` | as in the "with" run | solar (always) |

```yaml
cost_test:
  without:                    # per device, by name; omitted devices take their type's default
    battery: backup_only      # rule_based | removed | backup_only (required: it varies by program)
    ev: tou_timer             # tou_timer (default) | events_only | removed | rule_based
    thermostat: rule_based    # the default
  with: responding            # optional; responding with a DR program, else optimized
```

- **Defaults:** thermostat `rule_based`; EV `tou_timer`, the common EV behavior and
  the baseline for event programs; solar `unchanged`. **A battery has no default:** the
  comparison varies by program, so a cost test at a home with a battery must say which
  (validation names the three choices).
- **`tou_timer`:** the cheapest period comes from the premise's import tariff (E-ELEC:
  midnight to 3 pm, so a commuter EV home at 18:30 waits until midnight). `start:
  "HH:MM"` sets the timer instead. It's also a new `counterfactual` policy for EVs,
  usable anywhere `rule_based_ev` is.
- **`events_only` and `backup_only`** are solved: the device sees a flat energy price, so
  nothing rewards TOU shifting, plus the DR program's response. A tiny tie-break
  holds its resting behavior: the EV charges in the cheapest period, as on its timer;
  the battery stays full. It meets every departure and comfort limit like any run, and
  is billed on the real tariff afterwards.
- **How it runs:** rule-based and timer devices are simulated first and fixed as load
  profiles (none of their rules depends on another device); `events_only` and
  `backup_only` devices, if any, are solved together in one more solve; removed devices
  are dropped.
- **`scope` is retired.** A scenario that still sets it fails validation, naming
  `without`. The library's cost-test block drops it; its EV counterfactual becomes
  `tou_timer`.
- **Participant costs.** When a device is `removed`, the measure includes buying it, so
  `participant_costs.upfront` must include its installed cost. A study's portfolio
  already adds each removed device's `installed_cost`; a single-home run warns, naming
  the removed devices.

**Cases** (comparison cases spec v1.2): the comparison home becomes a case, `without`
(labeled from its modes, e.g. "Thermostat at setpoint, no battery"), replacing
`no_measure`, so the A/B page can show exactly what TRC and RIM compare against. The
report opens on `without` -> `with`.

**The five homes, as an example:**

| Home | Comparison home |
|---|---|
| 1 Battery | battery as the study chooses: `removed`, `rule_based` or `backup_only` |
| 2 V2X | EV on a TOU timer (or `events_only`, or `removed`) |
| 3 Thermostat + battery | thermostat at setpoint; battery as chosen |
| 4 Thermostat | thermostat at setpoint |
| 5 V2X + battery | EV on a TOU timer; battery as chosen |

- **As built:** a solved device's resting behavior is held as a rule, not only a
  tie-break: outside an event it can't discharge, and outside the event's recovery
  window (its start to the end of the next day) it keeps at least the energy it would
  have at rest. Smoothing is off for it. The measure's first-year DR incentive is the
  "with" run's less what the comparison home earns (an events-only EV earns some), and
  `measure.yaml` records each device's mode.

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
  revenue. Counting them as costs instead would leave net benefits the same and change
  the ratios. der-opt reports OpenBCA's classification as it is (decision, 2026-11-22).
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
- **Results:** OpenBCA's own output: each test's benefits, costs, net benefits and
  ratio, from its `jst_ratio.csv`, as it reports them.

### What implementing the export found (OpenBCA 7e13b36)

- **Avoided costs are rounded to 3 decimals.** OpenBCA's loading step casts them to
  `NUMERIC`, which DuckDB reads as `DECIMAL(18,3)`: $0.0949/kWh becomes $0.095, and
  emission intensities in tCO2/kWh become 0. This also affects OpenBCA's own Excel
  pathway. **Workaround (exact):** the export gives energy in MWh and avoided costs in
  $/MWh, so the same dollars are rounded at $0.001/MWh. OpenBCA's "kWh" outputs are
  therefore in MWh.
- **Annual cost fields aren't discounted.** OpenBCA's core leaves the discount factor
  off annual admin, O&M and utility incentives ("costs are accrued on an ongoing
  basis"). Benefits are discounted, so measures with ongoing costs score lower than they
  would with every stream discounted. **Decision (2026-11-20): keep OpenBCA's convention.** Every
  export notes it and reports, per test, how net benefits would change if annual cost
  fields were discounted at that test's rate (`net_change_if_annual_discounted`).
- **Load increases on their own row.** The measure is exported as two OpenBCA rows: the
  measure (hours it lowers load, plus all costs, capacity, bills and incentives) and its
  increases only. OpenBCA's totals are unchanged, and the re-tally can count increases
  as costs.
- **Custom streams:** all five custom slots must carry distinct impact categories,
  because OpenBCA uses them as map keys. Unused slots get excluded placeholders.
- **Verification:** an integration test runs OpenBCA on the export and reconciles its
  discounted energy stream with an independent calculation, to 32-bit precision.

## 4. Inputs

```yaml
cost_test:
  without: {battery: backup_only} # each device's comparison (section 1a); a battery must have one
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
  Replaced in 1.3 by `without`, below.
- **Capacity value:** R2 / import / counted by default, configurable.
- **Tests:** OpenBCA allocates, discounts and runs them. der-opt computes no cost tests.
  It writes one OpenBCA configuration per test (PCT, RIM, TRC, PAC, SCT) as **OpenBCA's
  input tables (its "integrated" pathway), not its Excel templates (v1.1)**. A test
  showed that re-saving the templates from Python changes OpenBCA's results (benefits
  off by up to $862 per measure; the templates' formulas lose their computed values)
  and takes about 4 minutes per load. Tables carry exact values and run in seconds.
  OpenBCA calls this pathway unsupported, so the mapping is pinned to the tested
  commit and rechecked when OpenBCA changes.
- **Tax credits:** OpenBCA's cost-treatment setting decides their treatment.
- **Later-year bills:** escalated, carried as a custom stream.
- **Negative streams:** OpenBCA's values and ratios, as it reports them. (Until 1.2,
  der-opt also re-tallied OpenBCA's streams with negative streams counted as costs and
  showed those ratios alongside, labeled "SPM convention". Dropped 2026-11-22: der-opt
  relies on OpenBCA.)
- **Impact degradation:** 0 for now.
- **First-year screening values:** der-opt keeps them, labeled as screening, not test
  results.
- **Annual costs (v1.1):** OpenBCA's convention of not discounting them is kept, noted and
  quantified on every export.
- **What each device is compared against (v1.3):** a battery's mode is required (it varies
  by program); an EV's default is `tou_timer`, and the library's EV counterfactual is the
  TOU timer too; `scope` is retired with a validation error; `events_only` and
  `backup_only` are solved against a flat price and the DR program, holding their
  resting behavior.

## Threads to pull

- **C1. OpenBCA fit check: done** (section 3). Its findings become checks in the
  export. Recheck the mapping when OpenBCA changes (pinned to commit 7e13b36).
- **C2. SCT externalities.** Which societal values OpenBCA should apply beyond the ACC.
