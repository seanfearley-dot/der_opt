"""The page a report is: sections of cards, charts and tables, rendered as one HTML
file that opens without a server (docs/REPORTS.md section 2).

Charts are Plotly figures given as dicts. A long time axis is sent once, as
{"start", "minutes", "n"}, and traces refer to it with {"axis": name}, so a year of
hourly data isn't written out once per trace. plotly.js is read from the nearest
`_assets/` folder, so one copy serves a study and every run inside it.
"""

from __future__ import annotations

import html
import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.results.summary import jsonable

ASSETS = "_assets"
PLOTLY = "plotly.min.js"


def plotly_source() -> Path:
    import plotly

    return Path(plotly.__file__).parent / "package_data" / PLOTLY


def ensure_assets(root: str | Path) -> Path:
    """`root/_assets/plotly.min.js`, copied there if it isn't already."""
    import shutil

    target = Path(root) / ASSETS / PLOTLY
    source = plotly_source()
    if not target.exists() or target.stat().st_size != source.stat().st_size:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    return target


def assets_for(folder: str | Path, levels: int = 3) -> str:
    """The script's path relative to `folder`: the nearest `_assets/` at or above it
    (a study's), or a new one in `folder`."""
    folder = Path(folder).resolve()
    here = folder
    for depth in range(levels + 1):
        if (here / ASSETS / PLOTLY).exists():
            return "/".join([".."] * depth + [ASSETS, PLOTLY])
        here = here.parent
    ensure_assets(folder)
    return f"{ASSETS}/{PLOTLY}"


# --- formatting ---------------------------------------------------------------------


def money(value, decimals: int = 2) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    return f"-${-value:,.{decimals}f}" if value < 0 else f"${value:,.{decimals}f}"


def number(value, decimals: int = 2, unit: str = "") -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "–"
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    if isinstance(value, (int, np.integer)):
        return f"{value:,}{unit}"
    return f"{value:,.{decimals}f}{unit}"


