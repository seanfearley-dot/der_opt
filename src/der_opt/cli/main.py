from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import typer

app = typer.Typer(help="DER Optimization Toolkit CLI")


@app.callback()
def _main() -> None:
    """DER Optimization Toolkit CLI. Explicit no-op callback so Typer keeps `run` as a
    named subcommand (der-opt run ...) instead of collapsing a single-command app into
    a bare `der-opt ...` invocation -- deliberate, since more subcommands land here
    later (e.g. validating a scenario without solving it)."""
    return


def _print_checks(report) -> None:
    typer.echo(f"\nChecks: {report.summary()}")
    for line in report.lines():
        typer.echo(f"  {line}")


def _write_checks(output_dir: Path, scenario: Path, config, report, results: dict) -> Path:
    from der_opt.run_record import write_run_record

    report.to_frame().to_csv(output_dir / "checks.csv", index=False)
    return write_run_record(output_dir, scenario, config.model_dump(mode="json"), report, results)


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


def _money(value: float) -> str:
    return f"-${-value:,.2f}" if value < 0 else f"${value:,.2f}"


def _run_feeder(scenario: Path, output_dir: Path | None, no_counterfactual: bool) -> None:
    import pandas as pd

    from der_opt.diagnostics import CheckReport, collect_data_warnings
    from der_opt.network import loading_summary
    from der_opt.scenarios import (
        build_feeder_scenario,
        load_feeder_config,
        run_feeder_study,
    )

    report = CheckReport()
    config = load_feeder_config(scenario)
    with collect_data_warnings(report):
        built = build_feeder_scenario(config)
    study = run_feeder_study(built, counterfactual=not no_counterfactual)
    report.extend(study.checks)
    runs = study.runs
    time_grid = built.premises[0].time_grid

    typer.echo(f"Feeder scenario: {built.config.name}")
    typer.echo(f"{len(built.premises)} houses on {len(built.feeder.elements)} network elements")
    counts = built.houses.groupby("element")["devices"].apply(
        lambda d: ", ".join(f"{sum(k in x.split(', ') for x in d)} {k}" for k in ("solar", "battery", "ev"))
    )
    for element, described in counts.items():
        typer.echo(f"  {element}: {(built.houses['element'] == element).sum()} houses ({described})")

    typer.echo(f"\nCosts over the run ({', '.join(runs)}):")
    rows = {
        "Customer bills": {k: r.house_costs["bill"].sum() for k, r in runs.items()},
        "Battery/EV degradation": {k: r.house_costs["degradation"].sum() for k, r in runs.items()},
    }
    for cost_name in next(iter(runs.values())).network_costs:
        rows["Transformer overload" if cost_name == "overload" else cost_name] = {
            k: r.network_costs[cost_name] for k, r in runs.items()
        }
    rows["Total"] = {k: r.total for k, r in runs.items()}
    width = max(len(name) for name in rows) + 2
    typer.echo(" " * (width + 2) + "".join(f"{label:>16}" for label in runs))
    for name, values in rows.items():
        typer.echo(f"  {name:<{width}}" + "".join(f"{_money(values[k]):>16}" for k in runs))

    typer.echo("\nPeak loading:")
    summaries = {label: loading_summary(run.flows, built.feeder, time_grid) for label, run in runs.items()}
    for element in built.feeder.elements:
        parts = []
        for label, summary in summaries.items():
            row = summary.loc[element.name]
            text = f"{label} {row['peak_kw']:.1f} kW"
            if element.rating_kw:
                text += f" ({row['peak_pct_of_rating']:.0f}%"
                text += f", {row['hours_over']:.0f} h over)" if row["hours_over"] else ")"
            parts.append(text)
        rating = f" [{element.rating_kw:g} kW]" if element.rating_kw else ""
        typer.echo(f"  {element.name}{rating}: " + "; ".join(parts))

    prices = study.congestion_prices
    typer.echo("\nCongestion prices, coordinated run ($/kWh through each element):")
    if not study.prices_exact:
        typer.echo("  (approximate: the model has integer variables, so these come from fix-and-resolve)")
    for element in built.feeder.elements:
        series = prices[element.name]
        binding = series[series.abs() > 1e-3]
        if binding.empty:
            typer.echo(f"  {element.name}: none -- the limit never binds")
        else:
            typer.echo(
                f"  {element.name}: up to ${binding.max():.2f} in {len(binding)} hours "
                f"(highest {binding.idxmax()}); ${binding.sum() * time_grid.dt_hours:.2f} per kW over the run"
            )

    if "independent" in runs:
        change = runs["coordinated"].house_costs.sum(axis=1) - runs["independent"].house_costs.sum(axis=1)
        worse = change[change > 0.005]
        typer.echo("\nCoordination vs each house optimizing alone:")
        if worse.empty:
            typer.echo("  no house pays more")
        else:
            typer.echo(
                f"  {len(worse)} of {len(change)} houses pay more (by {_money(worse.sum())} in total, "
                f"at most {_money(worse.max())}, {worse.idxmax()}), for a network saving of "
                f"{_money(sum(runs['independent'].network_costs.values()) - sum(runs['coordinated'].network_costs.values()))}"
            )
    _print_checks(report)

    if output_dir is not None:
        from der_opt.viz import feeder_loading_plot

        output_dir.mkdir(parents=True, exist_ok=True)
        houses = built.houses.copy()
        for label, run in runs.items():
            houses[f"bill_{label}"] = run.house_costs["bill"].round(2)
        houses.to_csv(output_dir / "houses.csv")
        pd.concat({label: run.flows for label, run in runs.items()}, axis=1).to_csv(output_dir / "flows_kw.csv")
        pd.concat(summaries).to_csv(output_dir / "loading.csv")
        prices.to_csv(output_dir / "congestion_prices.csv")
        for label, run in runs.items():
            frames = {}
            for name, result in run.dispatches.items():
                frame = result.device_dispatch.copy()
                frame["grid_import"] = result.grid_import
                frame["grid_export"] = result.grid_export
                frames[name] = frame
            pd.concat(frames, axis=1).to_csv(output_dir / f"dispatch_{label}.csv")
        feeder_loading_plot({label: run.flows for label, run in runs.items()}, built.feeder, title=built.config.name).savefig(
            output_dir / "feeder_loading.png", dpi=120
        )
        results = {
            label: {"total": run.total, "customer_bills": float(run.house_costs["bill"].sum()),
                    "network_costs": run.network_costs,
                    "peak_kw": {e.name: float(run.flows[e.name].max()) for e in built.feeder.elements}}
            for label, run in runs.items()
        }
        _write_checks(output_dir, scenario, config, report, results)
        import matplotlib.pyplot as plt

        plt.close("all")  # charts are saved; free them (matters when runs are scripted in a loop)
        typer.echo(f"\nWrote CSVs, charts and run_record.json to {output_dir}")


