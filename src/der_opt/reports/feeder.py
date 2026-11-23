"""The report page for a feeder (a study's feeder analysis; docs/REPORTS.md section 2)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from der_opt.reports.page import Card, Page, money, number
from der_opt.reports.scenario import (
    _checks,
    _read,
    _round,
    _settings,
    _subtitle,
    opening_window,
)

RUN_COLORS = {"coordinated": "#2563eb", "independent": "#f59e0b", "counterfactual": "#9ca3af"}
OTHER_COLORS = ["#10b981", "#8b5cf6", "#ef4444", "#0ea5e9", "#d946ef", "#84cc16", "#f97316"]


def _run_color(label: str, runs: list[str]) -> str:
    if label in RUN_COLORS:
        return RUN_COLORS[label]
    others = [r for r in runs if r not in RUN_COLORS]
    return OTHER_COLORS[others.index(label) % len(OTHER_COLORS)] if label in others else "#64748b"


def build(folder: Path, summary: dict, page: Page | None = None) -> Page:
    page = page or Page(summary["name"], _subtitle(summary), "feeder")
    runs = summary.get("runs") or {}
    labels = list(runs)

    top = page.section("summary", "Summary")
    cards = [Card("Premises", number(summary.get("premises")),
                  f"{len(summary.get('elements') or [])} network elements"
                  + (f"; background: {', '.join(summary['background'])}" if summary.get("background") else ""))]
    for label in ("coordinated", "independent", "counterfactual"):
        if label in runs:
            cards.append(Card(f"Total cost, {label}", money(runs[label]["total"]),
                              f"bills {money(runs[label]['customer_bills'], 0)}"))
    versus = summary.get("coordinated_vs_independent")
    if versus:
        cards.append(Card("Network saving from coordination", money(versus["network_saving"]),
                          "independent minus coordinated network cost", "good" if versus["network_saving"] > 0 else ""))
        cards.append(Card("Premises paying more", f"{versus['premises_paying_more']} of {versus['of']}",
                          f"{money(versus['total_paid_more'])} in all" + (
                              f", most {money(versus['most'])} ({versus['most_by']})" if versus["most_by"] else "")))
    page.cards(top, cards)
    mix = summary.get("mix") or {}
    if mix:
        page.table(top, pd.DataFrame([{"element": e, **m} for e, m in mix.items()]), caption="Premises by element")

    # --- costs by run
    if runs:
        section = page.section("costs", "Costs by run")
        rows = []
        for label, run in runs.items():
            row = {"run": label, "total": run["total"], "customer bills": run["customer_bills"],
                   "degradation": run["degradation"]}
            row |= {f"network: {k}": v for k, v in (run.get("network_costs") or {}).items()}
            rows.append(row)
        frame = pd.DataFrame(rows)
        page.table(section, frame, formats={c: money for c in frame.columns if c != "run"})

    _loading(page, folder, summary, labels)
    _congestion(page, folder, summary)
    _bills(page, folder, labels)
    _coordination(page, summary)
    _checks(page, summary)
    _settings(page, folder)
    return page


def _loading(page: Page, folder: Path, summary: dict, labels: list[str]) -> None:
    flows = _read(folder, "flows_kw.csv", header=[0, 1], index_col=0, parse_dates=True)
    section = page.section("loading", "Network loading")
    loading = summary.get("loading") or {}
    if loading:
        rows = []
        for label, elements in loading.items():
            for element, row in elements.items():
                rows.append({"run": label, "element": element, "rating kW": row.get("rating_kw"),
                             "peak kW": row.get("peak_kw"), "peak at": (row.get("peak_time") or "")[:16].replace("T", " "),
                             "% of rating": row.get("peak_pct_of_rating"), "hours over": row.get("hours_over"),
                             "kWh over": row.get("kwh_over"), "max backfeed kW": row.get("max_backfeed_kw")})
        frame = pd.DataFrame(rows)
        grid = frame.assign(cell=[f"{r['peak kW']:.1f} kW" + (f" ({r['% of rating']:.0f}%" if pd.notna(r["% of rating"])
                                   else "") + (f", {r['hours over']:g} h over)" if r["hours over"] else
                                               (")" if pd.notna(r["% of rating"]) else ""))
                                   for _, r in frame.iterrows()])
        pivot = grid.pivot(index="element", columns="run", values="cell")[list(loading)].reset_index()
        page.table(section, pivot, caption="Peak flow by element and run (% of rating, hours over it)")
        page.table(section, frame, filter=True, collapsed="Every peak figure, by run and element")
    if flows is None:
        return
    axis = page.time_axis("t", flows.index)
    elements = {e["name"]: e for e in summary.get("elements") or []}
    for element in dict.fromkeys(flows.columns.get_level_values(1)):
        traces = []
        for label in labels:
            if (label, element) not in flows:
                continue
            main = label in RUN_COLORS
            traces.append({"type": "scatter", "mode": "lines", "name": label, "x": axis,
                           "y": _round(flows[(label, element)]), "visible": True if main else "legendonly",
                           "line": {"color": _run_color(label, labels), "width": 1.6 if main else 1.2}})
        rating = (elements.get(element) or {}).get("rating_kw")
        emergency = (elements.get(element) or {}).get("emergency_rating_kw")
        layout = {"yaxis": {"title": "kW"}}
        lines = [(rating, "dash"), (emergency, "dot")]
        layout["shapes"] = [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": kw, "y1": kw,
                             "line": {"color": "#b91c1c", "dash": dash, "width": 1}} for kw, dash in lines if kw]
        page.chart(section, traces, layout, height=400, zoom=True, window=opening_window(flows.max(axis=1)), unit="kW",
                   caption=f"{element}" + (f", rating {rating:g} kW" if rating else "")
                   + (f", emergency {emergency:g} kW" if emergency else ""))
    page.text(section, "Flow through each element, kW (backfeed negative); the dashed line is its normal rating, the "
                       "dotted one its emergency rating where it has one. Mechanism runs are in the legend.")


def _congestion(page: Page, folder: Path, summary: dict) -> None:
    prices = _read(folder, "congestion_prices.csv", index_col=0, parse_dates=True)
    if prices is None or not (prices.abs() > 1e-3).any().any():
        return
    section = page.section("congestion", "Congestion prices")
    exact = (summary.get("congestion") or {}).get("exact", True)
    page.text(section, "The coordinated run's cost of one more kWh through each element, $/kWh"
              + ("." if exact else " (approximate: integer variables, so from fix-and-resolve)."))
    axis = page.time_axis("t", prices.index)
    traces = [{"type": "scatter", "mode": "lines", "name": c, "x": axis, "y": _round(prices[c], 4),
               "line": {"width": 1.4, "shape": "hv"}} for c in prices.columns if (prices[c].abs() > 1e-3).any()]
    page.chart(section, traces, {"yaxis": {"title": "$/kWh"}}, height=360, zoom=True,
               window=opening_window(prices.abs().max(axis=1)), unit="$/kWh")


def _bills(page: Page, folder: Path, labels: list[str]) -> None:
    table = _read(folder, "premises.csv")
    if table is None:
        return
    bills = [c for c in table.columns if c.startswith("bill_")]
    if not bills:
        return
    section = page.section("premises", "Premises and their bills")
    if "bill_coordinated" in table and "bill_independent" in table:
        change = (table["bill_coordinated"] - table["bill_independent"]).round(2)
        order = change.sort_values(ascending=False).index
        page.text(section, "Each premise's bill, coordinated minus independent: positive pays more under "
                           "coordination (the planner's optimum doesn't compensate them).")
        page.chart(section, [{"type": "bar", "x": table.loc[order, "premise_id"].tolist(), "y": change[order].tolist(),
                              "marker": {"color": ["#b91c1c" if v > 0.005 else "#15803d" for v in change[order]]}}],
                   {"yaxis": {"title": "$ over the run"}, "hovermode": "closest", "showlegend": False}, height=300)
    keep = [c for c in ("premise_id", "element_id", "data", "participation", "baseline_behavior", "response",
                        "n_solar", "solar_kw", "n_battery", "battery_kwh", "n_ev", "ev_kwh") if c in table] + bills
    page.table(section, table[keep], filter=True, formats={c: money for c in bills})


def _coordination(page: Page, summary: dict) -> None:
    coordination = summary.get("coordination")
    if not coordination:
        return
    section = page.section("coordination", "Coordination mechanisms")
    page.text(section, "Each mechanism run against independent (the feeder with no program) and the ideal "
                       "(coordinated). Customers: " + ", ".join(f"{n} {g}" for g, n in coordination["groups"].items()))
    runs = coordination["runs"]
    labels = list(runs)
    ideal = next((r.get("ideal_network_saving") for r in runs.values() if r.get("ideal_network_saving") is not None), None)
    traces = [{"type": "bar", "x": labels, "y": _round([runs[r].get("network_saving") for r in labels], 2),
               "marker": {"color": ["#15803d" if (runs[r].get("network_saving") or 0) >= 0 else "#b91c1c"
                                    for r in labels]},
               "text": [f"{100 * runs[r]['share_of_ideal']:.0f}% of ideal" if runs[r].get("share_of_ideal") is not None
                        else "" for r in labels], "textposition": "outside"}]
    layout = {"yaxis": {"title": "network saving, $"}, "hovermode": "closest", "showlegend": False}
    if ideal is not None:
        layout["shapes"] = [{"type": "line", "xref": "paper", "x0": 0, "x1": 1, "y0": ideal, "y1": ideal,
                             "line": {"dash": "dash", "width": 1, "color": "#2563eb"}}]
        layout["annotations"] = [{"xref": "paper", "x": 1, "y": ideal, "text": "ideal", "showarrow": False,
                                  "xanchor": "right", "yanchor": "bottom"}]
    page.chart(section, traces, layout, height=320)
    rows = [{"run": label, **{k: v for k, v in row.items() if k != "mechanisms"},
             "mechanisms": ", ".join(row.get("mechanisms") or [])} for label, row in runs.items()]
    frame = pd.DataFrame(rows)
    money_columns = [c for c in frame.columns if c.startswith(("bills", "network_", "signal", "dr_incentive",
                                                                "ideal", "degradation"))]
    page.table(section, frame, formats={c: money for c in money_columns})
