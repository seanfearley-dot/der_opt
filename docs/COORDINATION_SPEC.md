# der-opt feeder coordination specification

**Version 1.1**, approved 2026-11-22 (normal and emergency ratings, section 1b). Version
1.0 was approved 2026-11-21; decision 4 with `pro_rata_need` as the default allocation
method.
- v0.2: mechanisms combine in one run (section 2, decision 7).
- v0.3: decisions 1–3 and 5–7 approved. Background load is the one way to add load
  that isn't a customer. Envelope allocation is a set of interchangeable methods
  (section 4).
- v0.4: envelopes are built on network limits written as weighted sums, so voltage,
  reactive power and value- or bid-based allocation can be added later without
  redesign (section 4, "Room to grow"). None of those is built in this version.
- v0.5: participation splits into two settings, so a participant can run simple rules
  when there's no program (section 1). Participants can respond to the mechanisms with
  adjustable rules instead of an optimizer (section 1a; decisions 8 and 9).

## Purpose

Today a feeder scenario compares three runs of the same customers:
- **coordinated:** one optimizer controls every device, knowing the whole run ahead;
- **independent:** each customer optimizes their own bill, blind to the grid;
- **counterfactual:** rule-based behavior.

The coordinated run is the ideal, and no utility can run a feeder that way. This spec
adds:
1. **Three mechanisms a utility could use:**
   - a **price signal**;
   - **dynamic operating envelopes** (per-customer import and export limits by hour);
   - **feeder DR events**.

   A scenario defines runs that use any of them, alone or combined, and each run is
   measured against the ideal.
2. **Customers and load that don't take part**: customers outside the program, with
   or without their own optimization, and load on the feeder that isn't a customer at
   all.

Who pays whom (settlement) is the next spec. This one reports each mechanism's payments
so settlement can build on them.

## 1. Who takes part

### Customers: `participation` and `baseline_behavior`

```yaml
house_defaults:
  participation: participant # participant or non_participant
  baseline_behavior: own_bill # what its devices do without a program: own_bill or rule_based
houses:
  - name: c03
    baseline_behavior: rule_based # charges on plug-in today; enrolled, so it responds
  - name: c07
    participation: non_participant
    baseline_behavior: rule_based # in no program; its devices run their counterfactual rules
```

- **`baseline_behavior`** is what a customer's devices do when no mechanism applies:
  - `own_bill` (default): optimize the customer's own bill, ignoring the grid (a home
    battery or EV app);
  - `rule_based`: the counterfactual policies (self-consumption, charge on plug-in).
    Devices without one run passive.
- **A non-participant** behaves that way in every run.
- **A participant** behaves that way in `independent`, and responds to the mechanisms
  in the other runs. How it responds is set by `response` (section 1a).

| | `own_bill` | `rule_based` |
|---|---|---|
| **participant** | a smart home that also responds to the program | simple devices today, responding once enrolled |
| **non_participant** | a smart home outside the program | simple devices outside the program |

Non-participants behave the same in every run, so each run differs only in what
participants do. Their flows are solved first and are fixed for the participants'
solves (decision 1).

### 1a. How participants respond: `response` and `rules`

```yaml
house_defaults:
  response: optimized # optimized or rules
houses:
  - name: c03
    baseline_behavior: rule_based
    response: rules # keeps its rules, and reacts to the mechanisms by rule
    rules:
      dr_event:
        ev: {action: discharge, to: export, keep_soc_frac: 0.5} # a V2X EV exports during events
```

