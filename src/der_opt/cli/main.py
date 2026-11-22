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


def _write_checks(output_dir: Path, scenario: Path, config, report, results: dict, composed=None) -> Path:
    from der_opt.run_record import write_run_record

    report.to_frame().to_csv(output_dir / "checks.csv", index=False)
    return write_run_record(output_dir, scenario, config.model_dump(mode="json"), report, results, composed)


def _validate_first(scenario: Path, cost_tests: bool, openbca_path: Path | None = None):
    """Runs `der-opt validate`'s checks; stops the run on any error."""
    import time

    from der_opt.scenarios.validate import validate_scenario

    started = time.perf_counter()
    validation = validate_scenario(scenario, cost_tests=cost_tests, before_run=True, openbca=openbca_path)
    if not validation.ok:
        typer.echo(f"{scenario} didn't validate, so nothing was solved:")
        for line in validation.report.lines():
            if line.startswith("ERROR"):
                typer.echo(f"  {line}")
        typer.echo("Fix the errors above (`der-opt validate` shows every finding), or pass --no-validate.")
        raise typer.Exit(code=1)
    warned = len(validation.report.warnings)
    typer.echo(f"Validated in {time.perf_counter() - started:.1f} s"
               + (f" ({warned} warning{'s' if warned != 1 else ''}, listed with the checks below)" if warned else ""))
    return validation


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


def _run_feeder(scenario: Path, output_dir: Path | None, no_counterfactual: bool, validation=None) -> None:
    import pandas as pd

    from der_opt.diagnostics import CheckReport, collect_data_warnings
    from der_opt.network import loading_summary
    from der_opt.scenarios import (
        build_feeder_scenario,
        load_feeder_config,
        run_feeder_study,
    )

    if validation is not None:
        report, config, built, composed = validation.report, validation.config, validation.feeder, validation.composed
    else:
        from der_opt.scenarios.compose import load_composed

        report = CheckReport()
        composed = load_composed(scenario)
        config = load_feeder_config(scenario)
        with collect_data_warnings(report):
            built = build_feeder_scenario(config)
    study = run_feeder_study(built, counterfactual=not no_counterfactual)
    report.extend(study.checks)
    runs = study.runs
    time_grid = built.premises[0].time_grid

    typer.echo(f"Feeder scenario: {built.config.name}")
    typer.echo(f"{len(built.premises)} premises on {len(built.feeder.elements)} network elements")
    counts = built.houses.groupby("element")["devices"].apply(
        lambda d: ", ".join(f"{sum(k in x.split(', ') for x in d)} {k}" for k in ("solar", "battery", "ev"))
    )
    for element, described in counts.items():
        typer.echo(f"  {element}: {(built.houses['element'] == element).sum()} premises ({described})")

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
    columns = {label: max(16, len(label) + 2) for label in runs}
    typer.echo(" " * (width + 2) + "".join(f"{label:>{columns[label]}}" for label in runs))
    for name, values in rows.items():
        typer.echo(f"  {name:<{width}}" + "".join(f"{_money(values[k]):>{columns[k]}}" for k in runs))

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
        typer.echo("\nCoordination vs each premise optimizing alone:")
        if worse.empty:
            typer.echo("  no premise pays more")
        else:
            typer.echo(
                f"  {len(worse)} of {len(change)} premises pay more (by {_money(worse.sum())} in total, "
                f"at most {_money(worse.max())}, {worse.idxmax()}), for a network saving of "
                f"{_money(sum(runs['independent'].network_costs.values()) - sum(runs['coordinated'].network_costs.values()))}"
            )
    if study.coordination is not None:
        _print_coordination(study)
    _print_checks(report)

    if output_dir is not None:
        from der_opt.viz import feeder_loading_plot

        output_dir.mkdir(parents=True, exist_ok=True)
        from der_opt.population.report import parameters_table, premises_table

        table = premises_table(built.resolved, built.premises)
        for label, run in runs.items():  # each premise's bill in each run
            table[f"bill_{label}"] = table["premise_id"].map(run.house_costs["bill"].round(2))
        table.to_csv(output_dir / "premises.csv", index=False)
        parameters_table(built.resolved).to_csv(output_dir / "device_parameters.csv", index=False)
        if study.coordination is not None:
            study.coordination_table().to_csv(output_dir / "coordination.csv")
            study.customers_table().to_csv(output_dir / "customers.csv", index=False)
            for label, run in study.coordination.runs.items():
                slug = label.replace(" ", "_").replace("(", "").replace(")", "")
                if run.envelopes is not None:
                    run.envelopes.to_csv(output_dir / f"envelopes_{slug}.csv", index=False)
                if run.signal is not None:
                    run.signal.to_csv(output_dir / f"signal_{slug}.csv", index_label="timestamp")
                if run.calendar is not None:
                    run.calendar.to_csv(output_dir / f"dr_calendar_{slug}.csv", index=False)
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
            slug = label.replace(" ", "_").replace("(", "").replace(")", "")
            pd.concat(frames, axis=1).to_csv(output_dir / f"dispatch_{slug}.csv")
        feeder_loading_plot({label: run.flows for label, run in runs.items()}, built.feeder, title=built.config.name).savefig(
            output_dir / "feeder_loading.png", dpi=120
        )
        results = {
            label: {"total": run.total, "customer_bills": float(run.house_costs["bill"].sum()),
                    "network_costs": run.network_costs,
                    "peak_kw": {e.name: float(run.flows[e.name].max()) for e in built.feeder.elements}}
            for label, run in runs.items()
        }
        _write_checks(output_dir, scenario, config, report, results, composed)
        from der_opt.results.summary import feeder_summary, write_summary

        write_summary(output_dir, feeder_summary(built, study, summaries, report))
        import matplotlib.pyplot as plt

        plt.close("all")  # charts are saved; free them (matters when runs are scripted in a loop)
        typer.echo(f"\nWrote CSVs, charts, summary.json and run_record.json to {output_dir}")


