"""What each device is compared against (docs/COST_TEST_SPEC.md v1.3): the comparison
home the cost tests measure the measure against, built device by device.

  removed      gone, with everything it does (an EV takes its charging load with it)
  rule_based   its counterfactual policy (thermostat setpoint, battery self-consumption,
               EV charging on plug-in), or that type's usual rule when it has none
  tou_timer    an EV charging at full power in the tariff's cheapest period until full
  events_only  the TOU-timer EV that also responds to DR events
  backup_only  a battery held full for backup, discharging only into DR events
  unchanged    as in the "with" run (solar: it has nothing to decide)

Rule-based and timer devices are simulated first and fixed as load profiles (none of
their rules depends on another device); events_only and backup_only devices are then
solved together against a flat energy price and the DR program, with a tiny tie-break
holding their resting behavior (the EV on its timer, the battery full), the program's
expected baseline coming from that resting home as R2's does for the responding run.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from der_opt.cases import CasesError
from der_opt.premise.objective import ValueStream
from der_opt.results.schema import DispatchResult

MODES = {"battery": ("removed", "rule_based", "backup_only"),
         "ev": ("tou_timer", "events_only", "removed", "rule_based"),
         "thermostat": ("rule_based",), "solar": ("unchanged",), "flexible_load": ("rule_based", "removed")}
DEFAULTS = {"ev": "tou_timer", "thermostat": "rule_based", "solar": "unchanged", "flexible_load": "rule_based"}
SOLVED = ("events_only", "backup_only")
RULES = {"battery": {"type": "self_consumption"}, "ev": {"type": "rule_based_ev"}, "thermostat": {"type": "setpoint"}}
TIE_BREAK = 1e-3  # $ scale of the resting tie-break: far below any real price
WORDS = {"removed": "no {w}", "tou_timer": "{w} on a TOU timer", "events_only": "{w} on a TOU timer, events only",
         "backup_only": "{w} backup-only"}
RULE_WORDS = {"thermostat": "thermostat at setpoint", "battery": "battery on self-consumption",
              "ev": "EV charging on plug-in"}


class WithoutError(CasesError):
    """cost_test.without can't be applied to this home."""


def kind_of(device_type: str) -> str:
    return {"ev_v2x": "ev", "curtailable_load": "flexible_load"}.get(device_type, device_type)


def device_modes(devices: list[tuple[str, str]], without: dict, require_battery: bool = True
                 ) -> tuple[dict[str, dict], list[str]]:
    """{device name: {"mode": ..., other settings}} for (name, kind) pairs, and the keys of
    `without` that matched no device. A key is a device name or a type."""
    used, out = set(), {}
    for name, kind in devices:
        key = name if name in without else kind if kind in without else None
        spec = without.get(key) if key is not None else None
        if key is not None:
            used.add(key)
        if spec is None:
            if kind not in DEFAULTS:
                if require_battery:
                    raise WithoutError(f"cost_test.without needs a mode for {name} ({kind}): "
                                       f"{', '.join(MODES.get(kind, ('removed',)))}")
                spec = "removed"
            else:
                spec = DEFAULTS[kind]
        spec = {"mode": spec} if isinstance(spec, str) else dict(spec)
        if spec.get("mode") not in MODES.get(kind, ()):
            raise WithoutError(f"cost_test.without: {name} ({kind}) can't be {spec.get('mode')!r}; "
                               f"its modes are {', '.join(MODES.get(kind, ()))}")
        out[name] = spec
    return out, [k for k in without if k not in used]


def config_modes(config) -> dict[str, dict]:
    without = config.cost_test.without if config.cost_test is not None else {}
    modes, _ = device_modes([(d.name, kind_of(d.type)) for d in config.devices], without,
                            require_battery=config.cost_test is not None)
    return modes