def _print_dr(study, config, r2_bill) -> None:
    """The DR section of `der-opt run`."""
    s, perf, cap = study.summary, study.performance, study.capacity
    typer.echo(f"\nDemand response: program {s['program']} (response {s['response']} settled on {s['settlement_basis']}, "
               f"{s['customer_class']}, "
               f"events from {s['constructor']}, {len(study.calendars)} calendar(s))")
    if config.dr.program.capacity_reference == "all":
        chosen = ["R2", "R3", "S"]
    else:
        chosen = [config.dr.program.capacity_reference]
    typer.echo("  References: R1 counterfactual, R2 non-responding twin, R3 no measure, S settlement baseline")
    typer.echo("  Each figure is net / import: net counts exports as reduction, import counts grid import only")
    if len(perf):
        typer.echo(f"  Accounted (called events, mean kW reduction; {len(perf)} event-runs):")
        refs = ["R1"] + chosen
        typer.echo("    month  events  hours  " + "  ".join(f"{'vs ' + r:>13}" for r in refs))
        for month, group in perf.groupby("month"):
            n = len(group) / len(study.calendars)
            cells = []
            for r in refs:
                pair = [np.average(group[f"{pre}reduction_vs_{r}_kw"], weights=group["hours"]) for pre in ("", "import_")]
                cells.append(f"{pair[0]:6.2f} /{pair[1]:5.2f}")
            typer.echo(f"    {month:>5}  {n:>6.1f}  {group['hours'].sum() / len(study.calendars):>5.1f}  " + "  ".join(cells))
    counted = cap[cap["shadow_days"] > 0]
    if len(counted):
        typer.echo(f"  Counted (shadow events of {config.dr.program.max_hours:g} h on every eligible day; mean kW, net / import):")
        for _, row in counted.iterrows():
            parts = [f"vs {r} {row[f'counted_{r}_kw']:.2f} / {row[f'counted_import_{r}_kw']:.2f}"
                     for r in chosen if f"counted_{r}_kw" in row and pd.notna(row[f"counted_{r}_kw"])]
            typer.echo(f"    month {int(row['month']):>2}: {int(row['shadow_days'])} days, " + "; ".join(parts))
    if s.get("incentive_earned") is not None:
        typer.echo(f"  Incentive earned at settlement ({s['settlement_basis']} basis): ${s['incentive_earned']:,.2f} per calendar")
    if r2_bill is not None and study.responding:
        from der_opt.billing import bill_from_dispatch
        from der_opt.scenarios import build_billing

        tariff, program, kwargs = build_billing(config)
        grid = _time_grid(config)
        bills = [bill_from_dispatch(r, grid, tariff, program, **kwargs).summary["net_cost"] for r in study.responding]
        typer.echo(f"  Bill: responding ${np.mean(bills):,.2f} vs non-responding ${r2_bill.summary['net_cost']:,.2f}")


