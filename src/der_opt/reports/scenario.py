"""The report page for a single-home run (docs/REPORTS.md section 2)."""

from __future__ import annotations

import html
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
    tests = {t["test"]: t for t in summary.get("cost_tests") or []}
    featured = []
    for name, title in (("TRC", "Total Resource Cost"), ("RIM", "Ratepayer Impact")):
        test = tests.get(name)
        if test is None:
            continue
        ratio = test.get("openbca_ratio")
        featured.append(Card(f"{name} ratio · {title}", number(ratio),
                             f"OpenBCA · net benefits {money(test.get('net_benefits'), 0)}",
                             ("good" if (ratio or 0) >= 1 else "bad") + " feature"))
    from der_opt.reports import cases as compare

    paired = compare.setup(page, folder, summary)  # the A/B bar pinned at the top drives the page
    if featured:
        page.cards(top, featured)  # the cost tests lead the panel
        if paired:
            compare.fixed(page, top, "TRC and RIM measure the cost tests' own comparison"
                          + _cost_test_pair(summary) + ".")
    if paired:
        compare.summary_cards(page, top)
    else:  # a folder written before cases: optimal against counterfactual
        page.cards(top, cards)
    devices = ", ".join(f"{d['name']} ({d['type']}{', ' + Path(d['template']).stem if d.get('template') else ''})"
                        for d in summary.get("devices") or []) or "no devices"
    page.text(top, f"Devices: {devices}. Status: {summary.get('status')}.")
    for pattern in summary.get("ev_patterns") or []:
        page.text(top, f"EV {pattern['device']} daily pattern: {pattern['summary']}")

    if paired:  # the order: what happened, what it cost, DR, the cost tests, then the detail
        imported = _series(folder, "grid_import.csv")
        compare.dispatch(page, opening_window(imported) if imported is not None else None)
        compare.energy(page)
        compare.bills(page)
        _dr(page, folder, summary)
        _cost_tests(page, summary)
        _screening(page, summary)
        compare.value(page)
        for section in page.sections:
            what = {"dr": "it compares the DR run with the same home without events and with the "
                          "settlement baseline",
                    "screening": "screening values compare the cost tests' runs" + _cost_test_pair(summary),
                    "cost-tests": "the tests compare" + _cost_test_pair(summary)}.get(section.id)
            if what:
                section.parts.insert(0, f'<p class="note fixed">Not set by A and B: {html.escape(what)}.</p>')
        _checks(page, summary)
        _settings(page, folder)
        return page

    _dispatch(page, folder, summary)
    _energy(page, folder)
    _bills(page, summary)
    _dr(page, folder, summary)
    _cost_tests(page, summary)
    _screening(page, summary)
    _value(page, optimal, counterfactual)
    _checks(page, summary)
    _settings(page, folder)
    return page


def _value(page: Page, optimal: dict, counterfactual: dict | None) -> None:
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


def _cost_test_pair(summary: dict) -> str:
    cases, compare = summary.get("cases") or {}, summary.get("case_compare") or {}
    pair = compare.get("cost_tests")
    if not pair:
        return ""
    return f" ({cases[pair[0]]['label']} against {cases[pair[1]]['label']})"


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
    page.chart(section, traces, {"yaxis": {"title": "kW"}}, height=420, zoom=True, window=window, unit="kW")

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
                   height=420, zoom=True, window=window, unit="kW", unit2="kWh")


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


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
DR_RUN, NO_EVENTS, BASELINE = "#2563eb", "#9ca3af", "#f59e0b"


def _dr_names(summary: dict) -> dict:
    """The DR study's runs in the page's own words (the CSVs keep R2 and S)."""
    cases = summary.get("cases") or {}
    return {"responding": (cases.get("responding") or {}).get("label", "the DR run"),
            "R2": (cases.get("optimized") or {}).get("label", "no events"), "S": "settlement baseline"}


def _dr(page: Page, folder: Path, summary: dict) -> None:
    dr = summary.get("dr")
    if not dr:
        return
    section = page.section("dr", "Demand response")
    names = _dr_names(summary)
    basis = dr.get("settlement_basis", "net")
    page.text(section, f"Program {dr.get('program')}: {dr.get('response')} response, settled on "
                       f"{'net load' if basis == 'net' else 'grid import'} ({dr.get('customer_class')}); events from "
                       f"{dr.get('constructor')}. The DR run ({names['responding']}) is compared with the same home "
                       f"with no events ({names['R2']}) and with the settlement baseline, the load the program pays "
                       f"against. In the CSVs, {names['R2']} is R2 and the settlement baseline is S.")
    _event_day(page, section, folder, dr, names)
    _capacity(page, section, dr, summary, names)
    events = _read(folder, "dr_events.csv")
    if events is not None and len(events):
        page.table(section, events, filter=True, collapsed=f"Events ({len(events)})")


