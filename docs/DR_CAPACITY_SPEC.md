# der-opt demand response events and capacity specification

**Version 1.3 — approved 2026-11-22.** Implemented in `der_opt.dr`. Change it only by a
new version, approved the same way. Open threads are listed at the end. (1.3 adds the
program's `delivery` and the held kW, section 8; 1.2 the program's `settlement_basis`;
1.1 the two reduction bases, section 5; 1.0 approved 2026-11-19.)

## Purpose

A measure has two attributes, valued separately:

- **Energy:** the change in hourly net load against the counterfactual, all 8760 hours.
- **Capacity:** the load reduction the measure can deliver for a demand response (DR)
  program, both **counted** (what it could deliver in each month) and **accounted**
  (what it measurably delivered in called events, including test events).

This spec defines DR events, programs, the reference cases a response is measured
against, and how capacity is counted and accounted. Valuing capacity in dollars belongs
to the avoided-cost spec. Cost tests belong to the cost-test export spec.

## Scope and assumptions

- **One measure, one year:** one premise and a full 8760-hour year, the default for all
  analyses.
- **Perfect foresight:** the optimizer sees the whole year, every event included.
  Delivered response is an upper bound on what a controller with day-ahead or day-of
  notice would achieve, until the rolling-horizon solver exists.
- **Weather is an input.** The load, solar and price files come from the load
  forecasting model and its scenario tool, which set the weather (1-in-2, 1-in-10, and
  so on). der-opt records the scenario's label (`weather_case`) in the run record and
  doesn't generate or choose weather itself.
- **One program per measure.** Stacking programs comes later.

## 1. Events

Every event has an `event_type`:
- **`dispatch`**: a called event.
- **`test`**: a test event, which confirms capability and counts toward accounted
  capacity.

Events are built by one of four **constructors**. Each writes the resulting calendar to
`dr_events.csv` (same columns as the events file below), so a run can be reproduced and
its calendar edited.

### 1a. Events file

CSV in the input data spec's style:

| Column | Required | Meaning |
|---|---|---|
| `event_id` | yes | unique |
| `start`, `end` | yes | ISO 8601 with offset; `end` exclusive; on the run's step boundaries |
| `program_id` | yes | the program (section 2) |
| `event_type` | yes | `dispatch` or `test` |
| `scope` | no | `system` (default), or an `element_id` for a local event |

### 1b. Price-based

Calls events in the highest-price periods of a price series. That series is any column
in an input-spec time series file, for example an ACC total, an ACC capacity
allocation, LMP or a retail rate.

1. **Pick the days:** rank the eligible days (program season and day types) by the
   price series over the program window. Take the top N, subject to the program's call
   limits, per month or per year.
2. **Size each event, by either mode:**
   - `fixed`: a duration of D hours. The event goes where the D-hour block inside the
     window has the highest total price.
   - `variable`: start at the day's highest-price step and extend it, a step at a time
     on whichever side is higher, while the price stays at or above a share `alpha` of
     that day's window peak (e.g. 0.8). Every event is clipped to between `min_hours`
     and `max_hours`, and to the window.

### 1c. Randomized (proposal)

For test events and unbiased estimates of response: events whose timing carries no
information the optimizer could exploit, beyond the perfect foresight already noted.

1. **How many:** `events_per_month`, a number per month (or one number for all program
   months), capped by the call limits.
2. **Which days:** drawn without replacement from each month's eligible days, rejecting
   draws that break `max_consecutive_days` or a `min_days_between` spacing.
   - `weighting: uniform` (default): every eligible day is equally likely.
   - `weighting: price`: a day's chance is proportional to its price series value raised
     to `weight_power`. This leans toward high-value days while staying random.
3. **When and how long:** a start time drawn uniformly from the window's allowed starts
   on the step grid. A duration fixed at D, or drawn uniformly from `min_hours` to
   `max_hours`, and kept inside the window.
4. **Reproducibility:** a `seed` fixes the draw. `replicates: K` makes K calendars from
   seeds `seed`..`seed+K-1`, one run each, with results reported as a mean and spread
   across replicates.

### 1d. Fixed schedule

A list of recurring rules in the scenario YAML, expanded into events:

