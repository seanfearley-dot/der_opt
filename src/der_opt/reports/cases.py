"""The comparison cases on a report (docs/COMPARISON_CASES_SPEC.md v1.1, sections 5
and 6). One A/B choice, pinned at the top of the page, drives every card, chart and
table that can follow it; every difference is A − B. It's all drawn in the page from
the embedded cases, so changing the pair needs no re-run.
"""

from __future__ import annotations

import html
from pathlib import Path

import numpy as np
import pandas as pd

from der_opt.reports.page import Page, Section

A_COLOR, B_COLOR, DIFF_COLOR = "#9ca3af", "#2563eb", "#f59e0b"
SIGN = "Differences are A − B: positive means B is lower than A."


def _picker(names: dict[str, str], buttons: list[dict]) -> str:
    options = "".join(f'<option value="{html.escape(n)}">{html.escape(label)}</option>' for n, label in names.items())
    quick = "".join(f'<button type="button" data-without="{html.escape(b["without"])}" '
                    f'data-with="{html.escape(b["with"])}">{html.escape(b["label"])}</button>' for b in buttons)
    return (f'<div class="picker"><span class="ab a">A</span><select class="case-pick" data-side="0">{options}'
            f'</select><span class="ab b">B</span><select class="case-pick" data-side="1">{options}</select></div>'
            + (f'<div class="quick">{quick}</div>' if quick else ""))


def _rounded(values, decimals: int = 3) -> list:
    return [None if np.isnan(v) else round(float(v), decimals) for v in np.asarray(values, dtype=float)]


def _average_day(series: pd.Series) -> dict:
    index = pd.DatetimeIndex(series.index)
    table = pd.DataFrame({"kw": series.to_numpy(), "month": index.strftime("%Y-%m"), "hour": index.hour}).pivot_table(
        index="month", columns="hour", values="kw", aggfunc="mean").reindex(columns=range(24))
    return {m: _rounded(row) for m, row in zip(table.index, table.to_numpy(), strict=True)}


def setup(page: Page, folder: Path, summary: dict) -> bool:
    """Embeds the cases and pins the A/B bar at the top of the page. False if the
    folder has no cases (one written before them): the page then shows optimal against
    counterfactual, as it did."""
    from der_opt.reports.scenario import _color

    cases, compare = summary.get("cases"), summary.get("case_compare")
    net_path, device_path = folder / "cases_net_load_kw.csv", folder / "cases_device_dispatch.csv"
    if not cases or not compare or not net_path.exists():
        return False
    net = pd.read_csv(net_path, index_col=0, parse_dates=True)
    devices = pd.read_csv(device_path, index_col=0, parse_dates=True) if device_path.exists() else None
    page.time_axis("t", net.index)
    kinds = {d["name"]: d["type"] for d in summary.get("devices") or []}
    data = {}
    for name, case in cases.items():
        series = net.get(name)
        own = {}
        if devices is not None:
            for column in devices.columns:
                case_name, device, part = column.split(".", 2)
                if case_name == name:
                    own.setdefault(device, {})["kw" if part == "net_power" else "kwh"] = _rounded(devices[column])
        data[name] = {**{k: case.get(k) for k in ("label", "description", "tariff", "program", "costs", "energy",
                                                   "dr_incentive", "by_month", "bill")},
                      "net": _rounded(series) if series is not None else None,
                      "avg_day": _average_day(series) if series is not None else {}, "devices": own}
    load = None
    if "optimized" in net and devices is not None:  # the home's own load: the same in every case
        own = [c for c in devices.columns if c.startswith("optimized.") and c.endswith(".net_power")]
        load = _rounded(net["optimized"] + devices[own].sum(axis=1))
    events_path, dr_steps, dr_hours = folder / "dr_event_performance.csv", [], 0.0
    if events_path.exists():  # the steps inside the DR events (the first calendar's), for the summary card
        events = pd.read_csv(events_path, parse_dates=["start", "end"])
        events = events[events["replicate"] == 0]
        inside = np.zeros(len(net.index), dtype=bool)
        for start, end in zip(events["start"], events["end"], strict=True):
            inside |= (net.index >= start) & (net.index < end)
        dr_steps, dr_hours = np.flatnonzero(inside).tolist(), float(events["hours"].sum())
    page.extra["cases"] = {"cases": data, "compare": compare, "load": load, "dr_steps": dr_steps,
                           "dr_hours": dr_hours, "dr_events": len(events) if dr_steps else 0,
                           "colors": [A_COLOR, B_COLOR, DIFF_COLOR],
                           "device_colors": {d: _color(d, kinds) for d in kinds}, "charts": {}}
    labels = {n: c["label"] for n, c in cases.items()}
    page.banner = (f'<div class="pairbar">{_picker(labels, compare.get("buttons") or [])}'
                   f'<p class="note sign">{SIGN}</p></div>')
    page.scripts.append(JS)
    return True