def _time_grid(config):
    from der_opt.scenarios.loader import _build_time_grid

    return _build_time_grid(config.time_grid)


def _write_dr(study, output_dir: Path) -> None:
    import pandas as pd

    study.events.to_csv(output_dir / "dr_events.csv", index=False)
    study.performance.to_csv(output_dir / "dr_event_performance.csv", index=False)
    study.capacity.to_csv(output_dir / "dr_capacity.csv", index=False)
    study.slice_of_day.to_csv(output_dir / "dr_capacity_slice_of_day.csv", index=False)
    frames = {f"replicate_{i}": r.grid_import - r.grid_export for i, r in enumerate(study.responding)}
    frames |= {name: r.grid_import - r.grid_export for name, r in study.references.items()}
    pd.DataFrame(frames).to_csv(output_dir / "dr_net_load_kw.csv")


@app.command()
def run(
    scenario: Path = typer.Argument(..., help="Path to a scenario YAML file"),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Directory to write dispatch CSVs and charts to"
    ),
    duals: bool = typer.Option(False, "--duals", help="Print (and with -o, write) dual/shadow prices"),
    no_counterfactual: bool = typer.Option(
        False, "--no-counterfactual", help="Skip the scenario's counterfactual comparison"
    ),
) -> None:
    """Solve a scenario and print a summary. If the scenario has a `counterfactual`
    section, also simulates that rule-based baseline and reports the value of
    optimization. A scenario with a `feeder` section is a multi-house run: it compares
    coordinated, independent and rule-based dispatch on the network. With
    --output-dir, writes CSVs and charts for everything printed."""
    import pandas as pd

    from der_opt.pricing import extract_duals, value_breakdown
    from der_opt.scenarios import (
        build_billing,
        build_counterfactual_policies,
        build_premise,
        is_feeder_scenario,
        load_scenario_config,
    )
    from der_opt.simulation import CounterfactualSimulator

    if is_feeder_scenario(scenario):
        if duals:
            typer.echo("(feeder runs always report congestion prices; --duals adds nothing)")
        _run_feeder(scenario, output_dir, no_counterfactual)
        return

    from der_opt.diagnostics import (
        CheckReport,
        check_dispatch,
        check_end_states,
        check_load,
        check_prices,
        check_tariff_dates,
        collect_data_warnings,
    )

    report = CheckReport()
    config = load_scenario_config(scenario)
    with collect_data_warnings(report):
        premise = build_premise(config)
    dispatch, model, backend = premise.solve()

    typer.echo(f"Scenario: {premise.name}")
    typer.echo(f"Status: {dispatch.solver_status}")

    breakdown = value_breakdown(premise, dispatch)
    twin = " (no DR events: the non-responding twin, R2)" if config.dr is not None else ""
    typer.echo(f"\nOptimal dispatch{twin}: ${sum(breakdown.values()):.2f}")
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

    dr_study = None
    if config.dr is not None:
        from der_opt.dr import run_dr_study

        dr_study = run_dr_study(config, r2=dispatch, r2_premise=premise)
        _print_dr(dr_study, config, bills.get("optimal"))
        report.extend(dr_study.checks)

    report.extend(check_prices(premise))
    report.extend(check_load(premise))
    runs_checked = {"optimal": dispatch} | ({"counterfactual": counterfactual} if counterfactual is not None else {})
    for label, result in runs_checked.items():
        report.extend(check_dispatch(premise, result, run=label))
    report.extend(check_end_states(premise, runs_checked))
    if billing is not None:
        report.extend(check_tariff_dates(billing[0], premise.time_grid))
    _print_checks(report)

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

        results = {"status": dispatch.solver_status, "optimal": breakdown,
                   "counterfactual": cf_breakdown,
                   "bills": {label: bill.summary["net_cost"] for label, bill in bills.items()}}
        if dr_study is not None:
            _write_dr(dr_study, output_dir)
            results["dr"] = dr_study.summary
        _write_checks(output_dir, scenario, config, report, results)
        import matplotlib.pyplot as plt

        plt.close("all")  # charts are saved; free them (matters when runs are scripted in a loop)
        typer.echo(f"\nWrote CSVs, charts and run_record.json to {output_dir}")


