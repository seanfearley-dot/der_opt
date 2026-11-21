"""First-year screening values (avoided cost spec section 3; cost-test spec section 5):
the measure's analysis-year impacts at an avoided cost set's values. These are
screening values for sweeps and quick comparisons -- not cost-test results, which come
from OpenBCA.

  energy       sum over steps of (without - with) kWh x each energy-class component,
               split into hours the measure lowers load (benefit) and raises it
               (increased supply cost)
  dr_capacity  from capacity.csv only: price_per_kw_year x sum over months (share x the
               month's DR capacity kW), for each capacity component
  full_hourly  the hourly sum with every component, capacity included -- a comparison
               figure, never added to dr_capacity
  emissions    sum of (without - with) MWh x marginal tCO2/MWh
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from der_opt.avoided_costs.sets import AvoidedCostSet


def _hourly_on_steps(acs: AvoidedCostSet, index: pd.DatetimeIndex) -> pd.DataFrame:
    """The set's values at each run step (local clock), by the hour the step falls in."""
    years = sorted(set(index.year))
    table = pd.concat([acs.year_local(y) for y in years])
    return table.reindex(index.floor("h")).set_axis(index)


def screening_values(acs: AvoidedCostSet, without: pd.Series, with_: pd.Series, dt_hours: float,
                     capacity_kw: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """One row per value: category, component, part, value_usd (or tCO2), and for
    DR capacity, which capacity measure (`R2/import/counted`, ...)."""
    index = pd.DatetimeIndex(with_.index)
    kwh = (without.to_numpy() - with_.to_numpy()) * dt_hours
    values = _hourly_on_steps(acs, index)
    rows = []
    for kind, category in (("energy", "energy"), (None, "full_hourly")):
        names = acs.components_of("energy") + ([] if kind else acs.components_of("capacity"))
        for name in names:
            v = values[name].to_numpy()
            lowered = float((np.clip(kwh, 0, None) * v).sum())
            raised = float((np.clip(kwh, None, 0) * v).sum())
            rows += [
                {"category": category, "component": name, "part": "load lowered", "value": lowered, "unit": "$"},
                {"category": category, "component": name, "part": "load raised", "value": raised, "unit": "$"},
            ]
    if acs.manifest.emissions:
        e = values[acs.manifest.emissions].to_numpy()
        rows.append({"category": "emissions", "component": acs.manifest.emissions, "part": "net",
                     "value": float((kwh / 1000 * e).sum()), "unit": "tCO2"})
    year = int(index[0].year)
    for label, monthly in (capacity_kw or {}).items():
        kw = monthly.reindex(range(1, 13)).fillna(0.0).to_numpy()
        for component in ("generation", "transmission", "distribution"):
            priced = acs.capacity_price(year, component)
            if priced is None:
                continue
            price, shares = priced
            rows.append({"category": "dr_capacity", "component": component, "part": label,
                         "value": float(price * (shares * kw).sum()), "unit": "$"})
    frame = pd.DataFrame(rows)
    frame.insert(0, "set_id", acs.manifest.set_id)
    frame.insert(1, "year", year)
    return frame


def capacity_measures(capacity: pd.DataFrame) -> dict[str, pd.Series]:
    """Monthly DR capacity kW by `reference/basis/measure`, from a DR study's capacity
    table (counted and accounted, net and import, R2/R3/S as reported)."""
    out = {}
    for measure in ("counted", "accounted"):
        for basis, pre in (("net", ""), ("import", "import_")):
            for reference in ("R2", "R3", "S"):
                column = f"{measure}_{pre}{reference}_kw"
                if column in capacity:
                    out[f"{reference}/{basis}/{measure}"] = capacity.set_index("month")[column].fillna(0.0)
    return out