def _print_coordination(study) -> None:
    """The coordination runs against the ideal (docs/COORDINATION_SPEC.md section 6)."""
    table = study.coordination_table()
    houses = study.scenario.houses
    typer.echo("\nCoordination runs (each against independent, the feeder with no program):")
    groups = houses["group"].value_counts()
    typer.echo("  customers: " + ", ".join(f"{n} {g}" for g, n in groups.items())
               + (f"; background load: {', '.join(study.scenario.background)}" if study.scenario.background else ""))
    for label, run in study.coordination.runs.items():
        row = table.loc[label]
        share = row.get("share_of_ideal")
        saving, ideal = row.get("network_saving", 0.0), row.get("ideal_network_saving", 0.0)
        effect = f"saves {_money(saving)} in" if saving >= 0 else f"adds {_money(-saving)} to"
        share_text = (f"{effect} network cost (the ideal saves {_money(ideal)}"
                      + (f"; {100 * share:.0f}% of it)" if share is not None and pd.notna(share) else ")"))
        mechanisms = ", ".join(run.mechanisms)
        named = label if mechanisms == label else f"{label} ({mechanisms})"
        typer.echo(f"  {named}: network cost {_money(row['network_cost'])}, {share_text}")
        paid = []
        for column, text in (("signal_charges", "signal charges"), ("dr_incentive", "DR incentives")):
            if column in row and abs(row[column]) > 0.005:
                paid.append(f"{text} {_money(row[column])}")
        for column, text in (("dr_shortfall_kwh", "DR shortfall"), ("envelope_over_import_kwh", "over import envelopes"),
                             ("envelope_over_export_kwh", "over export envelopes")):
            if column in row and row[column] > 0.005:
                paid.append(f"{text} {row[column]:.1f} kWh")
        if run.calendar is not None:
            paid.append(f"{len(run.calendar)} events (triggered and settled on {run.twin})")
        if paid:
            typer.echo(f"    {'; '.join(paid)}")


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


def _print_screening(frame, acs, without_label: str, chosen: str) -> None:
    typer.echo(f"\nScreening values, first year ({acs.manifest.set_id}, {acs.manifest.dollars.basis} $): "
               "not cost-test results")
    typer.echo(f"  Measure vs {without_label}")
    energy = frame[frame["category"] == "energy"]
    lowered = energy.loc[energy["part"] == "load lowered", "value"].sum()
    raised = energy.loc[energy["part"] == "load raised", "value"].sum()
    typer.echo(f"  Energy: ${lowered:,.2f} avoided where load is lowered, ${raised:,.2f} where it's raised, "
               f"net ${lowered + raised:,.2f}")
    full = frame.loc[frame["category"] == "full_hourly", "value"].sum()
    typer.echo(f"  Full hourly (every component, for comparison only): ${full:,.2f}")
    capacity = frame[frame["category"] == "dr_capacity"]
    if len(capacity):
        for label, group in capacity.groupby("part"):
            marker = "  <- valued" if label == chosen else ""
            typer.echo(f"  DR capacity {label}: ${group['value'].sum():,.2f}{marker}")
    emissions = frame.loc[frame["category"] == "emissions", "value"]
    if len(emissions):
        typer.echo(f"  Emissions avoided: {emissions.sum():,.3f} tCO2")


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
    scenario: Path = typer.Argument(..., help="Path to a scenario or study YAML file"),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", "-o", help="Directory to write dispatch CSVs and charts to"
    ),
    duals: bool = typer.Option(False, "--duals", help="Print (and with -o, write) dual/shadow prices"),
    no_counterfactual: bool = typer.Option(
        False, "--no-counterfactual", help="Skip the scenario's counterfactual comparison"
    ),
    cost_tests: bool = typer.Option(
        False, "--cost-tests", help="Also run the cost tests in OpenBCA (needs a cost_test section; "
        "output goes to -o, default out/<scenario name>)"),
    openbca_path: Path | None = typer.Option(
        None, "--openbca", help="OpenBCA checkout for --cost-tests (default: from `der-opt setup openbca`)"),
    no_validate: bool = typer.Option(
        False, "--no-validate", help="Skip the checks `der-opt validate` runs before solving"),
    keep_openbca: bool = typer.Option(
        False, "--keep-openbca", help="With --cost-tests: keep OpenBCA's working files (about 36 MB a test), "
        "not only its results"),
) -> None:
    """Solve a scenario or a study and print a summary. Runs `der-opt validate`'s checks
    first and stops on any error, before solving anything.

    - A single-home scenario: optimal and (with a `counterfactual` section) rule-based
      runs, bills, DR, screening values and the measure impact package.
    - A scenario with a `feeder` section: coordinated, independent and rule-based runs
      on the network, and any coordination runs.
    - A study (a `population` section, docs/POPULATION_MODEL_SPEC.md): its feeder and
      portfolio analyses, each in its own folder under -o.

    With --output-dir, writes CSVs and charts for everything printed."""
    from der_opt.scenarios.compose import CompositionError, compose_scenario

    try:
        sections = compose_scenario(scenario).data
    except CompositionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    from der_opt.scenarios.schema import is_study

    if is_study(sections):
        _run_study(scenario, output_dir, no_counterfactual, cost_tests, openbca_path, no_validate, keep_openbca)
        return
    _run_scenario(scenario, output_dir, duals, no_counterfactual, cost_tests, openbca_path, no_validate, keep_openbca)