@app.command("fetch-lmp")
def fetch_lmp(
    node: str = typer.Argument(..., help='CAISO pricing node, e.g. TH_NP15_GEN-APND (NP15 trading hub)'),
    start: str = typer.Argument(..., help="First day, YYYY-MM-DD (from local midnight)"),
    end: str = typer.Argument(..., help="Day after the last day, YYYY-MM-DD (exclusive)"),
    output: Path = typer.Option(..., "--output", "-o", help="CSV to write"),
    market: str = typer.Option("DAM", help="DAM (day-ahead, hourly), RTPD (15-min) or RTM (5-min)"),
    resolution_minutes: int = typer.Option(60, help="Resolution of the scenario that will read it"),
) -> None:
    """Download CAISO LMP for one node and write it as a scenario price CSV
    (timestamp on the local clock, lmp_per_kwh). Use it in a scenario as
    {type: csv, path: <output>, column: lmp_per_kwh}."""
    from datetime import date

    from der_opt.integrations.caiso_lmp import (
        fetch_caiso_lmp,
        lmp_price_table,
        write_lmp_csv,
    )

    first, stop = date.fromisoformat(start), date.fromisoformat(end)
    raw = fetch_caiso_lmp(node, first, stop, market=market)
    table = lmp_price_table(raw, resolution_minutes=resolution_minutes, node=node)
    write_lmp_csv(table, output)
    prices = table["lmp_per_kwh"]
    typer.echo(
        f"{node} {market}: {len(raw)} intervals -> {len(table)} rows at {resolution_minutes} min, "
        f"{table.index[0]} to {table.index[-1]}"
    )
    typer.echo(f"  $/kWh: min {prices.min():.4f}, mean {prices.mean():.4f}, max {prices.max():.4f}")
    typer.echo(f"Wrote {output}")


def _print_data_report(summary: dict, report) -> None:
    for key, value in summary.items():
        if key == "energy":
            for premise, columns in value.items():
                described = ", ".join(f"{c} {v['kwh']:,} kWh (peak {v['peak_kw']} kW)" for c, v in columns.items())
                typer.echo(f"  {premise}: {described}")
        else:
            typer.echo(f"  {key}: {value}")
    typer.echo(f"\nChecks: {report.summary()}")
    for line in report.lines():
        typer.echo(f"  {line}")


@app.command("check-data")
def check_data(
    path: Path = typer.Argument(..., help="A time series, premises or network CSV"),
    premises: Path | None = typer.Option(None, "--premises", help="premises.csv, to cross-check premise IDs"),
    network: Path | None = typer.Option(None, "--network", help="network.csv, to cross-check element IDs"),
) -> None:
    """Check a file against the input data spec (docs/INPUT_DATA_SPEC.md). Errors mean
    the file won't load; warnings mean look before trusting it. Exits 1 on errors."""
    from der_opt.inputs import SPEC_VERSION, check_file

    kind, report, summary = check_file(path, premises=premises, network=network)
    typer.echo(f"{path}: {kind} file, input data spec v{SPEC_VERSION}")
    _print_data_report({k: v for k, v in summary.items() if k not in ("kind", "spec_version")}, report)
    if report.errors:
        typer.echo("\nRejected: fix the errors above; the file won't load until then.")
        raise typer.Exit(code=1)
    typer.echo("\nPasses the spec.")

if __name__ == "__main__":
    app()
