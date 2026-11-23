"""The comparison cases on a report (docs/COMPARISON_CASES_SPEC.md v1.0, sections 5
and 6): a "Compare [without] with [with]" selector, its cards, and a Compare section,
all drawn in the page from the embedded cases, so changing the pair needs no re-run.
"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.reports.page import Page, Section

WITHOUT, WITH, CHANGE = "#9ca3af", "#2563eb", "#f59e0b"


def _picker(names: dict[str, str], buttons: list[dict]) -> str:
    options = "".join(f'<option value="{html.escape(n)}">{html.escape(label)}</option>' for n, label in names.items())
    quick = "".join(f'<button type="button" data-without="{html.escape(b["without"])}" '
                    f'data-with="{html.escape(b["with"])}">{html.escape(b["label"])}</button>' for b in buttons)
    return (f'<div class="picker">Compare <select class="case-pick" data-side="0">{options}</select> with '
            f'<select class="case-pick" data-side="1">{options}</select></div>'
            + (f'<div class="quick">{quick}</div>' if quick else ""))


def selector(page: Page, section: Section, folder: Path, summary: dict) -> bool:
    """The selector and its cards, in `section`; sets up the page's case data. False if
    the run has no cases (a folder written before cases)."""
    cases, compare = summary.get("cases"), summary.get("case_compare")
    net = folder / "cases_net_load_kw.csv"
    if not cases or not compare or not net.exists():
        return False
    frame = pd.read_csv(net, index_col=0, parse_dates=True)
    page.time_axis("t", frame.index)
    data = {}
    for name, case in cases.items():
        series = frame.get(name)
        data[name] = {**{k: case.get(k) for k in ("label", "description", "tariff", "costs", "energy",
                                                   "dr_incentive", "by_month")},
                      "bill": (case.get("bill") or {}).get("net_cost"),
                      "net": None if series is None else [None if np.isnan(v) else round(float(v), 3)
                                                          for v in series.to_numpy()],
                      "avg_day": _average_day(series) if series is not None else {}}
    page.extra["cases"] = {"cases": data, "compare": compare}
    page.html(section, _picker({n: c["label"] for n, c in cases.items()}, compare.get("buttons") or [])
              + '<div class="cards case-cards"></div><p class="note case-note"></p>')
    page.scripts.append(JS)
    return True


def _average_day(series: pd.Series) -> dict:
    index = pd.DatetimeIndex(series.index)
    table = pd.DataFrame({"kw": series.to_numpy(), "month": index.strftime("%Y-%m"), "hour": index.hour}).pivot_table(
        index="month", columns="hour", values="kw", aggfunc="mean").reindex(columns=range(24))
    return {m: [None if np.isnan(v) else round(float(v), 3) for v in row]
            for m, row in zip(table.index, table.to_numpy(), strict=True)}


def section(page: Page, summary: dict, window) -> None:
    """The Compare section: bills by month, value streams, net load on the linked time
    axis, and the average day by month, for the selected pair."""
    if "cases" not in page.extra:
        return
    names = {n: c["label"] for n, c in summary["cases"].items()}
    compare = page.section("compare", "Compare")
    page.html(compare, _picker(names, summary["case_compare"].get("buttons") or [])
              + '<p class="note case-note"></p>')
    ids = {}
    page.text(compare, "Bill by month (amount due each billing cycle), both cases, and the saving from the first to "
                       "the second.")
    ids["bills"] = page.next_chart_id()
    page.chart(compare, [], {"barmode": "group", "xaxis": {"type": "category"}, "yaxis": {"title": "$"},
                                  "hovermode": "x unified"}, height=320)
    page.text(compare, "Cost by value stream over the run (cost +, value −), both cases.")
    ids["streams"] = page.next_chart_id()
    page.chart(compare, [], {"barmode": "group", "yaxis": {"title": "$ over the run"}, "hovermode": "closest"},
               height=300)
    page.text(compare, "Net load at the meter, kW (import positive, export negative). Zooming here zooms the "
                       "dispatch charts too.")
    ids["net"] = page.next_chart_id()
    page.chart(compare, [], {"yaxis": {"title": "kW"}}, height=380, zoom=True, window=window)
    page.text(compare, "The average day in each month: net load of both cases by hour, kW.")
    ids["day"] = page.next_chart_id()
    months = len(next(iter(page.extra["cases"]["cases"].values()))["avg_day"]) or 1
    page.chart(compare, [], {}, height=190 * ((months + 3) // 4) + 40)
    page.text(compare, "The change by month and hour: the second case minus the first, kW (blue: less from the "
                       "grid; red: more).")
    ids["heat"] = page.next_chart_id()
    page.chart(compare, [], {"xaxis": {"type": "category"}, "yaxis": {"title": "hour", "dtick": 3},
                                  "hovermode": "closest"}, height=300)
    page.extra["cases"]["charts"] = ids
    page.extra["cases"]["colors"] = [WITHOUT, WITH, CHANGE]


JS = r"""
(function(){
const all=JSON.parse(document.getElementById("report-data").textContent);const X=all.extra&&all.extra.cases;
if(!X)return;const cases=X.cases,cmp=X.compare,ids=X.charts||{},colors=X.colors||["#9ca3af","#2563eb","#f59e0b"];
let pair=[cmp.without,cmp.with];
const fmt=(v,d)=>Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const money=(v,d=2)=>v==null||isNaN(v)?"–":(v<0?"-$":"$")+fmt(v,d);
const num=(v,d=0,u="")=>v==null||isNaN(v)?"–":(v<0?"-":"")+fmt(v,d)+u;
const signed=(v,d=0,u="")=>v==null||isNaN(v)?"–":(v>0?"+":"")+num(v,d,u);
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function card(label,value,note,tone){return `<div class="card ${tone||""}"><div class="label">${esc(label)}</div>`+
`<div class="value">${esc(value)}</div>`+(note?`<div class="sub">${esc(note)}</div>`:"")+"</div>";}
function tone(v){return v>0.005?"good":v<-0.005?"bad":"";}
function cards(a,b){const A=cases[a],B=cases[b],out=[];
if(A.bill!=null&&B.bill!=null){const s=A.bill-B.bill;out.push(card("Bill savings",money(s),`${money(A.bill)} → ${money(B.bill)}`,tone(s)));}
const t=A.costs.total-B.costs.total;out.push(card("Total cost savings",money(t),"including degradation",tone(t)));
const ea=A.energy,eb=B.energy;
out.push(card("Import",signed(eb.import_kwh-ea.import_kwh,0," kWh"),`${num(ea.import_kwh)} → ${num(eb.import_kwh)} kWh`));
out.push(card("Export",signed(eb.export_kwh-ea.export_kwh,0," kWh"),`${num(ea.export_kwh)} → ${num(eb.export_kwh)} kWh`));
out.push(card("Peak import",signed(eb.peak_import_kw-ea.peak_import_kw,2," kW"),
`${num(ea.peak_import_kw,2)} → ${num(eb.peak_import_kw,2)} kW`));
for(const n of [a,b]){if(cases[n].dr_incentive!=null)out.push(card("DR incentive",money(cases[n].dr_incentive),
`earned by ${cases[n].label}, at settlement`,""));}
return out.join("");}
function note(a,b){const A=cases[a],B=cases[b];let text=`${A.label} (${A.description}) → ${B.label} (${B.description}).`;
if(a===b)text="Pick two different cases.";
const ct=cmp.cost_tests;if(ct&&ct[0]===a&&ct[1]===b)text+=" This is the comparison the cost tests measure, so these "+
"cards describe the same change as TRC and RIM.";
if(A.tariff&&B.tariff&&A.tariff!==B.tariff)text+=` The two are billed on different tariffs (${A.tariff}; ${B.tariff}).`;
return text;}
function base(id){const c=all.charts.find(c=>c.id===id);return JSON.parse(JSON.stringify(c?c.layout:{}));}
function react(id,traces,layout,keepRange){const gd=document.getElementById(id);if(!gd||!window.Plotly)return;
if(keepRange&&gd.layout&&gd.layout.xaxis&&gd.layout.xaxis.range&&!gd.layout.xaxis.autorange){
layout.xaxis=Object.assign({},layout.xaxis,{range:gd.layout.xaxis.range.slice()});}
Plotly.react(gd,traces,themed(layout),{responsive:true,displaylogo:false});}
let axis=null;
function charts(a,b){const A=cases[a],B=cases[b];
if(ids.bills){const months=A.by_month.map(r=>r.month);const bill=(C,m)=>{const r=C.by_month.find(r=>r.month===m);
return r&&r.bill!=null?r.bill:null;};const ya=months.map(m=>bill(A,m)),yb=months.map(m=>bill(B,m));
react(ids.bills,[{type:"bar",name:A.label,x:months,y:ya,marker:{color:colors[0]},hovertemplate:"$%{y:,.2f}"},
{type:"bar",name:B.label,x:months,y:yb,marker:{color:colors[1]},hovertemplate:"$%{y:,.2f}"},
{type:"scatter",mode:"lines+markers",name:"saving",x:months,y:ya.map((v,i)=>v==null||yb[i]==null?null:v-yb[i]),
line:{color:colors[2]},hovertemplate:"$%{y:,.2f}"}],base(ids.bills));}
if(ids.streams){const keys=[...new Set([...Object.keys(A.costs),...Object.keys(B.costs)])].filter(k=>k!=="total");
react(ids.streams,[{type:"bar",name:A.label,x:keys,y:keys.map(k=>A.costs[k]||0),marker:{color:colors[0]},
hovertemplate:"$%{y:,.2f}"},{type:"bar",name:B.label,x:keys,y:keys.map(k=>B.costs[k]||0),marker:{color:colors[1]},
hovertemplate:"$%{y:,.2f}"}],base(ids.streams));}
if(ids.net){if(!axis)axis=axisValues(all.axes.t);
react(ids.net,[{type:"scatter",mode:"lines",name:A.label,x:axis,y:A.net,line:{color:colors[0],width:1.2},
hovertemplate:"%{y:,.2f} kW"},{type:"scatter",mode:"lines",name:B.label,x:axis,y:B.net,
line:{color:colors[1],width:1.5},hovertemplate:"%{y:,.2f} kW"}],base(ids.net),true);}
if(ids.day){const months=Object.keys(A.avg_day),traces=[],layout={grid:{rows:Math.ceil(months.length/4),
columns:Math.min(4,months.length),pattern:"independent"},showlegend:true,hovermode:"x unified",annotations:[]};
const hours=[...Array(24).keys()];months.forEach((m,i)=>{const s=i?String(i+1):"";
traces.push({type:"scatter",mode:"lines",name:A.label,x:hours,y:A.avg_day[m],xaxis:"x"+s,yaxis:"y"+s,
line:{color:colors[0]},legendgroup:"a",showlegend:i===0,hovertemplate:"%{y:,.2f} kW"});
traces.push({type:"scatter",mode:"lines",name:B.label,x:hours,y:(B.avg_day[m]||[]),xaxis:"x"+s,yaxis:"y"+s,
line:{color:colors[1]},legendgroup:"b",showlegend:i===0,hovertemplate:"%{y:,.2f} kW"});
layout["xaxis"+s]={dtick:6,range:[0,23]};layout["yaxis"+s]={matches:"y"};
layout.annotations.push({text:m,xref:"x"+s+" domain",yref:"y"+s+" domain",x:0.02,y:1,showarrow:false,
xanchor:"left",yanchor:"bottom",font:{size:11}});});layout.yaxis={};layout.margin={l:40,r:10,t:50,b:30};
react(ids.day,traces,layout);}
if(ids.heat){const months=Object.keys(A.avg_day);const z=months.map(m=>A.avg_day[m].map((v,h)=>{
const w=(B.avg_day[m]||[])[h];return v==null||w==null?null:w-v;}));
const limit=Math.max(1e-6,...z.flat().filter(v=>v!=null).map(Math.abs));
const zt=[...Array(24).keys()].map(h=>z.map(r=>r[h]));
react(ids.heat,[{type:"heatmap",x:months,y:[...Array(24).keys()],z:zt,colorscale:"RdBu",reversescale:true,
zmin:-limit,zmax:limit,colorbar:{title:"kW"},hovertemplate:"%{x}, %{y}:00: %{z:,.2f} kW<extra></extra>"}],
base(ids.heat));}}
function show(){const [a,b]=pair;document.querySelectorAll(".case-pick").forEach(s=>{s.value=pair[+s.dataset.side];});
document.querySelectorAll(".quick button").forEach(q=>q.classList.toggle("on",q.dataset.without===a&&q.dataset.with===b));
document.querySelectorAll(".case-cards").forEach(c=>{c.innerHTML=cards(a,b);});
document.querySelectorAll(".case-note").forEach(n=>{n.textContent=note(a,b);});charts(a,b);}
document.querySelectorAll(".case-pick").forEach(s=>s.addEventListener("change",()=>{pair[+s.dataset.side]=s.value;show();}));
document.querySelectorAll(".quick button").forEach(q=>q.addEventListener("click",()=>{pair=[q.dataset.without,q.dataset.with];show();}));
window.addEventListener("load",show);
})();
"""


# --- a study's portfolio (section 6) -------------------------------------------------

STUDY_BUTTONS = (("The other devices", "no_devices", "no_measure"), ("The measure, rule-based", "no_measure", "rule_based"),
                 ("Optimization", "rule_based", "optimized"), ("DR participation", "optimized", "responding"),
                 ("Everything", "no_devices", "responding"))


def study_selector(page: Page, section: Section, folder: Path, table: pd.DataFrame, premises: dict) -> None:
    """A pair selector for the whole study: each premise's bill savings for that pair
    (its total cost savings where it has no bill), largest first, and the total."""
    import json

    from der_opt.cases import CASES, GENERIC_LABELS

    rows = table[table["premise_id"] != "total"]
    basis = "bill" if any(c.startswith("case_bill_") and rows[c].notna().any() for c in rows) else "cost"
    names = [n for n in CASES if f"case_{basis}_{n}" in rows and rows[f"case_{basis}_{n}"].notna().any()]
    if len(names) < 2:
        return
    values = {n: [None if pd.isna(v) else float(v) for v in rows[f"case_{basis}_{n}"]] for n in names}
    opening = None
    for item in premises.values():  # the first premise's own opening pair
        path = folder / str(item.get("folder") or "") / "summary.json"
        if item.get("folder") and path.exists():
            pair = json.loads(path.read_text(encoding="utf-8")).get("case_compare") or {}
            if pair.get("without") in names and pair.get("with") in names:
                opening = [pair["without"], pair["with"]]
                break
    if opening is None:
        opening = ["rule_based", "optimized"] if {"rule_based", "optimized"} <= set(names) else [names[0], names[-1]]
    buttons = [{"label": label, "without": a, "with": b} for label, a, b in STUDY_BUTTONS if a in names and b in names]
    chart = page.next_chart_id()
    page.extra["study_cases"] = {"premises": rows["premise_id"].astype(str).tolist(), "values": values,
                                 "labels": {n: GENERIC_LABELS[n] for n in names}, "pair": opening, "chart": chart,
                                 "basis": basis}
    page.html(section, _picker({n: GENERIC_LABELS[n] for n in names}, buttons)
              + '<div class="cards case-cards"></div>')
    page.chart(section, [], {"hovermode": "closest", "showlegend": False, "xaxis": {"type": "category"},
                             "yaxis": {"title": f"{basis} savings, $"}}, height=320)
    page.text(section, f"Each premise's {basis} savings going from the first case to the second, largest first. "
                       "A premise without one of the two cases (no measure to remove, say) has no bar.")
    page.scripts.append(STUDY_JS)


STUDY_JS = r"""
(function(){
const all=JSON.parse(document.getElementById("report-data").textContent);const S=all.extra&&all.extra.study_cases;
if(!S)return;let pair=S.pair.slice();
const money=v=>v==null||isNaN(v)?"–":(v<0?"-$":"$")+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,
maximumFractionDigits:2});
const card=(l,v,n,t)=>`<div class="card ${t||""}"><div class="label">${l}</div><div class="value">${v}</div>`+
(n?`<div class="sub">${n}</div>`:"")+"</div>";
function show(){const [a,b]=pair;document.querySelectorAll(".case-pick").forEach(s=>{s.value=pair[+s.dataset.side];});
document.querySelectorAll(".quick button").forEach(q=>q.classList.toggle("on",q.dataset.without===a&&q.dataset.with===b));
const rows=S.premises.map((p,i)=>({p,v:S.values[a][i]==null||S.values[b][i]==null?null:S.values[a][i]-S.values[b][i]}))
.filter(r=>r.v!=null).sort((x,y)=>y.v-x.v);const total=rows.reduce((s,r)=>s+r.v,0);
const saving=rows.filter(r=>r.v>0.005).length;
document.querySelectorAll(".case-cards").forEach(c=>{c.innerHTML=card(`Total ${S.basis} savings`,money(total),
`${S.labels[a]} → ${S.labels[b]}, ${rows.length} premises`,total>0.005?"good":total<-0.005?"bad":"")+
card("Premises that save",`${saving} of ${rows.length}`,"")+
(rows.length?card("Mean per premise",money(total/rows.length),""):"");});
const gd=document.getElementById(S.chart);if(!gd||!window.Plotly)return;
const layout=JSON.parse(JSON.stringify(all.charts.find(c=>c.id===S.chart).layout));
Plotly.react(gd,[{type:"bar",x:rows.map(r=>r.p),y:rows.map(r=>r.v),marker:{color:"#2563eb"},
hovertemplate:"%{x}: $%{y:,.2f}<extra></extra>"}],themed(layout),{responsive:true,displaylogo:false});}
document.querySelectorAll(".case-pick").forEach(s=>s.addEventListener("change",()=>{pair[+s.dataset.side]=s.value;show();}));
document.querySelectorAll(".quick button").forEach(q=>q.addEventListener("click",()=>{pair=[q.dataset.without,q.dataset.with];show();}));
window.addEventListener("load",show);
})();
"""
