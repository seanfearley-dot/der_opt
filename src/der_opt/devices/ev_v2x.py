from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pyomo.environ as pyo

from der_opt.devices.base import Device
from der_opt.time_grid import TimeGrid

if TYPE_CHECKING:
    from der_opt.devices.solar import SolarPV


class V2XVehicle(Device):
    """Same SOC recursion, round-trip efficiency, and linear degradation cost as
    BatteryStorage, but bounded by a known plug-in schedule (`connected[t]`, a
    parameter -- not a decision variable) and departure SOC requirements.

    `departure_period` / `soc_departure_min_frac` each accept either a single value
    (the original single-cycle MVP shape) or a list, for a horizon spanning multiple
    connect/disconnect cycles (e.g. a multi-day run with one departure per morning).
    When `soc_departure_min_frac` is a single float with a list of departure_period
    values, that same requirement applies to every departure. Without this, only the
    first cycle's departure would be checked and every later one would silently have
    no SOC requirement at all -- a real gap, not a hypothetical: feeding a 3-day
    horizon a single departure_period checked only day 1's morning.

    `soc_final_min_frac`: same purpose as BatteryStorage's -- without it, nothing stops
    the optimizer discharging the vehicle right down to soc_min by the end of the
    horizon for export revenue, even *after* its last departure_period requirement is
    already satisfied earlier in the same horizon (a real finding, not hypothetical:
    a V2X discharge for an evening price spike, after that day's departure requirement
    is already met, has nothing requiring it be recharged afterward -- set this to
    require the vehicle end the horizon ready for its next trip too).

    `charge_sources`: optional list of SolarPV devices this vehicle may charge from --
    same instantaneous-cap mechanism and caveats as BatteryStorage's."""

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
        departure_period: int | list[int] | None = None,
        soc_departure_min_frac: float | list[float] | None = None,
        soc_final_min_frac: float | None = None,
        degradation_cost_per_kwh: float = 0.0,
        charge_sources: list[SolarPV] | None = None,
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

        departure_periods = (
            [] if departure_period is None
            else list(departure_period) if isinstance(departure_period, (list, tuple))
            else [departure_period]
        )
        if soc_departure_min_frac is None:
            departure_mins = [None] * len(departure_periods)
        elif isinstance(soc_departure_min_frac, (list, tuple)):
            if len(soc_departure_min_frac) != len(departure_periods):
                raise ValueError(
                    "soc_departure_min_frac list must be the same length as "
                    f"departure_period ({len(departure_periods)}), got {len(soc_departure_min_frac)}"
                )
            departure_mins = [f * capacity_kwh if f is not None else None for f in soc_departure_min_frac]
        else:
            departure_mins = [soc_departure_min_frac * capacity_kwh] * len(departure_periods)
        self.departure_periods = departure_periods
        self.soc_departure_mins = departure_mins

        self.soc_final_min = (
            soc_final_min_frac * capacity_kwh if soc_final_min_frac is not None else None
        )
        self.degradation_cost_per_kwh = degradation_cost_per_kwh
        self.charge_sources = list(charge_sources) if charge_sources else []

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

        def _make_departure_rule(departure_t, soc_min):
            # A factory, not an inline closure in the loop below: each call gets its
            # own departure_t/soc_min cell, avoiding the classic Python late-binding
            # bug where every rule would otherwise end up reading the *last*
            # iteration's values. The inner rule takes a single arg (matching every
            # other scalar-Constraint rule in this codebase) -- Pyomo's own calling
            # convention for a scalar Constraint passes a second (None) positional
            # arg, which collided with an earlier attempt to capture via a default
            # keyword argument instead of this factory.
            def rule(_m):
                return self.soc[departure_t] >= soc_min

            return rule

        for idx, (departure_t, soc_min) in enumerate(zip(self.departure_periods, self.soc_departure_mins)):
            if soc_min is None:
                continue
            model.add_component(
                self._component_name(f"departure_soc_{idx}"),
                pyo.Constraint(rule=_make_departure_rule(departure_t, soc_min)),
            )

        if self.soc_final_min is not None:
            final_t = self.time_grid.horizon_steps

            def soc_final_rule(_m):
                return self.soc[final_t] >= self.soc_final_min

            model.add_component(self._component_name("soc_final"), pyo.Constraint(rule=soc_final_rule))

        if self.charge_sources:
            # See BatteryStorage.build_constraints for the full rationale and caveats
            # (conservative instantaneous cap, not true per-kWh source accounting).
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