def _run_study(study_path: Path, output_dir: Path | None, no_counterfactual: bool, cost_tests: bool,
               openbca_path: Path | None, no_validate: bool, keep_openbca: bool = False) -> None:
    """A study's analyses (docs/POPULATION_MODEL_SPEC.md section 4), each in its own folder."""
    import contextlib
    import io
    from types import SimpleNamespace

    import yaml

    from der_opt.diagnostics import CheckReport
    from der_opt.population.compile import resolve_study
    from der_opt.population.portfolio import portfolio_premises, summarize
    from der_opt.population.report import parameters_table, premises_table
    from der_opt.run_record import write_run_record
    from der_opt.scenarios.compose import load_composed
    from der_opt.scenarios.feeder import feeder_from_study
    from der_opt.scenarios.schema import StudyConfig

    composed = load_composed(study_path)
    study = StudyConfig.model_validate(composed.data)
    if cost_tests and (study.analyses.portfolio is None or study.analyses.portfolio.cost_test is None):
        raise typer.BadParameter("--cost-tests needs a portfolio analysis with a cost_test section")
    validation = None if no_validate else _validate_first(study_path, cost_tests, openbca_path)
    output_dir = output_dir or Path("out") / study.name
    output_dir.mkdir(parents=True, exist_ok=True)
    report = validation.report if validation is not None else CheckReport()
    details: dict = {}
    resolved = validation.resolved if validation is not None else resolve_study(study, details)[0]
    additions = validation.additions if validation is not None else details.get("add_devices")
    feeder = validation.feeder if validation is not None else None
    typer.echo(f"Study: {study.name}: {len(resolved)} premises")
    _print_additions(additions)
    built = feeder.premises if feeder is not None else None
    premises_table(resolved, built).to_csv(output_dir / "premises.csv", index=False)
    parameters_table(resolved).to_csv(output_dir / "device_parameters.csv", index=False)
    results: dict = {"premises": len(resolved), "analyses": [k for k in ("feeder", "portfolio")
                                                             if getattr(study.analyses, k) is not None]}
    if additions:
        results["add_devices"] = additions

    if study.analyses.feeder is not None:
        typer.echo("\n=== Feeder analysis")
        feeder = feeder or feeder_from_study(study)
        stand_in = SimpleNamespace(report=CheckReport(), config=feeder.config, feeder=feeder, composed=composed)
        _run_feeder(study_path, output_dir / "feeder", no_counterfactual, stand_in)

    if study.analyses.portfolio is not None:
        typer.echo("\n=== Portfolio analysis")
        premises = (validation.portfolio if validation is not None and validation.portfolio is not None
                    else portfolio_premises(study, resolved)[0])
        folder = output_dir / "portfolio"
        failed = []
        for item in premises:
            if item.config is None:
                typer.echo(f"  {item.premise_id}: {item.note}")
                continue
            sub = folder / item.premise_id
            sub.mkdir(parents=True, exist_ok=True)
            scenario_file = sub / "scenario.yaml"
            text = yaml.safe_dump(item.config.model_dump(mode="json", exclude_none=True, by_alias=True), sort_keys=False)
            scenario_file.write_text(text, encoding="utf-8")
            captured = io.StringIO()
            try:
                with contextlib.redirect_stdout(captured):
                    _run_scenario(scenario_file, sub, no_counterfactual=no_counterfactual, cost_tests=cost_tests,
                                  openbca_path=openbca_path, no_validate=True, keep_openbca=keep_openbca)
            except Exception as exc:  # noqa: BLE001 -- one premise failing doesn't stop the portfolio
                failed.append(item.premise_id)
                item.note = f"failed: {type(exc).__name__}: {exc}"
                typer.echo(f"  {item.premise_id}: {item.note}")
            finally:
                (sub / "report.txt").write_text(captured.getvalue(), encoding="utf-8")
            if item.premise_id not in failed:
                typer.echo(f"  {item.premise_id}: measure {', '.join(item.measure) or '-'}; report in {sub / 'report.txt'}")
        table = summarize(folder, premises)
        folder.mkdir(parents=True, exist_ok=True)
        table.to_csv(folder / "portfolio.csv", index=False)
        total = table.iloc[-1]
        shown = [c for c in table.columns if c.startswith(("value_of_optimization", "dr_incentive", "counted_",
                                                            "screening"))
                 and c.endswith(("optimization", "earned", "season_mean_kw", "total"))]
        typer.echo("  Totals: " + "; ".join(f"{c} {total[c]:,.2f}" for c in shown if pd.notna(total.get(c))))
        typer.echo(f"  Wrote {folder / 'portfolio.csv'} (one row per premise, and a total)")
        results["portfolio"] = {"premises": len(premises), "failed": failed}
    from der_opt.results.summary import study_summary, write_summary

    write_run_record(output_dir, study_path, study.model_dump(mode="json"), report, results, composed)
    portfolio = (premises, failed, table) if study.analyses.portfolio is not None else (None, None, None)
    write_summary(output_dir, study_summary(study, resolved, additions, *portfolio, report=report))
    if study.analyses.portfolio is not None and failed:
        raise typer.Exit(code=1)
    typer.echo(f"\nWrote premises.csv, device_parameters.csv, summary.json and run_record.json to {output_dir}")


def _print_additions(additions) -> None:
    """What each of a study's add_devices entries chose."""
    for entry in additions or []:
        typer.echo(f"  {entry['entry']}: {entry['device_id']} added at {len(entry['chosen'])} of {entry['eligible']} "
                   f"eligible premises ({entry['selected']} selected, {len(entry['already_have'])} already had one"
                   + (f", {len(entry['metered'])} metered" if entry["metered"] else "") + ")")


