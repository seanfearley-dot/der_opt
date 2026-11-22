"""The report page for a single-home run (docs/REPORTS.md section 2)."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.reports.page import Card, Page, money, number

OPTIMAL, COUNTERFACTUAL = "#2563eb", "#9ca3af"
DEVICE_COLORS = {"solar": "#f59e0b", "battery": "#10b981", "ev": "#8b5cf6", "thermostat": "#ef4444",
                 "curtailable": "#0ea5e9"}


def _read(folder: Path, name: str, **kwargs) -> pd.DataFrame | None:
    path = folder / name
    return pd.read_csv(path, **kwargs) if path.exists() else None


def _series(folder: Path, name: str) -> pd.Series | None:
    frame = _read(folder, name, index_col=0, parse_dates=True)
    return frame.iloc[:, 0] if frame is not None else None


def _color(device: str, kinds: dict) -> str:
    kind = kinds.get(device, device)
    return next((c for k, c in DEVICE_COLORS.items() if kind.startswith(k)), "#64748b")


def _round(values, decimals: int = 3) -> list:
    return [None if v is None or (isinstance(v, float) and np.isnan(v)) else round(float(v), decimals)
            for v in values]


def build(folder: Path, summary: dict, page: Page | None = None) -> Page:
    page = page or Page(summary["name"], _subtitle(summary), "single premise")
    costs = summary.get("costs") or {}
    optimal, counterfactual = costs.get("optimal") or {}, costs.get("counterfactual")

    # --- headline
    top = page.section("summary", "Summary")
    cards = [Card("Optimal cost", money(optimal.get("total")), "every value stream, over the run")]
    if counterfactual is not None:
        cards.append(Card("Counterfactual cost", money(counterfactual.get("total")),
                          ", ".join(f"{d}: {p}" for d, p in (summary.get("counterfactual_policies") or {}).items())
                          or "devices passive"))
        value = costs.get("value_of_optimization")
        cards.append(Card("Value of optimization", money(value), "counterfactual minus optimal",
                          "good" if value and value > 0 else "bad" if value and value < 0 else ""))
    for label, bill in (summary.get("bills") or {}).items():
        cards.append(Card(f"Bill, {label}", money(bill.get("net_cost")),
                          f"{number(bill.get('import_kwh'), 0)} kWh in, {number(bill.get('export_kwh'), 0)} kWh out"))
    dr = summary.get("dr")
    if dr and dr.get("incentive_earned") is not None:
        cards.append(Card("DR incentive", money(dr["incentive_earned"]), f"program {dr.get('program')}"))
    for test in summary.get("cost_tests") or []:
        if test["test"] in ("TRC", "PAC"):
            cards.append(Card(f"{test['test']} ratio", number(test.get("openbca_ratio")),
                              f"net {money(test.get('net_benefits'), 0)}",
                              "good" if (test.get("openbca_ratio") or 0) >= 1 else "bad"))
    page.cards(top, cards)
    devices = ", ".join(f"{d['name']} ({d['type']}{', ' + Path(d['template']).stem if d.get('template') else ''})"
                        for d in summary.get("devices") or []) or "no devices"
    page.text(top, f"Devices: {devices}. Status: {summary.get('status')}.")
    for pattern in summary.get("ev_patterns") or []:
        page.text(top, f"EV {pattern['device']} daily pattern: {pattern['summary']}")

    # --- value breakdown
    streams = [k for k in optimal if k != "total"]
    if streams:
        section = page.section("value", "Value breakdown")
        traces = [{"type": "bar", "name": "optimal", "x": streams, "y": _round([optimal[s] for s in streams], 2),
                   "marker": {"color": OPTIMAL}}]
        if counterfactual is not None:
            traces.append({"type": "bar", "name": "counterfactual", "x": streams,
                           "y": _round([counterfactual.get(s, 0.0) for s in streams], 2),
                           "marker": {"color": COUNTERFACTUAL}})
        page.chart(section, traces, {"barmode": "group", "yaxis": {"title": "$ over the run (cost +, value −)"},
                                     "hovermode": "closest"}, height=320)

    _dispatch(page, folder, summary)
    _energy(page, folder)
    _dr(page, folder, summary)
    _screening(page, summary)
    _cost_tests(page, summary)
    _bills(page, summary)
    _checks(page, summary)
    _settings(page, folder)
    return page


def _subtitle(summary: dict) -> str:
    grid = summary.get("time_grid") or {}
    return f"{grid.get('start', '')[:16].replace('T', ' ')} to {grid.get('end', '')[:16].replace('T', ' ')}, " \
           f"{grid.get('steps')} steps of {grid.get('resolution_minutes')} min"


def _dispatch(page: Page, folder: Path, summary: dict) -> None:
    dispatch = _read(folder, "device_dispatch.csv", index_col=0, parse_dates=True)
    imported, exported = _series(folder, "grid_import.csv"), _series(folder, "grid_export.csv")
    if imported is None or exported is None:
        return
    net = imported - exported
    section = page.section("dispatch", "Dispatch")
    axis = page.time_axis("t", net.index)
    kinds = {d["name"]: d["type"] for d in summary.get("devices") or []}
    traces = []
    if dispatch is not None:
        devices_net = dispatch[[c for c in dispatch.columns if c.endswith(".net_power")]].sum(axis=1)
        load = net + devices_net
        traces.append({"type": "scatter", "mode": "lines", "name": "load", "x": axis, "y": _round(load),
                       "line": {"color": "ink", "width": 1}})
    traces.append({"type": "scatter", "mode": "lines", "name": "grid, optimal", "x": axis, "y": _round(net),
                   "line": {"color": OPTIMAL, "width": 1.5}})
    cf_in, cf_out = _series(folder, "counterfactual_grid_import.csv"), _series(folder, "counterfactual_grid_export.csv")
    if cf_in is not None and cf_out is not None:
        traces.append({"type": "scatter", "mode": "lines", "name": "grid, counterfactual", "x": axis,
                       "y": _round(cf_in - cf_out), "line": {"color": COUNTERFACTUAL, "width": 1.2, "dash": "dot"}})
    window = opening_window(net)
    page.text(section, "Power at the meter, kW: import positive, export negative. "
              + ("It opens on the week with the highest import; " if window else "")
              + "zoom with the buttons or the slider, and click a legend entry to hide it.")
    page.chart(section, traces, {"yaxis": {"title": "kW"}}, height=380, zoom=True, window=window)

    if dispatch is None:
        return
    traces = []
    cf = _read(folder, "counterfactual_device_dispatch.csv", index_col=0, parse_dates=True)
    for column in dispatch.columns:
        device, _, part = column.partition(".")
        color = _color(device, kinds)
        if part in ("net_power", "p_gen"):
            if part == "p_gen" and f"{device}.net_power" in dispatch:
                continue
            traces.append({"type": "scatter", "mode": "lines", "name": f"{device} kW", "x": axis,
                           "y": _round(dispatch[column]), "line": {"color": color, "width": 1.3}})
            if cf is not None and column in cf:
                traces.append({"type": "scatter", "mode": "lines", "name": f"{device} kW, counterfactual",
                               "x": axis, "y": _round(cf[column]), "visible": "legendonly",
                               "line": {"color": color, "width": 1, "dash": "dot"}})
        elif part == "soc":
            traces.append({"type": "scatter", "mode": "lines", "name": f"{device} kWh stored", "x": axis,
                           "y": _round(dispatch[column]), "yaxis": "y2",
                           "line": {"color": color, "width": 1, "dash": "dash"}})
    if traces:
        page.text(section, "Each device's power, kW (generation and discharge positive), and stored energy, kWh "
                           "(right axis). Counterfactual lines are in the legend.")
        page.chart(section, traces, {"yaxis": {"title": "kW"}, "yaxis2": {"title": "kWh", "overlaying": "y",
                                                                           "side": "right", "showgrid": False}},
                   height=380, zoom=True, window=window)

    if cf_in is not None and cf_out is not None and len(net) >= 48:
        change = (net - (cf_in - cf_out))
        frame = pd.DataFrame({"kw": change.to_numpy()}, index=change.index)
        frame["day"], frame["hour"] = frame.index.strftime("%Y-%m-%d"), frame.index.hour
        grid = frame.pivot_table(index="hour", columns="day", values="kw", aggfunc="mean")
        limit = float(np.nanmax(np.abs(grid.to_numpy()))) or 1.0
        page.text(section, "Where optimization moved load: optimal minus counterfactual net load by hour and day "
                           "(blue: less from the grid; red: more).")
        page.chart(section, [{"type": "heatmap", "x": list(grid.columns), "y": list(grid.index),
                              "z": [_round(r, 2) for r in grid.to_numpy()], "colorscale": "RdBu", "reversescale": True,
                              "zmin": -limit, "zmax": limit, "colorbar": {"title": "kW"}}],
                   {"yaxis": {"title": "hour", "dtick": 3}, "hovermode": "closest"}, height=320)


def opening_window(series: pd.Series, days: int = 7) -> tuple[str, str] | None:
    """For a run longer than two weeks, the week around its highest value."""
    if len(series) < 2 or series.index[-1] - series.index[0] <= pd.Timedelta(days=14):
        return None
    peak = series.idxmax()
    start = max(series.index[0], peak.normalize() - pd.Timedelta(days=days // 2))
    end = min(series.index[-1], start + pd.Timedelta(days=days))
    return start.strftime("%Y-%m-%d %H:%M"), end.strftime("%Y-%m-%d %H:%M")


def _energy(page: Page, folder: Path) -> None:
    runs = {}
    for label, prefix in (("optimal", ""), ("counterfactual", "counterfactual_")):
        imported, exported = _series(folder, f"{prefix}grid_import.csv"), _series(folder, f"{prefix}grid_export.csv")
        if imported is not None:
            hours = (imported.index[1] - imported.index[0]).total_seconds() / 3600 if len(imported) > 1 else 1.0
            runs[label] = (imported.resample("MS").sum() * hours, exported.resample("MS").sum() * hours)
    if not runs or len(next(iter(runs.values()))[0]) < 2:
        return
    section = page.section("energy", "Energy by month")
    traces = []
    for label, (imported, exported) in runs.items():
        color = OPTIMAL if label == "optimal" else COUNTERFACTUAL
        months = [d.strftime("%b %Y") for d in imported.index]
        traces.append({"type": "bar", "name": f"import, {label}", "x": months, "y": _round(imported, 1),
                       "marker": {"color": color}, "offsetgroup": label})
        traces.append({"type": "bar", "name": f"export, {label}", "x": months, "y": _round(-exported, 1),
                       "marker": {"color": color, "opacity": 0.5}, "offsetgroup": label})
    page.chart(section, traces, {"barmode": "relative", "yaxis": {"title": "kWh (export negative)"},
                                 "hovermode": "closest"}, height=320)


def _dr(page: Page, folder: Path, summary: dict) -> None:
    dr = summary.get("dr")
    if not dr:
        return
    section = page.section("dr", "Demand response")
    page.text(section, f"Program {dr.get('program')}: response {dr.get('response')}, settled on "
                       f"{dr.get('settlement_basis')} ({dr.get('customer_class')}); events from {dr.get('constructor')}, "
                       f"{dr.get('calendars')} calendar(s). References: R1 counterfactual, R2 non-responding twin, "
                       "R3 no measure, S settlement baseline.")
    capacity = pd.DataFrame(dr.get("capacity_by_month") or [])
    if len(capacity):
        traces = []
        for ref, color in (("R2", "#2563eb"), ("R3", "#10b981"), ("S", "#f59e0b")):
            column = f"counted_{ref}_kw"
            if column in capacity:
                traces.append({"type": "bar", "name": f"counted vs {ref}", "x": capacity["month"].tolist(),
                               "y": _round(capacity[column], 2), "marker": {"color": color}})
        if traces:
            page.text(section, "Counted capacity by month: mean kW reduction over shadow events on every eligible day "
                               "(net basis).")
            page.chart(section, traces, {"barmode": "group", "xaxis": {"title": "month", "dtick": 1},
                                         "yaxis": {"title": "kW"}, "hovermode": "closest"}, height=300)
        keep = [c for c in ("month", "in_season", "shadow_days", "events", "event_hours", "counted_R2_kw",
                            "counted_R3_kw", "counted_S_kw", "counted_import_R2_kw", "accounted_R2_kw") if c in capacity]
        page.table(section, capacity[keep], collapsed="Capacity by month, as a table")
    events = _read(folder, "dr_events.csv")
    if events is not None and len(events):
        page.table(section, events, filter=True, collapsed=f"Events ({len(events)})")


def _screening(page: Page, summary: dict) -> None:
    screening = summary.get("screening")
    if not screening:
        return
    section = page.section("screening", "Screening values")
    page.text(section, f"First year, {screening.get('set_id')} ({screening.get('dollars')} $), measure vs "
                       f"{screening.get('measure_vs')}. Not cost-test results. DR capacity valued: "
                       f"{screening.get('capacity_valued')}.")
    rows = [{"category": c, "part": p, "value": v} for c, parts in screening["values"].items() for p, v in parts.items()]
    page.table(section, pd.DataFrame(rows), formats={"value": money})


def _cost_tests(page: Page, summary: dict) -> None:
    tests = summary.get("cost_tests")
    if not tests:
        return
    section = page.section("cost-tests", "Cost tests")
    frame = pd.DataFrame(tests)
    page.chart(section, [
        {"type": "bar", "name": "OpenBCA ratio", "x": frame["test"].tolist(), "y": _round(frame["openbca_ratio"], 3),
         "marker": {"color": OPTIMAL}},
        {"type": "bar", "name": "SPM-convention ratio", "x": frame["test"].tolist(), "y": _round(frame["spm_ratio"], 3),
         "marker": {"color": COUNTERFACTUAL}}],
        {"barmode": "group", "yaxis": {"title": "benefit-cost ratio"}, "hovermode": "closest",
         "shapes": [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": 1, "y1": 1,
                     "line": {"dash": "dot", "width": 1}}]}, height=300)
    shown = frame.rename(columns={"openbca_benefits": "benefits", "openbca_costs": "costs", "openbca_ratio": "ratio",
                                  "net_benefits": "net benefits", "spm_benefits": "SPM benefits", "spm_costs": "SPM costs",
                                  "spm_ratio": "SPM ratio",
                                  "net_change_if_annual_discounted": "net change if annual costs discounted"})
    page.table(section, shown, formats={c: money for c in ("benefits", "costs", "net benefits", "SPM benefits",
                                                           "SPM costs", "net change if annual costs discounted")})
    page.text(section, "Present values in $. OpenBCA's totals, then an SPM-convention re-tally of the same streams "
                       "(each counted by its sign).")


def _bills(page: Page, summary: dict) -> None:
    bills = summary.get("bills") or {}
    if not bills:
        return
    section = page.section("bills", "Bills")
    rows = []
    for label, bill in bills.items():
        row = {"run": label, **{k: v for k, v in bill.items() if not isinstance(v, dict)}}
        for key, value in (bill.get("unused_credits") or {}).items():
            row[f"unused {key}"] = value
        rows.append(row)
    page.table(section, pd.DataFrame(rows), formats={"billed": money, "net_cost": money})


def _checks(page: Page, summary: dict) -> None:
    checks = summary.get("checks") or {}
    findings = checks.get("findings") or []
    section = page.section("checks", "Checks")
    page.text(section, f"{checks.get('errors', 0)} errors, {checks.get('warnings', 0)} warnings." if findings
              else "Every check is clean.")
    if findings:
        page.table(section, pd.DataFrame(findings)[["level", "check", "run", "premise", "message"]], filter=True)


def _settings(page: Page, folder: Path) -> None:
    table = _read(folder, "device_parameters.csv")
    if table is None:
        return
    section = page.section("settings", "Settings and sources")
    page.text(section, "Every setting of the premise and its devices, with where its value came from.")
    page.table(section, table, filter=True, collapsed=f"All {len(table)} settings")
