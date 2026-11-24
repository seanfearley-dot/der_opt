# der-opt EV departure shortfall specification

**Version 1.0**, approved 2026-11-23: a hard target by default, one price per kWh
short, a hard floor, no away charging, the shortfall an intangible cost reported apart
from bills, simulated runs measured but not priced, and shadow prices per departure
(section 6).

## Purpose

Today an EV must leave at or above its departure target (`soc_departure_min_frac`) and
must never run below `soc_min` while it drives. Both are hard constraints. When the
charging a feeder's EVs need can't fit under a rating:

- with a soft rating, the coordinated run overloads, and the congestion price pegs at
  `overload_cost_per_kwh`, a penalty, not a comparison of real options;
- with a hard limit (an emergency rating with no violation cost), the run is infeasible.

Leaving a car short of its target is a real option a utility or customer can choose:
the customer gives up some range, or buys the energy elsewhere. This spec lets each EV
keep today's hard target or price the shortfall, and combine the two: priced down to
a floor, hard below it.

## 1. The settings (per EV)

```yaml
devices:
  - type: ev_v2x
    name: ev
    params:
      capacity_kwh: 75
      soc_departure_min_frac: 0.9         # the target (as today)
      # left out: the target is hard (today's behavior)
      departure_shortfall_per_kwh: 0.50   # every kWh short of the target costs this
      soc_departure_floor_frac: 0.4       # hard below this, priced or not
```

- **`departure_shortfall_per_kwh`:** left out, the target is hard, as today; a number,
  every kWh short of the target at a departure costs this. One price, no bands
  (decision 2).
- **`soc_departure_floor_frac`:** a hard minimum at every departure, whether or not
  the target is priced. Default: `soc_min_frac`, i.e. no floor beyond the battery's
  own. It must be at or below the target.
- **Hard, priced, or both:**

  | Settings | At departure |
  |---|---|
  | target only | at least the target (today) |
  | target + price | below the target at the price, down to the battery's minimum |
  | target + price + floor | priced between target and floor, hard at the floor |
  | target + floor, no price | the higher of the two is hard: a floor adds nothing |

- **Where they're set:** any EV's params in a scenario; a study's `defaults` (e.g.
  `where: {type: ev}`) and `overrides`; the devices file as columns
  (`departure_shortfall_per_kwh`, `soc_departure_floor_frac`); an EV library block.
- **Every run that optimizes uses them:** a single home, a portfolio home, and every
  feeder run (coordinated, independent, mechanism runs). The problem stays an LP, so
  congestion prices stay exact.

## 2. Driving when the car left short

Driving stays hard: a car can't run below `soc_min` on its trip, so a car left short
must still have enough for the trip (decision 4: no away charging). Validation warns
when an EV's floor can't cover its longest trip (floor kWh − `soc_min` < the most it
drives between plug-ins), since an optimized run could then fail.

## 3. Rule-based (simulated) runs

A rule-based run has no mechanism that shapes charging, so a shortfall there isn't a
choice and has no cost or value to realize. It isn't priced. Two things are measured
and reported, for the record:

- each departure's kWh short of the target (a car home briefly, or a rule that held
  charging back);
- **driving not covered, kWh:** the simulator today clamps the battery at `soc_min`
  when driving needs more than is stored, which quietly creates energy. It's reported,
  with a warning.

## 4. What's reported

- **Per departure,** in `ev_departures.csv` (every run, every EV with a target):
  premise, device, run, departure time, target kWh, floor kWh, stored kWh, kWh short,
  the shortfall cost, and, for optimized runs that are LPs, the **departure shadow
  price**: what one more kWh at that departure would cost the run. A hard target that
  binds shows its price here; a priced one that's short shows the shortfall price.
  Runs with on/off variables (a single meter) leave it blank.
- **An intangible cost, apart from bills** (decision 5). The optimizer weighs it, so
  it's in each run's total, on its own line: **EV shortfall (intangible)**. It never
  changes a bill, and **who pays more under coordination** compares bills (and
  degradation) only; kWh short is shown beside it.
- **Customers:** `customers.csv` gains `ev_shortfall_kwh` and `ev_shortfall_cost`.
- **Premise pages:** kWh short and its cost by run, and the departures marked on the
  stored energy chart against the target.
- **Single home:** the value breakdown gains an `<ev> shortfall` line where it's
  nonzero.
- **Congestion prices** now settle at the cheapest margin: other flexibility, overload,
  or a shortfall. A kWh short at the battery is `1 / eta_charge` kWh less at the meter,
  so a shortfall price `p` caps a congestion price near `p × eta_charge` in the hours
  that charging would have used.
- **Checks:** a warning naming every EV left short in the coordinated run (departures,
  kWh, cost); a warning naming EVs left short in a run where each home optimizes its
  own bill (its shortfall price is below what charging costs it, which may be what you
  want); driving not covered in a simulated run; errors for a negative price, a floor
  above the target, or a price with no target.

## 5. Not in this version

- **Bands** of shortfall prices, and **away charging** (decisions 2 and 4).
- **Cost tests and DR settlement** see the shortfall only through the reported totals.
- **Shortfall by time of day or trip purpose,** and range anxiety that depends on how
  often a customer is left short.

## 6. Decisions

All approved 2026-11-23.

1. **Default:** an EV with no new setting behaves exactly as today (a hard target).
2. **Pricing:** one price per kWh short. No bands.
3. **Floor:** `soc_departure_floor_frac`, default the battery's own minimum.
4. **Driving when short:** stays hard; no away charging. Validation warns when the
   floor can't cover the longest trip.
5. **Whose cost:** an intangible cost, reported on its own line, in run totals (the
   optimizer weighs it) but never in a bill or in who pays more.
6. **Simulated runs:** not priced (nothing shapes their charging); kWh short and driving
   not covered are measured and reported.
7. **Shadow prices:** reported per departure for optimized runs that are LPs.
