"""V2X EV, no solar: DAYS days of real PG&E residential load (LBNL Phase 4, a cluster
with pv_penetration=0.0) against a vehicle sized and scheduled from the user's own
ev-daily-charging-model project, exporting at real PG&E 2026 NBT rates. Compares
optimal (price-aware, can discharge back to the grid) against a rule-based "charge at
max rate immediately on plug-in, never discharge" counterfactual -- today's typical EV
charger behavior.

Run: python examples/05_v2x_lbnl_reference.py
Needs /Users/seanearley/Documents/LBNL_Phase_4 and
/Users/seanearley/claude runs/ev-daily-charging-model (neither is part of this repo).

Multi-day horizon: V2XVehicle's departure_period/soc_departure_min_frac both accept a
list, one entry per morning -- without that, a multi-day horizon would only enforce the
*first* morning's departure SOC requirement and silently have none for the rest.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pyomo.environ as pyo

from der_opt.devices import V2XVehicle
from der_opt.integrations.ev_profile import (
    charging_device_params,
    connected_schedule,
    load_model_params,
    typical_arrival_departure_hours,
)
from der_opt.integrations.lbnl_load import premise_net_load_kw
from der_opt.integrations.pge_nbt_export import NBTExportPriceSeries
from der_opt.premise import EnergyArbitrage, Premise
from der_opt.pricing import CSVPriceSeries, value_breakdown
from der_opt.simulation import CounterfactualSimulator, RuleBasedEVChargingPolicy
from der_opt.time_grid import TimeGrid
from der_opt.viz import comparison_plot, dispatch_plot

LBNL_SCENARIO_DIR = "/Users/seanearley/Documents/LBNL_Phase_4/anonymized-1in2-midDemand-with_EE_FS-2025"
# pv_penetration=0.0 PG&E residential cluster -- no solar anywhere in this scenario.
CLUSTER_NAME = "res-pge-res_unknown-allSize-hot_dry-nonCare-NoLCA-Flat-80_100pct"

EV_MODEL_DIR = "/Users/seanearley/claude runs/ev-daily-charging-model"
NBT_EEC_CSV = Path(__file__).parent / "data" / "pge_nbt_eec_2026.csv"
OUTPUT_DIR = Path(__file__).parent / "outputs"
START_HOUR_ENDING = 24 * (31 + 28 + 31 + 30 + 31 + 30 + 14) + 1  # mid-July
DAYS = 3


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    time_grid = TimeGrid(resolution_minutes=60, horizon_steps=24 * DAYS, start=datetime(2025, 7, 15))

    load_kw = premise_net_load_kw(LBNL_SCENARIO_DIR, CLUSTER_NAME, time_grid, start_hour_ending=START_HOUR_ENDING)
    print(f"Cluster: {CLUSTER_NAME} (no solar)")
    print(f"Load (kW): mean={load_kw.mean():.2f}, max={load_kw.max():.2f}")

    model_params = load_model_params(f"{EV_MODEL_DIR}/model_params.json")
    device_params = charging_device_params(model_params, family="weibull")
    arrival_hour, departure_hour = typical_arrival_departure_hours(
        f"{EV_MODEL_DIR}/excel_build/raw_arrivals.csv", f"{EV_MODEL_DIR}/excel_build/raw_departures.csv"
    )
    connected = connected_schedule(time_grid, arrival_hour, departure_hour)
    # connected_schedule is hour-of-day based, so it already repeats correctly across
    # DAYS days. departure_period needs the explicit list of every morning's absolute
    # period, though -- one entry per day, not just the first.
    departure_periods = [24 * day + departure_hour for day in range(DAYS)]
    print(f"\nEV: capacity={device_params['capacity_kwh']:.1f} kWh, charger={device_params['p_max_charge_kw']:.1f} kW")
    print(f"Plug-in window (from real PSRC arrival/departure data): home {arrival_hour}:00-{departure_hour}:00")
    print(f"Departures enforced at hours: {departure_periods}")

    # soc_final_min_frac matters here as much as departure_period does: without it,
    # nothing stops the optimizer draining the vehicle toward empty for export revenue
    # *after* the one enforced morning departure is already satisfied -- a real finding
    # from an earlier run of this script, not a hypothetical.
    #
    # Both use customer_target_soc_frac, NOT technical_min_departure_frac: the latter
    # is a bare statistical feasibility floor (just enough for a *typical* day's
    # driving), not what an EV owner actually expects to see at departure. Using the
    # technical minimum as the constraint lets the optimizer run the battery down to
    # ~10% for export revenue -- feasible, but not what a real customer would tolerate.
    # customer_target_soc_frac (80% by default) is the realistic stand-in.
    ev = V2XVehicle(
        "ev", time_grid, capacity_kwh=device_params["capacity_kwh"],
        p_max_charge_kw=device_params["p_max_charge_kw"], p_max_discharge_kw=device_params["p_max_charge_kw"],
        connected=connected, eta_charge=0.95, eta_discharge=0.95, soc_init_frac=0.5,
        departure_period=departure_periods, soc_departure_min_frac=device_params["customer_target_soc_frac"],
        soc_final_min_frac=device_params["customer_target_soc_frac"],
        degradation_cost_per_kwh=0.02,  # illustrative, not sourced from the EV model project
    )

    # Import: illustrative flat/TOU placeholder, as in the battery+solar example.
    # Export: real PG&E 2026 NBT rates -- a V2X vehicle discharging to the grid earns
    # the same EEC credit solar export does.
    hours_of_day = [ts.hour for ts in time_grid.timestamps()]
    import_price = [0.45 if 16 <= h < 21 else (0.12 if h < 6 else 0.22) for h in hours_of_day]
    export_price = NBTExportPriceSeries(NBT_EEC_CSV)
    stream = EnergyArbitrage(CSVPriceSeries(import_price), export_price, time_grid)

    premise = Premise(
        "v2x_reference_house", time_grid, devices=[ev], value_streams=[stream],
        external_load=load_kw, poi_limit_kw=15.0, exclusive_import_export=True,
    )

    dispatch, model, backend = premise.solve()
    print(f"\nOptimal dispatch: status={dispatch.solver_status}, objective=${dispatch.objective_value:.2f}")
    for label, value in value_breakdown(premise, dispatch).items():
        print(f"  {label}: ${value:.2f}")
    required_soc = device_params["customer_target_soc_frac"] * device_params["capacity_kwh"]
    for day, departure_t in enumerate(departure_periods):
        print(f"  day {day + 1} departure (hour {departure_t}) SOC: {pyo.value(ev.soc[departure_t]):.1f} kWh "
              f"(customer target >= {required_soc:.1f})")
    print(f"  end-of-horizon SOC: {pyo.value(ev.soc[time_grid.horizon_steps]):.1f} kWh "
          f"(customer target >= {required_soc:.1f})")

    counterfactual = CounterfactualSimulator(
        premise, policies={"ev": RuleBasedEVChargingPolicy(
            p_max_charge_kw=device_params["p_max_charge_kw"], soc_max=device_params["capacity_kwh"],
        )}
    ).run()
    cf_breakdown = value_breakdown(premise, counterfactual)
    print(f"\nRule-based ('charge immediately, never discharge') counterfactual: objective=${sum(cf_breakdown.values()):.2f}")
    for label, value in cf_breakdown.items():
        print(f"  {label}: ${value:.2f}")
    print(f"\nValue of V2X optimization: ${sum(cf_breakdown.values()) - dispatch.objective_value:.2f}")

    dispatch_plot(dispatch, price=import_price, title=f"Optimal V2X dispatch -- {CLUSTER_NAME}").savefig(
        OUTPUT_DIR / "09_v2x_dispatch.png"
    )
    comparison_plot(dispatch, counterfactual, premise=premise, title="Optimal V2X vs. rule-based charging").savefig(
        OUTPUT_DIR / "10_v2x_comparison.png"
    )
    print(f"\nWrote charts to {OUTPUT_DIR}")


if __name__ == "__main__":
    main()
