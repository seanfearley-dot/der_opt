from __future__ import annotations

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid


class V2XVehicle(Device):
    """Same SOC recursion, round-trip efficiency, and linear degradation cost as
    BatteryStorage, but bounded by a known plug-in schedule (`connected[t]`, a
    parameter -- not a decision variable) and an optional departure SOC requirement.
    MVP supports a single connect/disconnect window per horizon; multi-session support
    is a documented future extension."""

    def __init__(
        self,
        name: str,
        time_grid: TimeGrid,
        capacity_kwh: float,
        p_max_charge_kw: float,
        p_max_discharge_kw: float,
        connected,
        eta_charge: float = 0.95,
        eta_discharge: float = 0.95,
        soc_min_frac: float = 0.0,
        soc_max_frac: float = 1.0,
        soc_init_frac: float = 0.5,
        departure_period: int | None = None,
        soc_departure_min_frac: float | None = None,
        degradation_cost_per_kwh: float = 0.0,
    ):
        super().__init__(name, time_grid)
        conn = np.asarray(connected, dtype=float)
        if conn.shape[0] != time_grid.horizon_steps:
            raise ValueError("connected length must match time_grid.horizon_steps")
        self.connected = conn
        self.capacity_kwh = capacity_kwh
        self.p_max_charge_kw = p_max_charge_kw
        self.p_max_discharge_kw = p_max_discharge_kw
        self.eta_charge = eta_charge
        self.eta_discharge = eta_discharge
        self.soc_min = soc_min_frac * capacity_kwh
        self.soc_max = soc_max_frac * capacity_kwh
        self.soc_init = soc_init_frac * capacity_kwh
        self.departure_period = departure_period
        self.soc_departure_min = (
            soc_departure_min_frac * capacity_kwh if soc_departure_min_frac is not None else None
        )
        self.degradation_cost_per_kwh = degradation_cost_per_kwh

    def build_vars(self, model: pyo.ConcreteModel) -> None:
        periods = self.time_grid.periods
        soc_periods = range(self.time_grid.horizon_steps + 1)

        def ch_bounds(_m, t):
            return (0, self.p_max_charge_kw * self.connected[t])

        def dis_bounds(_m, t):
            return (0, self.p_max_discharge_kw * self.connected[t])

        self.p_ch = pyo.Var(periods, bounds=ch_bounds, domain=pyo.NonNegativeReals)
        self.p_dis = pyo.Var(periods, bounds=dis_bounds, domain=pyo.NonNegativeReals)
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

        if self.departure_period is not None and self.soc_departure_min is not None:
            departure_t = self.departure_period

            def departure_rule(_m):
                return self.soc[departure_t] >= self.soc_departure_min

            model.add_component(self._component_name("departure_soc"), pyo.Constraint(rule=departure_rule))

    def net_power_expr(self, model: pyo.ConcreteModel, t: int):
        return self.p_dis[t] - self.p_ch[t]

    def cost_expr(self, model: pyo.ConcreteModel, t: int):
        dt = self.time_grid.dt_hours
        return self.degradation_cost_per_kwh * (self.p_ch[t] + self.p_dis[t]) * dt

    def step(self, state: dict, action: dict) -> dict:
        t = state["t"]
        dt = self.time_grid.dt_hours
        soc = state["soc"]

        p_ch = min(max(action.get("p_ch", 0.0), 0.0), self.p_max_charge_kw * self.connected[t])
        p_dis = min(max(action.get("p_dis", 0.0), 0.0), self.p_max_discharge_kw * self.connected[t])

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
        return {
            "p_ch": np.array([pyo.value(self.p_ch[t]) for t in periods]),
            "p_dis": np.array([pyo.value(self.p_dis[t]) for t in periods]),
            "soc": np.array([pyo.value(self.soc[t]) for t in periods]),
            "net_power": np.array([pyo.value(self.p_dis[t] - self.p_ch[t]) for t in periods]),
        }
