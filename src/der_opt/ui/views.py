"""The app's pages and the fragments they load (docs/REPORTS.md section 3). Pages are
server-rendered HTML; a few lines of script load fragments and post forms."""

from __future__ import annotations

import html
import time
from datetime import datetime
from pathlib import Path

import pandas as pd
import yaml

from der_opt.reports.page import CSS, cell
from der_opt.ui.jobs import Job
from der_opt.ui.project import Project


def esc(value) -> str:
    return html.escape(str(value))


def q(value) -> str:
    from urllib.parse import quote

    return quote(str(value))


def table(frame: pd.DataFrame, links: dict | None = None, filter: bool = False, empty: str = "Nothing here.") -> str:
    """A sortable table; `links` maps a column to a function of the row giving an href."""
    if frame is None or frame.empty:
        return f'<p class="note">{esc(empty)}</p>'
    links = links or {}
    head = "".join(f"<th>{esc(c)}</th>" for c in frame.columns)
    rows = []
    for _, row in frame.iterrows():
        cells = []
        for column in frame.columns:
            value = row[column]
            numeric = isinstance(value, (int, float)) and not isinstance(value, bool) and not pd.isna(value)
            text = esc(cell(value))
            href = links[column](row) if column in links else None
            if href:
                text = f'<a href="{esc(href)}">{text}</a>'
            sort = f' data-sort="{value}" class="num"' if numeric else ""
            cells.append(f"<td{sort}>{text}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    search = '<input class="filter" type="search" placeholder="Filter rows">' if filter else ""
    return (f'<div class="table-wrap">{search}<table class="sortable"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(rows)}</tbody></table></div>')


def _when(stamp: float | str | None) -> str:
    if not stamp:
        return ""
    if isinstance(stamp, str):
        try:
            stamp = datetime.fromisoformat(stamp).timestamp()
        except ValueError:
            return stamp
    seconds = time.time() - stamp
    if seconds < 90:
        return "just now"
    if seconds < 3600:
        return f"{seconds / 60:.0f} min ago"
    if seconds < 86400:
        return f"{seconds / 3600:.0f} h ago"
    return datetime.fromtimestamp(stamp).strftime("%Y-%m-%d %H:%M")


def layout(project: Project, title: str, body: str, token: str, active: str = "") -> str:
    nav = "".join(f'<a href="{h}" class="{"on" if active == k else ""}">{t}</a>'
                  for k, h, t in (("home", "/", "Scenarios and runs"), ("jobs", "/jobs", "Run queue")))
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="token" content="{esc(token)}">
<title>{esc(title)} · der-opt</title><style>{CSS}{APP_CSS}</style></head>
<body><header class="bar"><a class="brand" href="/">der-opt</a><nav class="top">{nav}</nav>
<span class="where" title="the project folder">{esc(project.root)}</span></header>
<main class="page">{body}</main><script>{APP_JS}</script></body></html>"""


# --- home ----------------------------------------------------------------------------


def home(project: Project, jobs: list[Job], token: str) -> str:
    files = project.scenario_files()
    runs = project.runs()
    run_names = {str(r.path) for r in runs}
    rows = pd.DataFrame([{"file": str(f.path), "kind": f.kind, "name": f.name, "edited": _when(f.modified),
                          "last run": str(project.default_output(f.path))
                          if str(project.default_output(f.path)) in run_names else ""} for f in files])
    file_links = {"file": lambda r: f"/file?path={q(r['file'])}",
                  "last run": lambda r: f"/files/{r['last run']}/report.html" if r["last run"] else None}
    run_rows = pd.DataFrame([{"run": str(r.path), "kind": r.kind, "name": r.name, "when": _when(r.created),
                              "errors": r.errors, "warnings": r.warnings} for r in runs])
    run_links = {"run": lambda r: f"/files/{r['run']}/report.html"}
    active = [j for j in jobs if j.status in ("queued", "running")]
    queue_note = (f'<p class="note"><a href="/jobs">{len(active)} run(s) queued or running</a></p>' if active else "")
    body = f"""
<h1>Scenarios and runs</h1>{queue_note}
<section><h2>Scenario and study files</h2>
<p class="note">Every YAML file in the project with a scenario's or study's sections. Open one to edit it, check
it, browse its population, or run it.</p>{table(rows, file_links, filter=True, empty="No scenario files found.")}</section>
<section><h2>Runs</h2><p class="note">Output folders under out/ with a summary.json; each opens its report.</p>
{table(run_rows, run_links, filter=True, empty="No runs under out/ yet.")}</section>"""
    return layout(project, "Home", body, token, "home")


# --- a scenario or study file --------------------------------------------------------


def file_page(project: Project, relative: str, token: str) -> str:
    path = project.inside(relative)
    text = path.read_text(encoding="utf-8")
    output = project.default_output(relative)
    report = project.inside(output) / "report.html"
    last = (f'<a class="button ghost" href="/files/{q(output)}/report.html">Last run\'s report</a>'
            if report.exists() else "")
    body = f"""
<div class="crumbs"><a href="/">Scenarios and runs</a> / {esc(relative)}</div>
<h1>{esc(Path(relative).name)}</h1>
<div class="tabs" data-tabs>
  <button data-tab="edit" class="on">Edit</button><button data-tab="check">Check</button>
  <button data-tab="population">Population</button><button data-tab="merged">Merged</button>
  <button data-tab="run">Run</button>{last}
</div>
<section data-pane="edit">
  <form data-post="/save" data-target="#saved">
    <input type="hidden" name="path" value="{esc(relative)}">
    <textarea name="content" class="code" spellcheck="false">{esc(text)}</textarea>
    <div class="row"><button type="submit">Save and check</button>
    <span class="note">⌘S / Ctrl+S saves. Paths are relative to this file's folder.</span></div>
  </form>
  <div id="saved"></div>
</section>
<section data-pane="check" hidden><div data-lazy="/fragment/check?path={q(relative)}"></div></section>
<section data-pane="population" hidden><div data-lazy="/fragment/population?path={q(relative)}"></div></section>
<section data-pane="merged" hidden><div data-lazy="/fragment/merged?path={q(relative)}"></div></section>
<section data-pane="run" hidden>
  <form data-post="/run">
    <input type="hidden" name="path" value="{esc(relative)}">
    <label>Output folder <input name="output" value="{esc(output)}" class="wide"></label>
    <label class="check"><input type="checkbox" name="no_counterfactual"> skip the counterfactual run</label>
    <label class="check"><input type="checkbox" name="cost_tests"> run the cost tests (OpenBCA)</label>
    <label class="check"><input type="checkbox" name="keep_openbca"> keep OpenBCA's working files</label>
    <div class="row"><button type="submit">Add to the run queue</button>
    <span class="note">Runs one at a time, as <code>der-opt run</code> would; the output folder gets its report.</span></div>
  </form>
</section>"""
    return layout(project, Path(relative).name, body, token)


def findings(report) -> str:
    rows = pd.DataFrame([{"level": f.level, "check": f.check, "premise": f.premise or "", "run": f.run or "",
                          "message": f.message} for f in report.findings if f.level != "info"])
    state = ("ok", "Ready to run.") if not report.errors else ("bad", f"{len(report.errors)} error(s): fix them to run.")
    return (f'<p class="status {state[0]}">{esc(state[1])} {len(report.warnings)} warning(s).</p>'
            + table(rows, filter=len(rows) > 10, empty="Every check is clean."))


def check_fragment(project: Project, relative: str) -> str:
    from der_opt.scenarios.validate import validate_scenario

    started = time.perf_counter()
    result = validate_scenario(project.inside(relative))
    parts = [findings(result.report), f'<p class="note">Checked in {time.perf_counter() - started:.1f} s.</p>']
    if result.composed is not None and result.composed.fragments:
        items = "".join(f"<li>{esc(project.relative(f)) if project.root in Path(f).resolve().parents else esc(f)}</li>"
                        for f in result.composed.fragments)
        parts.append(f"<h3>Extends</h3><ul>{items}</ul>")
    for entry in result.additions or []:
        parts.append(f'<p class="note">{esc(entry["entry"])}: {esc(entry["device_id"])} added at '
                     f'{len(entry["chosen"])} of {entry["eligible"]} eligible premises.</p>')
    return "".join(parts)


def resolved(project: Project, relative: str):
    """The file's resolved premises (a study's, a feeder's, or the one home)."""
    from der_opt.population.compile import (
        resolve_feeder,
        resolve_scenario,
        resolve_study,
    )
    from der_opt.scenarios import load_feeder_config, load_study_config
    from der_opt.scenarios.compose import compose_scenario
    from der_opt.scenarios.loader import load_composed
    from der_opt.scenarios.schema import ScenarioConfig, is_study

    path = project.inside(relative)
    data = compose_scenario(path).data
    if is_study(data):
        return resolve_study(load_study_config(path))[0]
    if "feeder" in data:
        return resolve_feeder(load_feeder_config(path))[0]
    return [resolve_scenario(ScenarioConfig.model_validate(load_composed(path).data))]


def _problem(exc: Exception) -> str:
    problems = getattr(exc, "problems", None) or [f"{type(exc).__name__}: {exc}"]
    return '<p class="status bad">It doesn\'t resolve:</p><ul>' + "".join(f"<li>{esc(p)}</li>" for p in problems) + "</ul>"


def population_fragment(project: Project, relative: str) -> str:
    from der_opt.population.report import parameters_table, premises_table

    try:
        premises = resolved(project, relative)
    except Exception as exc:  # noqa: BLE001 -- shown to the user as it is
        return _problem(exc)
    mix = premises_table(premises)
    return f"""
<h3>Which premises does a selector pick?</h3>
<form data-get="/fragment/where" data-target="#where" class="row">
  <input type="hidden" name="path" value="{esc(relative)}">
  <input name="where" class="wide code" placeholder="{{type: battery, segment: late}}" autocomplete="off">
  <button type="submit">Preview</button>
</form>
<p class="note">A <code>where</code> as written in a study's defaults or add_devices: premise columns and tags
(element_id, kind, customer_class, …), and device keys (type, template, device_id). A list means any of.</p>
<div id="where"></div>
<h3>Premises ({len(premises)})</h3>{table(mix, filter=True)}
<details><summary>Every setting and where it came from</summary>{table(parameters_table(premises), filter=True)}</details>"""


def where_fragment(project: Project, relative: str, text: str) -> str:
    from der_opt.population.compile import matches
    from der_opt.population.portfolio import _row

    try:
        where = yaml.safe_load(text) if text.strip() else {}
    except yaml.YAMLError as exc:
        return f'<p class="status bad">That isn\'t YAML: {esc(exc)}</p>'
    if not isinstance(where, dict):
        return '<p class="status bad">Write a mapping, e.g. {type: battery, element_id: tx_1}.</p>'
    try:
        premises = resolved(project, relative)
    except Exception as exc:  # noqa: BLE001
        return _problem(exc)
    device_keys = {"type", "template", "device_id"} & set(where)
    rows = []
    for r in premises:
        row = _row(r) | {"kind": "premise"}
        if device_keys:
            picked = [d.device_id for d in r.devices
                      if matches(where, r.premise_id, row, {"device_id": d.device_id, "type": d.kind,
                                                            "template": d.template or ""})]
            if picked:
                rows.append({"premise_id": r.premise_id, "element": r.element_id or "", "devices": ", ".join(picked)})
        elif matches(where, r.premise_id, row):
            rows.append({"premise_id": r.premise_id, "element": r.element_id or "",
                         "devices": ", ".join(d.device_id for d in r.devices)})
    what = "devices" if device_keys else "premises"
    count = sum(len(r["devices"].split(", ")) for r in rows) if device_keys else len(rows)
    return (f'<p class="status ok">{count} {what} at {len(rows)} of {len(premises)} premises.</p>'
            + table(pd.DataFrame(rows), filter=len(rows) > 10, empty="It picks nothing."))


def merged_fragment(project: Project, relative: str) -> str:
    from der_opt.scenarios.compose import CompositionError, compose_scenario

    try:
        composed = compose_scenario(project.inside(relative))
    except CompositionError as exc:
        return _problem(exc)
    text = yaml.safe_dump(composed.data, sort_keys=False, allow_unicode=True)
    return ('<p class="note">The file with everything it extends merged in and every path resolved: what runs '
            f'(a run writes it as scenario.resolved.yaml).</p><pre class="code">{esc(text)}</pre>')


# --- the run queue -------------------------------------------------------------------


def jobs_table(jobs: list[Job]) -> str:
    rows = pd.DataFrame([{"#": j.id, "scenario": str(j.scenario), "output": str(j.output), "status": j.status,
                          "time": f"{j.elapsed:.0f} s" if j.elapsed is not None else "",
                          "queued": _when(j.queued_at)} for j in jobs])
    return table(rows, {"#": lambda r: f"/jobs/{r['#']}", "status": lambda r: f"/jobs/{r['#']}"},
                 empty="Nothing has been run from the app since it started.")


def jobs_page(project: Project, jobs: list[Job], token: str) -> str:
    body = f"""<h1>Run queue</h1><p class="note">Runs started here, one at a time. Each writes its output folder and
report as <code>der-opt run</code> does; this list lasts while the app runs, the outputs stay.</p>
<section><div data-load="/fragment/jobs" data-every="2000">{jobs_table(jobs)}</div></section>"""
    return layout(project, "Run queue", body, token, "jobs")


def job_page(project: Project, job: Job, token: str) -> str:
    body = f"""<div class="crumbs"><a href="/jobs">Run queue</a> / #{job.id}</div>
<h1>{esc(job.scenario)}</h1><p class="note"><code>{esc(" ".join(job.command))}</code></p>
<div data-load="/fragment/log?id={job.id}" data-every="1000" data-until="finished">{log_fragment(project, job)}</div>"""
    return layout(project, f"Run #{job.id}", body, token, "jobs")


def log_fragment(project: Project, job: Job) -> str:
    done = job.status in ("done", "failed", "cancelled")
    tone = {"done": "ok", "failed": "bad", "cancelled": "bad"}.get(job.status, "")
    actions = ""
    report = project.inside(job.output) / "report.html"
    if done and report.exists():
        actions = f'<a class="button" href="/files/{q(job.output)}/report.html">Open the report</a>'
    elif not done:
        actions = (f'<form data-post="/cancel" class="inline"><input type="hidden" name="id" value="{job.id}">'
                   '<button type="submit" class="ghost">Cancel</button></form>')
    elapsed = f", {job.elapsed:.0f} s" if job.elapsed is not None else ""
    marker = ' data-finished="1"' if done else ""
    return (f'<div{marker}><p class="status {tone}">{esc(job.status)}{elapsed}</p><div class="row">{actions}</div>'
            f'<pre class="log">{esc(chr(10).join(job.log[-2000:]))}</pre></div>')


APP_CSS = """
header.bar{display:flex;align-items:center;gap:20px;padding:10px 24px;border-bottom:1px solid var(--line);
background:var(--panel);position:sticky;top:0;z-index:5}
.brand{font-weight:700;color:var(--ink);text-decoration:none}
nav.top{display:flex;flex-direction:row;position:static;gap:4px}nav.top a{color:var(--muted);text-decoration:none;padding:5px 10px;border-radius:6px}
nav.top a.on,nav.top a:hover{background:var(--chip);color:var(--ink)}
.where{margin-left:auto;color:var(--muted);font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;
max-width:40vw}
main.page{max-width:1300px;padding:20px 24px}
h1{font-size:22px;margin:4px 0 14px}h3{font-size:14px;margin:16px 0 6px}
.tabs{display:flex;gap:4px;margin-bottom:12px;flex-wrap:wrap}
.tabs button{background:transparent;color:var(--muted);border:1px solid var(--line)}
.tabs button.on{background:var(--chip);color:var(--ink)}
button,.button{font:inherit;padding:6px 14px;border-radius:6px;border:1px solid var(--accent);background:var(--accent);
color:#fff;cursor:pointer;text-decoration:none;display:inline-block}
button.ghost,.button.ghost{background:transparent;color:var(--accent)}
textarea.code{width:100%;min-height:62vh;font:13px/1.45 ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
background:var(--bg);color:var(--ink);border:1px solid var(--line);border-radius:8px;padding:10px;tab-size:2}
input.wide{width:min(520px,100%)}input{font:inherit;padding:5px 8px;border:1px solid var(--line);border-radius:6px;
background:var(--bg);color:var(--ink)}
input.code,code,pre.code{font-family:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}
pre.code,pre.log{background:var(--bg);border:1px solid var(--line);border-radius:8px;padding:10px;overflow:auto;
font-size:12.5px;max-height:70vh}
pre.log{white-space:pre-wrap}
label{display:block;margin:8px 0}label.check{display:flex;gap:8px;align-items:center}
.row{display:flex;gap:12px;align-items:center;margin:10px 0;flex-wrap:wrap}
form.inline{display:inline}
.status{font-weight:600}.status.ok{color:var(--good)}.status.bad{color:var(--bad)}
.busy{opacity:.55}
"""

APP_JS = r"""
const TOKEN=document.querySelector('meta[name=token]').content;
function sortable(root){root.querySelectorAll("table.sortable").forEach(t=>{if(t.dataset.ready)return;t.dataset.ready=1;
t.querySelectorAll("th").forEach((th,i)=>th.addEventListener("click",()=>{const b=t.tBodies[0];const rows=[...b.rows];
const asc=th.dataset.dir!=="asc";t.querySelectorAll("th").forEach(h=>delete h.dataset.dir);th.dataset.dir=asc?"asc":"desc";
const k=r=>{const c=r.cells[i];return c.dataset.sort!==undefined?parseFloat(c.dataset.sort):c.textContent.toLowerCase();};
rows.sort((a,c)=>{const x=k(a),y=k(c);return(x>y?1:x<y?-1:0)*(asc?1:-1);});rows.forEach(r=>b.appendChild(r));}));});
root.querySelectorAll("input.filter").forEach(i=>{if(i.dataset.ready)return;i.dataset.ready=1;
const t=i.parentElement.querySelector("table");i.addEventListener("input",()=>{const s=i.value.toLowerCase();
for(const r of t.tBodies[0].rows)r.style.display=r.textContent.toLowerCase().includes(s)?"":"none";});});}
async function fill(el,url){el.classList.add("busy");try{const r=await fetch(url);el.innerHTML=await r.text();}
catch(e){el.textContent="Couldn't reach the app: "+e;}el.classList.remove("busy");init(el);}
function init(root){sortable(root);
root.querySelectorAll("[data-load]").forEach(el=>{if(el.dataset.ready)return;el.dataset.ready=1;
if(el.dataset.every){const tick=async()=>{await fill(el,el.dataset.load);el.dataset.ready=1;
if(el.dataset.until&&el.querySelector("[data-"+el.dataset.until+"]"))return;setTimeout(tick,+el.dataset.every);};
setTimeout(tick,+el.dataset.every);}});
root.querySelectorAll("form[data-post],form[data-get]").forEach(f=>{if(f.dataset.ready)return;f.dataset.ready=1;
f.addEventListener("submit",async ev=>{ev.preventDefault();const body=new URLSearchParams(new FormData(f));
const target=f.dataset.target?document.querySelector(f.dataset.target):null;if(target)target.classList.add("busy");
const r=f.dataset.get?await fetch(f.dataset.get+"?"+body):await fetch(f.dataset.post,{method:"POST",
headers:{"X-Token":TOKEN},body});const text=await r.text();
if(r.headers.get("X-Location")){location=r.headers.get("X-Location");return;}
if(target){target.innerHTML=text;target.classList.remove("busy");init(target);}});});
document.querySelectorAll("[data-tabs]").forEach(tabs=>{if(tabs.dataset.ready)return;tabs.dataset.ready=1;
tabs.querySelectorAll("button[data-tab]").forEach(b=>b.addEventListener("click",()=>{
tabs.querySelectorAll("button").forEach(x=>x.classList.toggle("on",x===b));
document.querySelectorAll("[data-pane]").forEach(p=>{const on=p.dataset.pane===b.dataset.tab;p.hidden=!on;
const lazy=p.querySelector("[data-lazy]");if(on&&lazy&&(!lazy.dataset.done||b.dataset.tab==="check")){
lazy.dataset.done=1;fill(lazy,lazy.dataset.lazy);}});}));});}
document.addEventListener("keydown",e=>{if((e.metaKey||e.ctrlKey)&&e.key==="s"){const f=document.querySelector(
"form[data-post='/save']");if(f){e.preventDefault();f.requestSubmit();}}});
document.addEventListener("keydown",e=>{const t=e.target;if(t.tagName==="TEXTAREA"&&e.key==="Tab"){e.preventDefault();
const s=t.selectionStart;t.setRangeText("  ",s,t.selectionEnd,"end");}});
init(document);
"""
