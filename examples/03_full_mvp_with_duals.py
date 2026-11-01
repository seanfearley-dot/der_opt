"""End-to-end MVP walkthrough, scripted directly against the Python API (the primary
way to use this toolkit): a single premise with all five device types, a real
URDB-format TOU + tiered + demand-charge tariff, optimal dispatch with dual/shadow-price
recovery, a rule-based counterfactual for comparison, and all five chart types.

Run: python examples/03_full_mvp_with_duals.py
Outputs land in examples/outputs/ (gitignored -- regenerate anytime by re-running this).
"""

from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from der_opt.devices import (
    BatteryStorage,
    CurtailableLoad,
    SmartThermostat,
    SolarPV,
    V2XVehicle,
)
from der_opt.premise import (
    DemandCharge,
    EnergyArbitrage,
    Premise,
    TariffEnergyCharge,
    TariffSchedule,
)
from der_opt.pricing import ConstantPriceSeries, extract_duals, value_breakdown
from der_opt.simulation import (
    CounterfactualSimulator,
    RuleBasedEVChargingPolicy,
    SelfConsumptionBatteryPolicy,
)
from der_opt.time_grid import TimeGrid
from der_opt.viz import (
    comparison_plot,
    dispatch_plot,
    duals_plot,
    interval_heatmap,
    value_stack,
)

HORIZON_HOURS = 24
OUTPUT_DIR = Path(__file__).parent / "outputs"
TARIFF_PATH = Path(__file__).parent / "data" / "sample_tariff_urdb.json"


def build_premise(time_grid: TimeGrid) -> tuple[Premise, TariffSchedule]:
    solar_available = [max(0.0, 5.0 * math.sin(math.pi * (h - 6) / 12)) if 6 <= h <= 18 else 0.0 for h in range(24)]
    ambient_temp = [26.0 + 6.0 * math.sin(math.pi * (h - 9) / 12) for h in range(24)]
    base_load = [1.2 + 0.6 * math.sin(math.pi * (h - 18) / 12) for h in range(24)]

    battery = BatteryStorage(
        "battery", time_grid, capacity_kwh=13.5, p_max_charge_kw=5.0, p_max_discharge_kw=5.0,
        eta_charge=0.96, eta_discharge=0.96, soc_init_frac=0.5, soc_final_min_frac=0.5,
        degradation_cost_per_kwh=0.015,
    )
    solar = SolarPV("solar", time_grid, available_kw=solar_available)
    thermostat = SmartThermostat(
        "ac", time_grid, thermal_resistance_r=2.5, thermal_capacitance_c=2.0,
        ambient_temp_c=ambient_temp, comfort_min_c=20.0, comfort_max_c=25.0,
        cop=3.2, p_max_kw=4.0, initial_temp_c=22.0, mode="cooling",
    )
    curtailable = CurtailableLoad("ev_charger_blocks", time_grid, baseline_load_kw=3.0, block_size_kw=1.5, max_blocks=2)
    ev = V2XVehicle(
        "ev", time_grid, capacity_kwh=60.0, p_max_charge_kw=7.0, p_max_discharge_kw=7.0,
        connected=[1.0 if h < 7 or h >= 18 else 0.0 for h in range(24)],
        soc_init_frac=0.3, departure_period=7, soc_departure_min_frac=0.8,
        degradation_cost_per_kwh=0.01,
    )

    tariff = TariffSchedule.from_urdb_json(TARIFF_PATH)
    # Import is tiered, so it needs TariffEnergyCharge's per-period tier allocation.
    # Export here is just a single flat rate, so it's simplest as an EnergyArbitrage
    # with a zero import price (TariffEnergyCharge above already prices import) and the
    # tariff's flat export_rate -- no need to force a flat rate into the tiered
    # TariffSchedule machinery.
    energy_charge = TariffEnergyCharge(tariff, time_grid, direction="import")
    export_credit = EnergyArbitrage(ConstantPriceSeries(0.0), ConstantPriceSeries(tariff.export_rate), time_grid)
    demand_charge = DemandCharge(rate_per_kw=tariff.demand_charge_rate)

    premise = Premise(
        "example_house", time_grid,
        devices=[battery, solar, thermostat, curtailable, ev],
        value_streams=[energy_charge, export_credit, demand_charge],
        external_load=base_load, poi_limit_kw=15.0,
    )
    return premise, tariff


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    time_grid = TimeGrid(resolution_minutes=60, horizon_steps=HORIZON_HOURS, start=datetime(2026, 7, 15))
    premise, tariff = build_premise(time_grid)

    dispatch, model, backend = premise.solve()
    print(f"Optimal dispatch: status={dispatch.solver_status}, objective=${dispatch.objective_value:.2f}")

    duals = extract_duals(model, backend)
    print("\nDual/shadow-price families (is_exact flags):")
    for name, dual in duals.items():
        print(f"  {name}: is_exact={dual.is_exact}")

    optimal_breakdown = value_breakdown(premise, dispatch)
    print("\nValue breakdown (optimal dispatch):")
    for label, value in optimal_breakdown.items():
        print(f"  {label}: ${value:.2f}")

    counterfactual = CounterfactualSimulator(
        premise,
        policies={
            "battery": SelfConsumptionBatteryPolicy(p_max_charge_kw=5.0, p_max_discharge_kw=5.0),
            "ev": RuleBasedEVChargingPolicy(p_max_charge_kw=7.0, soc_max=60.0),
        },
    ).run()
    cf_breakdown = value_breakdown(premise, counterfactual)
    print("\nValue breakdown (self-consumption + dumb-charging counterfactual):")
    for label, value in cf_breakdown.items():
        print(f"  {label}: ${value:.2f}")
    print(f"\nValue of optimization: ${sum(cf_breakdown.values()) - sum(optimal_breakdown.values()):.2f}")

    interval_heatmap(
        dispatch.device_dispatch["solar.p_gen"], title="Solar generation (kW)"
    ).savefig(OUTPUT_DIR / "01_interval_heatmap.png")

    import pandas as pd

    # tier 1 (marginal, lowest) rate per period, for an illustrative price overlay --
    # the tariff is tiered, so there's no single "the" price per period (see
    # TariffSchedule.energy_rate_array's docstring).
    price_series = pd.Series(
        [tariff.tiers_at(time_grid, t)[0].rate for t in time_grid.periods],
        index=dispatch.device_dispatch.index,
    )
    dispatch_plot(dispatch, price=price_series, title="Optimal dispatch").savefig(
        OUTPUT_DIR / "02_dispatch_plot.png"
    )
    value_stack(optimal_breakdown, title="Value stack (optimal)").savefig(OUTPUT_DIR / "03_value_stack.png")
    duals_plot(
        duals, names=["ac_comfort_band", "example_house_poi_balance"], title="Dual/shadow prices"
    ).savefig(OUTPUT_DIR / "04_duals_plot.png")
    comparison_plot(dispatch, counterfactual, premise=premise, title="Optimal vs. counterfactual").savefig(
        OUTPUT_DIR / "05_comparison_plot.png"
    )
    print(f"\nWrote 5 charts to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
