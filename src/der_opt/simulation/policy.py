from __future__ import annotations

from abc import ABC, abstractmethod


class ControlPolicy(ABC):
    """A rule-based (non-optimal) controller for one device. decide() is handed that
    device's own state and a dict of exogenous observations at period t, and returns
    the action dict to pass to the device's step(). A device with no policy assigned
    gets an empty action {} -- every device's step() already defines a sensible passive
    default for that case (solar: generate all available; battery/EV: hold; thermostat:
    no HVAC; curtailable load: fully served), so CounterfactualSimulator doesn't need
    per-device-type special-casing."""

    @abstractmethod
    def decide(self, state: dict, observations: dict, t: int) -> dict:
        raise NotImplementedError


class NoFlexibilityPolicy(ControlPolicy):
    """Returns an empty action every period. Applied to every flexible device (battery,
    EV, thermostat, curtailable load) this is the 'what is any DER worth at all'
    reference point -- solar still produces normally, since existing solar generation
    isn't itself a flexibility service."""

    def decide(self, state: dict, observations: dict, t: int) -> dict:
        return {}


class SelfConsumptionBatteryPolicy(ControlPolicy):
    """Charges from excess on-site solar and discharges to cover on-site deficit, with
    no price-arbitrage awareness -- the classic 'what most batteries actually do today'
    baseline. Needs `observations` to carry "solar_kw" and "load_kw" at each period
    (CounterfactualSimulator supplies these from the premise's own solar devices and
    external load)."""

    def __init__(self, p_max_charge_kw: float, p_max_discharge_kw: float):
        self.p_max_charge_kw = p_max_charge_kw
        self.p_max_discharge_kw = p_max_discharge_kw

    def decide(self, state: dict, observations: dict, t: int) -> dict:
        surplus = observations.get("solar_kw", 0.0) - observations.get("load_kw", 0.0)
        if surplus > 0:
            return {"p_ch": min(surplus, self.p_max_charge_kw)}
        return {"p_dis": min(-surplus, self.p_max_discharge_kw)}


class RuleBasedEVChargingPolicy(ControlPolicy):
    """Charges at max rate immediately on plug-in until full; never discharges. The
    device's own step() already zeroes power when disconnected, so this policy doesn't
    need to check the plug-in schedule itself -- isolates the value of V2X/timing
    optimization specifically."""

    def __init__(self, p_max_charge_kw: float, soc_max: float):
        self.p_max_charge_kw = p_max_charge_kw
        self.soc_max = soc_max

    def decide(self, state: dict, observations: dict, t: int) -> dict:
        soc = state.get("soc", 0.0)
        if soc >= self.soc_max:
            return {"p_ch": 0.0}
        return {"p_ch": self.p_max_charge_kw}