```yaml
schedule:
  - {months: [7, 8, 9], days: weekdays, start: "17:00", hours: 3, event_type: dispatch}
  - {nth_weekday: {n: 1, weekday: tue}, months: all, start: "16:00", hours: 1, event_type: test}
  - {dates: [2026-08-17, 2026-09-03], start: "16:00", hours: 4, event_type: dispatch}
```

- `days`: `all`, `weekdays`, `weekends`, or a list of weekdays.
- Rules expand in order, and a rule dropping an event on a day that already has one is
  an error.
- Holidays are skipped when the program's `day_types` exclude them.

**Whichever constructor is used**, the program's limits apply. A calendar that breaks
them, whether from a file or a schedule, is rejected, and constructors never produce
one.

## 2. Programs

Defined in the scenario YAML:

| Field | Meaning | Example |
|---|---|---|
| `program_id` | name | `summer_peak` |
| `season` | months the program is available | `[1, 2, ..., 12]` or `[5, 6, 7, 8, 9, 10]` |
| `day_types` | `weekday`, `weekend`, `holiday` | `[weekday]` |
| `window` | local hours events may fall in | `16:00-21:00` |
| `min_hours`, `max_hours` | event length limits | `1`, `4` |
| `max_events_per_year`, `max_events_per_month` | call limits (test events included) | `15`, `5` |
| `max_consecutive_days` | back-to-back limit | `3` |
| `response` | how the measure responds (section 3); default `incentive`, and every output names the one used | `incentive` |
| `incentive_per_kwh`, `committed_kw`, `cap_kw` | the response's parameter | `2.00` |
| `settlement` | baseline rule (section 4) | `by_class` |
| `settlement_basis` | what incentive and commitment settle on: `net` (default; exports count) or `import` (grid import only) (v1.2) | `net` |
| `delivery` | how an incentive pays: `per_kwh` (default) or `uniform`, the kW held in every event hour (v1.3, section 8) | `uniform` |
| `capacity_reference` | which reference capacity is valued against (section 4): `R2`, `R3`, `S`, or `all` (default; each one output) | `all` |

## 3. How the measure responds

| `response` | In the optimization |
|---|---|
| `incentive` | `delivery: per_kwh`: each event hour pays `incentive_per_kwh` for each kWh of reduction below the settlement baseline. `delivery: uniform` (v1.3): each event pays `incentive_per_kwh` x its hours x the held kW, the reduction kept in every one of its hours (section 8) |
| `commitment` | event-hour net import must be at least `committed_kw` below the settlement baseline: a constraint, plus a penalty per kWh short so the run stays feasible |
| `import_cap` | event-hour net import can't exceed `cap_kw` |

## 4. Reference cases

Every event's response is measured against each of these, and all are reported:

