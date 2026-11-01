"""V2X EV, no solar: one day of real PG&E residential load (LBNL Phase 4, a cluster
with pv_penetration=0.0) against a vehicle sized and scheduled from the user's own
ev-daily-charging-model project, exporting at real PG&E 2026 NBT rates. Compares
optimal (price-aware, can discharge back to the grid) against a rule-based "charge at
max rate immediately on plug-in, never discharge" counterfactual -- today's typical EV
charger behavior.

Run: python examples/05_v2x_lbnl_reference.py
Needs /Users/seanearley/Documents/LBNL_Phase_4 and
/Users/seanearley/claude runs/ev-daily-charging-model (neither is part of this repo).

Single-day horizon: V2XVehicle supports one connect/disconnect cycle per horizon (see
its docstring), so a multi-day week would only get one enforced morning departure --
one day keeps the departure-SOC requirement meaningful.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

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


def main() -> None:
    OUTPUT_DIR.mkdir(exist_ok=True)
    time_grid = TimeGrid(resolution_minutes=60, horizon_steps=24, start=datetime(2025, 7, 15))

    load_kw = premise_net_load_kw(LBNL_SCENARIO_DIR, CLUSTER_NAME, time_grid, start_hour_ending=START_HOUR_ENDING)
    print(f"Cluster: {CLUSTER_NAME} (no solar)")
    print(f"Load (kW): mean={load_kw.mean():.2f}, max={load_kw.max():.2f}")

    model_params = load_model_params(f"{EV_MODEL_DIR}/model_params.json")
    device_params = charging_device_params(model_params, family="weibull")
    arrival_hour, departure_hour = typical_arrival_departure_hours(
        f"{EV_MODEL_DIR}/excel_build/raw_arrivals.csv", f"{EV_MODEL_DIR}/excel_build/raw_departures.csv"
    )
    connected = connected_schedule(time_grid, arrival_hour, departure_hour)
    print(f"\nEV: capacity={device_params['capacity_kwh']:.1f} kWh, charger={device_params['p_max_charge_kw']:.1f} kW")
    print(f"Plug-in window (from real PSRC arrival/departure data): home {arrival_hour}:00-{departure_hour}:00")

    ev = V2XVehicle(
        "ev", time_grid, capacity_kwh=device_params["capacity_kwh"],
        p_max_charge_kw=device_params["p_max_charge_kw"], p_max_discharge_kw=device_params["p_max_charge_kw"],
        connected=connected, eta_charge=0.95, eta_discharge=0.95, soc_init_frac=0.5,
        departure_period=departure_hour, soc_departure_min_frac=device_params["soc_departure_min_frac"],
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
    print(f"  departure SOC: {dispatch.device_dispatch['ev.soc'].iloc[departure_hour]:.1f} kWh "
          f"(required >= {device_params['soc_departure_min_frac'] * device_params['capacity_kwh']:.1f})")

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
