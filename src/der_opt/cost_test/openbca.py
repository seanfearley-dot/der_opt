"""The OpenBCA export (docs/COST_TEST_SPEC.md v1.1, section 3): from a measure impact
package and its avoided cost set, OpenBCA's input tables for each test, run through
OpenBCA's own core (its "integrated" pathway), plus an SPM-convention re-tally.

Mapping, tested against OpenBCA commit 7e13b36:
  - one OpenBCA configuration per test: cost treatment PCT, RIM, CA TRC (TRC), USI
    (PAC), USI + Non-USI (SCT); its own discount rate; its own stream inclusion
  - two measure rows: `<id>` carries the hours the measure lowers load plus all costs,
    capacity, bills and incentives; `<id>-increases` carries only the hours it raises
    load, so the re-tally can count them as costs (OpenBCA itself nets them)
  - energy: a dimensioned ELECTRIC load shape in standard time, Feb 29 dropped;
    avoided costs re-dated to the analysis year's calendar by month, day and hour
  - DR capacity: "Peak Capacity - Annual" streams, one per capacity component; the
    monthly shares are applied here (value = price x sum of share x kW), so the measure
    carries coincident_peak_savings_kw = 1 and its own avoided-cost subset
  - bills: a custom annual stream, the first year's bill reduction x an escalation
    index; in PCT as a benefit, in RIM as lost revenue (negative), elsewhere excluded
  - DR incentive: utility_incentive_annual_dollar_per_year (held flat over the life)
  - line losses 0: the avoided cost set is at the meter
  - units: OpenBCA (7e13b36) casts avoided costs to DECIMAL(18,3), rounding every value
    to 3 decimals -- $0.0949/kWh becomes $0.095, and tCO2/kWh intensities become 0. So
    the export gives energy in MWh and avoided costs in $/MWh (and the bill escalation
    index x 1000 against savings / 1000): the same dollars, rounded at $0.001/MWh.
    OpenBCA's own "kWh savings" outputs are therefore in MWh.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from der_opt.avoided_costs import AvoidedCostSet, read_set

OPENBCA_COMMIT = "7e13b36"
SCALE = 1000.0  # kWh -> MWh in shapes, $/kWh -> $/MWh in avoided costs (see units above)
COST_TREATMENT = {"PCT": "PCT", "RIM": "RIM", "TRC": "CA TRC", "PAC": "USI", "SCT": "USI + Non-USI"}
COST_CATEGORIES = ("ADMIN", "UTILITY INCENTIVE", "MEASURE COST", "TAX INCENTIVE")
BILLS_STREAM, BILLS_CATEGORY = "Participant bill savings", "PARTICIPANT BILLS"
# OpenBCA maps each measure's five custom impact categories as map keys, so every slot
# needs a distinct, non-empty category (as its own templates fill them); slots 2-5 are
# placeholders, excluded from every test
PLACEHOLDERS = [(f"Unused custom {n}", f"UNUSED_CUSTOM_{n}") for n in range(2, 6)]
CUSTOM_SLOTS = {**{f"custom_{n}_value_stream_name": name for n, (name, _) in zip(range(2, 6), PLACEHOLDERS, strict=True)},
                **{f"custom_{n}_value_stream_impact_category": cat for n, (_, cat) in zip(range(2, 6), PLACEHOLDERS, strict=True)},
                "custom_1_value_stream_name": "Participant bill savings",
                "custom_1_value_stream_impact_category": "PARTICIPANT BILLS"}
GHG_STREAM = "GHG Intensity (E)"

# cost-field streams OpenBCA expects in value_stream_groups (as in its own templates)
COST_STREAMS = [
    ("admin_cost_upfront_dollar", "ADMIN", "Single Value - First Year", "first_year"),
    ("admin_cost_annual_dollar_per_year", "ADMIN", "Time Series - Annual", "annual"),
    ("program_admin_costs_dollar", "ADMIN", "Single Value - First Year", "first_year"),
    ("utility_direct_investment_in_ders_upfront_dollar", "ADMIN", "Single Value - First Year", "first_year"),
    ("utility_incentive_upfront_dollar", "UTILITY INCENTIVE", "Single Value - First Year", "first_year"),
    ("utility_incentive_annual_dollar_per_year", "UTILITY INCENTIVE", "Time Series - Annual", "annual"),
    ("program_utility_incentive_dollar", "UTILITY INCENTIVE", "Single Value - First Year", "first_year"),
    ("incremental_cost_upfront_dollar", "MEASURE COST", "Single Value - First Year", "first_year"),
    ("incremental_cost_annual_dollar_per_year", "MEASURE COST", "Time Series - Annual", "annual"),
    ("host_customer_transaction_cost_upfront_dollar", "MEASURE COST", "Single Value - First Year", "first_year"),
    ("host_customer_interconnection_cost_upfront_dollar", "MEASURE COST", "Single Value - First Year", "first_year"),
    ("host_customer_tax_incentive_upfront_dollar", "TAX INCENTIVE", "Single Value - First Year", "first_year"),
]

SCHEMAS = {  # OpenBCA's openbca_input tables: column -> DuckDB type
    "measures": {
        "id": "VARCHAR", "program_name": "VARCHAR", "measure_id": "VARCHAR", "project_id": "VARCHAR",
        "measure_name": "VARCHAR", "avoided_cost_subset": "VARCHAR", "start_year": "INTEGER", "start_quarter": "INTEGER",
        "measure_specific_discount_rate": "FLOAT", "measure_unit": "VARCHAR", "unit_quantity": "FLOAT",
        "estimated_useful_life": "INTEGER", "net_to_gross_ratio": "FLOAT", "admin_cost_upfront_dollar": "FLOAT",
        "admin_cost_annual_dollar_per_year": "FLOAT", "utility_direct_investment_in_ders_upfront_dollar": "FLOAT",
        "avoided_credit_and_collection_cost_annual_dollar_per_year": "FLOAT", "utility_incentive_upfront_dollar": "FLOAT",
        "utility_incentive_annual_dollar_per_year": "FLOAT", "incremental_cost_upfront_dollar": "FLOAT",
        "incremental_cost_annual_dollar_per_year": "FLOAT", "host_customer_transaction_cost_upfront_dollar": "FLOAT",
        "host_customer_interconnection_cost_upfront_dollar": "FLOAT", "host_customer_tax_incentive_upfront_dollar": "FLOAT",
        "electric_savings_load_shape": "VARCHAR", "annual_electric_savings_kwh": "FLOAT",
        "coincident_peak_savings_kw": "FLOAT", "natural_gas_savings_load_shape": "VARCHAR",
        "annual_natural_gas_savings_mmbtu": "FLOAT", "annual_propane_savings_mmbtu": "FLOAT",
        "annual_oil_savings_mmbtu": "FLOAT", "annual_diesel_savings_mmbtu": "FLOAT", "annual_wood_savings_mmbtu": "FLOAT",
        "host_customer_non_energy_impacts_upfront_dollar": "FLOAT",
        "host_customer_non_energy_impacts_low_income_upfront_dollar": "FLOAT",
        "change_in_host_customer_resilience": "FLOAT", "change_in_societal_resilience": "FLOAT",
        **{f"custom_{n}_{f}": t for n in range(1, 6) for f, t in
           (("value_stream_name", "VARCHAR"), ("value_stream_impact_category", "VARCHAR"), ("annual_savings", "FLOAT"))},
        **{f"label_{n}": "VARCHAR" for n in range(1, 6)},
    },
    "global_parameters": {
        "inflation_rate": "FLOAT", "dollar_year": "INTEGER", "discount_rate": "FLOAT", "discount_cadence": "INTEGER",
        "electric_line_loss": "FLOAT", "peak_capacity_line_loss": "FLOAT", "natural_gas_line_loss": "FLOAT",
        "cost_treatment": "VARCHAR",
    },
    "program_value_streams": {"program_name": "VARCHAR", "program_year": "INTEGER", "avoided_cost": "VARCHAR",
                              "avoided_cost_value": "FLOAT"},
    "value_stream_groups": {"avoided_cost": "VARCHAR", "impact_category": "VARCHAR", "include_in_test": "BOOLEAN",
                            "calc_type": "VARCHAR", "pct_adder": "FLOAT", "value_stream_group": "VARCHAR",
                            "marginal_ghg": "BOOLEAN"},
    "load_shapes_ts": {"impact_category": "VARCHAR", "quarter": "INTEGER", "month": "INTEGER", "day_of_year": "INTEGER",
                       "hour_of_year": "INTEGER", "load_shape": "VARCHAR", "load_shape_value": "FLOAT"},
    "avoided_costs_ts": {"avoided_cost": "VARCHAR", "avoided_cost_subset": "VARCHAR", "year": "INTEGER",
                         "quarter": "INTEGER", "month": "INTEGER", "day_of_year": "INTEGER", "hour_of_day": "INTEGER",
                         "hour_of_year": "INTEGER", "avoided_cost_value": "FLOAT"},
    "first_year_avoided_costs_by_id": {"id": "VARCHAR", "year": "INTEGER", "value_stream": "VARCHAR",
                                       "gross_dollar_value": "FLOAT"},
    "load_shape_normalization": {"impact_category": "VARCHAR", "load_shape": "VARCHAR", "normalized": "BOOLEAN"},
}

GATEWAY_CONFIG = """gateways:
  local_duckdb:
    connection:
      type: duckdb
      database: {{ env_var('DB') }}

