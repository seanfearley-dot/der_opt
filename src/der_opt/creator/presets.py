"""The creator's presets (docs/SCENARIO_CREATOR_SPEC.md v1.0, decision 4): today's four
`der-opt new` templates as answers the flow starts from. Each writes, setting for
setting, what its template wrote."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from der_opt.creator.answers import Answers, Data, Device, Meter, Prices, Value, When

PRESETS = {
    "dr-battery": "a battery home in a DR program, with cost tests (one home, full year)",
    "home-bill": "a solar and battery home on its bill, NBT or NEM2 (one home, full year)",
    "feeder-from-files": "a feeder study from files: premises, devices and network",
    "lmp-battery": "a standalone battery on a wholesale price",
}

HOME_BATTERY = {"capacity_kwh": 13.5, "p_max_charge_kw": 5.0, "p_max_discharge_kw": 5.0, "eta_charge": 0.95,
                "eta_discharge": 0.95, "soc_init_frac": 0.5, "soc_final_min_frac": 0.5,
                "degradation_cost_per_kwh": 0.015, "smoothing_cost_per_kw": 0.10}
GRID_BATTERY = {"capacity_kwh": 4000, "p_max_charge_kw": 1000, "p_max_discharge_kw": 1000, "eta_charge": 0.94,
                "eta_discharge": 0.94, "soc_init_frac": 0.5, "soc_final_min_frac": 0.5,
                "degradation_cost_per_kwh": 0.005}
DR_PROGRAM = {
    "customer_class": "residential", "measure_devices": ["battery"],
    "program": {"program_id": "summer_peak", "season": [5, 6, 7, 8, 9, 10], "day_types": ["weekday"],
                "window": "16:00-21:00", "min_hours": 1, "max_hours": 3, "max_events_per_month": 4,
                "max_consecutive_days": 2, "response": "incentive", "incentive_per_kwh": "TODO",
                "capacity_reference": "all"},
    "events": {"random": {"events_per_month": {6: 2, 7: 3, 8: 3, 9: 2}, "seed": 1, "hours": 3,
                          "min_days_between": 1}},
}


def cost_test(year: int, avoided_costs: str | None) -> dict:
    return {"without": {"battery": "rule_based"}, "start_year": year + 1, "measure_life_years": 10,
            "avoided_costs": {"set": avoided_costs or "TODO"},
            "capacity_value": {"reference": "R2", "basis": "import", "measure": "counted"}, "net_to_gross": 1.0,
            "bill_escalation": 0.03, "cost_escalation": 0.025, "participant_costs": {"upfront": "TODO", "annual_om": 0},
            "program_costs": {"admin_upfront": 0, "admin_annual": 0, "upfront_incentive": 0},
            "discount_rates": {"utility": "TODO", "participant": "TODO", "societal": "TODO"}}


def _when(opts, template: str) -> When:
    """The window, as `der-opt new` chose it: the price file's or the time series', else a year."""
    from der_opt.scenarios.templates import _price_grid, _timeseries_facts

    _, ts_grid = _timeseries_facts(opts.timeseries)
    grid = _price_grid(opts.price) if template == "lmp-battery" else None if opts.year else ts_grid
    year = opts.year or (int(grid[0][:4]) if grid else datetime.now().year)
    if grid is not None and grid[2] * grid[1] >= 365 * 24 * 60 and grid[0].endswith("-01-01T00:00:00"):
        grid = None  # a whole year
    if grid is None:
        return When(year=year)
    start, resolution, steps = grid
    return When(start=start, steps=steps, resolution_minutes=resolution)


def _premise(opts) -> str | None:
    from der_opt.scenarios.templates import _timeseries_facts

    found, _ = _timeseries_facts(opts.timeseries)
    return opts.premise or (found[0] if len(found) == 1 else None)


def _str(path: Path | None) -> str | None:
    return str(path) if path is not None else None


def preset(name: str, opts, scenario_name: str = "new_scenario") -> Answers:
    """A template's answers from `der-opt new`'s options (der_opt.scenarios.templates.Options)."""
    if name not in PRESETS:
        raise ValueError(f"no preset {name!r}; choose from {sorted(PRESETS)}")
    when = _when(opts, name)
    year = when.year or int((when.start or str(datetime.now().year))[:4])
    data = Data(timeseries=_str(opts.timeseries), premise=_premise(opts))
    if name == "dr-battery":
        return Answers(kind="home", name=scenario_name, when=when, data=data,
                       devices=[Device(type="battery", name="battery", template="battery_13kwh", params=HOME_BATTERY)],
                       prices=Prices(objective="arbitrage", tariff=_str(opts.tariff), billed=False, program="none"),
                       meter=Meter(poi_limit_kw=20), behavior={"battery": "self_consumption"}, dr=dict(DR_PROGRAM),
                       value=Value(cost_test=cost_test(year, _str(opts.avoided_costs))))
    if name == "home-bill":
        return Answers(kind="home", name=scenario_name, when=when, data=data,
                       devices=[Device(type="battery", name="battery", template="battery_13kwh", params=HOME_BATTERY)],
                       prices=Prices(objective="bill", tariff=_str(opts.tariff), program=opts.program,
                                     export_rates=_str(opts.export_rates),
                                     baseline_territory=opts.baseline_territory),
                       meter=Meter(poi_limit_kw=20, single_meter=True), behavior={"battery": "self_consumption"})
    if name == "lmp-battery":
        return Answers(kind="home", name=scenario_name, when=when, solar_from_data=False,
                       devices=[Device(type="battery", name="battery", template="battery_1mw_4mwh",
                                       params=GRID_BATTERY)],
                       prices=Prices(objective="lmp", price=_str(opts.price), billed=False, program="none"),
                       meter=Meter(poi_limit_kw=1000))
    from der_opt.scenarios.feeder import DEFAULT_TARIFF_DIR
    from der_opt.scenarios.templates import _root_element

    return Answers(
        kind="feeder", name=scenario_name, when=when,
        data=Data(premises=_str(opts.premises), devices=_str(opts.devices), network=_str(opts.network),
                  timeseries_files=[_str(opts.timeseries)] if opts.timeseries else []),
        prices=Prices(objective="bill", tariff=str(DEFAULT_TARIFF_DIR), export_rates=_str(opts.export_rates)),
        meter=Meter(poi_limit_kw=None, single_meter=True),
        behavior={"ev": "tou_timer", "battery": "self_consumption"},
        device_settings={"battery": {"eta_charge": 0.96, "eta_discharge": 0.96, "degradation_cost_per_kwh": 0.015,
                                     "charge_sources": ["solar"]},
                         "ev": {"degradation_cost_per_kwh": 0.0}},
        value=Value(deferral=[{"element": _root_element(opts.network), "value_per_kw": None}]))
