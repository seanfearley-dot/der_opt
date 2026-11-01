from __future__ import annotations

import pandas as pd

from der_opt.devices.solar import SolarPV
from der_opt.premise.premise import Premise
from der_opt.results.schema import DispatchResult
from der_opt.simulation.policy import ControlPolicy
from der_opt.simulation.state import PremiseState


class CounterfactualSimulator:
    """Steps a Premise's devices period-by-period using each device's numeric step(),
    driven by a ControlPolicy per device (or that device's own passive default when
    none is given) -- no Pyomo solve, so this is fast even run repeatedly. Produces a
    DispatchResult in the same schema as Premise.solve()'s output, so every viz
    function works on either unmodified.

    Note: objective_value here only totals device-intrinsic costs (e.g. battery/EV
    degradation) -- it does not yet evaluate the premise's tariff/arbitrage ValueStreams
    against the realized dispatch. That numeric evaluation belongs with the value-stack
    and comparison visualizations that actually need it, not here.
    """

    def __init__(self, premise: Premise, policies: dict[str, ControlPolicy] | None = None):
        self.premise = premise
        self.policies = policies or {}

    def _observations_at(self, t: int) -> dict:
        solar_kw = sum(
            device.available_kw[t]
            for device in self.premise.devices
            if isinstance(device, SolarPV)
        )
        return {"t": t, "load_kw": self.premise.external_load[t], "solar_kw": solar_kw}

    def run(self) -> DispatchResult:
        premise = self.premise
        time_grid = premise.time_grid

        state = PremiseState(
            t=0, device_states={device.name: device.initial_state() for device in premise.devices}
        )
        device_series: dict[str, dict[str, list]] = {device.name: {} for device in premise.devices}
        grid_import: list[float] = []
        grid_export: list[float] = []
        total_device_cost = 0.0

        for t in time_grid.periods:
            observations = self._observations_at(t)
            device_power_total = 0.0

            for device in premise.devices:
                policy = self.policies.get(device.name)
                current_state = state.device_states[device.name]
                action = policy.decide(current_state, observations, t) if policy else {}

                step_state = dict(current_state)
                step_state["t"] = t
                result = device.step(step_state, action)

                device_power_total += result.get("net_power", 0.0)
                total_device_cost += result.get("cost", 0.0)
                for key, value in result.items():
                    device_series[device.name].setdefault(key, []).append(value)

                state.device_states[device.name] = result

            net = premise.external_load[t] - device_power_total
            grid_import.append(max(net, 0.0))
            grid_export.append(max(-net, 0.0))

        try:
            time_index = pd.DatetimeIndex(time_grid.timestamps())
        except ValueError:
            time_index = None
        index = time_index if time_index is not None else list(time_grid.periods)

        device_columns = {
            f"{name}.{key}": values
            for name, series in device_series.items()
            for key, values in series.items()
        }
        device_dispatch = pd.DataFrame(device_columns, index=index)

        return DispatchResult(
            premise_name=premise.name,
            time_index=time_index,
            device_dispatch=device_dispatch,
            grid_import=pd.Series(grid_import, index=index),
            grid_export=pd.Series(grid_export, index=index),
            objective_value=total_device_cost,
            solver_status="simulated",
        )
