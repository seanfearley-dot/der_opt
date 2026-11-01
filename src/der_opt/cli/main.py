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


@app.command()
def run(
    scenario: Path = typer.Argument(..., help="Path to a scenario YAML file"),
    output_dir: Optional[Path] = typer.Option(
        None, "--output-dir", "-o", help="Directory to write dispatch CSVs and charts to"
    ),
    duals: bool = typer.Option(False, "--duals", help="Print the dual/shadow-price summary"),
) -> None:
    """Solve a scenario (built on the same Premise/Device objects as the Python API)
    and print a summary; optionally write dispatch CSVs and charts to --output-dir."""
    from der_opt.pricing import extract_duals, value_breakdown
    from der_opt.scenarios import load_scenario

    premise = load_scenario(scenario)
    dispatch, model, backend = premise.solve()

    typer.echo(f"Scenario: {premise.name}")
    typer.echo(f"Status: {dispatch.solver_status}")
    typer.echo(f"Objective: ${dispatch.objective_value:.2f}")

    breakdown = value_breakdown(premise, dispatch)
    typer.echo("\nValue breakdown:")
    for label, value in breakdown.items():
        typer.echo(f"  {label}: ${value:.2f}")

    if duals:
        dual_results = extract_duals(model, backend)
        typer.echo("\nDual/shadow-price families:")
        for name, dual in dual_results.items():
            note = " (fix-and-resolve approximation, not exact)" if not dual.is_exact else ""
            typer.echo(f"  {name}: is_exact={dual.is_exact}{note}")

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        dispatch.device_dispatch.to_csv(output_dir / "device_dispatch.csv")
        dispatch.grid_import.to_csv(output_dir / "grid_import.csv", header=["grid_import_kw"])
        dispatch.grid_export.to_csv(output_dir / "grid_export.csv", header=["grid_export_kw"])

        from der_opt.viz import dispatch_plot, value_stack

        dispatch_plot(dispatch, title=premise.name).savefig(output_dir / "dispatch.png")
        value_stack(breakdown, title=premise.name).savefig(output_dir / "value_stack.png")
        typer.echo(f"\nWrote dispatch CSVs and charts to {output_dir}")


if __name__ == "__main__":
    app()
