from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np
import pyomo.environ as pyo

from der_opt.premise.objective import ValueStream
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.premise.premise import Premise


@dataclass(frozen=True)
class EnergyTier:
    """One tier of a (possibly tiered) energy rate. `max_kwh` is the usage threshold,
    in kWh *within a single dispatch period*, up to which this tier's rate applies
    (None = unbounded, i.e. the top tier). This toolkit does not track cumulative
    monthly usage across periods, so true monthly-cumulative tiered billing (the
    common real-world case) is approximated as a per-period tier structure -- a
    documented limitation, not an oversight; see TariffEnergyCharge."""

    rate: float
    max_kwh: float | None = None


@dataclass(frozen=True)
class TOUPeriodSchedule:
    """Maps (month, hour, is_weekend) -> a TOU period index, via two 12x24 tables, the
    same convention OpenEI's URDB uses (energyweekdayschedule/energyweekendschedule)."""

    weekday_schedule: list[list[int]]  # [month 0-11][hour 0-23] -> period index
    weekend_schedule: list[list[int]]

    def period_for(self, month: int, hour: int, is_weekend: bool) -> int:
        table = self.weekend_schedule if is_weekend else self.weekday_schedule
        return table[month][hour]

    @classmethod
    def constant(cls, period_index: int = 0) -> TOUPeriodSchedule:
        table = [[period_index] * 24 for _ in range(12)]
        return cls(weekday_schedule=table, weekend_schedule=[row[:] for row in table])


def _validate_nondecreasing_tiers(periods: dict[int, list[EnergyTier]]) -> None:
    for period_idx, tiers in periods.items():
        rates = [tier.rate for tier in tiers]
        if any(rates[i] > rates[i + 1] for i in range(len(rates) - 1)):
            raise ValueError(
                f"Energy period {period_idx} has a declining-block tier structure "
                "(a later tier's rate is lower than an earlier one's), which "
                "TariffEnergyCharge's convex linearization does not support -- it relies "
                "on a cost-minimizer always filling the cheaper tier first, which only "
                "holds when tier rates are non-decreasing."
            )
        for tier in tiers[:-1]:
            if tier.max_kwh is None:
                raise ValueError("Only the last tier in a period may have max_kwh=None")