def _run_scenario(scenario: Path, output_dir: Path | None = None, duals: bool = False, no_counterfactual: bool = False,
                  cost_tests: bool = False, openbca_path: Path | None = None, no_validate: bool = False,
                  keep_openbca: bool = False) -> None:
    """One scenario: a single home, or a feeder."""
    import pandas as pd

    from der_opt.pricing import extract_duals, value_breakdown
    from der_opt.scenarios import (
        build_billing,
        build_counterfactual_policies,
        build_premise,
        load_scenario_config,
    )
    from der_opt.scenarios.compose import CompositionError, compose_scenario
    from der_opt.simulation import CounterfactualSimulator

    try:
        sections = compose_scenario(scenario).data
    except CompositionError as exc:
        raise typer.BadParameter(str(exc)) from exc
    feeder = "feeder" in sections
    if feeder and cost_tests:
        raise typer.BadParameter("--cost-tests is for single-measure runs; feeder scenarios don't have cost tests yet")
    if cost_tests and sections.get("cost_test") is None:
        raise typer.BadParameter("--cost-tests needs a cost_test section in the scenario")
    validation = None if no_validate else _validate_first(scenario, cost_tests, openbca_path)
    if feeder:
        if duals:
            typer.echo("(feeder runs always report congestion prices; --duals adds nothing)")
        _run_feeder(scenario, output_dir, no_counterfactual, validation)
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

    if validation is not None:
        report, config, premise, composed = validation.report, validation.config, validation.premise, validation.composed
    else:
        from der_opt.scenarios.compose import load_composed

        report = CheckReport()
        composed = load_composed(scenario)
        config = load_scenario_config(scenario)
        with collect_data_warnings(report):
            premise = build_premise(config)
    if cost_tests:
        from der_opt.settings import openbca_dir

        try:
            openbca_dir(openbca_path)  # fail now, not after the solves
        except FileNotFoundError as exc:
            raise typer.BadParameter(str(exc)) from exc
        output_dir = output_dir or Path("out") / config.name
    dispatch, model, backend = premise.solve()

    typer.echo(f"Scenario: {premise.name}")
    typer.echo(f"Status: {dispatch.solver_status}")
    from der_opt.devices.ev_pattern import scenario_patterns

    ev_patterns = []
    for name, pattern, expanded in scenario_patterns(config.devices, premise.time_grid):
        described = expanded.summary(premise.time_grid, pattern.kwh_per_mile)
        ev_patterns.append({"device": name, "summary": described})
        typer.echo(f"EV {name} (daily pattern): {described}")

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

    screening, impact_runs, screening_info = None, None, None
    set_path = (config.cost_test.avoided_costs.set if config.cost_test is not None
                else config.avoided_costs.set if config.avoided_costs is not None else None)
    if set_path is not None:
        from der_opt.avoided_costs import read_set
        from der_opt.avoided_costs.screening import capacity_measures, screening_values
        from der_opt.cost_test import resolve_runs

        acs = read_set(set_path)
        without, with_, without_label = resolve_runs(config, premise, dispatch, counterfactual, dr_study)
        impact_runs = (without, with_, without_label)
        capacity_kw = capacity_measures(dr_study.capacity) if dr_study is not None else {}
        screening = screening_values(acs, without.grid_import - without.grid_export,
                                     with_.grid_import - with_.grid_export, premise.time_grid.dt_hours, capacity_kw)
        chosen = config.cost_test.capacity_value.label if config.cost_test else "R2/import/counted"
        _print_screening(screening, acs, without_label, chosen)
        screening_info = {"set_id": acs.manifest.set_id, "dollars": acs.manifest.dollars.basis,
                          "measure_vs": without_label, "capacity_valued": chosen}
        report.ran("avoided cost set")

    if validation is None:  # validation ran these
        report.extend(check_prices(premise))
        report.extend(check_load(premise))
    runs_checked = {"optimal": dispatch} | ({"counterfactual": counterfactual} if counterfactual is not None else {})
    for label, result in runs_checked.items():
        report.extend(check_dispatch(premise, result, run=label))
    report.extend(check_end_states(premise, runs_checked))
    if billing is not None and validation is None:
        report.extend(check_tariff_dates(billing[0], premise.time_grid))
    _print_checks(report)

    if output_dir is not None:
        from der_opt.viz import comparison_plot, dispatch_plot, duals_plot, value_stack

        output_dir.mkdir(parents=True, exist_ok=True)
        dispatch.device_dispatch.to_csv(output_dir / "device_dispatch.csv")
        from der_opt.population.compile import resolve_scenario
        from der_opt.population.report import parameters_table

        raw = validation.config if validation is not None else config  # raw keeps template sources
        parameters_table([resolve_scenario(raw)]).to_csv(output_dir / "device_parameters.csv", index=False)
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
        if screening is not None:
            screening.to_csv(output_dir / "avoided_costs.csv", index=False)
            results["screening"] = screening.groupby(["category", "part"])["value"].sum().round(2).to_dict()
            results["screening"] = {f"{k[0]}: {k[1]}": v for k, v in results["screening"].items()}
        if config.cost_test is not None:
            from der_opt.billing import bill_from_dispatch
            from der_opt.cost_test import write_impact_package

            without, with_, without_label = impact_runs
            package_bills = None
            if billing is not None:
                tariff, program, bill_kwargs = billing
                package_bills = {label: bill_from_dispatch(r, premise.time_grid, tariff, program, **bill_kwargs)
                                 for label, r in (("without", without), ("with", with_))}
            write_impact_package(output_dir, config, without_label, without, with_, premise.time_grid,
                                 dr_study, package_bills, tz=config.dr.tz if config.dr else "America/Los_Angeles")
        _write_checks(output_dir, scenario, config, report, results, composed)
        from der_opt.results.summary import scenario_summary, write_summary

        write_summary(output_dir, scenario_summary(config, premise, dispatch, breakdown, counterfactual, cf_breakdown,
                                                   bills, dr_study, screening, screening_info, ev_patterns, report))
        import matplotlib.pyplot as plt

        plt.close("all")  # charts are saved; free them (matters when runs are scripted in a loop)
        typer.echo(f"\nWrote CSVs, charts, summary.json and run_record.json to {output_dir}")
        if cost_tests:
            _cost_tests(output_dir, openbca_path, keep_openbca)