def _event_day(page: Page, section, folder: Path, dr: dict, names: dict) -> None:
    """One event's day at a time, picked from a menu: the DR run, the same home with no
    events, and the settlement baseline during the event, with the window shaded."""
    loads = _read(folder, "dr_net_load_kw.csv", index_col=0, parse_dates=True)
    performance = _read(folder, "dr_event_performance.csv", parse_dates=["start", "end"])
    if loads is None or performance is None or not len(performance) or "replicate_0" not in loads:
        return
    performance = performance[performance["replicate"] == 0]
    imports = dr.get("settlement_basis") == "import"
    pre = "import_" if imports else ""
    lines = {"responding": loads["replicate_0"], "R2": loads["R2"], "S": loads.get("S_replicate_0")}
    if imports:  # the program settles on grid import: show it that way
        lines = {k: (v.clip(lower=0) if v is not None and k != "S" else v) for k, v in lines.items()}
    traces, buttons, per_event = [], [], 3
    for i, (_, event) in enumerate(performance.iterrows()):
        day = event["start"].normalize()
        rows = (loads.index >= day) & (loads.index < day + pd.Timedelta(days=1))
        hours = [round(t.hour + t.minute / 60, 3) for t in loads.index[rows]]
        visible = i == 0
        for key, color, dash, width in (("responding", DR_RUN, "solid", 2), ("R2", NO_EVENTS, "dot", 1.5),
                                        ("S", BASELINE, "dash", 2)):
            series = lines[key]
            traces.append({"type": "scatter", "mode": "lines", "name": names[key], "x": hours,
                           "y": _round(series[rows]) if series is not None else [], "visible": visible,
                           "showlegend": True, "line": {"color": color, "dash": dash, "width": width},
                           "hovertemplate": "%{y:,.2f} kW"})
        start = event["start"].hour + event["start"].minute / 60
        end = start + float(event["hours"])
        shape = [{"type": "rect", "xref": "x", "yref": "paper", "x0": start, "x1": end, "y0": 0, "y1": 1,
                  "fillcolor": "rgba(245,158,11,0.13)", "line": {"width": 0}, "layer": "below"}]
        incentive = event.get("incentive_earned")
        text = (f"During the event: {number(event[f'{pre}reduction_vs_S_kw'])} kW below the settlement baseline, "
                f"{number(event[f'{pre}reduction_vs_R2_kw'])} kW below {names['R2']}"
                + (f"; incentive {money(incentive)}" if incentive is not None and pd.notna(incentive) else "")
                + (f"; {number(event['rebound_kwh'], 1)} kWh more than {names['R2']} later that day"
                   if event.get("rebound_kwh", 0) > 0.5 else ""))
        note = [{"text": text, "xref": "paper", "yref": "paper", "x": 0, "y": -0.2, "showarrow": False,
                 "xanchor": "left", "font": {"size": 12}}]
        label = f"{event['start']:%a %b %-d}, {event['start']:%H:%M}–{event['end']:%H:%M} ({event['event_type']})"
        buttons.append({"label": label, "method": "update",
                        "args": [{"visible": [j // per_event == i for j in range(per_event * len(performance))]},
                                 {"shapes": shape, "annotations": note}]})
    first = buttons[0]["args"][1]
    page.text(section, "An event's day: pick one from the menu. The shaded hours are the event; the dashed line is "
                       "the settlement baseline there. kW at the meter"
                       + (", grid import only (the program's basis)." if imports else ", import positive."))
    page.chart(section, traces, {"xaxis": {"title": "hour of the day", "range": [0, 24], "dtick": 3},
                                 "yaxis": {"title": "kW"}, "shapes": first["shapes"],
                                 "annotations": first["annotations"], "margin": {"t": 70, "b": 80},
                                 "updatemenus": [{"buttons": buttons, "x": 0, "xanchor": "left", "y": 1.2,
                                                  "yanchor": "top", "direction": "down",
                                                  "bgcolor": "#e5e7eb", "bordercolor": "#9ca3af",
                                                  "font": {"color": "#111"}}]}, height=420)


def _capacity(page: Page, section, dr: dict, summary: dict, names: dict) -> None:
    """Capacity by month, the no-events home against the settlement baseline, counted
    and accounted side by side, on either basis."""
    capacity = pd.DataFrame(dr.get("capacity_by_month") or [])
    if not len(capacity):
        return
    refs = [r for r in ("R2", "S") if f"counted_{r}_kw" in capacity or f"accounted_{r}_kw" in capacity]
    if not refs:
        return
    valued = (summary.get("screening") or {}).get("capacity_valued") or ""
    bases = (("net", "", "Net load (exports count)"), ("import", "import_", "Grid import only"))
    opening = valued.split("/")[1] if valued.count("/") == 2 else dr.get("settlement_basis", "net")
    months = [MONTHS[int(m) - 1] for m in capacity["month"]]
    traces, keys = [], []
    for basis, pre, _ in bases:
        for ref in refs:
            color = DR_RUN if ref == "R2" else BASELINE
            for measure in ("counted", "accounted"):
                column = f"{measure}_{pre}{ref}_kw"
                if column not in capacity:
                    continue
                star = " (valued)" if valued == f"{ref}/{basis}/{measure}" else ""
                marker = {"color": color} | ({"pattern": {"shape": "/", "fgcolor": color, "bgcolor": "rgba(0,0,0,0)"},
                                              "color": "rgba(0,0,0,0)", "line": {"color": color, "width": 1.5}}
                                             if measure == "accounted" else {})
                traces.append({"type": "bar", "name": f"vs {names[ref]}, {measure}{star}", "x": months,
                               "y": _round(capacity[column], 2), "marker": marker, "visible": basis == opening,
                               "hovertemplate": "%{y:,.2f} kW"})
                keys.append(basis)
    shading = [{"type": "rect", "xref": "x", "yref": "paper", "x0": i - 0.5, "x1": i + 0.5, "y0": 0, "y1": 1,
                "fillcolor": "rgba(128,128,128,0.10)", "line": {"width": 0}, "layer": "below"}
               for i, inside in enumerate(capacity.get("in_season", [True] * len(capacity))) if not inside]
    buttons = [{"label": label, "method": "restyle", "args": [{"visible": [k == basis for k in keys]}]}
               for basis, _, label in bases if basis in keys]
    page.text(section, f"Capacity by month: the mean kW the home takes off the grid during events, measured against "
                       f"{names['R2']} and against the settlement baseline. Counted (solid) is a "
                       f"{number(dr.get('max_hours') or 3, 0)}-hour test event on every eligible day, what the home "
                       "could deliver; accounted (hatched) is the events actually called. "
                       + ("Grey months are outside the program's season. " if shading else "")
                       + (f"The cost tests value {valued}." if valued else ""))
    page.chart(section, traces, {"barmode": "group", "xaxis": {"type": "category"}, "yaxis": {"title": "kW"},
                                 "hovermode": "x unified", "shapes": shading, "margin": {"t": 60, "b": 70},
                                 "legend": {"orientation": "h", "x": 0, "xanchor": "left", "y": -0.12,
                                            "yanchor": "top"},
                                 "updatemenus": [{"type": "buttons", "direction": "right", "buttons": buttons,
                                                  "x": 0, "xanchor": "left", "y": 1.18, "yanchor": "top",
                                                  "bgcolor": "#e5e7eb", "bordercolor": "#9ca3af",
                                                  "font": {"color": "#111"},
                                                  "active": [b for b, _, _ in bases if b in keys].index(opening)
                                                  if opening in keys else 0}]}, height=400)
    keep = ["month", "in_season", "shadow_days", "events", "event_hours"] + [
        c for c in capacity.columns if c.startswith(("counted_", "accounted_")) and "R3" not in c]
    page.table(section, capacity[keep].rename(columns=lambda c: c.replace("R2", names["R2"]).replace(
        "_S_", "_settlement_")), collapsed="Capacity by month, as a table")


def _screening(page: Page, summary: dict) -> None:
    screening = summary.get("screening")
    if not screening:
        return
    tests = next((s for s in page.sections if s.id == "cost-tests"), None)
    rows = [{"category": c, "part": p, "value": v} for c, parts in screening["values"].items() for p, v in parts.items()]
    about = (f"First year, {screening.get('set_id')} ({screening.get('dollars')} $), measure vs "
             f"{screening.get('measure_vs')}. Not cost-test results. DR capacity valued: "
             f"{screening.get('capacity_valued')}.")
    if tests is not None:  # with the cost tests, folded beneath them
        page.text(tests, "Screening values: " + about)
        page.table(tests, pd.DataFrame(rows), formats={"value": money}, collapsed="Screening values")
        return
    section = page.section("screening", "Screening values")
    page.text(section, about)
    page.table(section, pd.DataFrame(rows), formats={"value": money})


def _cost_tests(page: Page, summary: dict) -> None:
    tests = summary.get("cost_tests")
    if not tests:
        return
    section = page.section("cost-tests", "Cost tests")
    frame = pd.DataFrame(tests)
    frame = frame.drop(columns=[c for c in frame.columns if c.startswith("spm_")])  # folders written before 2026-11-22
    page.chart(section, [
        {"type": "bar", "name": "OpenBCA ratio", "x": frame["test"].tolist(), "y": _round(frame["openbca_ratio"], 3),
         "marker": {"color": OPTIMAL}}],
        {"barmode": "group", "yaxis": {"title": "benefit-cost ratio"}, "hovermode": "closest",
         "shapes": [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": 1, "y1": 1,
                     "line": {"dash": "dot", "width": 1}}]}, height=300)
    shown = frame.rename(columns={"openbca_benefits": "benefits", "openbca_costs": "costs", "openbca_ratio": "ratio",
                                  "net_benefits": "net benefits",
                                  "net_change_if_annual_discounted": "net change if annual costs discounted"})
    page.table(section, shown, formats={c: money for c in ("benefits", "costs", "net benefits",
                                                           "net change if annual costs discounted")})
    page.text(section, "Present values in $, OpenBCA's results as it reports them. The last column is how net "
                       "benefits would change if OpenBCA discounted annual cost fields (it doesn't).")


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
