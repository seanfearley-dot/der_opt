"""The report page for a study: its population, and links to its feeder analysis and
each portfolio premise (docs/REPORTS.md section 2)."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from der_opt.reports.page import Card, Page, money, number
from der_opt.reports.scenario import _checks, _read, _round, _settings, _subtitle
from der_opt.results.summary import read_summary

# the portfolio columns shown first, and the metrics its chart can show
PORTFOLIO_FIRST = ("premise_id", "measure", "note", "optimal_cost", "counterfactual_cost", "value_of_optimization",
                   "dr_incentive_earned", "counted_R2_season_mean_kw", "TRC_ratio", "TRC_net_benefits", "RIM_ratio",
                   "RIM_net_benefits")
METRICS = ("value_of_optimization", "dr_incentive_earned", "counted_R2_season_mean_kw", "TRC_ratio", "TRC_net_benefits",
           "RIM_ratio", "RIM_net_benefits", "optimal_cost")


def build(folder: Path, summary: dict) -> Page:
    page = Page(summary["name"], _subtitle(summary), "study")
    population = summary.get("population") or {}

    top = page.section("summary", "Summary")
    cards = [Card("Premises", number(summary.get("premises")),
                  ", ".join(f"{n} {o}" for o, n in (population.get("origins") or {}).items()))]
    for kind, n in sorted((population.get("premises_with") or {}).items()):
        cards.append(Card(f"With {kind}", number(n), f"of {summary.get('premises')} premises"))
    cards.append(Card("Analyses", ", ".join(summary.get("analyses") or []) or "none"))
    page.cards(top, cards)

    feeder = summary.get("feeder")
    if feeder:
        section = page.section("feeder", "Feeder analysis")
        sub = read_summary(folder / feeder["folder"])
        link = f'{feeder["folder"]}/report.html'
        page.html(section, f'<p><a href="{link}">Open the feeder report</a></p>')
        if sub:
            runs = sub.get("runs") or {}
            cards = [Card(f"Total, {label}", money(runs[label]["total"])) for label in
                     ("coordinated", "independent", "counterfactual") if label in runs]
            versus = sub.get("coordinated_vs_independent")
            if versus:
                cards.append(Card("Network saving", money(versus["network_saving"]), "coordinated vs independent"))
            for label, run in ((sub.get("coordination") or {}).get("runs") or {}).items():
                if run.get("share_of_ideal") is not None:
                    cards.append(Card(f"{label}", f"{100 * run['share_of_ideal']:.0f}% of ideal",
                                      f"saves {money(run.get('network_saving'))}"))
            page.cards(section, cards)

    portfolio = summary.get("portfolio")
    if portfolio:
        _portfolio(page, folder, portfolio)

    _population(page, folder, summary)
    _checks(page, summary)
    _settings(page, folder)
    return page


def _portfolio(page: Page, folder: Path, portfolio: dict) -> None:
    section = page.section("portfolio", "Portfolio")
    table = _read(folder, portfolio["table"])
    premises = {p["premise_id"]: p for p in portfolio["premises"]}
    failed = [p for p, item in premises.items() if item.get("failed")]
    skipped = [p for p, item in premises.items() if item.get("folder") is None]
    page.text(section, f"Each of {len(premises)} premises run on its own: DR, screening values and cost tests."
              + (f" Failed: {', '.join(failed)}." if failed else "") + (f" Skipped: {', '.join(skipped)}." if skipped
                                                                          else ""))
    if table is None:
        return
    rows = table[table["premise_id"] != "total"]
    metrics = [m for m in METRICS if m in rows and rows[m].notna().any()]
    if metrics and len(rows):
        traces, buttons = [], []
        for i, metric in enumerate(metrics):
            ordered = rows.sort_values(metric, ascending=False)
            traces.append({"type": "bar", "name": metric, "x": ordered["premise_id"].tolist(),
                           "y": _round(ordered[metric], 3), "visible": i == 0, "marker": {"color": "#2563eb"}})
            buttons.append({"label": metric.replace("_", " "), "method": "update",
                            "args": [{"visible": [j == i for j in range(len(metrics))]},
                                     {"yaxis": {"title": metric.replace("_", " ")}}]})
        page.chart(section, traces, {"hovermode": "closest", "showlegend": False,
                                     "yaxis": {"title": metrics[0].replace("_", " ")},
                                     "updatemenus": [{"buttons": buttons, "x": 0, "xanchor": "left", "y": 1.18,
                                                      "yanchor": "top", "direction": "down"}],
                                     "margin": {"t": 60}}, height=340)
        page.text(section, "One bar per premise, largest first; pick the measure from the menu.")
    first = [c for c in PORTFOLIO_FIRST if c in table]
    links = {"premise_id": {p: f"{item['folder']}/report.html" for p, item in premises.items() if item.get("folder")}}
    money_columns = [c for c in table.columns if c.endswith(("_cost", "_optimization", "_earned", "_net_benefits"))
                     or c.startswith(("bill_", "screening"))]
    page.table(section, table[first], links=links, filter=True, formats={c: money for c in money_columns if c in first})
    page.table(section, table, links=links, filter=True, collapsed=f"Every column ({len(table.columns)})",
               formats={c: money for c in money_columns})


def _population(page: Page, folder: Path, summary: dict) -> None:
    table = _read(folder, "premises.csv")
    section = page.section("population", "Population")
    by_element = (summary.get("population") or {}).get("by_element") or {}
    if table is not None and len(table):
        kinds = [c for c in table.columns if c.startswith("n_")]
        if kinds and "element_id" in table and table["element_id"].notna().any():
            grouped = table.groupby("element_id")[kinds].apply(lambda g: (g > 0).sum())
            traces = [{"type": "bar", "name": k[2:], "x": grouped.index.tolist(), "y": grouped[k].tolist()}
                      for k in kinds]
            page.text(section, "Premises with each device type, by network element.")
            page.chart(section, traces, {"barmode": "group", "hovermode": "closest", "yaxis": {"title": "premises"}},
                       height=300)
        elif by_element:
            page.text(section, ", ".join(f"{e}: {n}" for e, n in by_element.items()))
        page.table(section, table, filter=True, caption="Premises: settings, sources and technology mix")
    additions = summary.get("add_devices") or []
    if additions:
        rows = [{"entry": a["entry"], "device": f"{a['device_id']} ({a['type']})", "share": a["share"],
                 "seed": a["seed"], "selected": a["selected"], "already had one": len(a["already_have"]),
                 "metered": len(a["metered"]), "eligible": a["eligible"], "added": len(a["chosen"]),
                 "premises": ", ".join(a["chosen"])} for a in additions]
        page.table(section, pd.DataFrame(rows), caption="add_devices: what each entry chose")
