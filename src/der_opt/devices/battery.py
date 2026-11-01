from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.devices.solar import SolarPV


class BatteryStorage(Device):
    """SOC dynamics with round-trip efficiency losses on both legs, plus a linear
    $/kWh-throughput degradation cost (same construct DER-VET/StorageVET use, chosen so
    duals on the surrounding problem stay LP-exact). No simultaneous charge/discharge
    binary is needed: with eta_charge, eta_discharge < 1, charging and discharging at
    once is pure loss, so a cost-minimizing LP never chooses both (see test_battery.py).

    `charge_sources`: optional list of SolarPV devices this battery may charge from.
    When given, charging is capped at those devices' concurrent generation each period
    -- see build_constraints for what this does and doesn't guarantee.
    """

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        capacity_kwh: float,
        p_max_charge_kw: float,
        p_max_discharge_kw: float,
        eta_charge: float = 0.95,
        eta_discharge: float = 0.95,
        soc_min_frac: float = 0.0,
        soc_max_frac: float = 1.0,
        soc_init_frac: float = 0.5,
        soc_final_min_frac: float | None = None,
        degradation_cost_per_kwh: float = 0.0,
        charge_sources: list[SolarPV] | None = None,
    ):
        super().__init__(name, time_grid)
        self.capacity_kwh = capacity_kwh
        self.p_max_charge_kw = p_max_charge_kw
        self.p_max_discharge_kw = p_max_discharge_kw
        self.eta_charge = eta_charge
        self.eta_discharge = eta_discharge
        self.soc_min = soc_min_frac * capacity_kwh
        self.soc_max = soc_max_frac * capacity_kwh
        self.soc_init = soc_init_frac * capacity_kwh
        self.soc_final_min = (
            soc_final_min_frac * capacity_kwh if soc_final_min_frac is not None else None
        )
        self.degradation_cost_per_kwh = degradation_cost_per_kwh
        self.charge_sources = list(charge_sources) if charge_sources else []

    def build_vars(self, model: pyo.ConcreteModel) -> None:
        periods = self.time_grid.periods
        soc_periods = range(self.time_grid.horizon_steps + 1)
        self.p_ch = pyo.Var(periods, bounds=(0, self.p_max_charge_kw), domain=pyo.NonNegativeReals)
        self.p_dis = pyo.Var(periods, bounds=(0, self.p_max_discharge_kw), domain=pyo.NonNegativeReals)
        self.soc = pyo.Var(soc_periods, bounds=(self.soc_min, self.soc_max), domain=pyo.NonNegativeReals)
        model.add_component(self._component_name("p_ch"), self.p_ch)
        model.add_component(self._component_name("p_dis"), self.p_dis)
        model.add_component(self._component_name("soc"), self.soc)

    def build_constraints(self, model: pyo.ConcreteModel) -> None:
        dt = self.time_grid.dt_hours

        def soc_init_rule(_m):
            return self.soc[0] == self.soc_init

        model.add_component(self._component_name("soc_init"), pyo.Constraint(rule=soc_init_rule))

        def soc_dynamics_rule(_m, t):
            return self.soc[t + 1] == self.soc[t] + dt * (
                self.eta_charge * self.p_ch[t] - self.p_dis[t] / self.eta_discharge
            )

        model.add_component(
            self._component_name("soc_dynamics"),
            pyo.Constraint(self.time_grid.periods, rule=soc_dynamics_rule),
        )

        if self.soc_final_min is not None:
            final_t = self.time_grid.horizon_steps

            def soc_final_rule(_m):
                return self.soc[final_t] >= self.soc_final_min

            model.add_component(self._component_name("soc_final"), pyo.Constraint(rule=soc_final_rule))

        if self.charge_sources:
            # Caps charge power by concurrent generation from the named source(s), so
            # the battery can never charge faster than they're producing at that
            # instant -- the practical mechanism some BTM storage programs require
            # (e.g. California's SGIP) to prevent a battery from importing grid power
            # and reselling it at the export price. This is a conservative instantaneous
            # proxy, not true per-kWh source accounting (power is fungible at a bus, so
            # "this kWh came from solar" isn't really well-defined once load is also
            # drawing on the same generation) -- it doesn't separately check whether
            # load is also competing for that same solar output at that instant, only
            # that the battery's charge rate itself never exceeds it. Each source
            # device must already have build_vars() called (true automatically inside
            # Premise.register(), which registers all devices' vars before any
            # device's constraints) for `src.p_gen` to exist.
            def solar_only_charge_rule(_m, t):
                return self.p_ch[t] <= sum(src.p_gen[t] for src in self.charge_sources)

            model.add_component(
                self._component_name("charge_from_sources_only"),
                pyo.Constraint(self.time_grid.periods, rule=solar_only_charge_rule),
            )

    def net_power_expr(self, model: pyo.ConcreteModel, t: int):
        return self.p_dis[t] - self.p_ch[t]

    def initial_state(self) -> dict:
        return {"soc": self.soc_init}

    def cost_expr(self, model: pyo.ConcreteModel, t: int):
        dt = self.time_grid.dt_hours
        return self.degradation_cost_per_kwh * (self.p_ch[t] + self.p_dis[t]) * dt

    def step(self, state: dict, action: dict) -> dict:
        dt = self.time_grid.dt_hours
        soc = state["soc"]

        p_ch = min(max(action.get("p_ch", 0.0), 0.0), self.p_max_charge_kw)
        p_dis = min(max(action.get("p_dis", 0.0), 0.0), self.p_max_discharge_kw)

        max_ch_room = (self.soc_max - soc) / (dt * self.eta_charge)
        p_ch = min(p_ch, max(max_ch_room, 0.0))
        max_dis_room = (soc - self.soc_min) * self.eta_discharge / dt
        p_dis = min(p_dis, max(max_dis_room, 0.0))

        next_soc = soc + dt * (self.eta_charge * p_ch - p_dis / self.eta_discharge)
        next_soc = min(max(next_soc, self.soc_min), self.soc_max)

        return {
            "soc": next_soc,
            "p_ch": p_ch,
            "p_dis": p_dis,
            "net_power": p_dis - p_ch,
            "cost": self.degradation_cost_per_kwh * (p_ch + p_dis) * dt,
        }

    def dispatch_series(self, model: pyo.ConcreteModel) -> dict[str, np.ndarray]:
        periods = self.time_grid.periods
        p_ch = np.array([pyo.value(self.p_ch[t]) for t in periods])
        p_dis = np.array([pyo.value(self.p_dis[t]) for t in periods])
        dt = self.time_grid.dt_hours
        return {
            "p_ch": p_ch,
            "p_dis": p_dis,
            "soc": np.array([pyo.value(self.soc[t]) for t in periods]),
            "net_power": p_dis - p_ch,
            "cost": self.degradation_cost_per_kwh * (p_ch + p_dis) * dt,
        }