- **`response: optimized` (default):** in a mechanism run the participant's devices are
  optimized for its bill plus the run's mechanisms. With `baseline_behavior:
  rule_based`, this models enrolling bringing a managed controller.
- **`response: rules`:** the devices keep their baseline rules, and each mechanism
  overrides them by rule, step by step, without looking ahead. This models direct load
  control: a charger that pauses, a battery that discharges on a signal.

**Rules are adjustable.** Each one names an action, with that action's settings. They
can be set in `house_defaults` and changed per house; they merge key by key. A rule is
keyed by device type (`ev`, `battery`, `thermostat`, `flexible_load`, `solar`), or by a
device's name, which takes precedence over its type. New actions plug in like
envelope methods do (decision 9). The defaults:

```yaml
house_defaults:
  rules:
    dr_event:
      ev: {action: pause, catch_up: true} # stop charging; resume at full power after
      battery: {action: discharge, to: home_load, precharge_hours: 0}
      thermostat: {action: setback, offset_c: 2.0, precool_hours: 0}
      flexible_load: {action: shed, blocks: all}
    price_signal: # while the run's adder is at or above the threshold
      ev: {action: pause, above_per_kwh: 0.20}
      battery: {action: discharge, to: home_load, above_per_kwh: 0.20}
      thermostat: {action: setback, offset_c: 2.0, above_per_kwh: 0.20}
      flexible_load: {action: shed, blocks: all, above_per_kwh: 0.20}
    envelope:
      ev: {action: limit} # cut charging power to fit
      battery: {action: limit} # limit charging, and discharge that would export
      solar: {action: curtail} # curtail export to the export envelope
      flexible_load: {action: shed}
```

| Device | Actions |
|---|---|
| EV | `pause` (`catch_up`); `discharge`, V2X only (`to: home_load` or `export`, `max_kw`, `keep_soc_frac`, the charge it won't go below); `limit`; `none` |
| Battery | `discharge` (`to: home_load` or `export`, `max_kw`, `keep_soc_frac`, `precharge_hours`: charge to full from the grid in the hours before an event); `limit`; `none` |
| Thermostat | `setback` (`offset_c`, `precool_hours`, `precool_offset_c`): moves the setpoint of a thermostat whose baseline is the `setpoint` counterfactual policy (cool just enough to hold the comfort limit); `none` |
| Flexible load | `shed` (`blocks`: `all` or a number); `none` |
| Solar | `curtail`; `none` |

- **Stacking:** in a run with several mechanisms, the rules stack. An EV pauses for an
  event and also stays within its envelope. When two rules conflict, the one that
  draws less from the grid wins.
- **Limits of rules:** they don't plan ahead. A paused EV can leave short of its
  departure charge, and a discharging battery can be empty when the event ends. The
  existing departure and end-state checks report this rather than hiding it.
- **Checked by `validate`:**
  - an action that doesn't fit its device (`discharge` on an EV with no V2X, `curtail`
    on a battery);
  - a rule for a device the customer doesn't have, unless set in `house_defaults`;
  - rules on a customer with `response: optimized`, where they'd do nothing;
  - a missing threshold.

### Load that isn't a customer: `background_load`

```yaml
feeder:
  elements: [...]
  background_load:
    - name: strip_mall
      element: tx_b
      load: {source: timeseries, path: ../data/strip_mall.csv, column: load_kw}
    - name: existing_bank_load
      element: bank
      load: {source: loadcast, path: ../data/loadcast/bank_a_2026_planning.nc, trace: poe10, var: net}
```

Background load is kW flowing through its element in every run: not billed, no devices,
never flexible. It's the one way to add load that isn't a customer (decision 2).

A house with no devices is still a customer: it has a bill and can be a
non-participant, but it has nothing to flex. `feeder_bank_planning_trace.yaml` uses a
device-less, unbilled house to stand in for the bank's existing load; it moves to
`background_load`.

### 1b. Element ratings: normal, emergency, and beyond (v1.1)

A hard rating gives the cleanest congestion prices, but a run fails outright when load
nobody can move exceeds it in a single hour. Utilities rate equipment twice, normal and
emergency, and the model follows them with up to three bands per element:

| Flow (either direction) | Treatment | The congestion price there |
|---|---|---|
| up to `rating_kw` (normal) | free | 0, or up to the overload cost while it binds at normal |
| normal to `emergency_rating_kw` | allowed at `overload_cost_per_kwh` | the overload cost |
| above emergency | at `violation_cost_per_kwh`; without one, a hard wall at the emergency rating | up to the violation cost |

```yaml
elements:
  - name: tx_1
    rating_kw: 75                 # normal
    emergency_rating_kw: 90
    overload_cost_per_kwh: 1.0    # between normal and emergency
    violation_cost_per_kwh: 100   # above emergency; left out: a hard wall there
    emergency_kwh_per_day: 30     # optional: at most this much above normal each day