default_gateway: local_duckdb

model_defaults:
  dialect: duckdb
"""

RUNNER = '''"""Runs OpenBCA's core on one test's input project (written by der-opt)."""
import os, sys
from sqlmesh.core.console import set_console, TerminalConsole
from sqlmesh.core.context import Context
input_project, core_project, out_dir = sys.argv[1], sys.argv[2], sys.argv[3]
os.environ["DB"] = os.path.join(out_dir, "openbca.db")
set_console(TerminalConsole())
ctx = Context(paths=[input_project, core_project])
try:
    ctx.plan(run=True, ignore_cron=True, auto_apply=True, no_prompts=True)
finally:
    ctx.close()
import duckdb
con = duckdb.connect(os.environ["DB"], read_only=True)
for table in ("results_summary_by_id", "jst_ratio"):
    con.execute(f"COPY (SELECT * FROM openbca.core_layer3_finalization.{table}) TO "
                f"'{os.path.join(out_dir, table + '.csv')}' (HEADER, DELIMITER ',')")
con.execute("COPY (SELECT id, impact_category, value_stream, SUM(final_dollar_value) AS present_value, "
            "SUM(marginal_ghg_savings) AS ghg_savings FROM openbca.core_layer3_finalization.final_value_calculations_ts "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3) TO '" + os.path.join(out_dir, "streams.csv") + "' (HEADER, DELIMITER ',')")
'''


def _standard_calendar(year: int) -> pd.DatetimeIndex:
    hours = pd.date_range(f"{year}-01-01", f"{year + 1}-01-01", freq="h", inclusive="left")
    return hours[~((hours.month == 2) & (hours.day == 29))]  # OpenBCA has no leap days


def _standard_offset(tz: str, year: int) -> pd.Timedelta:
    return pd.Timestamp(f"{year}-01-15", tz=tz).utcoffset()


def avoided_costs_for_year(acs: AvoidedCostSet, year: int, analysis_year: int, columns: list[str]) -> pd.DataFrame:
    """A year's hourly values in standard time, laid on the analysis year's calendar
    (month, day, hour; 8760 rows). Beyond the set, the last year escalated."""
    source_year, factor = acs._escalation(year)
    tz = acs.manifest.tz
    standard = pd.date_range(f"{source_year}-01-01", f"{source_year + 1}-01-01", freq="h", inclusive="left")
    instants = (standard - _standard_offset(tz, source_year)).tz_localize("UTC")
    values = acs.hourly.reindex(instants)[columns]
    values.index = pd.MultiIndex.from_arrays([standard.month, standard.day, standard.hour])
    target = _standard_calendar(analysis_year)
    day = np.where((target.month == 2) & (target.day == 29), 28, target.day)
    out = values.reindex(pd.MultiIndex.from_arrays([target.month, day, target.hour]))
    out.index = target
    return out * factor


@dataclass
class ExportedTest:
    test: str
    folder: Path


def _write_table(folder: Path, name: str, frame: pd.DataFrame) -> None:
    schema = SCHEMAS[name]
    frame = frame.reindex(columns=list(schema))
    frame.to_csv(folder / "data" / f"{name}.csv", index=False)
    columns = ", ".join(f"'{c}': '{t}'" for c, t in schema.items())
    sql = (f"MODEL (name openbca_input.{name}, kind FULL);\n\n"
           f"SELECT * FROM read_csv('{(folder / 'data' / f'{name}.csv').resolve()}', header = true, "
           f"columns = {{{columns}}})\n")
    (folder / "input_project" / "models" / f"{name}.sql").write_text(sql, encoding="utf-8")


def export_openbca(package: str | Path, out_dir: str | Path) -> list[ExportedTest]:
    """Writes OpenBCA input tables for each test in the package's measure.yaml."""
    package, out_dir = Path(package), Path(out_dir)
    measure = yaml.safe_load((package / "measure.yaml").read_text(encoding="utf-8"))
    if not measure.get("discount_rates"):
        raise ValueError("cost_test.discount_rates (utility, participant, societal) is needed for the OpenBCA export")
    acs = read_set(measure["avoided_cost_set"])
    if acs.manifest.at != "meter":
        raise ValueError("the OpenBCA export expects avoided costs at the meter (line losses are set to 0)")
    analysis_year, start, life = measure["analysis_year"], measure["start_year"], measure["measure_life_years"]
    years = list(range(start, start + life))
    for year in years:
        acs._escalation(year)  # fails clearly if the set doesn't cover the life
    mid = measure["measure_id"]
    first = measure["first_year"]

    energy = pd.read_csv(package / "energy_impact.csv")
    stamps = pd.DatetimeIndex(pd.to_datetime(energy["timestamp"].str[:19]))
    keep = ~((stamps.month == 2) & (stamps.day == 29))
    energy, stamps = energy[keep].reset_index(drop=True), stamps[keep]
    hour_of_year = np.arange(len(energy))
    shapes = pd.concat([
        pd.DataFrame({"impact_category": "Electric", "month": stamps.month, "hour_of_year": hour_of_year,
                      "load_shape": shape, "load_shape_value": values})
        for shape, values in ((mid, energy["decrease_kwh"].to_numpy() / SCALE),
                              (f"{mid}-increases", -energy["increase_kwh"].to_numpy() / SCALE))
    ])
    capacity = pd.read_csv(package / "capacity.csv").set_index("month")["valued_kw"]

    energy_components = acs.components_of("energy", include_sct_only=True)
    hourly_rows = []
    calendar = _standard_calendar(analysis_year)
    for year in years:
        values = avoided_costs_for_year(acs, year, analysis_year, energy_components + ([acs.manifest.emissions] if acs.manifest.emissions else []))
        for column in energy_components:
            hourly_rows.append(pd.DataFrame({"avoided_cost": column, "avoided_cost_subset": "System-wide", "year": year,
                                             "month": calendar.month, "hour_of_day": calendar.hour + 1,
                                             "hour_of_year": np.arange(len(calendar)),
                                             "avoided_cost_value": values[column].to_numpy() * SCALE}))
        if acs.manifest.emissions:  # tCO2/MWh, matching shapes in MWh
            hourly_rows.append(pd.DataFrame({"avoided_cost": GHG_STREAM, "avoided_cost_subset": "System-wide", "year": year,
                                             "month": calendar.month, "hour_of_day": calendar.hour + 1,
                                             "hour_of_year": np.arange(len(calendar)),
                                             "avoided_cost_value": values[acs.manifest.emissions].to_numpy()}))
    capacity_rows, capacity_streams = [], []
    for component in ("generation", "transmission", "distribution"):
        stream = f"DR capacity ({component})"
        rows = []
        for year in years:
            priced = acs.capacity_price(year, component)
            if priced is None:
                break
            price, shares = priced
            kw = capacity.reindex(range(1, 13)).fillna(0.0).to_numpy()
            rows.append({"avoided_cost": stream, "avoided_cost_subset": mid, "year": year,
                         "avoided_cost_value": float(price * (shares * kw).sum())})
        if len(rows) == len(years):
            capacity_rows += rows
            capacity_streams.append(stream)

    bill_reduction = first.get("bill_reduction")
    escalation = measure.get("bill_escalation") or 0.0
    rates = measure["discount_rates"]
    exported = []
    for test in measure.get("tests") or list(COST_TREATMENT):
        folder = out_dir / test
        (folder / "data").mkdir(parents=True, exist_ok=True)
        (folder / "input_project" / "models").mkdir(parents=True, exist_ok=True)
        (folder / "output").mkdir(parents=True, exist_ok=True)
        (folder / "input_project" / "config.yaml").write_text(GATEWAY_CONFIG, encoding="utf-8")
        supply = test != "PCT"
        bills = bill_reduction is not None and test in ("PCT", "RIM")
        sct_only = {name for name, c in acs.manifest.components.items() if c.sct_only}

        groups = [{"avoided_cost": c, "impact_category": "Electric", "include_in_test": supply and (c not in sct_only or test == "SCT"),
                   "calc_type": "Time Series - Hourly", "value_stream_group": "electric", "marginal_ghg": False}
                  for c in energy_components]
        if acs.manifest.emissions:
            groups.append({"avoided_cost": GHG_STREAM, "impact_category": "Electric", "include_in_test": True,
                           "calc_type": "Time Series - Hourly", "value_stream_group": "electric", "marginal_ghg": True})
        groups += [{"avoided_cost": s, "impact_category": "Electric", "include_in_test": supply,
                    "calc_type": "Peak Capacity - Annual", "value_stream_group": "capacity", "marginal_ghg": False}
                   for s in capacity_streams]
        groups.append({"avoided_cost": BILLS_STREAM, "impact_category": BILLS_CATEGORY, "include_in_test": bills,
                       "calc_type": "Time Series - Annual", "value_stream_group": "annual", "marginal_ghg": False})
        groups += [{"avoided_cost": name, "impact_category": cat, "include_in_test": False, "calc_type": None,
                    "value_stream_group": "annual", "marginal_ghg": False} for name, cat in PLACEHOLDERS]
        groups += [{"avoided_cost": n, "impact_category": cat, "include_in_test": True, "calc_type": calc,
                    "value_stream_group": grp, "marginal_ghg": False} for n, cat, calc, grp in COST_STREAMS]
        _write_table(folder, "value_stream_groups", pd.DataFrame(groups))

        sign = -1.0 if test == "RIM" else 1.0  # RIM: the participant's savings are the utility's lost revenue
        bill_rows = [{"avoided_cost": BILLS_STREAM, "avoided_cost_subset": "System-wide", "year": y,
                      "avoided_cost_value": sign * SCALE * (1 + escalation) ** (y - start)} for y in years]
        _write_table(folder, "avoided_costs_ts", pd.concat(hourly_rows + [pd.DataFrame(capacity_rows + bill_rows)]))
        _write_table(folder, "load_shapes_ts", shapes)
        _write_table(folder, "load_shape_normalization", pd.DataFrame(
            [{"impact_category": "Electric", "load_shape": s, "normalized": False} for s in (mid, f"{mid}-increases")]))

        pc, prog = measure["participant_costs"], measure["program_costs"]
        main = {
            "id": mid, "program_name": mid, "measure_id": mid, "measure_name": mid, "avoided_cost_subset": mid,
            "start_year": start, "start_quarter": 1, "unit_quantity": 1.0, "estimated_useful_life": life,
            "net_to_gross_ratio": measure["net_to_gross"],
            "admin_cost_upfront_dollar": prog["admin_upfront"], "admin_cost_annual_dollar_per_year": prog["admin_annual"],
            "utility_incentive_upfront_dollar": prog["upfront_incentive"],
            "utility_incentive_annual_dollar_per_year": first.get("dr_incentive") or 0.0,
            "incremental_cost_upfront_dollar": pc["upfront"], "incremental_cost_annual_dollar_per_year": pc["annual_om"],
            "host_customer_tax_incentive_upfront_dollar": measure["tax_credits"],
            "electric_savings_load_shape": mid, "annual_electric_savings_kwh": 1.0, "coincident_peak_savings_kw": 1.0,
            **CUSTOM_SLOTS, "custom_1_annual_savings": bill_reduction / SCALE if bills else None, "label_1": test,
        }
        increases = {"id": f"{mid}-increases", "program_name": mid, "measure_id": mid,
                     "measure_name": f"{mid} (load increases)", "avoided_cost_subset": mid, "start_year": start,
                     "start_quarter": 1, "unit_quantity": 1.0, "estimated_useful_life": life,
                     "net_to_gross_ratio": measure["net_to_gross"], "electric_savings_load_shape": f"{mid}-increases",
                     "annual_electric_savings_kwh": 1.0, "coincident_peak_savings_kw": 0.0, **CUSTOM_SLOTS,
                     "label_1": test}
        _write_table(folder, "measures", pd.DataFrame([main, increases]))
        rate = {"PCT": rates["participant"], "SCT": rates["societal"]}.get(test, rates["utility"])
        dollars = acs.manifest.dollars
        _write_table(folder, "global_parameters", pd.DataFrame([{
            "inflation_rate": 0.0, "dollar_year": dollars.base_year, "discount_rate": rate, "discount_cadence": 1,
            "electric_line_loss": 0.0, "peak_capacity_line_loss": 0.0, "natural_gas_line_loss": 0.0,
            "cost_treatment": COST_TREATMENT[test]}]))
        _write_table(folder, "program_value_streams", pd.DataFrame(columns=list(SCHEMAS["program_value_streams"])))
        _write_table(folder, "first_year_avoided_costs_by_id", pd.DataFrame(columns=list(SCHEMAS["first_year_avoided_costs_by_id"])))
        settings = json.dumps({
            "test": test, "cost_treatment": COST_TREATMENT[test], "discount_rate": rate, "measure_life_years": life,
            "dollars": f"{dollars.basis} ({dollars.base_year})", "openbca_commit_tested": OPENBCA_COMMIT,
            "units": "energy in MWh, avoided costs in $/MWh: OpenBCA rounds avoided costs to 3 decimals",
            "notes": [n for n in (
                "OpenBCA counts annual cost fields (admin, O&M, the annual DR incentive) undiscounted; benefits are "
                "discounted (OpenBCA's convention, kept)" if (prog["admin_annual"] or pc["annual_om"]
                                                              or (first.get("dr_incentive") or 0)) else None,
                "participant replacements aren't carried (OpenBCA has no field for them)" if pc.get("replacements") else None,
                "annual costs aren't escalated (OpenBCA holds them flat)" if measure.get("cost_escalation") else None,
                "the DR incentive is held flat over the life" if (first.get("dr_incentive") or 0) else None,
                "impact degradation isn't applied (OpenBCA holds the shape constant)" if measure.get("impact_degradation") else None,
            ) if n],
        }, indent=2)
        (folder / "test.json").write_text(settings, encoding="utf-8")
        exported.append(ExportedTest(test, folder))
    (out_dir / "run_core.py").write_text(RUNNER, encoding="utf-8")
    return exported


