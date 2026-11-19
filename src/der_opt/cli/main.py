from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

app = typer.Typer(help="DER Optimization Toolkit CLI")


@app.callback()
def _main() -> None:
    """DER Optimization Toolkit CLI. Explicit no-op callback so Typer keeps `run` as a
    named subcommand (der-opt run ...) instead of collapsing a single-command app into
    a bare `der-opt ...` invocation -- deliberate, since more subcommands land here
    later (e.g. validating a scenario without solving it)."""
    return


def _end_state_warnings(premise, simulator, tolerance_frac: float = 0.01) -> list[str]:
    """Storage devices that finish the horizon with a different SOC under the optimal
    and counterfactual dispatches. Neither side values energy left in storage at the
    end, so a difference silently tilts the comparison -- the trap that once made a
    self-consumption counterfactual score *better* than the optimum."""
    import pyomo.environ as pyo

    warnings = []
    end = premise.time_grid.horizon_steps
    for device in premise.devices:
        if not hasattr(device, "soc") or "soc" not in simulator.final_device_states.get(device.name, {}):
            continue
        optimal_end = pyo.value(device.soc[end])
        counterfactual_end = simulator.final_device_states[device.name]["soc"]
        difference = counterfactual_end - optimal_end
        if abs(difference) <= tolerance_frac * getattr(device, "capacity_kwh", 1.0):
            continue
        favors = "optimal" if difference > 0 else "counterfactual"
        warnings.append(
            f"{device.name} ends at {optimal_end:.1f} kWh (optimal) vs {counterfactual_end:.1f} kWh "
            f"(counterfactual). Energy left in storage isn't valued by either side, so this "
            f"{abs(difference):.1f} kWh difference tilts the comparison toward the {favors} dispatch."
        )
    return warnings


def _print_bills(bills: dict, tariff, program) -> None:
    from der_opt.billing import NEM2Program

    nem2 = isinstance(program, NEM2Program)
    program_label = "NEM2" if nem2 else "NBT" if program is not None else "no export credit"
    typer.echo(f"\nBill ({tariff.name}, {program_label}):")
    for label, bill in bills.items():
        summary = bill.summary
        unused = summary["unused_credits"]
        if nem2:
            # NEM2 bills non-bypassable + base services monthly; energy settles at true-up
            typer.echo(
                f"  {label}: ${summary['net_cost']:.2f} "
                f"(monthly charges ${summary['billed']:.2f}"
                + (f", energy at true-up ${bill.true_up['energy_owed']:.2f}" if bill.true_up else "")
                + (f", NSC -${bill.true_up['nsc_credit']:.2f}" if bill.true_up and bill.true_up["nsc_credit"] else "")
                + ")"
            )
            if bill.true_up and bill.true_up["credit_zeroed"]:
                typer.echo(f"    energy credit zeroed at true-up: ${bill.true_up['credit_zeroed']:.2f}")
            elif unused["energy"]:
                typer.echo(f"    energy credit accrued, not yet settled: ${unused['energy']:.2f}")
            continue
        typer.echo(
            f"  {label}: billed ${summary['billed']:.2f}"
            + (f", net of true-up ${summary['net_cost']:.2f}" if bill.true_up is not None else "")
        )
        if program is not None:
            typer.echo(
                f"    unused export credit carried forward: generation ${unused['generation']:.2f}, "
                f"delivery ${unused['delivery']:.2f}, ACC Plus ${unused['acc_plus']:.2f}"
            )
    if {"optimal", "counterfactual"} <= set(bills):
        difference = bills["counterfactual"].summary["net_cost"] - bills["optimal"].summary["net_cost"]
        typer.echo(f"  Bill savings from optimization: ${difference:.2f}")
        unused_diff = sum(bills["optimal"].summary["unused_credits"].values()) - sum(
            bills["counterfactual"].summary["unused_credits"].values()
        )
        if abs(unused_diff) > 0.01 and not nem2:
            typer.echo(
                f"  Note: the two runs end with ${abs(unused_diff):.2f} different unused credit, which "
                "carries forward to later bills and isn't counted in the savings above."
            )
    # a note both runs share is printed once; one that differs says which run it's about
    shared = set.intersection(*(set(bill.notes) for bill in bills.values()))
    for note in sorted(shared):
        typer.echo(f"  Note: {note}")
    for label, bill in bills.items():
        for note in bill.notes:
            if note not in shared:
                typer.echo(f"  Note ({label}): {note}")


