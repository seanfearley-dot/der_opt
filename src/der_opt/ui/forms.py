"""The app's forms (docs/REPORTS.md section 3): a new scenario or study from a template
or from library blocks, and a study's defaults, overrides and add_devices."""

from __future__ import annotations

import json
from pathlib import Path

from der_opt.ui.project import Project
from der_opt.ui.views import esc, layout, q

# the options each template takes (`der-opt new TEMPLATE --help`)
TEMPLATE_FIELDS = {
    "dr-battery": ("timeseries", "premise", "tariff", "avoided_costs", "year"),
    "home-bill": ("timeseries", "premise", "tariff", "program", "export_rates", "baseline_territory", "year"),
    "feeder-from-files": ("premises", "devices", "network", "timeseries", "export_rates", "year"),
    "lmp-battery": ("price", "year"),
}
FIELD_LABELS = {
    "timeseries": ("Time series file", "files", "input-spec CSV with load_kw (and pv_kw)"),
    "premise": ("Premise", None, "its premise_id (default: the file's only one)"),
    "tariff": ("Retail tariff", "tariffs", "a tariff YAML"),
    "program": ("Export program", None, "nbt or nem2"),
    "export_rates": ("NBT export rates", "files", "CSV"),
    "baseline_territory": ("Baseline territory", None, "PG&E territory letter, for tariffs with a baseline"),
    "avoided_costs": ("Avoided cost set", "sets", "a folder with manifest.yaml"),
    "price": ("Price file", "files", "CSV from `der-opt fetch-lmp`"),
    "premises": ("premises.csv", "files", ""),
    "devices": ("devices.csv", "files", "input spec 2.0"),
    "network": ("network.csv", "files", ""),
    "year": ("Year", None, "default: the time series file's"),
}


def data_files(project: Project) -> dict[str, list[str]]:
    """Files a form can suggest: CSVs, tariff YAMLs and avoided cost sets."""
    files, tariffs, sets = [], [], []
    for path in project._walk("*", 6):
        relative = str(project.relative(path))
        if path.suffix == ".csv":
            files.append(relative)
        elif path.suffix == ".yaml" and path.parent.name == "tariffs":
            tariffs.append(relative)
        elif path.name == "manifest.yaml":
            sets.append(str(project.relative(path.parent)))
    return {"files": sorted(files), "tariffs": sorted(tariffs), "sets": sorted(sets)}


def _datalists(lists: dict[str, list[str]]) -> str:
    return "".join(f'<datalist id="dl-{name}">' + "".join(f'<option value="{esc(v)}">' for v in values) + "</datalist>"
                   for name, values in lists.items())


# --- new ------------------------------------------------------------------------------