def run_openbca(exported: list[ExportedTest], openbca_dir: str | Path, runner: Path) -> None:
    """Runs OpenBCA's core (its own Python environment) on each exported test.

    OpenBCA runs from its own checkout (cwd), so every path handed to it is made
    absolute first; a path relative to the caller's folder (the default out/<name> of
    `der-opt run --cost-tests`, or a relative --openbca) would otherwise point inside
    the checkout. That includes the interpreter, which is looked up relative to cwd."""
    openbca_dir = Path(openbca_dir).expanduser().resolve()
    python = openbca_dir / ".venv" / "bin" / "python"
    if not python.exists():
        python = openbca_dir / ".venv" / "Scripts" / "python.exe"
    if not python.exists():
        raise FileNotFoundError(f"no OpenBCA environment in {openbca_dir}; run `uv sync` there first")
    for item in exported:
        # a fresh database each run: OpenBCA's engine keeps state in it and would reuse
        # input tables whose model definitions haven't changed, even if the files have
        for stale in (item.folder / "output").glob("openbca.db*"):
            stale.unlink()
        result = subprocess.run([str(python), str(Path(runner).resolve()), str((item.folder / "input_project").resolve()),
                                 str(openbca_dir / "core"), str((item.folder / "output").resolve())],
                                capture_output=True, text=True, cwd=openbca_dir)
        (item.folder / "output" / "openbca.log").write_text(result.stdout + result.stderr, encoding="utf-8")
        if result.returncode != 0:
            raise RuntimeError(f"OpenBCA failed on {item.test}; see {item.folder / 'output' / 'openbca.log'}\n"
                               + (result.stderr or result.stdout)[-2000:])