@app.command()
def run(
    scenario: Path = typer.Argument(..., help="Path to a scenario YAML file"),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Directory to write dispatch CSVs and charts to"
    ),
    duals: bool = typer.Option(False, "--duals", help="Print (and with -o, write) dual/shadow prices"),
    no_counterfactual: bool = typer.Option(
        False, "--no-counterfactual", help="Skip the scenario's counterfactual comparison"
    ),
) -> None:
    """Solve a scenario and print a summary. If the scenario has a `counterfactual`
    section, also simulates that rule-based baseline and reports the value of
    optimization. With --output-dir, writes CSVs and charts for everything printed."""
    import pandas as pd

    from der_opt.pricing import extract_duals, value_breakdown
    from der_opt.scenarios import (
        build_billing,
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )
    from der_opt.simulation import CounterfactualSimulator

    config = load_scenario_config(scenario)
    premise = build_premise(config)
    dispatch, model, backend = premise.solve()

    typer.echo(f"Scenario: {premise.name}")
    typer.echo(f"Status: {dispatch.solver_status}")

    breakdown = value_breakdown(premise, dispatch)
    typer.echo(f"\nOptimal dispatch: ${sum(breakdown.values()):.2f}")
    for label, value in breakdown.items():
        typer.echo(f"  {label}: ${value:.2f}")

    counterfactual = None
    cf_breakdown = None
    policies = None if no_counterfactual else build_counterfactual_policies(config, premise)
    if policies is not None:
        simulator = CounterfactualSimulator(premise, policies)
        counterfactual = simulator.run()
        cf_breakdown = value_breakdown(premise, counterfactual)

        described = ", ".join(
            f"{name}: {config.counterfactual.policies[name]['type']}" for name in policies
        ) or "all devices passive"
        typer.echo(f"\nCounterfactual ({described}): ${sum(cf_breakdown.values()):.2f}")
        for label, value in cf_breakdown.items():
            typer.echo(f"  {label}: ${value:.2f}")

        value_of_optimization = sum(cf_breakdown.values()) - sum(breakdown.values())
        typer.echo(f"\nValue of optimization: ${value_of_optimization:.2f}")
        for warning in _end_state_warnings(premise, simulator):
            typer.echo(f"  Note: {warning}")

    bills = {}
    billing = build_billing(config)
    if billing is not None:
        from der_opt.billing import bill_from_dispatch

        tariff, program, bill_kwargs = billing
        runs = {"optimal": dispatch}
        if counterfactual is not None:
            runs["counterfactual"] = counterfactual
        for label, result in runs.items():
            bills[label] = bill_from_dispatch(result, premise.time_grid, tariff, program, **bill_kwargs)
        _print_bills(bills, tariff, program)

    dual_results = None
    if duals:
        dual_results = extract_duals(model, backend)
        typer.echo("\nDual/shadow-price families:")
        for name, dual in dual_results.items():
            note = " (fix-and-resolve approximation, not exact)" if not dual.is_exact else ""
            typer.echo(f"  {name}: is_exact={dual.is_exact}{note}")

    if output_dir is not None:
        from der_opt.viz import comparison_plot, dispatch_plot, duals_plot, value_stack

        output_dir.mkdir(parents=True, exist_ok=True)
        dispatch.device_dispatch.to_csv(output_dir / "device_dispatch.csv")
        dispatch.grid_import.to_csv(output_dir / "grid_import.csv", header=["grid_import_kw"])
        dispatch.grid_export.to_csv(output_dir / "grid_export.csv", header=["grid_export_kw"])
        dispatch_plot(dispatch, title=premise.name).savefig(output_dir / "dispatch.png")
        value_stack(breakdown, title=premise.name).savefig(output_dir / "value_stack.png")

        breakdown_table = pd.DataFrame({"optimal": pd.Series(breakdown)})
        if counterfactual is not None:
            breakdown_table["counterfactual"] = pd.Series(cf_breakdown)
            counterfactual.device_dispatch.to_csv(output_dir / "counterfactual_device_dispatch.csv")
            counterfactual.grid_import.to_csv(
                output_dir / "counterfactual_grid_import.csv", header=["grid_import_kw"]
            )
            counterfactual.grid_export.to_csv(
                output_dir / "counterfactual_grid_export.csv", header=["grid_export_kw"]
            )
            dispatch_plot(counterfactual, title=f"{premise.name} (counterfactual)").savefig(
                output_dir / "counterfactual_dispatch.png"
            )
            comparison_plot(dispatch, counterfactual, premise=premise, title=premise.name).savefig(
                output_dir / "comparison.png"
            )
        breakdown_table = breakdown_table.fillna(0.0)
        breakdown_table.loc["total"] = breakdown_table.sum()
        breakdown_table.to_csv(output_dir / "value_breakdown.csv", index_label="source")

        for label, bill in bills.items():
            prefix = "" if label == "optimal" else "counterfactual_"
            bill.cycles.to_csv(output_dir / f"{prefix}bill_cycles.csv")

        if dual_results is not None:
            rows = [
                {"family": name, "index": index, "value": value, "is_exact": dual.is_exact}
                for name, dual in dual_results.items()
                for index, value in dual.values.items()
            ]
            pd.DataFrame(rows).to_csv(output_dir / "duals.csv", index=False)
            poi_family = f"{premise.name}_poi_balance"
            if poi_family in dual_results:
                duals_plot(
                    dual_results, names=[poi_family], title=f"{premise.name}: marginal value of energy at the meter"
                ).savefig(output_dir / "duals.png")

        typer.echo(f"\nWrote CSVs and charts to {output_dir}")


if __name__ == "__main__":
    app()