@app.command()
def validate(
    scenario: Path = typer.Argument(..., help="Path to a scenario YAML file"),
    cost_tests: bool = typer.Option(
        False, "--cost-tests", help="Also require what --cost-tests needs (OpenBCA set up, discount rates)"),
    population: Path | None = typer.Option(
        None, "--population", help="Write premises.csv (each premise and its devices) and device_parameters.csv "
        "(every setting's value and source) to this folder"),
) -> None:
    """Check a scenario without solving it: what it extends, its schema, placeholders,
    every file it names, its input data, the population (devices, series, templates,
    what each analysis needs), avoided cost set, tariffs, device names, DR event calendar
    and cost-test settings. Exits 1 on any error."""
    import time

    from der_opt.scenarios.validate import validate_scenario

    started = time.perf_counter()
    result = validate_scenario(scenario, cost_tests=cost_tests)
    if result.composed is not None and result.composed.fragments:
        typer.echo(f"{scenario} extends {len(result.composed.fragments)} file(s):")
        for fragment in result.composed.fragments:
            typer.echo(f"  {fragment}")
    _print_additions(result.additions)
    if result.feeder is not None:
        _print_mix(result.feeder)
    if population is not None and (result.feeder is not None or result.premise is not None):
        written = _write_population(population, result)
        typer.echo(f"Wrote {', '.join(str(w) for w in written)}")
    _print_checks(result.report)
    typer.echo(f"\n({time.perf_counter() - started:.1f} s)")
    if not result.ok:
        typer.echo("Not ready to run: fix the errors above.")
        raise typer.Exit(code=1)
    typer.echo("Ready to run.")


def _resolved_population(validation):
    """(resolved premises, built premises) for a validated scenario."""
    if validation.feeder is not None:
        return validation.feeder.resolved, validation.feeder.premises
    from der_opt.population.compile import resolve_scenario

    return [resolve_scenario(validation.config)], [validation.premise]


def _write_population(folder: Path, validation) -> list[Path]:
    from der_opt.population.report import parameters_table, premises_table

    resolved, built = _resolved_population(validation)
    folder.mkdir(parents=True, exist_ok=True)
    premises_table(resolved, built).to_csv(folder / "premises.csv", index=False)
    parameters_table(resolved).to_csv(folder / "device_parameters.csv", index=False)
    return [folder / "premises.csv", folder / "device_parameters.csv"]


def _print_mix(feeder) -> None:
    """Each element's premises and devices, by type, before anything is solved."""
    from der_opt.population.report import premises_table

    table = premises_table(feeder.resolved, feeder.premises)
    groups = feeder.houses["group"]
    typer.echo(f"Population: {len(table)} premises" + (f", {len(feeder.background)} background loads"
                                                         if feeder.background else ""))
    for element, rows in table.groupby("element_id", sort=False):
        parts = []
        for kind, size, unit in (("solar", "solar_kw", "kW"), ("battery", "battery_kwh", "kWh"), ("ev", "ev_kwh", "kWh"),
                                 ("thermostat", "thermostat_kw", "kW"), ("flexible_load", "flexible_kw", "kW")):
            count = int(rows[f"n_{kind}"].sum()) if f"n_{kind}" in rows else 0
            if count:
                total = rows[size].sum() if size in rows else float("nan")
                extra = f" ({int(rows['ev_v2x'].fillna(False).astype(bool).sum())} V2X)" if kind == "ev" and "ev_v2x" in rows else ""
                parts.append(f"{count} {kind.replace('_', ' ')} {total:,.1f} {unit}{extra}")
        who = groups.loc[rows["premise_id"]].value_counts()
        typer.echo(f"  {element}: {len(rows)} premises ({', '.join(f'{n} {g}' for g, n in who.items())})"
                   + (f"; {', '.join(parts)}" if parts else "; no devices"))


