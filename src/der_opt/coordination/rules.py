"""Rule-based responses (docs/COORDINATION_SPEC.md section 1a): a participant's devices
keep their baseline rules, and each mechanism overrides them by rule, step by step,
without looking ahead.

Each step:
  1. every device's baseline rule picks its action;
  2. active DR-event and price-signal rules replace it (when both are active for one
     device, the one that draws less from the grid wins);
  3. if the home is over its import envelope, EV charging, then battery charging, then
     flexible loads are cut, as their envelope rules allow; over the export envelope,
     solar is curtailed, then battery and EV discharge cut. What's left over is a
     violation, reported, not hidden.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from der_opt.coordination.roles import Role, device_kind
from der_opt.devices.solar import SolarPV
from der_opt.results.schema import DispatchResult
from der_opt.simulation.policy import SetpointThermostatPolicy, setpoint_power


@dataclass
class Context:
    """What the mechanisms tell a participant, by step."""

    event: np.ndarray  # bool: a DR event is on
    to_event: np.ndarray  # steps until the next event starts (inf if none ahead today)
    adder: np.ndarray | None = None  # $/kWh price signal
    import_caps: np.ndarray | None = None
    export_caps: np.ndarray | None = None

    @classmethod
    def empty(cls, steps: int) -> Context:
        return cls(np.zeros(steps, dtype=bool), np.full(steps, np.inf))


def to_event_steps(event: np.ndarray) -> np.ndarray:
    """For each step, how many steps until the next event starts (0 during one)."""
    out = np.full(len(event), np.inf)
    next_start = np.inf
    for t in range(len(event) - 1, -1, -1):
        if event[t] and (t == 0 or not event[t - 1]):
            next_start = t
        out[t] = 0 if event[t] else (next_start - t if np.isfinite(next_start) else np.inf)
    return out


def _discharge(device, state, rule, observations, dt) -> dict:
    floor = rule.get("keep_soc_frac")
    p_max = rule.get("max_kw", device.p_max_discharge_kw)
    if rule.get("to", "home_load") == "home_load":
        p_max = min(p_max, max(observations["load_kw"] - observations["solar_kw"], 0.0))
    if floor is not None:
        p_max = min(p_max, max((state["soc"] - floor * device.capacity_kwh) * device.eta_discharge / dt, 0.0))
    return {"p_ch": 0.0, "p_dis": max(p_max, 0.0)}


def _rule_action(device, kind, rule, state, observations, policy, t, dt, phase: str, paused: dict) -> dict | None:
    """The action a rule calls for, or None if it doesn't change anything."""
    action = rule["action"]
    if kind == "ev":
        if action == "pause":
            if not rule.get("catch_up", True):
                paused[device.name] = True
            return {"p_ch": 0.0, "p_dis": 0.0}
        if action == "discharge":
            return _discharge(device, state, rule, observations, dt)
    if kind == "battery" and action == "discharge":
        if phase == "precharge":
            return {"p_ch": device.p_max_charge_kw, "p_dis": 0.0}
        return _discharge(device, state, rule, observations, dt)
    if kind == "thermostat" and action == "setback" and isinstance(policy, SetpointThermostatPolicy):
        offset = -rule.get("precool_offset_c", 1.0) if phase == "precharge" else rule.get("offset_c", 2.0)
        return {"p_hvac": setpoint_power(device, state["temp"], t, policy.setpoint(t, offset))}
    if kind == "flexible_load" and action == "shed":
        blocks = rule.get("blocks", "all")
        return {"n_blocks": device.max_blocks if blocks == "all" else int(blocks)}
    return None