```

- **Without an emergency rating** nothing changes: `overload_cost_per_kwh` prices every
  kWh over normal, or, left out, normal is a hard limit.
- **An emergency rating needs `overload_cost_per_kwh`** (what running between the two
  costs), and must be above normal; `violation_cost_per_kwh` and `emergency_kwh_per_day`
  need an emergency rating. Validation says so.
- **`emergency_kwh_per_day`** is a linear stand-in for a time limit on emergency
  operation (counting hours would need on/off variables and cost exact prices): the
  kWh above normal, both directions, summed over each local day, is at most this.
  What doesn't fit goes to the violation band, or, without one, can make the run
  infeasible.
- **Every band is linear,** so the run stays an LP and congestion prices stay exact duals
  (the single meter's on/off variables aside).
- **Envelopes and feeder DR triggers** work from the normal rating, as before.
- **Reported:** hours and kWh over normal and over emergency, per element and run; the
  overload (emergency band) and violation costs apart; and a warning naming each element
  that went above its emergency rating in the coordinated run, with how many hours and by
  how much.
- **`network.csv`** gains the three columns, all optional.

## 2. The runs

Each mechanism's settings are written once. `runs` names the runs and lists the
mechanisms each one combines:

```yaml
coordination:
  price_signal: {...} # section 3
  envelopes: {...} # section 4
  dr_events: {...} # section 5
  runs:
    signal: [price_signal]
    envelopes: [envelopes]
    signal_and_envelopes: [price_signal, envelopes]
    all_three: [price_signal, envelopes, dr_events]
```

- **A run combining several** applies them all at once:
  - participants pay the signal;
  - they stay within their envelopes;
  - they respond to events.
- **Each mechanism's payments** are reported separately within the run.
- **`runs` left out:** each mechanism configured runs alone, and all of them together
  in one more run (`combined`). The report then shows what each adds (decision 7).
- **Run names** are the labels in every report. `coordinated`, `independent` and
  `counterfactual` are taken.

Every feeder scenario still runs the three it runs today, with the same names; with
the settings in section 1, they mean:
- **`coordinated`:** the ideal. Participants are controlled together, whatever their
  `response`, and non-participants are as above.
- **`independent`:** no program. Every customer follows its `baseline_behavior`, with
  no mechanism at all (no signal, no envelopes, no events). This is the reference every
  run in `runs` is measured against.
- **`counterfactual`:** every customer rule-based, as today.

Each run in `runs` adds one where participants respond to its mechanisms, each on its
own (decision 6):
- **`response: optimized`:** a bill optimization, with the run's mechanisms added (the
  signal as a cost, envelopes as limits, events as an incentive or limit);
- **`response: rules`:** the baseline rules, overridden by the mechanism rules.

So a run and `independent` differ only in the mechanisms. Without a `coordination` section a scenario runs exactly as
today.

**How mechanisms combine:**
- **Price signal and envelopes don't depend on each other.** The signal is an adder in
  each participant's objective. Envelopes are limits computed from non-participants and
  background load alone. Both apply as configured.
- **DR events depend on what else is running.** The utility calls events on the
  loading it expects with its other mechanisms already in place. So a run with
  `dr_events` first solves a **twin without the events**: the same run's other
  mechanisms, or `independent` when DR is alone. That twin is used twice (section 5):
  - its element loading is the event forecast;
  - each participant's history in it is that participant's expected settlement
    baseline.

  The twin is reported too, as `<run> (no events)`, so the events' own effect is
  visible. When the twin is also one of the listed runs (`signal_and_envelopes` for
  `all_three` above), that run is reused, not solved twice.
- **Two limits at once.** With an envelope and a DR `import_cap` response in the same
  hour, the participant must meet the lower of the two, and shortfalls are reported
  under each.

## 3. Price signal

Participants pay or earn a $/kWh adder on their net import (import minus export), on top
of their bill. The adder is the sum of the prices on the elements between the customer
and the top of the feeder.

```yaml
coordination:
  price_signal:
    source: coordinated # the coordinated run's congestion prices, known a day ahead
    # source: schedule:  {months: [6, 7, 8, 9], hours: "16:00-21:00", per_kwh: 0.50, element: tx_a}
    # source: file:      {path: ../data/feeder_prices.csv}  # timestamp, element_id, price_per_kwh
    paid: true # the adder is charged on the bill (false: it steers the optimizer only)
