"""Optional offline cross-check of this toolkit's own TariffSchedule-based cost against
NREL PySAM's UtilityRate5 -- an independent SAM bill calculator, not the LP/MILP-
embeddable TariffEnergyCharge machinery used inside the optimization itself. Never
called from premise/tariff.py or the solve loop; call it after a solve, on the result.

Requires the optional NREL-PySAM dependency: `pip install 'der-opt[validation]'`.
"""

from __future__ import annotations

import json
from pathlib import Path

from der_opt.results.schema import DispatchResult
from der_opt.time_grid import TimeGrid

_HOURS_PER_YEAR = 8760


def crosscheck_annual_bill(
    dispatch: DispatchResult, time_grid: TimeGrid, urdb_json_path: str
) -> dict:
    """Runs PySAM's UtilityRate5 on a solved/simulated DispatchResult's realized grid
    import/export (mapped to PySAM's Load.load / SystemOutput.gen -- verified these net
    the same way our own POI balance does: PySAM's with/without-system bill difference
    tracks the gen array exactly as expected), using the same URDB tariff JSON
    TariffSchedule.from_urdb_json would use for the optimization itself.

    Needs a full 8760-hour, hourly TimeGrid -- PySAM computes an *annual* bill, which
    isn't a meaningful number for a sub-year horizon, so this raises rather than
    silently returning a misleading figure for one. Returns PySAM's own bill
    (utility_bill_w_sys_year1/_wo_sys_year1); compare against the tariff-related
    entries of der_opt.pricing.evaluate.value_breakdown(premise, dispatch) yourself --
    this function doesn't do that comparison, since value_breakdown also includes
    non-tariff costs (e.g. device degradation) that PySAM's bill calculator has no
    concept of.
    """
    try:
        import PySAM.Utilityrate5 as utility_rate5
        import PySAM.UtilityRateTools as urdb_tools
    except ImportError as exc:
        raise ImportError(
            "crosscheck_annual_bill requires the optional 'NREL-PySAM' dependency: "
            "pip install 'der-opt[validation]'"
        ) from exc

    if time_grid.resolution_minutes != 60 or time_grid.horizon_steps != _HOURS_PER_YEAR:
        raise ValueError(
            "crosscheck_annual_bill needs a full 8760-hour, hourly TimeGrid to compute "
            f"a meaningful annual bill -- got resolution_minutes={time_grid.resolution_minutes}, "
            f"horizon_steps={time_grid.horizon_steps}"
        )

    urdb_data = json.loads(Path(urdb_json_path).read_text(encoding="utf-8"))
    if "items" in urdb_data:
        urdb_data = urdb_data["items"][0]

    model = utility_rate5.new()
    model.ElectricityRates.assign(urdb_tools.URDBv8_to_ElectricityRates(urdb_data))
    model.Lifetime.analysis_period = 1
    model.Lifetime.inflation_rate = 2.5
    model.Lifetime.system_use_lifetime_output = 0
    model.SystemOutput.degradation = [0.0]
    model.Load.load = dispatch.grid_import.to_numpy().tolist()
    model.SystemOutput.gen = dispatch.grid_export.to_numpy().tolist()
    model.execute()

    return {
        "utility_bill_w_sys_year1": model.Outputs.utility_bill_w_sys_year1,
        "utility_bill_wo_sys_year1": model.Outputs.utility_bill_wo_sys_year1,
    }
