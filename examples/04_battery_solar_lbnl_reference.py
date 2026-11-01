"""Battery + solar, on real reference data: a week of load and behind-the-meter PV from
a real LBNL-LOAD Phase 4 PG&E residential cluster (see der_opt.integrations.lbnl_load),
under a simple TOU tariff. Shows the optimizer's battery dispatch against the cluster's
own real solar profile, and the value of optimizing vs. a self-consumption baseline.

Run: python examples/04_battery_solar_lbnl_reference.py
Needs /Users/seanearley/Documents/LBNL_Phase_4 (not part of this repo).
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from der_opt.devices import BatteryStorage, SolarPV
from der_opt.integrations.lbnl_load import premise_gross_load_kw, premise_solar_available_kw
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import CSVPriceSeries, value_breakdown
from der_opt.simulation import CounterfactualSimulator, SelfConsumptionBatteryPolicy
from der_opt.time_grid import TimeGrid
from der_opt.viz import comparison_plot, dispatch_plot, interval_heatmap

LBNL_SCENARIO_DIR = "/Users/seanearley/Documents/LBNL_Phase_4/anonymized-1in2-midDemand-with_EE_FS-2025"
# High-PV-penetration PG&E residential cluster (pv_penetration=1.0, net annual exporter)
# -- see list_clusters(LBNL_SCENARIO_DIR, sector="res") for other segments.
CLUSTER_NAME = "res-pge-res_unknown-allSize-hot_dry-nonCare-NoLCA-AllDay-0_50pct"

OUTPUT_DIR = Path(__file__).parent / "outputs"
DAYS = 7
START_HOUR_ENDING = 24 * (31 + 28 + 31 + 30 + 31 + 30 + 14) + 1  # mid-July, a hot_dry climate cluster


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    time_grid = TimeGrid(resolution_minutes=60, horizon_steps=24 * DAYS, start=datetime(2025, 7, 15))

    load_kw = premise_gross_load_kw(LBNL_SCENARIO_DIR, CLUSTER_NAME, time_grid, start_hour_ending=START_HOUR_ENDING)
    solar_kw = premise_solar_available_kw(
        LBNL_SCENARIO_DIR, CLUSTER_NAME, time_grid, start_hour_ending=START_HOUR_ENDING
    )
    print(f"Cluster: {CLUSTER_NAME}")
    print(f"Load (kW): mean={load_kw.mean():.2f}, max={load_kw.max():.2f}")
    print(f"Solar available (kW): mean={solar_kw.mean():.2f}, max={solar_kw.max():.2f}")

    # No soc_final_min_frac: a terminal SOC floor would let the optimizer "bank" stored
    # energy the self-consumption counterfactual below isn't bound by, making the two
    # dispatches end in different storage states and the comparison unfair (the
    # counterfactual could look artificially better just by draining the battery
    # further at the end of the horizon). Leaving the end state free keeps the
    # comparison apples-to-apples: same starting SOC, no terminal constraint on either.
    battery = BatteryStorage(
        "battery", time_grid, capacity_kwh=13.5, p_max_charge_kw=5.0, p_max_discharge_kw=5.0,
        eta_charge=0.96, eta_discharge=0.96, soc_init_frac=0.5,
        degradation_cost_per_kwh=0.015,
    )
    solar = SolarPV("solar", time_grid, available_kw=solar_kw)

    # a simple illustrative TOU tariff: cheap overnight, expensive 4-9pm, mid otherwise
    hours_of_day = [ts.hour for ts in time_grid.timestamps()]
    import_price = [0.45 if 16 <= h < 21 else (0.12 if h < 6 else 0.22) for h in hours_of_day]
    stream = EnergyArbitrage(CSVPriceSeries(import_price), CSVPriceSeries([0.08] * len(hours_of_day)), time_grid)

    premise = Premise(
        "lbnl_reference_house", time_grid, devices=[battery, solar], value_streams=[stream],
        external_load=load_kw, poi_limit_kw=15.0,
    )

    dispatch, model, backend = premise.solve()
    print(f"\nOptimal dispatch: status={dispatch.solver_status}, objective=${dispatch.objective_value:.2f}")
    for label, value in value_breakdown(premise, dispatch).items():
        print(f"  {label}: ${value:.2f}")

    counterfactual = CounterfactualSimulator(
        premise, policies={"battery": SelfConsumptionBatteryPolicy(p_max_charge_kw=5.0, p_max_discharge_kw=5.0)}
    ).run()
    cf_breakdown = value_breakdown(premise, counterfactual)
    print(f"\nSelf-consumption counterfactual: objective=${sum(cf_breakdown.values()):.2f}")
    for label, value in cf_breakdown.items():
        print(f"  {label}: ${value:.2f}")

    interval_heatmap(
        dispatch.device_dispatch["solar.p_gen"], title="Real LBNL cluster PV generation (kW)"
    ).savefig(OUTPUT_DIR / "06_lbnl_solar_heatmap.png")
    dispatch_plot(dispatch, price=import_price, title=f"Optimal dispatch -- {CLUSTER_NAME}").savefig(
        OUTPUT_DIR / "07_lbnl_dispatch.png"
    )
    comparison_plot(dispatch, counterfactual, premise=premise, title="Optimal vs. self-consumption").savefig(
        OUTPUT_DIR / "08_lbnl_comparison.png"
    )
    print(f"\nWrote charts to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