```

- **`coordinated`:** the congestion prices from the coordinated run. It tests how well
  the ideal's own prices steer customers acting alone. Exact prices don't always
  reproduce the ideal: customers can all pile into the same unpriced hour, and the run
  shows it when they do.
- **`schedule`:** a fixed adder by month and hour on an element's customers, like a
  critical-peak adder.
- **`file`:** prices by hour and element, from a utility or another study.
- **`paid`:**
  - `true` (default): the adder is charged, and reported per customer as **signal
    charges**, apart from the bill;
  - `false`: it only shapes what the optimizer does, like an advisory price.

Not in this version: prices updated in rounds until flows fit (price discovery). That's
the decomposition in the scale work.

## 4. Dynamic operating envelopes

Each participant gets an import limit and an export limit for every step: its share of
what the network has left after non-participants and background load. Envelopes are
built in four stages, and each stage is a setting with interchangeable methods. A new
method plugs into its stage without changing anything else (decision 4).

```yaml
coordination:
  envelopes:
    import: {method: pro_rata_need} # how import headroom is shared
    export: {method: max_min} # export can be shared another way, or left out
    headroom: {margin_pct: 10} # keep 10% of each rating unallocated
    nesting: min_along_path # or top_down
    granularity: step # or {block_hours: 4}, or static
    min_kw: 1.0 # no import envelope below this, so essential load is always served
    violation_penalty_per_kwh: 5.0 # soft limits: going over costs this, and is reported
```

### Stage 1: headroom, as network limits

Every limit the envelopes must respect is written as a **weighted sum**:
`Σ coefficient × participant's flow ≤ limit`, for one quantity (import or export kW)
at one step.
- **Thermal limits** (the only kind in this version): one per element, direction and
  step. Each participant below the element has coefficient 1, and everyone else 0.
- **The limit** is the rating, minus `margin_pct`, minus the non-participants' and
  background load's net flow through the element. For export, it's the same for flow
  back up the feeder. Non-participants' and
