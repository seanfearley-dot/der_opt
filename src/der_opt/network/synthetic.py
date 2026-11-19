"""Synthetic houses for feeder studies: no external data, reproducible from a seed.

Each house gets a load built from a few interpretable parts, with randomized sizes and
timing so no two houses peak at the same moment:

  - a base load, a morning bump and an evening bump (occupancy)
  - air conditioning: kW = min(AC capacity, sensitivity x (indoor-lagged outdoor
    temperature - balance point)), so load tracks a heat wave with a few hours' lag
  - slow multiplicative noise

and, by share, rooftop solar (a clear-sky curve, derated, for the date's sun), an EV
(home in the evening, away during the day, drawing the day's trip energy while away),
and a battery on solar homes (charging only from solar, as ITC/SGIP-funded batteries
must). The parameter ranges are judgment calls for an inland California summer, not
fitted to data; they're meant to put a realistic amount of load and diversity on a
service transformer, not to stand in for any real customer.

A horizon starting at noon (cars away) and lasting whole days avoids two edge effects:
starting with every car just plugged in, and ending before the night's charging.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from der_opt.time_grid import TimeGrid


@dataclass
class SyntheticHouse:
    name: str
    load_kw: np.ndarray
    solar: dict | None = None  # SolarPV kwargs
    battery: dict | None = None  # BatteryStorage kwargs (charge_sources is solar)
    ev: dict | None = None  # V2XVehicle kwargs
    traits: dict = field(default_factory=dict)  # the draws behind it, for reporting


def _hours(time_grid: TimeGrid, offset_periods: int = 0) -> np.ndarray:
    """Clock hour (fractional) at each period's midpoint."""
    start = time_grid.start or datetime(2026, 1, 1)
    hour0 = start.hour + start.minute / 60
    t = np.arange(-offset_periods, time_grid.horizon_steps) + 0.5
    return (hour0 + t * time_grid.dt_hours) % 24


def heat_wave_temperature(time_grid: TimeGrid, t_min_c: float = 22.0, t_max_c: float = 40.0, warmup_periods: int = 0) -> np.ndarray:
    """Outdoor temperature by period: coolest near 6 am, hottest near 4 pm, the same
    every day (a sustained heat wave). With warmup_periods, the array starts that
    many periods before the horizon."""
    h = _hours(time_grid, warmup_periods)
    # rises 6 am -> 4 pm (10 h), falls 4 pm -> 6 am (14 h)
    rising = (h >= 6) & (h < 16)
    phase = np.where(rising, (h - 6) / 10, 1 + ((h - 16) % 24) / 14)
    shape = 0.5 - 0.5 * np.cos(np.pi * phase)
    return t_min_c + (t_max_c - t_min_c) * shape


def _bump(h: np.ndarray, center: float, width: float) -> np.ndarray:
    d = np.abs((h - center + 12) % 24 - 12)
    return np.exp(-0.5 * (d / width) ** 2)


def _solar_shape(time_grid: TimeGrid) -> np.ndarray:
    """kW per kW-AC of a south-facing array under clear sky, for the run's date at
    ~38 N, on a daylight-saving clock in summer."""
    start = time_grid.start or datetime(2026, 7, 1)
    doy = start.timetuple().tm_yday
    declination = 23.45 * np.sin(2 * np.pi * (284 + doy) / 365)
    day_length = 12 + 2.4 * np.sin(2 * np.pi * (doy - 80) / 365)
    noon = 13.1 if 3 <= start.month <= 10 else 12.1
    h = _hours(time_grid)
    x = (h - (noon - day_length / 2)) / day_length
    shape = np.where((x > 0) & (x < 1), np.sin(np.pi * np.clip(x, 0, 1)) ** 1.3, 0.0)
    elevation_factor = np.cos(np.radians(38 - declination))
    return 0.85 * elevation_factor * shape  # 0.85: temperature, inverter and wiring losses


def _ev(time_grid: TimeGrid, rng: np.random.Generator) -> tuple[dict, dict]:
    arrival = float(np.clip(rng.normal(18.0, 1.5), 15.0, 22.5))
    departure = float(np.clip(rng.normal(7.5, 1.0), 5.0, 10.0))
    daily_kwh = float(np.clip(rng.lognormal(np.log(11.0), 0.45), 3.0, 30.0))
    charger_kw = 7.2 if rng.random() < 0.8 else 11.5
    capacity = float(rng.choice([60.0, 75.0, 82.0]))
    target = 0.9  # a typical owner charge limit; both the optimizer and the plug-in
    # charger fill to it, so every run charges the same energy -- only timing differs

    h = _hours(time_grid) - 0.5 * time_grid.dt_hours  # period start
    h = h % 24
    away = (h >= departure) & (h < arrival)
    connected = (~away).astype(float)

    # each away block uses one day's trip energy, pro rata for a block cut off by the
    # start or end of the horizon (on the period grid, so a full day's block sums to
    # exactly one trip)
    periods_per_day = round(24 / time_grid.dt_hours)
    day_hours = (h[0] + np.arange(periods_per_day) * time_grid.dt_hours) % 24
    away_periods_per_day = int(((day_hours >= departure) & (day_hours < arrival)).sum())
    per_period = daily_kwh / away_periods_per_day
    driving = np.where(away, per_period, 0.0)
    if away[0]:  # the car left this morning, full; some of today's trip is behind it
        first_block = int(np.argmin(away)) if not away.all() else len(away)
        soc_init = target * capacity - per_period * (away_periods_per_day - first_block)
    else:  # home and not yet charged since the last trip
        soc_init = target * capacity - daily_kwh
    departures = [t for t in range(1, time_grid.horizon_steps) if connected[t - 1] and not connected[t]]

    params = {
        "capacity_kwh": capacity,
        "p_max_charge_kw": charger_kw,
        "p_max_discharge_kw": 0.0,
        "connected": connected.tolist(),
        "driving_kwh": driving.round(4).tolist(),
        "eta_charge": 0.92,
        "eta_discharge": 0.92,
        "soc_max_frac": target,
        "soc_init_frac": round(soc_init / capacity, 4),
        "departure_period": departures,
        "soc_departure_min_frac": target,
        "degradation_cost_per_kwh": 0.0,
    }
    traits = {"ev_arrival_hour": round(arrival, 1), "ev_departure_hour": round(departure, 1),
              "ev_daily_kwh": round(daily_kwh, 1), "ev_charger_kw": charger_kw}
    return params, traits