def summary_cards(page: Page, section: Section) -> None:
    page.html(section, '<div class="cards case-cards"></div><p class="note case-note"></p>')


def _chart(page: Page, section: Section, key: str, layout: dict, height: int, **kwargs) -> None:
    page.extra["cases"]["charts"][key] = page.next_chart_id()
    page.chart(section, [], layout, height=height, **kwargs)


def value(page: Page) -> None:
    section = page.section("value", "Value breakdown")
    page.text(section, "Cost by value stream over the run (cost +, value −), A and B, and the difference.")
    _chart(page, section, "streams", {"barmode": "group", "yaxis": {"title": "$ over the run"},
                                      "hovermode": "closest"}, 320)


def dispatch(page: Page, window) -> None:
    section = page.section("dispatch", "Dispatch")
    page.text(section, "Power at the meter, kW: import positive, export negative. The home's load, A and B; the "
                       "difference is in the legend. Zoom with the buttons or the slider; every time chart on the page "
                       "follows.")
    _chart(page, section, "meter", {"yaxis": {"title": "kW"}}, 420, zoom=True, window=window)
    page.text(section, "Each device's power, kW (generation and discharge positive), and stored energy, kWh (right "
                       "axis): B solid, A dotted. A device a case doesn't have has no line for it.")
    _chart(page, section, "devices", {"yaxis": {"title": "kW"}, "yaxis2": {"title": "kWh", "overlaying": "y",
                                                                            "side": "right", "showgrid": False}},
           420, zoom=True, window=window)
    months = len(next(iter(page.extra["cases"]["cases"].values()))["avg_day"]) or 1
    page.text(section, "The average day in each month: net load of A and B by hour, kW.")
    _chart(page, section, "day", {}, 190 * ((months + 3) // 4) + 50)


def energy(page: Page) -> None:
    section = page.section("energy", "Energy by month")
    page.text(section, "Import (up) and export (down), kWh, A and B side by side.")
    _chart(page, section, "energy", {"barmode": "relative", "xaxis": {"type": "category"},
                                     "yaxis": {"title": "kWh (export negative)"}, "hovermode": "x unified"}, 320)


def bills(page: Page) -> None:
    if not any(c.get("bill") for c in page.extra["cases"]["cases"].values()):
        return  # no billing section
    section = page.section("bills", "Bills")
    page.text(section, "Amount due each billing cycle, A and B, and the difference.")
    _chart(page, section, "bills", {"barmode": "group", "xaxis": {"type": "category"}, "yaxis": {"title": "$"},
                                    "hovermode": "x unified"}, 320)
    page.html(section, '<div class="table-wrap case-bill-table"></div>')


def fixed(page: Page, section: Section, what: str) -> None:
    """A section the A/B choice doesn't change says so, and what it compares instead."""
    page.html(section, f'<p class="note fixed">Not set by A and B: {html.escape(what)}</p>')


JS = r"""
(function(){
const all=JSON.parse(document.getElementById("report-data").textContent);const X=all.extra&&all.extra.cases;
if(!X)return;const cases=X.cases,cmp=X.compare,ids=X.charts||{},[CA,CB,CD]=X.colors,dev=X.device_colors||{};
let pair=[cmp.without,cmp.with];
const fmt=(v,d)=>Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:d,maximumFractionDigits:d});
const bad=v=>v==null||isNaN(v);
const money=(v,d=2)=>bad(v)?"–":(v<0?"−$":"$")+fmt(v,d);
const smoney=(v,d=2)=>bad(v)?"–":(v>0.005?"+$":v<-0.005?"−$":"$")+fmt(v,d);
const num=(v,d=0,u="")=>bad(v)?"–":(v<0?"−":"")+fmt(v,d)+u;
const snum=(v,d=0,u="")=>bad(v)?"–":(v>0?"+":v<0?"−":"")+fmt(v,d)+u;
const diff=(x,y)=>bad(x)||bad(y)?null:x-y;
const esc=t=>String(t).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function card(label,value,note,tone){return `<div class="card ${tone||""}"><div class="label">${label}</div>`+
`<div class="value">${esc(value)}</div>`+(note?`<div class="sub">${note}</div>`:"")+"</div>";}
const tone=v=>v>0.005?"good":v<-0.005?"bad":"";
function cards(A,B){const out=[];
if(A.bill&&B.bill){const d=A.bill.net_cost-B.bill.net_cost;out.push(card("Bill",smoney(d),
`A ${money(A.bill.net_cost)} · B ${money(B.bill.net_cost)}`,tone(d)));}
const t=A.costs.total-B.costs.total;out.push(card("Total cost",smoney(t),
`A ${money(A.costs.total)} · B ${money(B.costs.total)}, with degradation`,tone(t)));
const ea=A.energy,eb=B.energy;
out.push(card("Import",snum(ea.import_kwh-eb.import_kwh,0," kWh"),`A ${num(ea.import_kwh)} · B ${num(eb.import_kwh)} kWh`));
out.push(card("Export",snum(ea.export_kwh-eb.export_kwh,0," kWh"),`A ${num(ea.export_kwh)} · B ${num(eb.export_kwh)} kWh`));
const steps=X.dr_steps||[];
if(steps.length&&A.net&&B.net){const mean=C=>steps.reduce((s,i)=>s+(C.net[i]||0),0)/steps.length;const ma=mean(A),mb=mean(B);
out.push(card("During DR events",snum(ma-mb,2," kW"),`mean net load: A ${num(ma,2)} · B ${num(mb,2)} kW, over `+
`${X.dr_events} events (${num(X.dr_hours,0)} h)`,tone(ma-mb)));}
else out.push(card("Peak import",snum(ea.peak_import_kw-eb.peak_import_kw,2," kW"),
`A ${num(ea.peak_import_kw,2)} · B ${num(eb.peak_import_kw,2)} kW`));
[[A,"A"],[B,"B"]].forEach(([C,s])=>{if(C.dr_incentive!=null)out.push(card(`DR incentive earned by ${s}`,
money(C.dr_incentive),"at settlement; not in the costs or bill above",""));});
return out.join("");}
function note(a,b){const A=cases[a],B=cases[b];if(a===b)return "A and B are the same case: pick two different ones.";
let text=`A: ${A.label}, ${A.description}. B: ${B.label}, ${B.description}.`;
const ct=cmp.cost_tests;if(ct&&ct[0]===a&&ct[1]===b)text+=" This is the comparison the cost tests measure, so these "+
"cards describe the same change as TRC and RIM.";
if(A.tariff&&B.tariff&&A.tariff!==B.tariff)text+=` A and B are billed on different tariffs (${A.tariff}; ${B.tariff}).`;
return text;}
function base(id){const c=all.charts.find(c=>c.id===id);return JSON.parse(JSON.stringify(c?c.layout:{}));}
function react(id,traces,layout,keepRange){const gd=document.getElementById(id);if(!gd||!window.Plotly)return;
if(keepRange&&gd.layout&&gd.layout.xaxis&&gd.layout.xaxis.range&&!gd.layout.xaxis.autorange){
layout.xaxis=Object.assign({},layout.xaxis,{range:gd.layout.xaxis.range.slice(),autorange:false});}
Plotly.react(gd,traces,themed(layout),{responsive:true,displaylogo:false});}
const ink=()=>getComputedStyle(document.documentElement).getPropertyValue("--ink").trim();
let axis=null;const time=()=>axis||(axis=axisValues(all.axes.t));
const hov=u=>`%{y:,.2f} ${u}`;
function streams(A,B){if(!ids.streams)return;const keys=[...new Set([...Object.keys(A.costs),...Object.keys(B.costs)])]
.filter(k=>k!=="total");const ya=keys.map(k=>A.costs[k]||0),yb=keys.map(k=>B.costs[k]||0);
react(ids.streams,[{type:"bar",name:"A: "+A.label,x:keys,y:ya,marker:{color:CA},hovertemplate:"$%{y:,.2f}"},
{type:"bar",name:"B: "+B.label,x:keys,y:yb,marker:{color:CB},hovertemplate:"$%{y:,.2f}"},
{type:"scatter",mode:"markers",name:"difference",x:keys,y:ya.map((v,i)=>v-yb[i]),marker:{color:CD,size:11,symbol:"diamond"},
hovertemplate:"$%{y:,.2f}"}],base(ids.streams));}
function meter(A,B){if(!ids.meter)return;const t=time(),tr=[];
if(X.load)tr.push({type:"scatter",mode:"lines",name:"home load",x:t,y:X.load,line:{color:ink(),width:1},hovertemplate:hov("kW")});
tr.push({type:"scatter",mode:"lines",name:"A: "+A.label,x:t,y:A.net,line:{color:CA,width:1.2,dash:"dot"},hovertemplate:hov("kW")});
tr.push({type:"scatter",mode:"lines",name:"B: "+B.label,x:t,y:B.net,line:{color:CB,width:1.5},hovertemplate:hov("kW")});
tr.push({type:"scatter",mode:"lines",name:"difference",x:t,y:A.net.map((v,i)=>diff(v,B.net[i])),visible:"legendonly",
line:{color:CD,width:1.2},hovertemplate:hov("kW")});react(ids.meter,tr,base(ids.meter),true);}
function devices(A,B){if(!ids.devices)return;const t=time(),tr=[];
const names=[...new Set([...Object.keys(B.devices),...Object.keys(A.devices)])];
for(const d of names){const c=dev[d]||"#64748b";
for(const [C,s,dash,w] of [[B,"B","solid",1.4],[A,"A","dot",1.1]]){const own=C.devices[d];if(!own)continue;
if(own.kw)tr.push({type:"scatter",mode:"lines",name:`${d} kW, ${s}`,x:t,y:own.kw,line:{color:c,width:w,dash},
hovertemplate:hov("kW")});
if(own.kwh)tr.push({type:"scatter",mode:"lines",name:`${d} kWh, ${s}`,x:t,y:own.kwh,yaxis:"y2",
line:{color:c,width:1,dash:s==="B"?"dash":"dot"},visible:s==="B"?true:"legendonly",hovertemplate:hov("kWh")});}}
react(ids.devices,tr,base(ids.devices),true);}
function day(A,B){if(!ids.day)return;const months=Object.keys(A.avg_day),traces=[],hours=[...Array(24).keys()];
const layout={grid:{rows:Math.ceil(months.length/4),columns:Math.min(4,months.length),pattern:"independent"},
hovermode:"x unified",annotations:[],margin:{l:40,r:10,t:50,b:30}};
months.forEach((m,i)=>{const s=i?String(i+1):"";
traces.push({type:"scatter",mode:"lines",name:"A: "+A.label,x:hours,y:A.avg_day[m],xaxis:"x"+s,yaxis:"y"+s,
line:{color:CA,dash:"dot"},legendgroup:"a",showlegend:i===0,hovertemplate:hov("kW")});
traces.push({type:"scatter",mode:"lines",name:"B: "+B.label,x:hours,y:B.avg_day[m]||[],xaxis:"x"+s,yaxis:"y"+s,
line:{color:CB},legendgroup:"b",showlegend:i===0,hovertemplate:hov("kW")});
layout["xaxis"+s]={dtick:6,range:[0,23]};layout["yaxis"+s]=i?{matches:"y"}:{};
layout.annotations.push({text:m,xref:"x"+s+" domain",yref:"y"+s+" domain",x:0.02,y:1,showarrow:false,
xanchor:"left",yanchor:"bottom",font:{size:11}});});react(ids.day,traces,layout);}
function energy(A,B){if(!ids.energy)return;const months=A.by_month.map(r=>r.month),tr=[];
for(const [C,s,c] of [[A,"A",CA],[B,"B",CB]]){const get=(m,k)=>{const r=C.by_month.find(r=>r.month===m);return r?r[k]:null;};
tr.push({type:"bar",name:`import, ${s}`,x:months,y:months.map(m=>get(m,"import_kwh")),marker:{color:c},offsetgroup:s,
hovertemplate:hov("kWh")});
tr.push({type:"bar",name:`export, ${s}`,x:months,y:months.map(m=>-get(m,"export_kwh")),marker:{color:c,opacity:0.5},
offsetgroup:s,hovertemplate:hov("kWh")});}react(ids.energy,tr,base(ids.energy));}
function bills(A,B){if(ids.bills&&A.bill&&B.bill){const months=A.by_month.map(r=>r.month);
const get=(C,m)=>{const r=C.by_month.find(r=>r.month===m);return r&&r.bill!=null?r.bill:null;};
const ya=months.map(m=>get(A,m)),yb=months.map(m=>get(B,m));
react(ids.bills,[{type:"bar",name:"A: "+A.label,x:months,y:ya,marker:{color:CA},hovertemplate:"$%{y:,.2f}"},
{type:"bar",name:"B: "+B.label,x:months,y:yb,marker:{color:CB},hovertemplate:"$%{y:,.2f}"},
{type:"scatter",mode:"lines+markers",name:"difference",x:months,y:ya.map((v,i)=>diff(v,yb[i])),line:{color:CD},
hovertemplate:"$%{y:,.2f}"}],base(ids.bills));}
const box=document.querySelector(".case-bill-table");if(!box)return;
if(!A.bill||!B.bill){box.innerHTML='<p class="note">No bill: the scenario has no billing section.</p>';return;}
const flat=b=>{const o={};for(const [k,v] of Object.entries(b)){if(v&&typeof v==="object"){for(const [k2,v2] of
Object.entries(v))if(typeof v2==="number")o[`${k.replace(/_/g," ")}: ${k2}`]=v2;}else if(typeof v==="number")
o[k.replace(/_/g," ")]=v;}return o;};
const fa=flat(A.bill),fb=flat(B.bill),keys=[...new Set([...Object.keys(fa),...Object.keys(fb)])];
const cell=(k,v,s)=>bad(v)?"":(k.includes("kwh")?(s?snum(v,1," kWh"):num(v,1," kWh")):(s?smoney(v):money(v)));
box.innerHTML='<table><thead><tr><th></th><th class="num">A: '+esc(A.label)+'</th><th class="num">B: '+esc(B.label)+
'</th><th class="num">Difference</th></tr></thead><tbody>'+keys.map(k=>`<tr><td>${esc(k)}</td><td class="num">${cell(k,fa[k])}</td>`+
`<td class="num">${cell(k,fb[k])}</td><td class="num">${cell(k,diff(fa[k],fb[k]),true)}</td></tr>`).join("")+
"</tbody></table>"+(A.tariff?`<p class="note">A on ${esc(A.tariff)}${A.program?", "+esc(A.program):""}; B on `+
`${esc(B.tariff)}${B.program?", "+esc(B.program):""}.</p>`:"");}
function show(){const [a,b]=pair,A=cases[a],B=cases[b];
document.querySelectorAll(".case-pick").forEach(s=>{s.value=pair[+s.dataset.side];});
document.querySelectorAll(".quick button").forEach(q=>q.classList.toggle("on",q.dataset.without===a&&q.dataset.with===b));
document.querySelectorAll(".case-cards").forEach(c=>{c.innerHTML=cards(A,B);});
document.querySelectorAll(".case-note").forEach(n=>{n.textContent=note(a,b);});
streams(A,B);meter(A,B);devices(A,B);day(A,B);energy(A,B);bills(A,B);}
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
              + f'<p class="note sign">Differences are A − B: positive means a premise\'s {basis} is lower in B.</p>'
                '<div class="cards case-cards"></div>')
    page.chart(section, [], {"hovermode": "closest", "showlegend": False, "xaxis": {"type": "category"},
                             "yaxis": {"title": f"{basis} difference, $"}}, height=320)
    page.text(section, f"One bar per premise: its {basis} difference, largest first. A premise without one of the "
                       "two cases (no measure to remove, say) has no bar.")
    page.scripts.append(STUDY_JS)


STUDY_JS = r"""
(function(){
const all=JSON.parse(document.getElementById("report-data").textContent);const S=all.extra&&all.extra.study_cases;
if(!S)return;let pair=S.pair.slice();
const money=v=>v==null||isNaN(v)?"–":(v>0.005?"+$":v<-0.005?"−$":"$")+Math.abs(v).toLocaleString(undefined,{minimumFractionDigits:2,
maximumFractionDigits:2});
const card=(l,v,n,t)=>`<div class="card ${t||""}"><div class="label">${l}</div><div class="value">${v}</div>`+
(n?`<div class="sub">${n}</div>`:"")+"</div>";
function show(){const [a,b]=pair;document.querySelectorAll(".case-pick").forEach(s=>{s.value=pair[+s.dataset.side];});
document.querySelectorAll(".quick button").forEach(q=>q.classList.toggle("on",q.dataset.without===a&&q.dataset.with===b));
const rows=S.premises.map((p,i)=>({p,v:S.values[a][i]==null||S.values[b][i]==null?null:S.values[a][i]-S.values[b][i]}))
.filter(r=>r.v!=null).sort((x,y)=>y.v-x.v);const total=rows.reduce((s,r)=>s+r.v,0);
const saving=rows.filter(r=>r.v>0.005).length;
document.querySelectorAll(".case-cards").forEach(c=>{c.innerHTML=card(`Total ${S.basis} difference`,money(total),
`A ${S.labels[a]} · B ${S.labels[b]}, ${rows.length} premises`,total>0.005?"good":total<-0.005?"bad":"")+
card("Premises lower in B",`${saving} of ${rows.length}`,"")+
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