@app.command()
def new(
    template: str | None = typer.Argument(None, help="Template name (see --list)"),
    target: Path | None = typer.Argument(None, help="Scenario file to write"),
    list_templates: bool = typer.Option(False, "--list", help="List the templates"),
    timeseries: Path | None = typer.Option(None, help="Input-spec time series file (load_kw, pv_kw)"),
    premise: str | None = typer.Option(None, help="premise_id in the time series file (default: its only one)"),
    tariff: Path | None = typer.Option(None, help="Retail tariff YAML"),
    program: str = typer.Option("nbt", help="home-bill export program: nbt or nem2"),
    export_rates: Path | None = typer.Option(None, help="NBT export rates CSV"),
    baseline_territory: str | None = typer.Option(None, help="PG&E baseline territory, for tariffs with a baseline"),
    avoided_costs: Path | None = typer.Option(None, help="Avoided cost set folder"),
    price: Path | None = typer.Option(None, help="lmp-battery: price CSV (from `der-opt fetch-lmp`)"),
    premises: Path | None = typer.Option(None, help="feeder-from-files: premises.csv"),
    devices: Path | None = typer.Option(None, help="feeder-from-files: devices.csv (input spec 2.0)"),
    network: Path | None = typer.Option(None, help="feeder-from-files: network.csv"),
    year: int | None = typer.Option(None, help="Run year (default: the time series file's, else this year)"),
    library: Path | None = typer.Option(
        None, help="Templates: put the shared parts in fragment files here. --pick/--use: the library to pick "
        "from (default: `der-opt setup library`'s, else the starter library)"),
    force: bool = typer.Option(False, "--force", help="Overwrite the scenario file if it exists"),
    pick: bool = typer.Option(False, "--pick", help="Choose blocks from the library from menus"),
    use: list[str] | None = typer.Option(None, "--use", help="A library block to extend (repeat for each)"),
) -> None:
    """Write a starting scenario: from a template (`der-opt new TEMPLATE FILE`), or from
    library blocks (`der-opt new FILE --pick`, or `--use BLOCK` for each). What isn't
    filled in is written as TODO; `der-opt validate` lists what's left."""
    if pick or use:
        if template is not None and target is not None:
            raise typer.BadParameter("--pick and --use take the scenario file only, not a template")
        _new_from_blocks(Path(template) if target is None else target, use or [], pick, timeseries, premise,
                         library, force)
        return
    from der_opt.scenarios.templates import TEMPLATES, Options, write_template

    if list_templates or template is None:
        for name, described in TEMPLATES.items():
            typer.echo(f"  {name:<19}{described}")
        if template is None and not list_templates:
            typer.echo("\nUsage: der-opt new TEMPLATE SCENARIO.yaml [options]")
            typer.echo("   or: der-opt new SCENARIO.yaml --pick (menus of library blocks; `der-opt blocks` lists them)")
        return
    if template not in TEMPLATES:
        raise typer.BadParameter(f"no template {template!r}; choose from {', '.join(TEMPLATES)}")
    if target is None:
        raise typer.BadParameter("name the scenario file to write")
    if program not in ("nbt", "nem2"):
        raise typer.BadParameter("--program is nbt or nem2")
    for option, value in (("--timeseries", timeseries), ("--tariff", tariff), ("--export-rates", export_rates),
                          ("--avoided-costs", avoided_costs), ("--price", price), ("--premises", premises),
                          ("--devices", devices), ("--network", network)):
        if value is not None and not value.exists():
            raise typer.BadParameter(f"{option} {value} doesn't exist")
    opts = Options(timeseries=timeseries, premise=premise, tariff=tariff, program=program, export_rates=export_rates,
                   baseline_territory=baseline_territory, avoided_costs=avoided_costs, price=price,
                   premises=premises, devices=devices, network=network, year=year)
    try:
        written = write_template(template, target, opts, library=library, force=force)
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    typer.echo(f"Wrote {written.scenario}")
    for fragment in written.fragments_written:
        typer.echo(f"  new fragment {fragment}")
    for fragment in written.fragments_kept:
        typer.echo(f"  uses existing fragment {fragment} (left as it is)")
    if written.illustrative:
        typer.echo(f"Note: {len(written.illustrative)} of the fragments it uses have values marked illustrative "
                   "(e.g. the starter library's); check them before trusting results")
    if written.todos:
        typer.echo(f"{written.todos} TODO{'s' if written.todos != 1 else ''} to fill in; then: der-opt validate {target}")
    else:
        typer.echo(f"Next: der-opt validate {target}")


def _new_from_blocks(target: Path | None, use: list[str], pick: bool, timeseries: Path | None,
                     premise: str | None, library: Path | None, force: bool) -> None:
    from der_opt.scenarios.library import (
        KINDS,
        find,
        home_data,
        read_library,
        time_matches,
        write_picked,
    )
    from der_opt.settings import library_dir

    if target is None:
        raise typer.BadParameter("name the scenario file to write")
    if timeseries is not None and not timeseries.exists():
        raise typer.BadParameter(f"--timeseries {timeseries} doesn't exist")
    try:
        folder, source = library_dir(library)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    blocks = read_library(folder)
    home = home_data(timeseries, premise)
    try:
        picks = [find(blocks, name) for name in use]
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if pick:
        typer.echo(f"Blocks from {folder} ({source}); Enter skips an optional menu.")
        for kind in KINDS:
            options = [b for b in blocks if b.kind == kind.key and b not in picks]
            if not options:
                continue
            typer.echo(f"\n{kind.title}:")
            width = max(len(b.stem) for b in options) + 2
            for number, block in enumerate(options, 1):
                marks = [m for m, on in (("matches your time series", time_matches(block, home)),
                                         ("illustrative values", block.illustrative)) if on]
                needs = f"  needs: {', '.join(n.split('.')[-1] for n in block.needs)}" if block.needs else ""
                extra = f"  [{'; '.join(marks)}]" if marks else ""
                typer.echo(f"  {number:>2} {block.stem:<{width}}{block.description}{needs}{extra}")
            how = "any, e.g. 1,3" if kind.many else "one"
            while True:
                answer = typer.prompt(f"  {how}{'' if kind.required else ', or Enter'}", default="", show_default=False)
                try:
                    numbers = [int(x) for x in answer.replace(" ", ",").split(",") if x]
                except ValueError:
                    numbers = [0]
                if any(not 1 <= n <= len(options) for n in numbers):
                    typer.echo(f"  numbers 1-{len(options)}, please")
                elif kind.required and not numbers:
                    typer.echo("  this one is needed")
                elif not kind.many and len(numbers) > 1:
                    typer.echo("  just one here")
                else:
                    break
            picks += [options[n - 1] for n in dict.fromkeys(numbers)]
    try:
        written = write_picked(target, picks, home, folder, force)
    except FileExistsError as exc:
        raise typer.BadParameter(str(exc)) from exc
    except ValueError as exc:
        typer.echo(str(exc))
        command = " ".join([f"der-opt new {target}"] + [f"--use {b.name}" for b in picks])
        typer.echo(f"Change the picks and run it again:\n  {command}")
        raise typer.Exit(code=1) from exc
    typer.echo(f"\nWrote {written.scenario}: extends {len(written.blocks)} blocks from {folder}")
    if home.path is not None:
        what = " and ".join(x for x, on in (("solar", "pv_kw" in home.columns), ("load", True)) if on)
        typer.echo(f"  {what} from {home.path} ({home.premise or 'premise_id: TODO'})")
    illustrative = [b.name for b in written.blocks if b.illustrative]
    if illustrative:
        typer.echo(f"Note: {', '.join(illustrative)} hold values marked illustrative; check them before trusting results")
    if written.todos_in_scenario:
        typer.echo(f"{written.todos_in_scenario} TODO(s) to fill in, in {written.scenario.name}")
    for where in written.todos_in_blocks:
        typer.echo(f"TODO left in a block (shared by every scenario that uses it): {where}")
    if pick:
        typer.echo(f"To make this again without the menus:\n  {written.command}")
    typer.echo(f"Next: der-opt validate {written.scenario}")