background flows are known exactly (they're solved first), so `margin_pct` stands in
for the forecast error a utility would allow for.

### Stage 2: allocation, separately for import and export

An allocation method receives every limit at once, plus any per-participant inputs it
needs. It returns each participant's envelope by step and quantity, within all the
limits. With thermal limits, that's sharing each element's headroom among the
participants below it. Methods:

| `method` | Each participant's share | Needs |
|---|---|---|
| `equal` | the headroom split evenly | — |
| `service_size` | in proportion to its import (or export) limit | each participant's limits |
| `weights` | in proportion to a weight you give | `weight` per house, or a `premises.csv` column named by `column:` |
| `pro_rata_need` (default) | in proportion to what it would draw (or export) with no program. A participant that would draw nothing gets nothing beyond `min_kw` | the `independent` run |
| `max_min` | equal shares, but no participant gets more than it would use. What one doesn't need is shared among the rest until headroom or needs run out (water-filling, max-min fair) | the `independent` run |
| `fixed` | the same kW every step, e.g. a 5 kW export limit: a static limit, the usual comparison for dynamic ones | `kw:` |
| `file` | given step by step | a CSV: timestamp, premise_id, import_kw, export_kw |

"What it would use" is the participant's own flow in the `independent` run at that
step. A share is never above the participant's own service limit.

Not in this version: shares from an optimization over all the limits jointly
(`max_total`; see "Room to grow").

### Stage 3: nesting, on a feeder with more than one level

- **`min_along_path` (default):** each element shares its own headroom among every
  participant below it. A participant's envelope is the smallest of its shares along
  its path to the top. It's simple, and never overloads any element, but can leave a
  parent's headroom unused.
- **`top_down`:** each element shares its headroom among its children (elements and
  customers), in proportion to the participants under each and by the stage-2 method.
  No child gets more than it can pass on (an element child's own headroom), and what a
  child can't use goes to the others. Each child then shares what it got the same way.
  It uses all the headroom the limits allow.

For example, take a lateral with 60 kW of headroom. Below it, transformer A has 15 kW
and 3 participants, and transformer B has 60 kW and 3 participants. Shares are equal.

| | `min_along_path` | `top_down` |
|---|---|---|
| Each customer on A | min(60 ÷ 6, 15 ÷ 3) = 5 kW | A takes 15 kW, all it can pass on: 5 kW |
| Each customer on B | min(60 ÷ 6, 60 ÷ 3) = 10 kW | B gets the other 45 kW: 15 kW |
| Lateral headroom allocated | 45 of 60 kW | 60 of 60 kW |

### Stage 4: granularity

- **`step` (default):** a new envelope every step.
- **`{block_hours: N}`:** the same envelope for each block of N hours (the smallest of
  the block's steps), as when envelopes are issued for 4-hour blocks.
- **`static`:** the same envelope all run, the smallest of any step.

### Then

- **The floor:** an import envelope is never below `min_kw`.
- **Soft, not hard:** a customer whose own fixed load is over its envelope can't
  comply, and a hard limit would make the run unsolvable. Each kWh over costs
  `violation_penalty_per_kwh`. Violations are reported per customer, and the penalty
  isn't counted in bills.
- **Files written:** `envelopes.csv`, one row per participant, step and quantity
  (`import_kw`, `export_kw`; later quantities add rows, not columns). With it, a run can be repeated with `method: file`, or the envelopes compared
  with a utility's.

### Room to grow: allocation methods from the literature

These are left out of this version. Each fits the design above without changing it:

| Method | Needs first | Where it fits |
|---|---|---|
| Equal real and reactive capacity, per DER asset or per connection point | Reactive power in devices and the network model. Counting DERs instead of customers needs nothing: `weights` set to each customer's DER count | a second quantity (kVAR) in the limits and `envelopes.csv`; `equal` with `per: asset` |
| Equal voltage impact at the binding constraint (favours customers near the transformer, so may be judged unfair) | Voltage in the network model (e.g. linearized power flow), giving each participant's voltage sensitivity | voltage limits as weighted sums, with sensitivities as coefficients; `equal_impact`, with shares inversely proportional to them |
| By the marginal cost of generation | Each participant's value of capacity by step (from its own optimization, or given) | `max_total` with values: an optimization allocating capacity where it's worth most, within every limit, with an optional fairness floor |
| By energy or ancillary service bid price (a DER merit order) | Bids by participant and step | `max_total` with bids as values; with one binding limit this is the merit order. Capacity payments go to settlement |

The design commitments that keep these open:
- limits are weighted sums;
- methods see all the limits and per-participant inputs together;
- envelopes are keyed by quantity.

## 5. Feeder DR events

The utility calls events on an element when it expects that element to be heavily
loaded. Participants below it respond as in a DR program
([docs/DR_CAPACITY_SPEC.md](DR_CAPACITY_SPEC.md)).

```yaml
coordination:
  dr_events:
    element: tx_a # participants below this element get the events
    trigger: {above_pct: 90} # call an event where the forecast loading is above 90%
    duration: variable # variable: the hours over the trigger; fixed: `hours` around the forecast peak
    program: # the DR program section, as for a single home (window, limits, response, incentive, settlement)
      program_id: tx_a_peak
      window: "15:00-22:00"
      max_hours: 4
      max_events_per_month: 8
      response: incentive
      incentive_per_kwh: 0.75
```

- **`trigger`:** a new event constructor. It forecasts the element's loading from the
  run's twin without events (section 2): `independent` when DR runs alone, or the
  run's other mechanisms when it's combined. It calls events on the days and hours
  that forecast is above `above_pct` of the rating. The program's limits (window, events per
  month, consecutive days) still apply, and the calendar is checked against them.
- **Other constructors:** `file`, `schedule`, `price` and `random` work here too, in
  place of `trigger`.
- **Settlement:** each optimized participant is paid against a baseline expected from
  its own history in the twin without events, as a single home's DR is against R2.
  Rule-responding participants don't plan for payment, but are settled the same way. The
  reported settlement uses the responding run's own history, as in the DR spec. Incentives, commitments and import
  caps work as in the DR spec.
- **Reported:** the calendar, each participant's performance, and incentive payments.

## 6. What's reported

For every run (coordinated, independent, counterfactual, each run in `runs`, and the twins
without events):
- **Network:**
  - peak loading of each element, and the hours over its rating (and over its emergency
    rating, v1.1);
  - overload, violation and deferral costs;
  - for mechanisms, the **share of the ideal captured**: (network cost, independent −
    the run's) ÷ (independent − coordinated).
  - for a run combining mechanisms, the same share for each of its mechanisms alone,
    where those runs exist, so you can see what each one adds.
- **Customers**, by group: non-participants, optimized participants, and
  rule-responding participants. This lets the two kinds of response be compared
  directly:
  - bills, and degradation;
  - the mechanism's own payments: signal charges, DR incentives, envelope violations.
- **Each customer:** the same, per run.
- **Files:** `coordination.csv` (one row per run), `customers.csv` (one row per
  customer and run), envelopes or signal prices by hour where the run used them, and
  the DR calendar.

`der-opt validate` checks:
- `participation`, `baseline_behavior` and `response` values, and rules (section 1a);
- background load elements;
- that each mechanism's settings are complete;
- that a `dr_events` element exists and has participants below it;
- that envelopes and signal files cover the run.

## 7. Decisions

All approved 2026-11-21.

1. **Non-participants:** each customer chooses `own_bill` or `rule_based`. Approved;
   expressed since v0.5 as `baseline_behavior` (decision 8).
2. **Load that isn't a customer:** `background_load` on the feeder, the one way to add
   it. Device-less houses stay customers. Approved.
3. **Price signal:** sources `coordinated`, `schedule` and `file`, paid by default and
   reported apart from the bill. Approved.
4. **Envelopes:** the flexible design in section 4, with `pro_rata_need` as the
   default method. Approved. It covers:
   - the stages: limits, allocation, nesting, granularity;
   - limits written as weighted sums, so the methods in "Room to grow" can be added
     later;
   - the methods in stage 2: `pro_rata_need` (default), `equal`, `service_size`,
     `weights`, `max_min`, `fixed`, `file`;
   - the defaults: `min_along_path`, `step`, soft limits;
   - `max_total`, voltage and reactive power left for later.
5. **Feeder DR trigger:** forecast loading from the run's twin without events
   (`independent` when DR runs alone), plus the existing constructors. Approved.
6. **How participants respond:** each solving on its own, with non-participants fixed
   first. Approved.
7. **Combining mechanisms:** named runs, each listing the mechanisms it combines.
   Without `runs`, each mechanism runs alone plus all together. Approved.
8. **Participation and baseline behavior as two settings (v0.5):** `participation`
   (participant or non-participant) and `baseline_behavior` (`own_bill` or
   `rule_based`). A participant can then run simple rules without the program. The
   default stays `own_bill`, so `independent` means what it does today. Approved.
9. **Rule-based responses (v0.5):** `response: optimized` (default) or `rules`. Rules
   are adjustable per mechanism and device, as actions with settings. The defaults are
   in section 1a, including V2X EVs and batteries discharging to the home or exporting.
   New actions plug in. Approved.

Approved with v1.1 (2026-11-22):

10. **Normal and emergency ratings** (section 1b): an emergency band priced at the
    overload cost, a violation band above it (or a hard wall), and an optional daily kWh
    budget above normal, all linear.
