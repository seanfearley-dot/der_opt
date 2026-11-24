"""The app's forms (docs/REPORTS.md section 3): a new scenario or study from a template
or from library blocks, and a study's defaults, overrides and add_devices."""

from __future__ import annotations

import json

from der_opt.ui.project import Project
from der_opt.ui.views import esc, q


SCOPES = {"time series": "timeseries", "premises": "premises", "devices": "devices", "network": "network",
          "prices": "prices", "export rates": "export_rates", "events": "events", "price signal": "price_signal"}


def data_files(project: Project) -> dict[str, list[str]]:
    """Files a form can suggest: CSVs, tariff YAMLs and avoided cost sets. Each CSV is
    also filed under its kind (its header says: time series, premises, devices,
    network, prices, export rates, events, price signal), so a field offers only its
    own; an avoided cost set's parts aren't offered on their own."""
    from der_opt.inputs.spec import detect_kind

    files, tariffs, sets = [], [], []
    scoped: dict[str, list[str]] = {scope: [] for scope in SCOPES.values()}
    for path in project._walk("*", 6):
        relative = str(project.relative(path))
        if path.suffix == ".csv":
            files.append(relative)
            scope = SCOPES.get(detect_kind(path))
            if scope:
                scoped[scope].append(relative)
        elif path.suffix == ".yaml" and path.parent.name == "tariffs":
            tariffs.append(relative)
        elif path.name == "manifest.yaml":
            sets.append(str(project.relative(path.parent)))
    return {"files": sorted(files), "tariffs": sorted(tariffs), "sets": sorted(sets),
            **{scope: sorted(values) for scope, values in scoped.items()}}


def _datalists(lists: dict[str, list[str]]) -> str:
    return "".join(f'<datalist id="dl-{name}">' + "".join(f'<option value="{esc(v)}">' for v in values) + "</datalist>"
                   for name, values in lists.items())


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
