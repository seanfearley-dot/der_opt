# der-opt EV daily pattern specification

**Version 1.0**, approved 2026-11-20: the recommended choice on decisions 1–4, and miles
with a kWh/mile efficiency for decision 5. Implemented in `der_opt.devices.ev_pattern`.

## Purpose

Today an EV in a single-home scenario needs three per-step inputs: a 0/1 plug-in
schedule (`connected`), a list of departure steps (`departure_period`, one per day,
365 for a year), and optionally driving energy (`driving_kwh`). A **daily pattern**
replaces all three with a few lines: when the car leaves and comes back on weekdays
and on weekends, how far it goes each day, and how much energy a mile takes. der-opt
builds the three inputs from it.

## 1. The `pattern` block

```yaml
devices:
  - type: ev_v2x
    name: ev
    params:
      capacity_kwh: 75
      p_max_charge_kw: 7.2
      p_max_discharge_kw: 7.2 # 0 for a car that can't discharge
      soc_init_frac: 0.8
      soc_departure_min_frac: 0.8 # applies at every departure the pattern makes
      pattern:
        weekday: {depart: "07:30", arrive: "18:00", miles: 35}
        weekend: {depart: "10:00", arrive: "16:00", miles: 15}
        kwh_per_mile: 0.30 # at the battery, so charging losses aren't in it (eta_charge is)
        holidays: weekend # PG&E holidays use the weekend pattern (decision 3)
```

- **`weekday`, `weekend`:** each is `{depart, arrive, miles}`, or the word `home`
  for days the car doesn't leave. Times are `HH:MM` on the local clock.
- **A day's trip** runs from that day's `depart` to the next `arrive` after it. If
  `arrive` is later in the day than `depart`, that's the same day (a day job); if it's
  earlier or equal, it's the next day (a night shift: depart 22:00, arrive 06:00). The
  day of departure decides which pattern applies, so Friday's trip uses the weekday
  pattern even if it ends Saturday.
- **`miles`** is how far that day's trip goes, and **`kwh_per_mile`** (required; one
  value for the car) is the energy a mile takes from the battery. A trip uses
  `miles × kwh_per_mile`, spread evenly over the steps the car is away (as synthetic
  feeder houses already do).
- **`holidays`:** `weekend` (default) or `weekday`.
- `pattern` can't be combined with `connected`, `departure_period` or `driving_kwh`:
  that's an error naming the clash. Any of those three on its own still works as today.
- `pattern` merges through `extends` like any mapping, so a fragment can hold a
  commuter pattern and a scenario can override only `weekend`.

## 2. What der-opt builds from it

- **`connected`:** 1 for every step the car is home for the whole step. A departure
  that isn't on a step boundary counts the step it falls in as away; an arrival counts
  its step as away too (the input data spec's `ev_connected` rule: plugged in for the
  whole interval).
- **Departures:** the step each trip starts. The departure charge level
  (`soc_departure_min_frac`) is required at every one.
- **`driving_kwh`:** each trip's kWh over its away steps.
- **The edges of the run:** if the run starts while the car is away, the trip that's
  already under way uses the part of its energy still ahead of it, and `soc_init_frac`
  is the charge at the run's start. A trip still under way when the run ends uses its
  energy for the steps inside the run.

## 3. Checks

`der-opt validate` (and so every run) adds:
- **Errors:**
  - times that aren't `HH:MM`;
  - `depart` equal to `arrive`;
  - `miles` below 0, or `kwh_per_mile` not above 0;
  - a day's trip energy (`miles × kwh_per_mile`) above the usable charge
    (`soc_max_frac - soc_min_frac` × capacity);
  - `pattern` together with `connected`, `departure_period` or `driving_kwh`.
- **Warning:** a home window too short to put back a day's driving energy at the
  charger's full rate (`p_max_charge_kw` × home hours × `eta_charge` < the trip's
  energy). The run would end up short of the departure charge, or not solve at all.
- **Note** (info), when a `depart` or `arrive` time isn't on the run's steps: how it
  was rounded and what that costs, e.g. "weekday: 07:30 departure counts as 07:00 on
  this 60-minute run; 0.5 h less plug-in per day; use 15- or 30-minute steps if the
  timing matters". Both round toward away (section 2): the run never counts charging
  the car couldn't do.

`der-opt run` prints one line for the EV: number of departures, miles and driving kWh
over the run, and hours plugged in on a weekday and a weekend day.

## 4. Also fixed (decision 4)

Where `departure_period` is left out, departures are found from `connected` (each step
where it goes from 1 to 0), as feeder houses built from files already do. Today the
departure charge level is then silently not enforced.

## 5. Not in this version

Possible later:
- day-to-day variation (times drawn around the pattern with a seed);
- more than one trip a day;
- per-weekday patterns (Monday to Sunday);
- patterns for feeder houses built from `premise_data`. Those read `ev_connected` and
  `ev_driving_kwh` from the time series file, as now.

## 6. Decisions

Approved 2026-11-20.

1. **Where the pattern goes:**
   - **a `pattern` block on the EV device that fills in all three inputs (recommended);**
   - a data source for `connected` only, like `ev_connected_schedule` today, which still
     leaves departures and driving energy to you.
2. **Which day's pattern a trip uses:**
   - **the day it departs (recommended);**
   - the day it arrives.
3. **Holidays:** **the weekend pattern by default (recommended)**, settable to weekday; or
   ignore holidays.
4. **Departures found from `connected` when `departure_period` is left out:** **yes, for
   every EV (recommended)**; or only for EVs using `pattern`.
5. **Driving energy:** kWh per day (recommended in the draft); or **miles per day with a
   kWh/mile efficiency (chosen)**.