class TariffSchedule:
    """The rate structure actually embedded in the optimization. TOU energy rates
    (with optional per-period tiers), a flat export rate, and a flat demand charge.
    Import via TariffSchedule.from_urdb_json() for a real utility rate, or build one
    directly with flat_tou() for a hand-specified TOU-only rate."""

    def __init__(
        self,
        energy_periods: dict[int, list[EnergyTier]],
        energy_schedule: TOUPeriodSchedule,
        export_rate: float = 0.0,
        demand_charge_rate: float | None = None,
    ):
        _validate_nondecreasing_tiers(energy_periods)
        self.energy_periods = energy_periods
        self.energy_schedule = energy_schedule
        self.export_rate = export_rate
        self.demand_charge_rate = demand_charge_rate

    @classmethod
    def flat_tou(
        cls,
        rates_by_period: dict[int, float],
        schedule: TOUPeriodSchedule,
        export_rate: float = 0.0,
        demand_charge_rate: float | None = None,
    ) -> TariffSchedule:
        """Convenience constructor for a non-tiered TOU tariff: one rate per period."""
        energy_periods = {idx: [EnergyTier(rate=rate)] for idx, rate in rates_by_period.items()}
        return cls(energy_periods, schedule, export_rate=export_rate, demand_charge_rate=demand_charge_rate)

    @classmethod
    def from_urdb_json(cls, path_or_dict) -> TariffSchedule:
        """Imports a tariff from an OpenEI URDB JSON record (a single rate, not the
        wrapped {"items": [...]} API response). Covers energyweekdayschedule/
        energyweekendschedule + energyratestructure (TOU + per-period tiers), `sell`
        for export compensation, and flatdemandstructure/flatdemandmonths for a flat
        monthly demand charge. NOT covered: TOU demand charges (demandratestructure),
        tiered demand charges, fixed monthly charges, multiple meters/tariff variants.
        URDB entries are community-maintained and manually transcribed from tariff
        sheets -- treat this as a reference, not a substitute for the actual tariff
        sheet, when accuracy matters."""
        if isinstance(path_or_dict, (str, Path)):
            data = json.loads(Path(path_or_dict).read_text())
        else:
            data = path_or_dict
        if "items" in data:
            data = data["items"][0]

        energy_periods: dict[int, list[EnergyTier]] = {}
        export_rate = 0.0
        for idx, tiers in enumerate(data["energyratestructure"]):
            parsed_tiers = []
            for tier in tiers:
                rate = tier.get("rate", 0.0) + tier.get("adj", 0.0)
                parsed_tiers.append(EnergyTier(rate=rate, max_kwh=tier.get("max")))
                if "sell" in tier:
                    export_rate = max(export_rate, tier["sell"])
            energy_periods[idx] = parsed_tiers

        energy_schedule = TOUPeriodSchedule(
            weekday_schedule=data["energyweekdayschedule"],
            weekend_schedule=data["energyweekendschedule"],
        )

        demand_charge_rate = None
        flat_structure = data.get("flatdemandstructure")
        if flat_structure:
            # flatdemandmonths maps each of the 12 months to a structure index; use the
            # first tier of the first month's structure as a single representative flat
            # rate (sufficient for a horizon that doesn't span multiple demand-charge
            # seasons -- a full monthly-varying flat demand charge is a future extension).
            month_structures = data.get("flatdemandmonths", [0] * 12)
            demand_charge_rate = flat_structure[month_structures[0]][0]["rate"]

        return cls(
            energy_periods,
            energy_schedule,
            export_rate=export_rate,
            demand_charge_rate=demand_charge_rate,
        )

    def is_tiered(self) -> bool:
        return any(len(tiers) > 1 for tiers in self.energy_periods.values())

    def period_index_at(self, time_grid: TimeGrid, t: int) -> int:
        timestamp = time_grid.timestamps()[t]
        is_weekend = timestamp.weekday() >= 5
        return self.energy_schedule.period_for(timestamp.month - 1, timestamp.hour, is_weekend)

    def tiers_at(self, time_grid: TimeGrid, t: int) -> list[EnergyTier]:
        return self.energy_periods[self.period_index_at(time_grid, t)]

    def energy_rate_array(self, time_grid: TimeGrid, stream: str = "import") -> np.ndarray:
        """Per-period $/kWh array for a non-tiered tariff. Raises if any period in this
        horizon has more than one tier -- use TariffEnergyCharge for a tiered tariff."""
        if stream == "export":
            return np.full(time_grid.horizon_steps, self.export_rate, dtype=float)
        rates = []
        for t in time_grid.periods:
            tiers = self.tiers_at(time_grid, t)
            if len(tiers) > 1:
                raise ValueError(
                    f"Period index {self.period_index_at(time_grid, t)} has {len(tiers)} tiers; "
                    "a tiered tariff can't be flattened into a fixed price array. Use "
                    "TariffEnergyCharge, which embeds the tiers as optimization constraints."
                )
            rates.append(tiers[0].rate)
        return np.array(rates, dtype=float)