def new_page(project: Project, token: str, library: Path, blocks) -> str:
    from der_opt.scenarios.library import KINDS
    from der_opt.scenarios.templates import TEMPLATES

    lists = data_files(project)
    template_options = "".join(f'<option value="{esc(n)}">{esc(n)}: {esc(d)}</option>' for n, d in TEMPLATES.items())
    fields = []
    for name, (label, listed, hint) in FIELD_LABELS.items():
        uses = " ".join(t for t, f in TEMPLATE_FIELDS.items() if name in f)
        if name == "program":
            control = '<select name="program"><option>nbt</option><option>nem2</option></select>'
        else:
            control = (f'<input name="{name}" class="wide" autocomplete="off"'
                       + (f' list="dl-{listed}"' if listed else "") + ">")
        fields.append(f'<label data-for="{uses}">{esc(label)} {control} <span class="note">{esc(hint)}</span></label>')
    groups = []
    for kind in KINDS:
        options = [b for b in blocks if b.kind == kind.key]
        if not options:
            continue
        kind_input = "checkbox" if kind.many else "radio"
        items = "".join(
            f'<label class="check"><input type="{kind_input}" name="block:{esc(kind.key)}" value="{esc(b.name)}">'
            f'<span><b>{esc(b.stem)}</b> <span class="note">{esc(b.description)}'
            + (" · illustrative values" if b.illustrative else "")
            + (f" · needs {esc(', '.join(n.split('.')[-1] for n in b.needs))}" if b.needs else "")
            + "</span></span></label>" for b in options)
        none = ("" if kind.required or kind.many else
                f'<label class="check"><input type="radio" name="block:{esc(kind.key)}" value="" checked>'
                '<span class="note">none</span></label>')
        groups.append(f'<fieldset><legend>{esc(kind.title)}{" (required)" if kind.required else ""}</legend>'
                      f"{none}{items}</fieldset>")
    body = f"""<div class="crumbs"><a href="/">Scenarios and runs</a> / new</div><h1>New scenario or study</h1>
{_datalists(lists)}
<div class="tabs" data-tabs><button data-tab="template" class="on">From a template</button>
<button data-tab="blocks">From library blocks</button></div>
<section data-pane="template">
<p class="note">A commented starting file, as <code>der-opt new TEMPLATE</code> writes it: what the files tell it is
filled in, the rest is TODO for the Check tab to list. <b>feeder-from-files</b> writes a study.</p>
<form data-post="/new/template" data-target="#template-result" class="grid">
<label>Template <select name="template" data-switch>{template_options}</select></label>
<label>File to write <input name="target" class="wide" value="scenarios/new_scenario.yaml"> <span class="note">
in the project; relative paths in it are written from its folder</span></label>
{''.join(fields)}
<div class="row"><button type="submit">Write it</button></div></form><div id="template-result"></div></section>
<section data-pane="blocks" hidden>
<p class="note">A single-home scenario that extends the blocks you pick from {esc(library)}. (For a study, start
from the feeder-from-files template; study blocks go in its <code>extends</code>.) The check below says whether
the picks fit together.</p>
<form data-post="/new/blocks" data-target="#blocks-result" data-check="/fragment/blocks-check" class="grid">
<label>File to write <input name="target" class="wide" value="scenarios/new_home.yaml"></label>
<label>Time series file <input name="timeseries" class="wide" list="dl-files" autocomplete="off">
<span class="note">optional: load and solar come from it</span></label>
<label>Premise <input name="premise"> <span class="note">its premise_id in that file</span></label>
{''.join(groups)}
<div id="blocks-check" class="check-box"></div>
<div class="row"><button type="submit">Write it</button></div></form><div id="blocks-result"></div></section>"""
    return layout(project, "New", body, token, "new", extra=NEW_JS)


NEW_JS = """<script>
(function(){const sel=document.querySelector("select[data-switch]");
function sync(){const t=sel.value;document.querySelectorAll("label[data-for]").forEach(l=>{
l.hidden=!l.dataset.for.split(" ").includes(t);});}
if(sel){sel.addEventListener("change",sync);sync();}
const form=document.querySelector("form[data-check]");if(!form)return;let timer;
const check=async()=>{const body=new URLSearchParams(new FormData(form));
const r=await fetch(form.dataset.check+"?"+body);document.getElementById("blocks-check").innerHTML=await r.text();};
form.addEventListener("change",()=>{clearTimeout(timer);timer=setTimeout(check,150);});check();})();
</script>"""


def blocks_check(blocks, picks_names: list[str], timeseries: str, premise: str, project: Project) -> str:
    from der_opt.scenarios.library import find, home_data, problems

    picks = [find(blocks, n) for n in picks_names]
    try:
        home = home_data(project.inside(timeseries) if timeseries else None, premise or None)
    except Exception as exc:  # noqa: BLE001 -- shown as it is
        return f'<p class="status bad">The time series file doesn\'t read: {esc(exc)}</p>'
    found = problems(picks, home)
    if found:
        return '<p class="status bad">These picks don\'t fit together yet:</p><ul>' + "".join(
            f"<li>{esc(p)}</li>" for p in found) + "</ul>"
    return f'<p class="status ok">{len(picks)} blocks that fit together.</p>'


# --- a study's settings --------------------------------------------------------------