def synthetic_houses(
    time_grid: TimeGrid,
    count: int,
    seed: int = 0,
    name_prefix: str = "house",
    solar_share: float = 0.4,
    battery_share: float = 0.3,
    ev_share: float = 0.4,
    ev_v2x: bool = False,
    t_min_c: float = 22.0,
    t_max_c: float = 40.0,
) -> list[SyntheticHouse]:
    """`count` houses. Shares are fractions of houses (battery_share: of the solar
    homes) and are met as closely as the count allows, not drawn independently, so a
    10-house transformer with ev_share 0.6 gets exactly 6 EVs. ev_v2x lets EVs
    discharge to the home and grid at their charger rating."""
    rng = np.random.default_rng(seed)
    warmup = round(24 / time_grid.dt_hours)
    temperature = heat_wave_temperature(time_grid, t_min_c, t_max_c, warmup_periods=warmup)
    h = _hours(time_grid)
    solar_shape = _solar_shape(time_grid)

    def pick(share, population):
        chosen = rng.choice(population, size=round(share * len(population)), replace=False)
        return {int(i) for i in chosen}

    has_solar = pick(solar_share, list(range(count)))
    has_battery = pick(battery_share, sorted(has_solar)) if has_solar else set()
    has_ev = pick(ev_share, list(range(count)))

    houses = []
    for i in range(count):
        size = float(rng.lognormal(0.0, 0.2))
        base = rng.uniform(0.25, 0.6)
        morning = rng.uniform(0.3, 0.8) * _bump(h, rng.normal(7.0, 0.7), 1.2)
        evening = rng.uniform(0.6, 1.6) * _bump(h, rng.normal(19.5, 1.0), 2.0)

        has_ac = rng.random() > 0.1
        ac_kw = rng.uniform(2.5, 5.0)
        sensitivity = rng.uniform(0.25, 0.5)  # kW per deg C
        balance = rng.uniform(22.0, 27.0)
        lag_hours = rng.uniform(2.0, 4.0)
        alpha = min(1.0, time_grid.dt_hours / lag_hours)
        lagged = np.empty_like(temperature)
        lagged[0] = temperature[0]
        for t in range(1, len(temperature)):
            lagged[t] = lagged[t - 1] + alpha * (temperature[t] - lagged[t - 1])
        cooling = np.minimum(ac_kw, sensitivity * np.clip(lagged[warmup:] - balance, 0, None)) if has_ac else 0.0

        noise = np.empty(time_grid.horizon_steps)
        noise[0] = rng.normal(0, 0.08)
        for t in range(1, time_grid.horizon_steps):
            noise[t] = 0.7 * noise[t - 1] + rng.normal(0, 0.08)
        load = size * (base + morning + evening + cooling) * np.exp(noise)

        house = SyntheticHouse(
            name=f"{name_prefix}{i + 1:02d}",
            load_kw=load.round(4),
            traits={"size_factor": round(size, 2), "ac_kw": round(ac_kw, 1) if has_ac else 0.0,
                    "balance_point_c": round(balance, 1) if has_ac else None},
        )
        if i in has_solar:
            kw_ac = round(float(rng.uniform(4.0, 9.0)), 1)
            house.solar = {"available_kw": (kw_ac * solar_shape).round(4).tolist()}
            house.traits["solar_kw_ac"] = kw_ac
        if i in has_battery:
            house.battery = {
                "capacity_kwh": 13.5, "p_max_charge_kw": 5.0, "p_max_discharge_kw": 5.0,
                "eta_charge": 0.96, "eta_discharge": 0.96, "soc_init_frac": 0.5,
                # end where it started, so no run gets free energy out of the battery
                "soc_final_min_frac": 0.5, "degradation_cost_per_kwh": 0.015,
            }
            house.traits["battery_kwh"] = 13.5
        if i in has_ev:
            house.ev, ev_traits = _ev(time_grid, rng)
            if ev_v2x:
                house.ev["p_max_discharge_kw"] = house.ev["p_max_charge_kw"]
            house.traits.update(ev_traits)
        houses.append(house)
    return houses