@app.command()
def blocks(
    library: Path | None = typer.Option(None, help="Library folder (default: `der-opt setup library`'s, "
                                        "else the starter library)"),
) -> None:
    """List a library's blocks, by kind, with what each needs from the scenario."""
    from der_opt.scenarios.library import KINDS, read_library
    from der_opt.settings import library_dir

    try:
        folder, source = library_dir(library)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    found = read_library(folder)
    typer.echo(f"{folder} ({source}): {len(found)} blocks. Use them with `der-opt new FILE --pick` or --use NAME.")
    titles = {k.key: k.title for k in KINDS} | {"study": "Study (objective and defaults for a study's premises)",
                                                "feeder": "Feeder (the old feeder form)"}
    width = max((len(b.name) for b in found), default=0) + 2
    for key in [*[k.key for k in KINDS], "study", "feeder"]:
        group = [b for b in found if b.kind == key]
        if not group:
            continue
        typer.echo(f"\n{titles[key]}:")
        for block in group:
            flags = [f"needs: {', '.join(n.split('.')[-1] for n in block.needs)}"] if block.needs else []
            flags += ["illustrative values"] if block.illustrative else []
            typer.echo(f"  {block.name:<{width}}{block.description}" + (f"  [{'; '.join(flags)}]" if flags else ""))


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
    path: Path = typer.Argument(..., help="A time series, premises or network CSV, or an avoided cost set folder"),
    premises: Path | None = typer.Option(None, "--premises", help="premises.csv, to cross-check premise IDs"),
    network: Path | None = typer.Option(None, "--network", help="network.csv, to cross-check element IDs"),
) -> None:
    """Check a file against the input data spec (docs/INPUT_DATA_SPEC.md). Errors mean
    the file won't load; warnings mean look before trusting it. Exits 1 on errors."""
    from der_opt.inputs import SPEC_VERSION, check_file

    if path.is_dir():  # an avoided cost set (docs/AVOIDED_COST_SPEC.md)
        from der_opt.avoided_costs import describe, read_set

        acs = read_set(path, strict=False)
        report, summary = acs.report, describe(acs)
        typer.echo(f"{path}: avoided cost set, avoided cost spec v1.0")
    else:
        kind, report, summary = check_file(path, premises=premises, network=network)
        typer.echo(f"{path}: {kind} file, input data spec v{SPEC_VERSION}")
    _print_data_report({k: v for k, v in summary.items() if k not in ("kind", "spec_version")}, report)
    if report.errors:
        typer.echo("\nRejected: fix the errors above; the file won't load until then.")
        raise typer.Exit(code=1)
    typer.echo("\nPasses the spec.")

def _cost_tests(run_dir: Path, openbca_path: Path | None, keep_openbca: bool = False) -> None:
    """Export the measure impact package in run_dir to OpenBCA, run it, print results."""
    import json
    import subprocess

    from der_opt.cost_test.openbca import (
        OPENBCA_COMMIT,
        export_openbca,
        run_openbca,
        tally,
    )
    from der_opt.settings import openbca_dir

    try:
        checkout = openbca_dir(openbca_path)
    except FileNotFoundError as exc:
        raise typer.BadParameter(str(exc)) from exc
    head = subprocess.run(["git", "-C", str(checkout), "rev-parse", "--short=7", "HEAD"],
                          capture_output=True, text=True).stdout.strip()
    out = run_dir / "openbca"
    exported = export_openbca(run_dir / "measure_impacts", out)
    typer.echo(f"\nCost tests: OpenBCA at {checkout} (commit {head or 'unknown'})")
    if head and head != OPENBCA_COMMIT:
        typer.echo(f"  Warning: the export was tested against OpenBCA {OPENBCA_COMMIT}, not {head}; "
                   "check results, or run `der-opt setup openbca` to pin the tested commit")
    run_openbca(exported, checkout, out / "run_core.py")
    table = tally(exported)
    table.to_csv(out / "cost_tests.csv", index=False)
    typer.echo("  Present values in $:")
    typer.echo(f"  {'test':<5}{'benefits':>12}{'costs':>12}{'net':>12}{'ratio':>8}   SPM convention: {'benefits':>10}{'costs':>11}{'ratio':>8}")
    for _, r in table.iterrows():
        typer.echo(f"  {r['test']:<5}{r['openbca_benefits']:>12,.2f}{r['openbca_costs']:>12,.2f}{r['net_benefits']:>12,.2f}"
                   f"{r['openbca_ratio']:>8.2f}   {'':16}{r['spm_benefits']:>10,.2f}{r['spm_costs']:>11,.2f}{r['spm_ratio']:>8.2f}")
    notes = {n for e in exported for n in json.loads((e.folder / "test.json").read_text(encoding="utf-8"))["notes"]}
    for note in sorted(notes):
        typer.echo(f"  Note: {note}")
    effect = table.set_index("test")["net_change_if_annual_discounted"]
    if (effect.abs() > 0.005).any():
        typer.echo("  If OpenBCA discounted annual cost fields at each test's rate, net benefits would change by: "
                   + ", ".join(f"{t} {'+' if v >= 0 else '-'}${abs(v):,.2f}" for t, v in effect.items() if abs(v) > 0.005))
    from der_opt.results.summary import update_summary

    update_summary(run_dir, cost_tests=table)
    if keep_openbca:
        typer.echo(f"  Wrote {out / 'cost_tests.csv'}; OpenBCA's own outputs are in {out}/<test>/output/")
        return
    from der_opt.cost_test.openbca import remove_working_files

    freed = remove_working_files(exported)
    typer.echo(f"  Wrote {out / 'cost_tests.csv'}; OpenBCA's results are in {out}/<test>/output/ "
               f"(its working files, {freed / 1e6:,.0f} MB, were removed; --keep-openbca keeps them)")