def study_settings_fragment(relative: str, state: dict) -> str:
    inherited = "".join(f"<li><code>{esc(d)}</code></li>" for d in state["inherited"])
    lists = {"where": state["suggest"]["where"], "premise": state["suggest"]["premise"],
             "device": state["suggest"]["device"], "targets": state["suggest"]["targets"],
             "add-device": state["suggest"]["add_device"]}
    payload = json.dumps({"path": relative, **{k: state[k] for k in ("defaults", "overrides", "add_devices")},
                          "types": state["suggest"]["types"], "templates": state["suggest"]["templates"]})
    return f"""{_datalists(lists)}
<p class="note">Each entry as rows of a setting and its value. A value is YAML: <code>0.9</code>,
<code>[solar]</code>, <code>{{type: self_consumption}}</code>; a list in a <code>where</code> means any of. Saving rewrites
these three sections of the file (comments above them stay; comments inside them don't) and checks the study.</p>
{f'<details><summary>{len(state["inherited"])} defaults inherited from the blocks this study extends (they apply first)'
 f'</summary><ul>{inherited}</ul></details>' if state["inherited"] else ''}
<div id="study-form" data-state="{esc(payload)}"></div>
<div class="row"><button type="button" id="study-save">Save and check</button>
<span class="note">⌘S / Ctrl+S in the Edit tab saves the YAML instead.</span></div>
<div id="study-result"></div>"""