def label(config, modes: dict[str, dict]) -> tuple[str, str]:
    """(label, description) of the comparison home."""
    words = []
    for d in config.devices:
        kind, mode = kind_of(d.type), modes[d.name]["mode"]
        word = "EV" if kind == "ev" else kind.replace("_", " ")
        if mode == "unchanged":
            continue
        words.append(RULE_WORDS.get(kind, f"{word} rule-based") if mode == "rule_based"
                     else WORDS[mode].format(w=word))
    text = ", ".join(words) or "as optimized"
    described = "; ".join(f"{name}: {m['mode']}" for name, m in modes.items()) or "no devices"
    return text[:1].upper() + text[1:], f"the cost tests' comparison home ({described})"


def removed(modes: dict[str, dict]) -> list[str]:
    return [name for name, m in modes.items() if m["mode"] == "removed"]


class RestingBehavior(ValueStream):
    """Holds each solved device to its resting behavior away from DR events: outside an
    event it can't discharge, and outside the event's recovery window (the event's
    start to the end of the next day) it keeps at least the energy it would have at
    rest (the EV on its timer, the battery full). Charging leans, by a steering-only
    tie-break, to the cheapest hours and the earliest of them, as a timer would."""

    def __init__(self, devices, import_rates: np.ndarray, dt: float, resting: pd.DataFrame, event: np.ndarray,
                 recovery: np.ndarray):
        self.devices, self.rates, self.dt = devices, import_rates, dt
        self.resting, self.event, self.recovery = resting, event, recovery

    def apply(self, model, premise):
        import pyomo.environ as pyo

        for d in self.devices:
            periods = list(d.time_grid.periods)
            quiet = [t for t in periods if not self.event[t]]
            held = [t for t in periods if not self.recovery[t] and t > 0]
            soc = self.resting[f"{d.name}.soc"].to_numpy()
            model.add_component(f"{d.name}_rest_no_discharge",
                                pyo.Constraint(quiet, rule=lambda _m, t, d=d: d.p_dis[t] == 0))
            model.add_component(f"{d.name}_rest_energy",
                                pyo.Constraint(held, rule=lambda _m, t, d=d, soc=soc: d.soc[t] >= min(soc[t], d.soc_max)))

    def total_cost_expr(self, model, premise):
        from der_opt.devices import V2XVehicle

        expr = 0
        for d in self.devices:
            if isinstance(d, V2XVehicle):
                expr += TIE_BREAK * self.dt * sum((self.rates[t] + 1e-4 * t / len(self.rates)) * d.p_ch[t]
                                                  for t in d.time_grid.periods)
        return expr

    def evaluate_numeric(self, grid_import, grid_export, time_grid) -> float:
        return 0.0


def _policies(config, premise, modes):
    from der_opt.scenarios.loader import _build_policy
    from der_opt.simulation import HoldFullBatteryPolicy

    rules = config.counterfactual.policies if config.counterfactual is not None else {}
    kinds = {d.name: kind_of(d.type) for d in config.devices}
    out = {}
    for device in premise.devices:
        spec = modes[device.name]
        mode = spec["mode"]
        if mode == "rule_based":
            rule = rules.get(device.name) or RULES.get(kinds[device.name])
            if rule:
                out[device.name] = _build_policy(device.name, dict(rule), device, config)
        elif mode in ("tou_timer", "events_only"):
            rule = {"type": "tou_timer", **({"start": spec["start"]} if spec.get("start") else {})}
            out[device.name] = _build_policy(device.name, rule, device, config)
        elif mode == "backup_only":
            out[device.name] = HoldFullBatteryPolicy(device.p_max_charge_kw, device.soc_max)
    return out


def _incentive(program, net: pd.Series, events, customer_class: str, dt: float) -> float | None:
    """What the comparison home earns at settlement, against its own history."""
    from der_opt.dr.settlement import baseline_series, settlement_baselines

    if program.response != "incentive":
        return None
    load = net.clip(lower=0) if program.settlement_basis == "import" else net
    index = pd.DatetimeIndex(load.index)
    s = baseline_series(settlement_baselines(load, events, customer_class), events, index)
    total = 0.0
    for e in events:
        rows = (index >= e.start) & (index < e.end)
        gap = (s[rows] - load[rows]).dropna()
        if not len(gap):
            continue
        if program.delivery == "uniform":
            total += program.incentive_per_kwh * max(0.0, float(gap.min())) * int(rows.sum()) * dt
        else:
            total += program.incentive_per_kwh * max(0.0, float(gap.sum() * dt))
    return total