| Reference | What it is | Answers |
|---|---|---|
| **R1 Counterfactual** | the measure's rule-based "without" case (e.g. plug-in EV charging, a self-consumption battery), no event response | the energy attribute; what the optimized measure does differently from today's behavior |
| **R2 Non-responding twin** | the same customer, measure and optimization, with the events removed: a separate run that never sees the calendar | **the DR response the events themselves cause** (the Load Impact Protocols' reference load) |
| **R3 No measure** | the same customer without the measure's devices: load and any pre-existing devices only | the measure's total effect, for completeness |
| **S Settlement baseline** | a baseline rule applied to the responding run's own history | what the participant is paid |

Capacity, counted and accounted, is valued against the program's `capacity_reference`:
R2, R3 or S, or all three, each output side by side. R1 is always reported but anchors
the energy attribute, not capacity. Counted capacity (shadow events) is measured
against R2's load. With R3 or S chosen, the shadow event's reduction is restated
against that reference over the same hours.

**Settlement rules (`settlement: by_class`, from the premises file's `customer_class`):**
- **Residential:** 5-in-10. The average of the 5 highest-load days (load over the event
  hours) among the 10 most recent eligible non-event days, scaled by a day-of adjustment.
- **Commercial:** 10-in-10. The average of the 10 most recent eligible non-event days, with
  a day-of adjustment.
- **Day-of adjustment:** the ratio of the event day's load to the baseline's over the 4
  hours ending 1 hour before the event, capped at ±20% (thread to pull: T1).
- **Weekend and holiday events:** the same rule over the most recent eligible weekend
  and holiday days, using 4-in-4 if there aren't 10 such days (thread to pull: T2).

`customer_class` is a new column in `premises.csv`: input data spec v1.1, approved
along with this spec.

## 5. Capacity: counting and accounting

**Two bases, both always reported (v1.1).** Every reduction, against every reference,
counted and accounted, is reported twice:

- **net:** the difference in net load (import - export), so energy exported beyond the
  home's own load counts as reduction;
- **import:** the difference in grid import, each step's load floored at zero, so exports
  don't count. The settlement baseline S on this basis is computed from import-only
  history.

The incentive and commitment settle on the program's `settlement_basis` (v1.2): `net`
by default, or `import`, where reduction stops at zero import and exporting earns
nothing. Both bases are reported either way.

### Counting: what the measure could deliver, in every month

Measured whether or not any event was called.

- **Shadow events:** for every eligible day of every month in the program's `season`,
  a separate short optimization starts from the R2 run's device state at midnight
  (battery and EV charge and so on). It maximizes reduction below R2's load over a
  standard event in the window: duration `capability_hours`, placed like a
  `fixed`-mode price event, or the window's most valuable block. Each shadow event is
  solved on its own and never changes the annual runs.
- **Reported:** per month, the **average** shadow-event kW over eligible days (the valued
  number), with the minimum and maximum alongside. A month outside the program's season
  reports nothing. Shadow events last the program's `max_hours`.
- **What it captures:** a December program reports December capability from December's
  real load, solar and device state, with no test event needed.

### Accounting: what the measure measurably delivered

- **Per event:** the average kW reduction over its hours, against R1, R2, R3 and S, and
  the incentive earned under S. Test events count here as dispatch events do.
- **Per month:** the average over that month's called events (dispatch and test), with the
  event count. Months with no events report counted capacity only.
- **Slice-of-Day profile:** for each month, the average reduction in each of the 24 hours
  over its event days (24 x 12), for California resource adequacy's format. It's built
  from accounted events, or from shadow events in months without any.

## 6. Keeping energy and capacity apart

Energy is valued with energy-only avoided-cost components, and capacity with the
capacity components, so capacity is never counted twice. The avoided-cost spec defines
the split, for ACC files and internal curves alike.

## 7. Outputs

| File | Contents |
|---|---|
| `dr_events.csv` | the calendar: id, program, type, start, end, scope, constructor and seed |
| `dr_event_performance.csv` | per event: load under the responding run, R1, R2, R3 and S; reduction against each (the mean over its hours) and the held kW (its smallest hour's, v1.3); incentive earned; shortfall |
| `dr_capacity.csv` | per month: counted kW (mean, min, max over shadow events), accounted kW, event count, event hours |
| `dr_capacity_slice_of_day.csv` | month x hour reduction, marked accounted or counted |
| `run_record.json` | also lists the constructor, its settings and seed, the program, and `weather_case` |

Checks added to every run:
- **Errors:**
  - an event outside its program's season, window, length or call limits;
  - overlapping events;
  - more than one event on a day (v1.3: no grid-services program calls twice a day);
  - a schedule rule landing on a day that already has an event.
- **Warnings:**
  - delivered reduction larger than the devices' combined power;
  - same-day rebound after an event, in kWh;
  - settlement payment more than 25% away from the R2 load impact;
  - an accounted month more than 25% away from that month's counted capacity;
  - an event with an hour delivering under a quarter of the event's mean reduction
    against S (`dr skipped hours`, v1.3).

## 8. Uniform delivery (v1.3)

**Why.** Paid per kWh, an event hour's kWh of reduction is worth the same wherever it
lands, so the optimizer puts limited energy where the bill pays most. With NBT export
credits rising through the evening ($0.14/kWh at 16:00 to $0.46 at 19:00 in July), a
13.5 kWh battery skipped the first hour of every 4-hour event and delivered in three
(the July run, 4-hour events, 16:00-20:00). The reported mean reduction hid it. A
commitment program needs the same kW in every event hour, and sizing one needs the kW
the home can hold that way.

**A program setting, `delivery`:**

| `delivery` | `incentive` pays | Shadow events maximize |
|---|---|---|
| `per_kwh` (default; today's behavior) | each kWh below the expected baseline, in every event step | the total reduction over the block |
| `uniform` | the **held kW**: the reduction kept in *every* step of the event, times the event's hours | the held kW over the block |

- **Held kW, in the optimization:** one variable per event, at most the reduction below
  the expected baseline in each of the event's steps; the incentive pays
  `incentive_per_kwh x hours x held kW`. It's linear, so the solve stays an LP and its
  duals stay exact. Like `per_kwh`, it isn't floored in the optimization: an event whose
  worst hour can't get below the baseline (an EV still away, say) costs the optimizer
  rather than making the run infeasible; the reported settlement floors at zero. Reduction above the held kW in any hour earns only what the bill
  pays for it. (Prototyped on the July run: the reduction came out flat, 0.90 kW in each
  of the four hours on 8 July against 0, 2.36, 0.92, 0.30 per kWh, for the same
  incentive and $0.61 a month in bill value given up.)
- **`commitment` and `import_cap`** already ask for the same kW in every hour; `delivery`
  doesn't change them.
- **Settlement, as reported:** under `uniform`, the incentive earned is the held kW
  against the settlement baseline S (from the responding run's own history) times the
  event's hours, floored at zero (decision 10).
- **Under `uniform`, capacity is the held kW.** Shadow events maximize the kW held in
  every step of the standard event below R2 (on the program's basis), the total cut only
  breaking ties, and each counted value is the smallest step's reduction; accounted
  capacity averages the events' held kW.
- **Fitting a commitment:** with `delivery: uniform`, counted capacity's monthly minimum
  over eligible days (`counted_..._min_kw`, already written) is the kW the home could
  have committed to every day of that month, and accounted capacity is what it held in
  the events called.

**Reported for every program, whatever its `delivery`:**
- `dr_event_performance.csv` gains `held_kw` against R1, R2, R3 and S on both bases: the
  smallest step's reduction in the event, next to the mean already there.
- **A new warning, `dr skipped hours`:** events with an hour whose reduction below S is
  under a quarter of the event's mean (decision 11), with how many.
- **The report** shows the held kW beside the mean on the event-day chart, and the
  capacity chart says which delivery the program uses.

## Decisions (1-6 approved with version 1.0, 7 with 1.1, 8 with 1.2, 9-11 with 1.3)

1. **Capacity reference:** selectable per program (`capacity_reference`): R2, R3, S, or
   `all` (the default), each output side by side.
2. **Response:** `incentive` is the default; every output states the response used.
3. **Day-of adjustment:** 4 hours ending 1 hour before the event, capped at ±20%, for
   now (T1).
4. **Weekend and holiday settlement:** the same rule, with 4-in-4 when fewer than 10
   days exist, for now (T2).
5. **Counted capacity:** the average over eligible days; shadow events last `max_hours`.
6. **Randomized constructor:** as proposed in 1c.

7. **Exports (v1.1):** reported on both bases; neither is dropped.
8. **Settlement basis (v1.2):** a program setting, `net` (default) or `import`, for
   incentive and commitment.
9. **Delivery (v1.3):** `per_kwh` by default, so existing results don't change;
   `uniform` is set per program.
10. **Settlement under `uniform` (v1.3):** on the held kW against S, matching what the
    optimizer was paid.
11. **Skipped hours (v1.3):** an event hour under a quarter of the event's mean
    reduction against S.
12. **One event a day (v1.3):** a calendar with two events on a day is an error, whatever
    built it. Under `uniform`, an event is then each day's run of event hours.

## Threads to pull

Approved as placeholders; expect revisions, each as a new spec version:

- **T1. Settlement day-of adjustment.** The window and the ±20% cap for both 5-in-10
  (residential) and 10-in-10 (commercial) are provisional, not verified against the
  CAISO PDR baseline rules in use. Revise to the rules the program follows. Note when
  revising: a battery recharging inside the adjustment window raises the event day's
  load and can push the adjustment to its cap, so settlement pays for more than the
  load impact (seen in testing: +20%, $9.00 paid for $7.50 of avoided import).
- **T2. Weekend and holiday settlement.** The fallback (the same rule over recent weekend
  and holiday days, 4-in-4 when fewer than 10 exist) is provisional. Revise with T1.