STUDY_JS = r"""<script>
(function(){
let root=null,state=null;
function el(tag,attrs,...kids){const e=document.createElement(tag);for(const[k,v]of Object.entries(attrs||{})){
if(k==="on")for(const[ev,fn]of Object.entries(v))e.addEventListener(ev,fn);else if(v!==undefined&&v!==null)e.setAttribute(k,v);}
for(const k of kids)e.append(k);return e;}
function rows(list,datalist,label,onchange){const box=el("div",{class:"kv"});
const add=(k,v)=>{const key=el("input",{class:"key",list:"dl-"+datalist,placeholder:"setting",value:k||""});
const val=el("input",{class:"val code",placeholder:"value (YAML)",value:v||""});
[key,val].forEach(i=>i.addEventListener("input",()=>onchange&&onchange()));
const row=el("div",{class:"kvrow"},key,val,el("button",{type:"button",class:"ghost small",title:"remove",
on:{click:()=>{row.remove();onchange&&onchange();}}},"×"));box.insertBefore(row,more);};
const more=el("button",{type:"button",class:"ghost small",on:{click:()=>add("","")}},"+ "+label);
box.append(more);(list||[]).forEach(([k,v])=>add(k,v));if(!(list||[]).length)add("","");return box;}
function read(box){return[...box.querySelectorAll(".kvrow")].map(r=>[r.querySelector(".key").value,r.querySelector(".val").value]);}
function flowOf(box){const parts=read(box).filter(([k,v])=>k.trim()&&v.trim()).map(([k,v])=>k.trim()+": "+v.trim());
return "{"+parts.join(", ")+"}";}
function preview(box,out){let t;return()=>{clearTimeout(t);t=setTimeout(async()=>{const w=flowOf(box);
if(w==="{}"){out.innerHTML='<span class="note">no where: every premise</span>';return;}
const r=await fetch("/fragment/where?compact=1&path="+encodeURIComponent(state.path)+"&where="+encodeURIComponent(w));
out.innerHTML=await r.text();},250);};}
function card(kind,i,entry,list){const head=el("div",{class:"cardhead"},el("b",{},kind+"["+i+"]"));
const c=el("div",{class:"entry"},head);
const move=(d)=>{const j=i+d;if(j<0||j>=list.length)return;collect();const l=state[kind];[l[i],l[j]]=[l[j],l[i]];render();};
head.append(el("span",{class:"grow"}),el("button",{type:"button",class:"ghost small",on:{click:()=>move(-1)}},"↑"),
el("button",{type:"button",class:"ghost small",on:{click:()=>move(1)}},"↓"),
el("button",{type:"button",class:"ghost small",on:{click:()=>{collect();state[kind].splice(i,1);render();}}},"remove"));
return c;}
function render(){root.innerHTML="";
const sec=(title,kind,make,blank,help)=>{const s=el("div",{class:"group"},el("h3",{},title),el("p",{class:"note"},help));
state[kind].forEach((e,i)=>s.append(make(e,i)));
s.append(el("button",{type:"button",class:"ghost",on:{click:()=>{collect();state[kind].push(blank());render();}}},"+ add an entry"));
root.append(s);};
sec("Defaults","defaults",(e,i)=>{const c=card("defaults",i,e,state.defaults);const out=el("div",{class:"pick"});
const w=rows(e.where,"where","where key",null);const p=preview(w,out);w.addEventListener("input",p);
c.append(el("div",{class:"label"},"where (none: every premise)"),w,out,el("div",{class:"label"},"premise settings"),
rows(e.premise,"premise","premise setting"),el("div",{class:"label"},"device settings"),rows(e.device,"device","device setting"));
c._parts={where:w,premise:c.querySelectorAll(".kv")[1],device:c.querySelectorAll(".kv")[2]};p();return c;},
()=>({where:[],premise:[],device:[]}),"Applied in order to every premise (and device) their where picks; a later entry wins. The population's own rows beat them.");
sec("Overrides","overrides",(e,i)=>{const c=card("overrides",i,e,state.overrides);
const t=el("input",{class:"wide",list:"dl-targets",placeholder:"premise, or premise/device",value:e.target||""});
const r=rows(e.settings,"premise","setting");t.addEventListener("input",()=>{r.querySelectorAll(".key").forEach(k=>
k.setAttribute("list",t.value.includes("/")?"dl-device":"dl-premise"));});t.dispatchEvent(new Event("input"));
c.append(el("div",{class:"label"},"premise or premise/device"),t,el("div",{class:"label"},"settings"),r);
c._parts={target:t,settings:r};return c;},()=>({target:"",settings:[]}),"One premise or one device; beats everything else.");
sec("Add devices","add_devices",(e,i)=>{const c=card("add_devices",i,e,state.add_devices);const out=el("div",{class:"pick"});
const w=rows(e.where,"where","where key",null);const p=preview(w,out);w.addEventListener("input",p);
const share=el("input",{class:"short",value:e.share||"",placeholder:"0.3"}),seed=el("input",{class:"short",value:e.seed||"0"});
c.append(el("div",{class:"label"},"where (none: every premise)"),w,out,el("div",{class:"row"},el("label",{},"share ",share),
el("label",{},"seed ",seed)),el("div",{class:"label"},"the device (device_id and type are needed)"),rows(e.device,"add-device","device field"));
c._parts={where:w,share,seed,device:c.querySelectorAll(".kv")[1]};p();return c;},
()=>({where:[],share:"0.3",seed:"1",device:[["device_id","battery"],["type","battery"],["template",""],["kw",""],["kwh",""]]}),
"Adoption what-ifs: share of the premises where picks that don't have this device_id (metered ones aside) get it, chosen at random from seed.");
}
function collect(){const pick=(kind,fn)=>{const cards=[...root.querySelectorAll(".entry")].filter(c=>c.querySelector(".cardhead b").textContent.startsWith(kind+"["));
state[kind]=cards.map(fn);};
pick("defaults",c=>({where:read(c._parts.where),premise:read(c._parts.premise),device:read(c._parts.device)}));
pick("overrides",c=>({target:c._parts.target.value,settings:read(c._parts.settings)}));
pick("add_devices",c=>({where:read(c._parts.where),share:c._parts.share.value,seed:c._parts.seed.value,device:read(c._parts.device)}));}
window.initStudyForm=function(){root=document.getElementById("study-form");if(!root||root.dataset.ready)return;root.dataset.ready=1;
state=JSON.parse(root.dataset.state);render();
document.getElementById("study-save").addEventListener("click",async()=>{collect();const out=document.getElementById("study-result");
out.classList.add("busy");const body=new URLSearchParams({path:state.path,form:JSON.stringify({defaults:state.defaults,
overrides:state.overrides,add_devices:state.add_devices})});
const r=await fetch("/study-settings",{method:"POST",headers:{"X-Token":TOKEN},body});out.innerHTML=await r.text();
out.classList.remove("busy");const fresh=out.querySelector("template#newtext");if(fresh){
const ta=document.querySelector("textarea[name=content]");if(ta)ta.value=fresh.content.textContent;}if(window.init)init(out);});};
})();
</script>"""



def file_link(relative: str) -> str:
    return f"/file?path={q(relative)}"