def build_without(config, dr_study=None, modes: dict[str, dict] | None = None
                  ) -> tuple[DispatchResult, object, float | None]:
    """(the comparison home's dispatch, the premise that prices it, its DR incentive)."""
    from der_opt.dr.response import DREventResponse
    from der_opt.dr.settlement import baseline_series, settlement_baselines
    from der_opt.premise import EnergyArbitrage
    from der_opt.pricing.sources import ConstantPriceSeries
    from der_opt.scenarios.loader import build_billing, build_premise
    from der_opt.simulation import CounterfactualSimulator

    modes = modes if modes is not None else config_modes(config)
    kept = [d for d in config.devices if modes[d.name]["mode"] != "removed"]
    reduced = config.model_copy(update={"devices": kept, "counterfactual": None, "dr": None, "cost_test": None,
                                        "cases": None})
    pricing = build_premise(reduced)
    rest = CounterfactualSimulator(pricing, _policies(config, pricing, modes)).run()
    solved = [d.name for d in kept if modes[d.name]["mode"] in SOLVED]
    if not solved or dr_study is None or config.dr is None:
        return rest, pricing, None

    program, dt = config.dr.program, pricing.time_grid.dt_hours
    index = pd.DatetimeIndex(pricing.time_grid.timestamps())
    events = dr_study.calendars[0]
    fixed = [d.name for d in pricing.devices if d.name not in solved]
    frame = rest.device_dispatch
    fixed_net = sum((frame[f"{n}.net_power"].to_numpy() for n in fixed if f"{n}.net_power" in frame),
                    np.zeros(len(index)))
    solve_devices = [d.model_copy(update={"params": {**d.params, "smoothing_cost_per_kw": 0.0}})
                     for d in kept if d.name in solved]  # a rule-like home: no steering toward steady output
    solve_config = reduced.model_copy(update={"devices": solve_devices})
    for d in solve_config.devices:
        sources = set(d.params.get("charge_sources", []))
        if sources:
            raise WithoutError(f"{d.name} charges only from {sorted(sources)}; {modes[d.name]['mode']} can't model "
                               "that yet")
    solve = build_premise(solve_config)
    solve.external_load = pricing.external_load - fixed_net
    billing = build_billing(config)
    rates = (np.asarray(billing[0].total_rate(index), dtype=float) if billing is not None
             else np.full(len(index), 0.30))
    # the resting home's net load gives the program's expected baseline, as R2 does for the responding run
    resting = rest.grid_import - rest.grid_export
    basis_load = resting.clip(lower=0) if program.settlement_basis == "import" else resting
    expected = baseline_series(settlement_baselines(basis_load, events, config.dr.customer_class), events, index)
    mask = np.zeros(len(index), dtype=bool)
    recovery = np.zeros(len(index), dtype=bool)
    for e in events:
        mask |= (index >= e.start) & (index < e.end)
        recovery |= (index >= e.start) & (index < pd.Timestamp(e.day) + pd.Timedelta(days=2))
    flat = ConstantPriceSeries(float(rates.mean()))
    solve.value_streams = [EnergyArbitrage(flat, ConstantPriceSeries(0.0), solve.time_grid),
                           RestingBehavior(solve.devices, rates, dt, frame, mask, recovery),
                           DREventResponse(program, mask, expected.to_numpy(), solve.time_grid)]
    result = solve.solve()[0]
    dispatch = pd.concat([frame[[c for c in frame.columns if c.split(".")[0] in fixed]],
                         result.device_dispatch], axis=1)
    combined = DispatchResult(pricing.name, result.time_index, dispatch, result.grid_import, result.grid_export,
                              result.objective_value, result.solver_status,
                              {**rest.final_state, **result.final_state})
    net = combined.grid_import - combined.grid_export
    return combined, pricing, _incentive(program, net, events, config.dr.customer_class, dt)
