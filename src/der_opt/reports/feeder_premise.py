"""A page for each premise of a feeder (docs/REPORTS.md section 2c): what it did in
every feeder run, under the limits and mechanisms the feeder applied. Built from the
feeder folder's CSVs, like the feeder's own report, so `der-opt report` rebuilds it."""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.reports.page import Card, Page, assets_for, money
from der_opt.reports.scenario import DEVICE_COLORS, _read, _round, opening_window

FOLDER = "premises"
EVENT_FILL = "rgba(234,88,12,0.10)"


def slug(label: str) -> str:
    """A run label as its files name it (der_opt.cli.main)."""
    return label.replace(" ", "_").replace("(", "").replace(")", "")


def href(premise: str) -> str:
    return f"{FOLDER}/{premise}.html"


def _dispatches(folder: Path, runs: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for label in runs:
        frame = _read(folder, f"dispatch_{slug(label)}.csv", header=[0, 1], index_col=0, parse_dates=True)
        if frame is not None:
            out[label] = frame
    return out


def _path(elements: list[dict], element: str | None) -> list[str]:
    """The element and every one above it."""
    parents = {e["name"]: e.get("parent") for e in elements}
    path = []
    while element and element not in path:
        path.append(element)
        element = parents.get(element)
    return path


def _events(folder: Path, runs: list[str]) -> dict[str, pd.DataFrame]:
    out = {}
    for label in runs:
        calendar = _read(folder, f"dr_calendar_{slug(label)}.csv", parse_dates=["start", "end"])
        if calendar is not None and not calendar.empty:
            out[label] = calendar
    return out


def _shading(calendar: pd.DataFrame | None) -> list[dict]:
    if calendar is None:
        return []
    return [{"type": "rect", "xref": "x", "yref": "paper", "x0": str(r["start"]), "x1": str(r["end"]), "y0": 0,
             "y1": 1, "fillcolor": EVENT_FILL, "line": {"width": 0}, "layer": "below"} for _, r in calendar.iterrows()]


def _import_price(folder: Path, row: pd.Series, index: pd.DatetimeIndex) -> np.ndarray | None:
    """The premise's tariff import rate, $/kWh, where its premises.csv row names one."""
    tariff = row.get("tariff")
    if not isinstance(tariff, str) or not tariff:
        return None
    try:
        import yaml

        from der_opt.billing import load_retail_tariff

        path = Path(tariff)
        if path.suffix not in (".yaml", ".yml"):
            resolved = yaml.safe_load((folder / "scenario.resolved.yaml").read_text(encoding="utf-8")) or {}
            directory = (resolved.get("tariffs") or {}).get("dir")
            if directory is None:
                from der_opt.scenarios.feeder import DEFAULT_TARIFF_DIR

                directory = DEFAULT_TARIFF_DIR
            path = Path(directory) / f"{tariff}.yaml"
        return np.asarray(load_retail_tariff(path).total_rate(list(index.to_pydatetime())), dtype=float)
    except Exception:  # noqa: BLE001 (the page shows the rest without it)
        return None


def _devices(frame: pd.DataFrame) -> list[str]:
    return [c.split(".", 1)[0] for c in frame.columns if c.endswith(".net_power")]


def _load(frame: pd.DataFrame) -> pd.Series:
    """The premise's own load: what the meter and its devices together served."""
    load = frame["grid_import"] - frame["grid_export"]
    for device in _devices(frame):
        load = load + frame[f"{device}.net_power"]
    return load


def build(folder: Path, summary: dict, premise: str, table: pd.DataFrame, dispatches: dict[str, pd.DataFrame],
          customers: pd.DataFrame | None, events: dict[str, pd.DataFrame], prices: pd.DataFrame | None,
          signals: dict[str, pd.DataFrame], envelopes: dict[str, pd.DataFrame],
          departures: pd.DataFrame | None = None) -> Page:
    from der_opt.reports.feeder import RUN_COLORS, _run_color

    runs = [r for r in summary.get("runs") or {} if r in dispatches and premise in dispatches[r].columns.levels[0]]
    if departures is not None:
        departures = departures[departures["premise_id"] == premise]
    row = table.set_index("premise_id").loc[premise]
    element = row.get("element_id") if isinstance(row.get("element_id"), str) else None
    path = _path(summary.get("elements") or [], element)
    grid = summary.get("time_grid") or {}
    page = Page(premise, f"on {' → '.join(path) or 'the feeder'}; {grid.get('start', '')[:16].replace('T', ' ')} to "
                         f"{grid.get('end', '')[:16].replace('T', ' ')}", "feeder premise")
    mine = {r: dispatches[r][premise] for r in runs}
    index = next(iter(mine.values())).index if mine else pd.DatetimeIndex([])
    devices = _devices(next(iter(mine.values()))) if mine else []
    main = [r for r in runs if r in RUN_COLORS]

    # --- summary
    top = page.section("summary", "Summary")
    have = [f"{int(row[f'n_{k}'])} {name}" for k, name in (("solar", "solar"), ("battery", "battery"), ("ev", "EV"))
            if f"n_{k}" in row and pd.notna(row[f"n_{k}"]) and row[f"n_{k}"]]
    cards = [Card("Element", element or "-", " → ".join(path[1:]) and f"under {' → '.join(path[1:])}"),
             Card("Devices", ", ".join(have) or "none", f"{row.get('participation', '')}, {row.get('response', '')}"
                  if isinstance(row.get("participation"), str) else "")]
    bills = {r: row.get(f"bill_{r}") for r in runs if pd.notna(row.get(f"bill_{r}"))}
    if "coordinated" in bills:
        cards.append(Card("Bill, coordinated", money(bills["coordinated"]), "over the run"))
    for other in ("independent", "counterfactual"):
        if "coordinated" in bills and other in bills:
            change = bills["coordinated"] - bills[other]
            cards.append(Card(f"Coordinated vs {other}", ("+" if change > 0.005 else "") + money(change),
                              "pays more" if change > 0.005 else "pays less" if change < -0.005 else "the same",
                              "bad" if change > 0.005 else "good" if change < -0.005 else ""))
    page.cards(top, cards)
    settings = {k: row.get(k) for k in ("tariff", "export_program", "participation", "baseline_behavior", "response",
                                        "data") if isinstance(row.get(k), str) and row.get(k)}
    if settings:
        page.text(top, "; ".join(f"{k.replace('_', ' ')}: {v}" for k, v in settings.items()))

    # --- costs by run
    section = page.section("costs", "Costs by run")
    rows = []
    for label in runs:
        item = {"run": label, "bill": bills.get(label)}
        if customers is not None:
            match = customers[(customers["premise_id"] == premise) & (customers["run"] == label)]
            if not match.empty:
                c = match.iloc[0]
                item |= {k: c[k] for k in ("degradation", "ev_shortfall_cost", "ev_shortfall_kwh", "signal_charges",
                                           "dr_incentive", "dr_shortfall_kwh", "envelope_over_import_kwh",
                                           "envelope_over_export_kwh") if k in c}
        if departures is not None and "ev_shortfall_kwh" not in item:  # every feeder run writes these
            own = departures[departures["run"] == label]
            item |= {"ev_shortfall_cost": float(own["shortfall_cost"].sum()),
                     "ev_shortfall_kwh": float(own["short_kwh"].sum())}
        frame = mine[label]
        item["import kWh"] = float(frame["grid_import"].sum() * _hours(index))
        item["export kWh"] = float(frame["grid_export"].sum() * _hours(index))
        item["peak import kW"] = float(frame["grid_import"].max())
        rows.append(item)
    costs = pd.DataFrame(rows).dropna(axis=1, how="all")
    for column in ("ev_shortfall_cost", "ev_shortfall_kwh"):  # only where some EV was left short
        if column in costs and not (costs[column].fillna(0) > 0.005).any():
            costs = costs.drop(columns=column)
    if "independent" in bills and "bill" in costs:
        costs.insert(2, "bill vs independent", costs["bill"] - bills["independent"])
    costs = costs.rename(columns={"ev_shortfall_cost": "EV shortfall (intangible)", "ev_shortfall_kwh": "EV kWh short",
                                  "signal_charges": "signal charges", "dr_incentive": "DR incentive",
                                  "dr_shortfall_kwh": "DR shortfall kWh", "envelope_over_import_kwh": "kWh over import envelope",
                                  "envelope_over_export_kwh": "kWh over export envelope"})
    money_columns = [c for c in costs.columns if c in ("bill", "bill vs independent", "degradation", "signal charges",
                                                       "DR incentive", "EV shortfall (intangible)")]
    page.table(section, costs.round(3), formats={c: money for c in money_columns})
    page.text(section, "Bills are over the run; DR incentives and signal charges are paid on top of them. An EV "
                       "shortfall is intangible: the optimizer weighs it, but it's never part of a bill.")

    if not mine:
        return page
    axis = page.time_axis("t", index)
    window = opening_window(_load(next(iter(mine.values()))))
    shared_events = events.get("dr_events") if "dr_events" in events else next(iter(events.values()), None)

    # --- the meter
    section = page.section("meter", "At the meter, by run")
    traces = []
    for label in runs:
        net = mine[label]["grid_import"] - mine[label]["grid_export"]
        traces.append({"type": "scatter", "mode": "lines", "name": label, "x": axis, "y": _round(net),
                       "visible": True if label in main else "legendonly",
                       "line": {"color": _run_color(label, runs), "width": 1.6 if label in main else 1.2}})
    for label, frame in envelopes.items():
        for quantity, sign in (("import_kw", 1), ("export_kw", -1)):
            limit = frame[(frame["premise_id"] == premise) & (frame["quantity"] == quantity)]
            if limit.empty:
                continue
            limit = limit.set_index(pd.to_datetime(limit["timestamp"]))["kw"].reindex(index)
            traces.append({"type": "scatter", "mode": "lines", "name": f"{label}: {quantity.split('_')[0]} envelope",
                           "x": axis, "y": _round(sign * limit), "visible": "legendonly",
                           "line": {"color": _run_color(label, runs), "dash": "dot", "width": 1.2, "shape": "hv"}})
    page.chart(section, traces, {"yaxis": {"title": "kW (import +, export −)"}, "shapes": _shading(shared_events)},
               height=380, zoom=True, window=window, unit="kW")
    page.text(section, "Net import at the meter in each run (export negative). Mechanism runs and envelopes (dotted) "
                       "are in the legend" + (f"; shading marks the {next(k for k, v in events.items() if v is shared_events)}"
                                              " run's DR events." if shared_events is not None else "."))

    # --- devices, run by run
    section = page.section("devices", "Devices, run by run")
    traces, groups = [], []
    for label in runs:
        frame = mine[label]
        start = len(traces)
        traces.append({"type": "scatter", "mode": "lines", "name": "load", "x": axis, "y": _round(_load(frame)),
                       "line": {"color": "#64748b", "width": 1.4}})
        for device in devices:
            color = next((c for k, c in DEVICE_COLORS.items() if device.startswith(k)), "#64748b")
            if f"{device}.p_gen" in frame:
                traces.append({"type": "scatter", "mode": "lines", "name": f"{device} output", "x": axis,
                               "y": _round(frame[f"{device}.p_gen"]), "line": {"color": color, "width": 1.4}})
            if f"{device}.p_ch" in frame:
                flow = frame[f"{device}.p_ch"] - frame.get(f"{device}.p_dis", 0)
                traces.append({"type": "bar", "name": f"{device} charge (+) / discharge (−)", "x": axis,
                               "y": _round(flow), "marker": {"color": color}, "opacity": 0.75})
            other = [c for c in frame.columns if c.startswith(f"{device}.") and c.endswith("_kw")]
            for column in other:
                traces.append({"type": "scatter", "mode": "lines", "name": column, "x": axis,
                               "y": _round(frame[column]), "line": {"color": color, "dash": "dot", "width": 1.2}})
        groups.append((label, start, len(traces)))
    first = runs.index("coordinated") if "coordinated" in runs else 0
    for i, (label, start, end) in enumerate(groups):
        for trace in traces[start:end]:
            trace["visible"] = i == first
    buttons = [{"label": label, "method": "update",
                "args": [{"visible": [start <= j < end for j in range(len(traces))]},
                         {"shapes": _shading(events.get(label))}]} for label, start, end in groups]
    page.chart(section, traces, {"yaxis": {"title": "kW"}, "barmode": "relative", "shapes": _shading(events.get(runs[first])),
                                 "updatemenus": [{"buttons": buttons, "active": first, "x": 1, "xanchor": "right",
                                                  "y": 1.2, "yanchor": "top", "direction": "down"}],
                                 "margin": {"t": 60}}, height=420, zoom=True, window=window, unit="kW")
    page.text(section, "Pick a run from the menu: the home's load, its solar, and each battery's and EV's charging "
                       "(+) and discharging (−). Shading marks that run's DR events, if it had any.")
    stores = [d for d in devices if f"{d}.soc" in next(iter(mine.values()))]
    if stores:
        traces = [{"type": "scatter", "mode": "lines", "name": f"{device}, {label}", "x": axis,
                   "y": _round(mine[label][f"{device}.soc"]), "visible": True if label in main else "legendonly",
                   "line": {"color": _run_color(label, runs), "width": 1.5 if label in main else 1.1,
                            "dash": "solid" if i == 0 else "dash"}}
                  for i, device in enumerate(stores) for label in runs]
        for device in stores:
            marks = departures[(departures["device"] == device)] if departures is not None else None
            if marks is None or marks.empty:
                continue
            first_run = marks[marks["run"] == marks["run"].iloc[0]]
            traces.append({"type": "scatter", "mode": "markers", "name": f"{device} departure target",
                           "x": first_run["departure"].astype(str).tolist(), "y": _round(first_run["target_kwh"]),
                           "marker": {"symbol": "line-ew-open", "size": 14, "color": "#b91c1c"}})
            short = marks[marks["short_kwh"] > 0.01]
            if not short.empty:
                traces.append({"type": "scatter", "mode": "markers", "name": f"{device} left short",
                               "x": short["departure"].astype(str).tolist(), "y": _round(short["stored_kwh"]),
                               "text": [f"{r.run}: {r.short_kwh:.1f} kWh short" for r in short.itertuples()],
                               "marker": {"symbol": "x", "size": 9, "color": "#b91c1c"}})
        page.chart(section, traces, {"yaxis": {"title": "kWh stored"}}, height=320, zoom=True, window=window,
                   unit="kWh", caption="Stored energy by run" + (f" ({', '.join(stores)}; dashed after the first)"
                                                                 if len(stores) > 1 else f" ({stores[0]})"))

    # --- prices
    section = page.section("prices", "Prices it faced")
    traces = []
    rate = _import_price(folder, row, index)
    if rate is not None:
        traces.append({"type": "scatter", "mode": "lines", "name": f"import rate ({row.get('tariff')})", "x": axis,
                       "y": _round(rate, 4), "line": {"color": "#64748b", "width": 1.4, "shape": "hv"}})
    on_path = [e for e in path if prices is not None and e in prices]
    if on_path:
        congestion = prices[on_path].sum(axis=1).reindex(index)
        traces.append({"type": "scatter", "mode": "lines", "name": "congestion price on its path (coordinated)",
                       "x": axis, "y": _round(congestion, 4),
                       "line": {"color": RUN_COLORS["coordinated"], "width": 1.4, "shape": "hv"}})
    for label, signal in signals.items():
        columns = [e for e in path if e in signal]
        if columns:
            traces.append({"type": "scatter", "mode": "lines", "name": f"{label}: signal adder", "x": axis,
                           "y": _round(signal[columns].sum(axis=1).reindex(index), 4),
                           "line": {"color": _run_color(label, runs), "width": 1.3, "dash": "dash", "shape": "hv"}})
    if traces:
        page.chart(section, traces, {"yaxis": {"title": "$/kWh"}, "shapes": _shading(shared_events)}, height=340,
                   zoom=True, window=window, unit="$/kWh")
        page.text(section, "Its tariff's import rate; the coordinated run's congestion price summed along its path "
                           f"({' + '.join(on_path) or 'none'}), what one more kWh at this meter costs the feeder; and "
                           "each price-signal run's adder, paid per kWh of net import.")
    return page


def _hours(index: pd.DatetimeIndex) -> float:
    return (index[1] - index[0]).total_seconds() / 3600 if len(index) > 1 else 1.0


def write_pages(folder: Path, summary: dict, crumbs: list[tuple[str, str]]) -> list[Path]:
    """premises/<id>.html for every premise in the feeder's premises.csv."""
    table = _read(folder, "premises.csv")
    runs = list(summary.get("runs") or {})
    dispatches = _dispatches(folder, runs)
    if table is None or not dispatches:
        return []
    customers = _read(folder, "customers.csv")
    departures = _read(folder, "ev_departures.csv", parse_dates=["departure"])
    events = _events(folder, runs)
    prices = _read(folder, "congestion_prices.csv", index_col=0, parse_dates=True)
    signals = {label: frame for label in runs
               if (frame := _read(folder, f"signal_{slug(label)}.csv", index_col=0, parse_dates=True)) is not None}
    envelopes = {label: frame for label in runs
                 if (frame := _read(folder, f"envelopes_{slug(label)}.csv")) is not None}
    out = folder / FOLDER
    out.mkdir(exist_ok=True)
    script = assets_for(out)
    back = [(label, link if link.startswith(("/", "http")) else f"../{link}") for label, link in crumbs]
    back.append(("feeder", "../report.html"))
    written = []
    for premise in table["premise_id"]:
        page = build(folder, summary, premise, table, dispatches, customers, events, prices, signals, envelopes,
                     departures)
        page.links = back
        path = out / f"{premise}.html"
        path.write_text(page.render(script), encoding="utf-8")
        written.append(path)
    return written


def picker(premises: list[str]) -> str:
    """A menu on the feeder's report that opens a premise's page."""
    options = "".join(f'<option value="{html.escape(href(p))}">{html.escape(p)}</option>' for p in premises)
    return ('<label class="premise-pick">Open a premise: <select onchange="if(this.value)location.href=this.value">'
            f'<option value="">choose…</option>{options}</select></label>')