def cell(value) -> str:
    """A table cell: numbers to 2 decimals (3 when small), the rest as text."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, (bool, np.bool_)):
        return "yes" if value else "no"
    if isinstance(value, (int, np.integer)):
        return f"{value:,}"
    if isinstance(value, (float, np.floating)):
        return f"{value:,.3f}" if 0 < abs(value) < 1 else f"{value:,.2f}"
    return str(value)


# --- the page ------------------------------------------------------------------------


@dataclass
class Card:
    label: str
    value: str
    note: str = ""
    tone: str = ""  # "good", "bad" or ""


@dataclass
class Section:
    id: str
    title: str
    parts: list[str] = field(default_factory=list)


class Page:
    def __init__(self, title: str, subtitle: str = "", kind: str = ""):
        self.title, self.subtitle, self.kind = title, subtitle, kind
        self.sections: list[Section] = []
        self.charts: list[dict] = []
        self.axes: dict[str, dict] = {}
        self.links: list[tuple[str, str]] = []  # (label, href) above the title
        self.extra: dict = {}  # more data for `scripts`, at report-data's "extra"
        self.scripts: list[str] = []  # run after the page's own script (charts are drawn first)
        self.banner = ""  # markup pinned at the top of the main column, above every section

    # sections and parts
    def section(self, id: str, title: str) -> Section:
        section = Section(id, title)
        self.sections.append(section)
        return section

    @staticmethod
    def text(section: Section, text: str) -> None:
        section.parts.append(f'<p class="note">{html.escape(text)}</p>')

    @staticmethod
    def html(section: Section, markup: str) -> None:
        section.parts.append(markup)

    @staticmethod
    def cards(section: Section, cards: list[Card]) -> None:
        items = "".join(
            f'<div class="card {c.tone}"><div class="label">{html.escape(c.label)}</div>'
            f'<div class="value">{html.escape(c.value)}</div>'
            + (f'<div class="sub">{html.escape(c.note)}</div>' if c.note else "") + "</div>"
            for c in cards)
        section.parts.append(f'<div class="cards">{items}</div>')

    def time_axis(self, name: str, index) -> dict:
        """A shared axis for an evenly spaced DatetimeIndex: {"axis": name} in a trace."""
        index = pd.DatetimeIndex(index)
        minutes = int((index[1] - index[0]).total_seconds() // 60) if len(index) > 1 else 60
        self.axes[name] = {"start": index[0].strftime("%Y-%m-%dT%H:%M:%S"), "minutes": minutes, "n": len(index)}
        return {"axis": name}

    def next_chart_id(self) -> str:
        """The id the next chart gets: a script can fill a chart drawn empty."""
        return f"chart{len(self.charts)}"

    def chart(self, section: Section, traces: list[dict], layout: dict | None = None, height: int = 360,
              zoom: bool = False, window: tuple[str, str] | None = None, caption: str = "", unit: str = "",
              unit2: str = "") -> None:
        """A Plotly chart. `zoom` adds day/week/month/all buttons and a range slider;
        `window` is the range it opens on. A color "ink" follows the page's theme. With
        `unit` (and `unit2` for the right-hand axis), hovering shows each value with it."""
        chart_id = self.next_chart_id()
        layout = dict(layout or {})
        if unit or unit2:
            for trace in traces:
                if "hovertemplate" not in trace and trace.get("type", "scatter") in ("scatter", "bar"):
                    shown = unit2 if trace.get("yaxis") == "y2" else unit
                    trace["hovertemplate"] = "%{y:,.2f}" + (f" {shown}" if shown else "")
        if zoom:
            layout.setdefault("legend", {"orientation": "h", "x": 0, "xanchor": "left", "y": -0.32, "yanchor": "top"})
            layout.setdefault("margin", {"l": 56, "r": 20, "t": 40, "b": 90})
            xaxis = layout.setdefault("xaxis", {})
            if window is not None:
                xaxis["range"] = list(window)
            xaxis["rangeselector"] = {"buttons": [
                {"count": 1, "label": "1 day", "step": "day", "stepmode": "backward"},
                {"count": 7, "label": "1 week", "step": "day", "stepmode": "backward"},
                {"count": 1, "label": "1 month", "step": "month", "stepmode": "backward"},
                {"step": "all", "label": "all"}]}
            xaxis["rangeslider"] = {"visible": True, "thickness": 0.06}
        self.charts.append({"id": chart_id, "traces": traces, "layout": layout, "sync": zoom})
        if caption:
            section.parts.append(f'<div class="caption">{html.escape(caption)}</div>')
        section.parts.append(f'<div class="chart" id="{chart_id}" style="height:{height}px"></div>')

    @staticmethod
    def table(section: Section, frame: pd.DataFrame, links: dict[str, dict] | None = None, filter: bool = False,
              caption: str = "", formats: dict | None = None, collapsed: str | None = None) -> None:
        """A sortable table. `links` maps a column to {value: href}; `formats` a column to
        a function of the value. `collapsed` puts it in a <details> with that summary."""
        links, formats = links or {}, formats or {}

        def numeric(column) -> bool:
            values = frame[column].dropna()
            return len(values) > 0 and all(isinstance(v, (int, float, np.integer, np.floating))
                                           and not isinstance(v, (bool, np.bool_)) for v in values)

        head = "".join(f'<th{" class=num" if numeric(c) else ""}>{html.escape(str(c))}</th>' for c in frame.columns)
        rows = []
        for _, row in frame.iterrows():
            cells = []
            for column in frame.columns:
                value = row[column]
                shown = formats[column](value) if column in formats else cell(value)
                sort = value if isinstance(value, (int, float, np.integer, np.floating)) and not (
                    isinstance(value, float) and math.isnan(value)) else ""
                key = f' data-sort="{sort}"' if sort != "" else ""
                href = links.get(column, {}).get(value)
                text = html.escape(shown)
                if href:
                    text = f'<a href="{html.escape(href)}">{text}</a>'
                numeric = " class=\"num\"" if sort != "" else ""
                cells.append(f"<td{key}{numeric}>{text}</td>")
            rows.append("<tr>" + "".join(cells) + "</tr>")
        search = '<input class="filter" type="search" placeholder="Filter rows">' if filter else ""
        cap = f'<div class="caption">{html.escape(caption)}</div>' if caption else ""
        markup = (f'<div class="table-wrap">{cap}{search}<table class="sortable"><thead><tr>{head}</tr></thead>'
                  f'<tbody>{"".join(rows)}</tbody></table></div>')
        if collapsed:
            markup = f"<details><summary>{html.escape(collapsed)}</summary>{markup}</details>"
        section.parts.append(markup)

    # rendering
    def render(self, script: str) -> str:
        nav = "".join(f'<a href="#{s.id}">{html.escape(s.title)}</a>' for s in self.sections if s.parts)
        body = self.banner + "".join(f'<section id="{s.id}"><h2>{html.escape(s.title)}</h2>{"".join(s.parts)}</section>'
                       for s in self.sections if s.parts)
        crumbs = " / ".join(f'<a href="{html.escape(h)}">{html.escape(t)}</a>' for t, h in self.links)
        data = json.dumps(jsonable({"axes": self.axes, "charts": self.charts, "extra": self.extra}),
                          separators=(",", ":"))
        data = data.replace("</", "<\\/")
        return TEMPLATE.format(
            title=html.escape(self.title), subtitle=html.escape(self.subtitle), kind=html.escape(self.kind),
            crumbs=f'<div class="crumbs">{crumbs}</div>' if crumbs else "", nav=nav, body=body, script=script,
            data=data, css=CSS, js=JS + "".join(self.scripts), main_class=' class="has-bar"' if self.banner else "")


CSS = """
:root{--bg:#f7f7f5;--panel:#fff;--ink:#1d1f23;--muted:#61656e;--line:#e2e2de;--accent:#2563eb;
--good:#15803d;--bad:#b91c1c;--chip:#eef1f6;--grid:#e9e9e6}
@media (prefers-color-scheme:dark){:root{--bg:#141518;--panel:#1c1e22;--ink:#e8e9ec;--muted:#9aa0aa;
--line:#2d3036;--accent:#7aa2ff;--good:#4ade80;--bad:#f87171;--chip:#262a31;--grid:#2a2d33}}
*{box-sizing:border-box}
.premise-pick{display:inline-flex;gap:8px;align-items:center;margin:10px 0 4px;color:var(--muted);font-size:13px}
.premise-pick select{font:inherit;color:var(--ink);background:var(--panel);border:1px solid var(--line);
border-radius:6px;padding:3px 6px}
body{margin:0;background:var(--bg);color:var(--ink);font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",
Roboto,Helvetica,Arial,sans-serif}
header{padding:20px 24px 12px;border-bottom:1px solid var(--line);background:var(--panel)}
header h1{margin:2px 0 2px;font-size:22px;font-weight:650;letter-spacing:-.01em}
header .sub{color:var(--muted)}
.kind{display:inline-block;font-size:11px;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);
background:var(--chip);border-radius:4px;padding:1px 6px}
.crumbs{font-size:13px;margin-bottom:6px}
.layout{display:grid;grid-template-columns:190px minmax(0,1fr);gap:24px;padding:20px 24px;max-width:1500px}
nav{position:sticky;top:16px;align-self:start;display:flex;flex-direction:column;gap:2px}
nav a{color:var(--muted);text-decoration:none;padding:4px 8px;border-radius:6px}
nav a:hover{background:var(--chip);color:var(--ink)}
a{color:var(--accent)}
section{background:var(--panel);border:1px solid var(--line);border-radius:10px;padding:16px 18px;margin-bottom:18px}
section h2{margin:0 0 12px;font-size:16px;font-weight:650}
.note{color:var(--muted);margin:4px 0 10px}
.cards{display:grid;grid-template-columns:repeat(auto-fill,minmax(170px,1fr));gap:10px;margin-bottom:6px}
.card{border:1px solid var(--line);border-radius:8px;padding:10px 12px}
.card .label{color:var(--muted);font-size:12px}
.card .value{font-size:20px;font-weight:650;font-variant-numeric:tabular-nums}
.card .sub{color:var(--muted);font-size:12px}
.card.good .value{color:var(--good)} .card.bad .value{color:var(--bad)}
.card.feature{border-width:2px;border-color:var(--accent)} .card.feature .value{font-size:26px}
.chart{width:100%;margin:6px 0 14px}
.table-wrap{overflow-x:auto;margin:6px 0 14px}
.caption{font-weight:600;margin-bottom:4px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;white-space:nowrap}
th{position:sticky;top:0;background:var(--panel);cursor:pointer;color:var(--muted);font-weight:600;user-select:none}
th:hover{color:var(--ink)}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
tbody tr:hover{background:var(--chip)}
.filter{margin:0 0 6px;padding:5px 8px;border:1px solid var(--line);border-radius:6px;background:var(--bg);
color:var(--ink);width:260px}
details summary{cursor:pointer;color:var(--accent);margin:4px 0}
.level-error{color:var(--bad);font-weight:600} .level-warning{color:#b45309}
.picker{display:flex;flex-wrap:wrap;align-items:center;gap:8px;margin:14px 0 8px}
.picker select{padding:4px 6px;border:1px solid var(--line);border-radius:6px;background:var(--bg);color:var(--ink);
font:inherit}
.has-bar section{scroll-margin-top:120px}
.pairbar{position:sticky;top:0;z-index:20;background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:10px 16px;margin-bottom:18px;box-shadow:0 4px 14px rgba(0,0,0,.12)}
.pairbar .picker{margin:0 0 6px} .pairbar .quick{margin:0 0 6px} .pairbar .sign{margin:0;font-size:13px}
.ab{display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;border-radius:5px;
font-weight:700;font-size:12px;color:#fff} .ab.a{background:#9ca3af;color:#111} .ab.b{background:#2563eb}
.fixed{border-left:3px solid var(--line);padding-left:8px}
.section-meta{text-align:right;color:var(--muted);font-size:13px;margin:-6px 0 8px}
.quick{display:flex;flex-wrap:wrap;gap:6px;margin:0 0 10px}
.quick button{padding:3px 10px;border:1px solid var(--line);border-radius:999px;background:var(--panel);
color:var(--ink);font:inherit;font-size:12px;cursor:pointer}
.quick button:hover{background:var(--chip)} .quick button.on{border-color:var(--accent);color:var(--accent)}
@media (max-width:820px){.layout{grid-template-columns:1fr;padding:12px}nav{position:static;flex-direction:row;
flex-wrap:wrap}header{padding:16px 12px}}
"""

JS = r"""
function axisValues(spec){const out=new Array(spec.n);const t0=Date.parse(spec.start+"Z");
for(let i=0;i<spec.n;i++){out[i]=new Date(t0+i*spec.minutes*60000).toISOString().slice(0,16).replace("T"," ");}
return out;}
function expand(value,axes,cache){if(value&&typeof value==="object"&&!Array.isArray(value)&&value.axis){
if(!cache[value.axis])cache[value.axis]=axisValues(axes[value.axis]);return cache[value.axis];}return value;}
function themed(layout){const css=getComputedStyle(document.documentElement);const v=n=>css.getPropertyValue(n).trim();
const base={paper_bgcolor:"rgba(0,0,0,0)",plot_bgcolor:"rgba(0,0,0,0)",font:{color:v("--ink"),size:12},
margin:{l:56,r:20,t:40,b:40},legend:{orientation:"h",y:1.02,yanchor:"bottom",x:1,xanchor:"right"},
hovermode:"x unified",hoverlabel:{bgcolor:v("--panel"),bordercolor:v("--line"),font:{color:v("--ink"),size:12}}};
const out=Object.assign({},base,layout);for(const key of Object.keys(out)){if(/^[xy]axis\d*$/.test(key)){
out[key]=Object.assign({gridcolor:v("--grid"),zerolinecolor:v("--line"),linecolor:v("--line")},out[key]);
if(out[key].rangeselector)out[key].rangeselector=Object.assign({bgcolor:v("--chip"),activecolor:v("--line")},
out[key].rangeselector);}}
if(!out.xaxis)out.xaxis={gridcolor:v("--grid")};if(!out.yaxis)out.yaxis={gridcolor:v("--grid")};return out;}
function draw(){const data=JSON.parse(document.getElementById("report-data").textContent);const cache={};
const ink=getComputedStyle(document.documentElement).getPropertyValue("--ink").trim();
for(const chart of data.charts){const traces=chart.traces.map(t=>{const c=Object.assign({},t);c.x=expand(c.x,data.axes,cache);
if(c.line&&c.line.color==="ink")c.line=Object.assign({},c.line,{color:ink});
c.y=expand(c.y,data.axes,cache);return c;});
Plotly.newPlot(chart.id,traces,themed(chart.layout),{responsive:true,displaylogo:false});}
linkZoom(data.charts.filter(c=>c.sync).map(c=>document.getElementById(c.id)));}
function linkZoom(charts){let busy=false;
for(const chart of charts){chart.on("plotly_relayout",ev=>{if(busy)return;let update=null;
if(ev["xaxis.range[0]"]!==undefined)update={"xaxis.range":[ev["xaxis.range[0]"],ev["xaxis.range[1]"]]};
else if(ev["xaxis.range"])update={"xaxis.range":ev["xaxis.range"]};
else if(ev["xaxis.autorange"])update={"xaxis.autorange":true};
if(!update)return;busy=true;
Promise.all(charts.filter(c=>c!==chart).map(c=>Plotly.relayout(c,update))).finally(()=>{busy=false;});});}}
function sortTables(){document.querySelectorAll("table.sortable").forEach(table=>{table.querySelectorAll("th").forEach((th,i)=>{
th.addEventListener("click",()=>{const body=table.tBodies[0];const rows=[...body.rows];const asc=th.dataset.dir!=="asc";
table.querySelectorAll("th").forEach(h=>delete h.dataset.dir);th.dataset.dir=asc?"asc":"desc";
const key=r=>{const c=r.cells[i];return c.dataset.sort!==undefined?parseFloat(c.dataset.sort):c.textContent.toLowerCase();};
rows.sort((a,b)=>{const x=key(a),y=key(b);return (x>y?1:x<y?-1:0)*(asc?1:-1);});rows.forEach(r=>body.appendChild(r));});});});
document.querySelectorAll("input.filter").forEach(input=>{const table=input.parentElement.querySelector("table");
input.addEventListener("input",()=>{const q=input.value.toLowerCase();for(const r of table.tBodies[0].rows){
r.style.display=r.textContent.toLowerCase().includes(q)?"":"none";}});});}
window.addEventListener("load",()=>{sortTables();if(window.Plotly)draw();else document.querySelectorAll(".chart")
.forEach(c=>c.textContent="Charts need plotly.js next to this report (run `der-opt report` on the folder).");});
"""

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title><style>{css}</style><script src="{script}"></script></head>
<body><header>{crumbs}<span class="kind">{kind}</span><h1>{title}</h1><div class="sub">{subtitle}</div></header>
<div class="layout"><nav>{nav}</nav><main{main_class}>{body}</main></div>
<script type="application/json" id="report-data">{data}</script><script>{js}</script></body></html>
"""