@app.command()
def openbca(
    run_dir: Path = typer.Argument(..., help="A `der-opt run -o` folder with a measure_impacts/ package"),
    openbca_path: Path | None = typer.Option(
        None, "--openbca", help="An OpenBCA checkout (default: the one `der-opt setup openbca` set up)"),
    keep_openbca: bool = typer.Option(
        False, "--keep-openbca", help="Keep OpenBCA's working files (about 36 MB a test), not only its results"),
) -> None:
    """Run the cost tests in OpenBCA from a measure impact package (docs/COST_TEST_SPEC.md):
    writes OpenBCA's input tables for each test, runs OpenBCA's core on them, and reports
    OpenBCA's results next to an SPM-convention re-tally of the same values."""
    _cost_tests(run_dir, openbca_path, keep_openbca)


convert_app = typer.Typer(help="Convert input files to the current input data spec")
app.add_typer(convert_app, name="convert")


@convert_app.callback()
def _convert() -> None:
    """Convert input files to the current input data spec."""
    return


@convert_app.command("premises")
def convert_premises_command(
    premises: Path = typer.Argument(..., help="A 1.1 premises.csv (with device columns)"),
    timeseries: list[Path] = typer.Option(..., "--timeseries", help="Its time series file(s); repeat for each"),
    out: Path = typer.Option(..., "--out", help="Folder to write the 2.0 premises.csv and devices.csv to"),
) -> None:
    """Write a 1.1 premises file as the 2.0 pair: premises.csv without device columns, and
    devices.csv with one row per device (docs/INPUT_DATA_SPEC.md section 0)."""
    from der_opt.population.convert import convert_premises

    try:
        result = convert_premises(premises, timeseries, out)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    counts = ", ".join(f"{n} {kind}" for kind, n in sorted(result.counts.items())) or "no devices"
    typer.echo(f"Wrote {result.premises} and {result.devices} ({counts}).")
    typer.echo("In the study's population, point premises at the new file and add `devices:` with the devices file.")


setup_app = typer.Typer(help="One-time setup of tools der-opt works with")
app.add_typer(setup_app, name="setup")


@setup_app.callback()
def _setup() -> None:
    """One-time setup of tools der-opt works with."""
    return


@setup_app.command("library")
def setup_library(
    directory: Path = typer.Argument(..., help="A folder of scenario blocks"),
) -> None:
    """Remember the block library `der-opt new --pick` and `der-opt blocks` use."""
    from der_opt.scenarios.library import read_library
    from der_opt.settings import load_settings, save_settings

    folder = directory.expanduser().resolve()
    if not folder.is_dir():
        raise typer.BadParameter(f"{folder} isn't a folder")
    found = read_library(folder)
    if not found:
        raise typer.BadParameter(f"{folder} has no scenario blocks (.yaml files)")
    settings = load_settings()
    settings["library"] = {"path": str(folder)}
    saved = save_settings(settings)
    typer.echo(f"Remembered {folder} ({len(found)} blocks) in {saved}.")


@setup_app.command("openbca")
def setup_openbca(
    directory: Path | None = typer.Option(None, "--dir", help="Where to put OpenBCA (default ~/.der-opt/openbca)"),
    commit: str | None = typer.Option(None, help="Commit to check out (default: the one the export was tested against)"),
) -> None:
    """Install OpenBCA for `der-opt openbca` and `der-opt run --cost-tests`: clone it, check
    out the commit der-opt's export was tested against, install its Python environment
    with uv, and remember where it is."""
    import subprocess

    from der_opt.settings import (
        DEFAULT_OPENBCA_DIR,
        OPENBCA_REPO,
        OPENBCA_TESTED_COMMIT,
        find_uv,
        load_settings,
        save_settings,
    )

    target = (directory or DEFAULT_OPENBCA_DIR).expanduser().resolve()
    commit = commit or OPENBCA_TESTED_COMMIT
    uv = find_uv()
    if uv is None:
        raise typer.BadParameter("uv isn't installed; OpenBCA uses it for its environment. Install it "
                                 "(https://docs.astral.sh/uv/getting-started/installation/) and run this again")

    def step(description: str, command: list[str], cwd: Path | None = None) -> None:
        typer.echo(f"  {description}...")
        result = subprocess.run(command, cwd=cwd, capture_output=True, text=True)
        if result.returncode != 0:
            raise typer.BadParameter(f"{description} failed:\n{(result.stderr or result.stdout)[-1500:]}")

    typer.echo(f"Setting up OpenBCA in {target}")
    if (target / ".git").is_dir():
        step("fetching updates", ["git", "-C", str(target), "fetch", "--quiet", "origin"])
    elif target.exists() and any(target.iterdir()):
        raise typer.BadParameter(f"{target} exists and isn't an OpenBCA checkout; choose another --dir")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        step("cloning OpenBCA", ["git", "clone", "--quiet", OPENBCA_REPO, str(target)])
    step(f"checking out {commit}", ["git", "-C", str(target), "checkout", "--quiet", "--detach", commit])
    step("installing its environment (uv sync)", [uv, "sync", "--quiet"], cwd=target)
    settings = load_settings()
    settings["openbca"] = {"path": str(target), "commit": commit}
    saved = save_settings(settings)
    typer.echo(f"Done. Remembered in {saved}; `der-opt openbca` and `der-opt run --cost-tests` use it.")
    if commit != OPENBCA_TESTED_COMMIT:
        typer.echo(f"Warning: der-opt's export was tested against {OPENBCA_TESTED_COMMIT}, not {commit}.")

if __name__ == "__main__":
    app()