class RuleSimulator:
    """Like CounterfactualSimulator, with the mechanisms' rules layered on."""

    def __init__(self, premise, policies: dict, role: Role, context: Context):
        self.premise, self.policies, self.role, self.context = premise, policies or {}, role, context

    def run(self) -> tuple[DispatchResult, dict[str, float]]:
        premise, ctx, role = self.premise, self.context, self.role
        tg = premise.time_grid
        dt = tg.dt_hours
        devices = premise.devices
        kinds = {d.name: device_kind(d) for d in devices}
        states = {d.name: d.initial_state() for d in devices}
        state_keys = {d.name: set(d.initial_state()) for d in devices}
        series: dict[str, dict[str, list]] = {d.name: {} for d in devices}
        grid_import, grid_export = [], []
        over = {"import_kwh": 0.0, "export_kwh": 0.0}
        paused: dict[str, bool] = {}

        for t in tg.periods:
            observations = {
                "t": t, "load_kw": premise.external_load[t],
                "solar_kw": sum(d.available_kw[t] for d in devices if isinstance(d, SolarPV)),
            }
            actions, results = {}, {}
            for device in devices:
                kind, state = kinds[device.name], states[device.name]
                policy = self.policies.get(device.name)
                base = policy.decide(state, observations, t) if policy else {}
                connected = getattr(device, "connected", None)
                if connected is not None and connected[t] <= 0.5:
                    paused.pop(device.name, None)  # unplugged: a no-catch-up pause ends
                candidates = []
                dr_rule = role.rule_for("dr_event", device)
                if dr_rule is not None:
                    precharge = dr_rule.get("precharge_hours", 0) or dr_rule.get("precool_hours", 0)
                    if ctx.event[t]:
                        candidates.append(_rule_action(device, kind, dr_rule, state, observations, policy, t, dt,
                                                       "event", paused))
                    elif precharge and 0 < ctx.to_event[t] <= round(precharge / dt):
                        candidates.append(_rule_action(device, kind, dr_rule, state, observations, policy, t, dt,
                                                       "precharge", paused))
                signal_rule = role.rule_for("price_signal", device)
                if signal_rule is not None and ctx.adder is not None and ctx.adder[t] >= signal_rule["above_per_kwh"]:
                    candidates.append(_rule_action(device, kind, signal_rule, state, observations, policy, t, dt,
                                                   "event", paused))
                if paused.get(device.name) and not ctx.event[t]:
                    candidates.append({"p_ch": 0.0, "p_dis": 0.0})  # no catch-up: stays off until unplugged
                candidates = [c for c in candidates if c is not None]
                step_state = dict(state, t=t)
                if not candidates:
                    action = base
                    result = device.step(step_state, action)
                else:  # the one that draws least from the grid
                    tried = [(device.step(step_state, c), c) for c in candidates]
                    result, action = max(tried, key=lambda rc: rc[0].get("net_power", 0.0))
                actions[device.name], results[device.name] = dict(action), result

            net = premise.external_load[t] - sum(r.get("net_power", 0.0) for r in results.values())
            net = self._fit_envelope(t, net, actions, results, kinds, states, over, dt)
            limit = premise.export_limit_kw
            if limit is not None and -net > limit + 1e-9:
                net = self._curtail_export(t, net, -net - limit, actions, results, kinds, states, curtail_only=True)
            for device in devices:
                result = results[device.name]
                for key, value in result.items():
                    recorded = states[device.name][key] if key in state_keys[device.name] else value
                    series[device.name].setdefault(key, []).append(recorded)
                states[device.name] = result
            grid_import.append(max(net, 0.0))
            grid_export.append(max(-net, 0.0))

        index = pd.DatetimeIndex(tg.timestamps())
        columns = {f"{name}.{key}": values for name, s in series.items() for key, values in s.items()}
        final = {d.name: {k: states[d.name][k] for k in state_keys[d.name]} for d in devices}
        result = DispatchResult(
            premise_name=premise.name, time_index=index, device_dispatch=pd.DataFrame(columns, index=index),
            grid_import=pd.Series(grid_import, index=index), grid_export=pd.Series(grid_export, index=index),
            objective_value=float(sum(sum(s.get("cost", [])) for s in series.values())), solver_status="simulated",
            final_state={k: v for k, v in final.items() if v},
        )
        return result, over

    def _restep(self, device, t, action, states):
        return device.step(dict(states[device.name], t=t), action)

    def _fit_envelope(self, t, net, actions, results, kinds, states, over, dt) -> float:
        ctx, role = self.context, self.role
        if ctx.import_caps is not None and np.isfinite(ctx.import_caps[t]) and net > ctx.import_caps[t] + 1e-9:
            excess = net - ctx.import_caps[t]
            for kind in ("ev", "battery", "flexible_load"):
                for device in self.premise.devices:
                    if excess <= 1e-9 or kinds[device.name] != kind or role.rule_for("envelope", device) is None:
                        continue
                    action = dict(actions[device.name])
                    if kind in ("ev", "battery"):
                        cut = min(excess, results[device.name].get("p_ch", 0.0))
                        action["p_ch"] = results[device.name].get("p_ch", 0.0) - cut
                    else:
                        action["n_blocks"] = device.max_blocks
                    before = results[device.name].get("net_power", 0.0)
                    results[device.name] = self._restep(device, t, action, states)
                    actions[device.name] = action
                    gained = results[device.name].get("net_power", 0.0) - before
                    net -= gained
                    excess -= gained
            over["import_kwh"] += max(net - ctx.import_caps[t], 0.0) * dt
        if ctx.export_caps is not None and np.isfinite(ctx.export_caps[t]) and -net > ctx.export_caps[t] + 1e-9:
            net = self._curtail_export(t, net, -net - ctx.export_caps[t], actions, results, kinds, states)
            over["export_kwh"] += max(-net - ctx.export_caps[t], 0.0) * dt
        return net

    def _curtail_export(self, t, net, excess, actions, results, kinds, states, curtail_only=False) -> float:
        order = ("solar",) if curtail_only else ("solar", "battery", "ev")
        for kind in order:
            for device in self.premise.devices:
                if excess <= 1e-9 or kinds[device.name] != kind:
                    continue
                if not curtail_only and self.role.rule_for("envelope", device) is None:
                    continue
                action = dict(actions[device.name])
                if kind == "solar":
                    action["p_gen"] = max(results[device.name]["p_gen"] - excess, 0.0)
                else:
                    action["p_dis"] = max(results[device.name].get("p_dis", 0.0) - excess, 0.0)
                    action.setdefault("p_ch", results[device.name].get("p_ch", 0.0))
                before = results[device.name].get("net_power", 0.0)
                results[device.name] = self._restep(device, t, action, states)
                actions[device.name] = action
                cut = before - results[device.name].get("net_power", 0.0)
                net += cut
                excess -= cut
        return net