class TariffEnergyCharge(ValueStream):
    """Embeds a (possibly tiered) TOU energy tariff directly into the optimization. For
    tiers with non-decreasing marginal price, grid_import[t] (or grid_export[t]) is
    allocated across per-tier variables that sum to it; because cheaper tiers are always
    filled first by a cost-minimizer, this is LP-exact under that non-decreasing
    assumption (validated at TariffSchedule construction time)."""

    def __init__(self, tariff: TariffSchedule, time_grid: TimeGrid, direction: str = "import"):
        if direction not in ("import", "export"):
            raise ValueError("direction must be 'import' or 'export'")
        self.tariff = tariff
        self.time_grid = time_grid
        self.direction = direction
        self._period_index = [tariff.period_index_at(time_grid, t) for t in time_grid.periods]
        self._max_tiers = max(
            len(tariff.energy_periods[idx]) for idx in self._period_index
        )

    def apply(self, model: pyo.ConcreteModel, premise: Premise) -> None:
        periods = self.time_grid.periods
        tier_var = pyo.Var(periods, range(self._max_tiers), domain=pyo.NonNegativeReals)
        self.tier_kwh = tier_var
        model.add_component(f"{premise.name}_tariff_{self.direction}_tier", tier_var)

        bus_var = premise.grid_import if self.direction == "import" else premise.grid_export
        dt = self.time_grid.dt_hours

        def alloc_rule(_m, t):
            tiers = self.tariff.energy_periods[self._period_index[t]]
            return sum(tier_var[t, k] for k in range(len(tiers))) == bus_var[t] * dt

        model.add_component(
            f"{premise.name}_tariff_{self.direction}_alloc",
            pyo.Constraint(periods, rule=alloc_rule),
        )

        def bound_rule(_m, t, k):
            tiers = self.tariff.energy_periods[self._period_index[t]]
            if k >= len(tiers):
                return tier_var[t, k] == 0
            tier = tiers[k]
            if tier.max_kwh is None:
                return pyo.Constraint.Skip
            prev_max = tiers[k - 1].max_kwh if k > 0 else 0.0
            return tier_var[t, k] <= tier.max_kwh - prev_max

        model.add_component(
            f"{premise.name}_tariff_{self.direction}_tier_bound",
            pyo.Constraint(periods, range(self._max_tiers), rule=bound_rule),
        )

    def cost_expr(self, model: pyo.ConcreteModel, premise: Premise, t: int):
        tiers = self.tariff.energy_periods[self._period_index[t]]
        tier_cost = sum(tiers[k].rate * self.tier_kwh[t, k] for k in range(len(tiers)))
        if self.direction == "export":
            return -tier_cost
        return tier_cost


class DemandCharge(ValueStream):
    """cost = rate_per_kw * peak_demand, where peak_demand >= grid_import[t] for every
    t in the horizon. For a horizon shorter than a full billing month, this values this
    dispatch window's contribution to the monthly peak; carrying a running peak across a
    rolling-horizon sequence of solves is a future extension, not needed for a
    single-horizon solve."""

    def __init__(self, rate_per_kw: float):
        self.rate_per_kw = rate_per_kw

    def apply(self, model: pyo.ConcreteModel, premise: Premise) -> None:
        peak_var = pyo.Var(domain=pyo.NonNegativeReals)
        self.peak_demand = peak_var
        model.add_component(f"{premise.name}_demand_charge_peak", peak_var)

        def peak_rule(_m, t):
            return peak_var >= premise.grid_import[t]

        model.add_component(
            f"{premise.name}_demand_charge_peak_constraint",
            pyo.Constraint(premise.time_grid.periods, rule=peak_rule),
        )

    def total_cost_expr(self, model: pyo.ConcreteModel, premise: Premise):
        return self.rate_per_kw * self.peak_demand


class CapacityReliabilityPayment(ValueStream):
    """Revenue for committing to have at least `committed_kw` of reducible/dischargeable
    capacity available during named event periods, paid at a flat $/kW rate per event.
    This stream only credits the revenue; it does not itself enforce the reserve -- pair
    it with a device-level constraint (e.g. a battery's reserved SOC) to make the
    commitment binding."""

    def __init__(self, event_periods: list[int], payment_per_kw_event: float, committed_kw: float):
        self.event_periods = event_periods
        self.payment_per_kw_event = payment_per_kw_event
        self.committed_kw = committed_kw

    def total_cost_expr(self, model: pyo.ConcreteModel, premise: Premise):
        return -self.payment_per_kw_event * self.committed_kw * len(self.event_periods)