def tally(exported: list[ExportedTest]) -> pd.DataFrame:
    """Per test: OpenBCA's own totals and ratio, and the SPM-convention re-tally of the
    same streams, where each stream counts by its sign: a positive present value is a
    benefit, a negative one a cost. That moves load increases and RIM's lost revenue to
    costs, and a participant's incentive (positive in PCT) to benefits. Every dollar
    comes from OpenBCA; only the classification differs."""
    rows = []
    for item in exported:
        out = item.folder / "output"
        ratio = pd.read_csv(out / "jst_ratio.csv").iloc[0].fillna(0.0)
        streams = pd.read_csv(out / "streams.csv")
        pv = streams.groupby(["id", "impact_category", "value_stream"])["present_value"].sum().fillna(0.0)
        benefits, costs = float(pv[pv > 0].sum()), float(-pv[pv < 0].sum())
        openbca_benefits, openbca_costs = float(ratio["total_benefits"]), float(-ratio["total_costs"])
        # OpenBCA leaves annual cost fields undiscounted (its convention, kept by decision
        # 2026-11-20): how net benefits would change if they were discounted at the test's
        # rate (they carry their sign: a cost in RIM, a participant benefit in PCT)
        settings = json.loads((item.folder / "test.json").read_text(encoding="utf-8"))
        rate, life = settings["discount_rate"], settings["measure_life_years"]
        annuity = sum(1 / (1 + rate) ** t for t in range(life)) / life
        annual = streams[streams["value_stream"].str.endswith("_annual_dollar_per_year")
                         & streams["impact_category"].isin(COST_CATEGORIES)]["present_value"].fillna(0.0).sum()
        rows.append({
            "test": item.test, "openbca_benefits": openbca_benefits, "openbca_costs": openbca_costs,
            "net_change_if_annual_discounted": float(-annual * (1 - annuity)),
            "net_benefits": openbca_benefits - openbca_costs,
            "openbca_ratio": openbca_benefits / openbca_costs if openbca_costs > 0 else np.nan,
            "spm_benefits": benefits, "spm_costs": costs, "spm_ratio": benefits / costs if costs > 0 else np.nan,
        })
    return pd.DataFrame(rows)
