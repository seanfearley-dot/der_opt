"""A deterministic 4-DER, 24h/15-min scenario used as the golden-file regression fixture
(tests/integration/test_single_premise_mvp.py). Everything here is a fixed formula, no
randomness, so re-running produces bit-identical inputs."""

from __future__ import annotations

import math
from datetime import datetime

from der_opt.devices import BatteryStorage, CurtailableLoad, SmartThermostat, SolarPV
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import ConstantPriceSeries, CSVPriceSeries
from der_opt.time_grid import TimeGrid

RESOLUTION_MINUTES = 15
HORIZON_STEPS = 24 * 60 // RESOLUTION_MINUTES  # 96


def _hour_of(t: int) -> float:
    return (t * RESOLUTION_MINUTES / 60.0) % 24


def build_premise() -> Premise:
    tg = TimeGrid(resolution_minutes=RESOLUTION_MINUTES, horizon_steps=HORIZON_STEPS, start=datetime(2026, 7, 15))

    solar_available = [
        max(0.0, 6.0 * math.sin(math.pi * (_hour_of(t) - 6) / 12)) if 6 <= _hour_of(t) <= 18 else 0.0
        for t in tg.periods
    ]
    ambient_temp = [26.0 + 6.0 * math.sin(math.pi * (_hour_of(t) - 9) / 12) for t in tg.periods]
    base_load = [1.5 + 0.8 * math.sin(math.pi * (_hour_of(t) - 18) / 12) for t in tg.periods]
    import_price = [0.45 if 16 <= _hour_of(t) < 21 else (0.12 if _hour_of(t) < 6 else 0.22) for t in tg.periods]

    battery = BatteryStorage(
        "battery", tg, capacity_kwh=13.5, p_max_charge_kw=5.0, p_max_discharge_kw=5.0,
        eta_charge=0.96, eta_discharge=0.96, soc_init_frac=0.5, soc_final_min_frac=0.5,
        degradation_cost_per_kwh=0.015,
    )
    solar = SolarPV("solar", tg, available_kw=solar_available)
    thermostat = SmartThermostat(
        "ac", tg, thermal_resistance_r=2.5, thermal_capacitance_c=2.0,
        ambient_temp_c=ambient_temp, comfort_min_c=20.0, comfort_max_c=25.0,
        cop=3.2, p_max_kw=4.0, initial_temp_c=22.0, mode="cooling",
    )
    curtailable = CurtailableLoad("ev_charger", tg, baseline_load_kw=3.0, block_size_kw=1.5, max_blocks=2)

    stream = EnergyArbitrage(CSVPriceSeries(import_price), ConstantPriceSeries(0.04), tg)

    return Premise(
        "golden_house", tg, devices=[battery, solar, thermostat, curtailable],
        value_streams=[stream], external_load=base_load, poi_limit_kw=15.0,
    )
