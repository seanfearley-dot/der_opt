"""`der-opt ui` (docs/REPORTS.md section 3): the project it shows, its safety checks,
saving and checking a file, the selector preview, and a queued run."""

from __future__ import annotations

import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest

from der_opt.ui.project import OutsideProject, Project
from der_opt.ui.server import App, make_handler

REPO = Path(__file__).parent.parent.parent


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    (root / "scenarios").mkdir(parents=True)
    shutil.copy(REPO / "tests/fixtures/scenario_basic.yaml", root / "scenarios/basic.yaml")
    study = (REPO / "examples/scenarios/feeder_from_files.yaml").read_text(encoding="utf-8")
    study = study.replace("../library/", f"{REPO}/examples/library/").replace("../data/", f"{REPO}/examples/data/")
    (root / "scenarios/files_study.yaml").write_text(study, encoding="utf-8")
    (root / "scenarios/block.yaml").write_text("time_grid: {resolution_minutes: 60}\n", encoding="utf-8")  # not a scenario
    return root


@pytest.fixture
def server(project):
    app = App(project)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(app, 0))
    port = httpd.server_address[1]
    httpd.RequestHandlerClass = make_handler(app, port)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield app, port
    httpd.shutdown()
    httpd.server_close()


def _get(port, path, host=None):
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host or f"localhost:{port}"})
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def _post(port, path, data, token=None):
    headers = {"Host": f"localhost:{port}"} | ({"X-Token": token} if token else {})
    request = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=urllib.parse.urlencode(data).encode(),
                                     headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, response.read().decode("utf-8"), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8"), dict(exc.headers)


def test_the_project_lists_scenarios_and_keeps_paths_inside(project):
    files = {str(f.path): f.kind for f in Project(project).scenario_files()}
    assert files == {"scenarios/basic.yaml": "scenario", "scenarios/files_study.yaml": "study"}
    with pytest.raises(OutsideProject):
        Project(project).inside("../elsewhere.yaml")


def test_requests_must_name_localhost_and_posts_need_the_token(server):
    app, port = server
    assert _get(port, "/", host="attacker.example")[0] == 403
    status, page, _ = _get(port, "/")
    assert status == 200 and "scenarios/files_study.yaml" in page and app.token in page
    assert _post(port, "/save", {"path": "scenarios/basic.yaml", "content": "x"})[0] == 403
    assert _post(port, "/save", {"path": "../outside.yaml", "content": "x"}, app.token)[0] == 403
    assert _get(port, "/file?path=../../etc/hosts")[0] == 403


def test_saving_checks_the_file_and_the_population_previews_a_selector(server, project):
    app, port = server
    path = "scenarios/files_study.yaml"
    assert "Population" in _get(port, f"/file?path={path}")[1]
    text = (project / path).read_text(encoding="utf-8")
    status, body, _ = _post(port, "/save", {"path": path, "content": text.replace("value_per_kw: 40.0",
                                                                                   "value_per_kw: 45.0")}, app.token)
    assert status == 200 and "Saved." in body and "Ready to run." in body
    assert "value_per_kw: 45.0" in (project / path).read_text(encoding="utf-8")
    broken = _post(port, "/save", {"path": path, "content": text.replace("objective", "objectve")}, app.token)[1]
    assert "Saved." in broken  # saved as written; the check says what's wrong
    (project / path).write_text(text, encoding="utf-8")
    assert "Premises (6)" in _get(port, f"/fragment/population?path={path}")[1]
    where = urllib.parse.quote("{type: ev, element_id: tx_1}")
    assert "3 devices at 3 of 6 premises" in _get(port, f"/fragment/where?path={path}&where={where}")[1]
    assert "isn't YAML" in _get(port, f"/fragment/where?path={path}&where=%7Btype:")[1]
    assert "extends" in _get(port, f"/fragment/merged?path={path}")[1] or "time_grid" in _get(
        port, f"/fragment/merged?path={path}")[1]


def test_a_queued_run_writes_its_folder_and_report(server, project):
    app, port = server
    status, _, headers = _post(port, "/run", {"path": "scenarios/basic.yaml", "output": "out/basic"}, app.token)
    assert status == 200 and headers["X-Location"] == "/jobs/1"
    job = app.jobs.jobs[1]
    deadline = time.time() + 60
    while job.status in ("queued", "running") and time.time() < deadline:
        time.sleep(0.2)
    assert job.status == "done", "\n".join(job.log)
    log = _get(port, "/fragment/log?id=1")[1]
    assert "Open the report" in log and "data-finished" in log
    status, report, _ = _get(port, "/files/out/basic/report.html")
    assert status == 200 and re.search(r'<script src="_assets/plotly.min.js">', report)
    assert "out/basic" in _get(port, "/")[1]


# --- study settings and new files ----------------------------------------------------


def test_sections_are_rewritten_in_place_keeping_everything_else():
    from der_opt.ui.sections import replace_sections

    text = ("# header\nname: s\n\n# about the defaults\ndefaults:\n  - set: {premise: {poi_limit_kw: 30}} # old\n\n"
            "# about overrides\noverrides:\n  a: {premise: {x: 1}}\n\nanalyses: {feeder: {}} # keep me\n")
    new = replace_sections(text, {"defaults": [{"where": {"type": "battery"}, "set": {"device": {"eta_charge": 0.9}}}],
                                  "overrides": {}, "add_devices": [{"share": 0.5, "seed": 1,
                                                                    "device": {"device_id": "b", "type": "battery"}}]})
    assert new == ("# header\nname: s\n\n# about the defaults\ndefaults:\n  - where: {type: battery}\n"
                   "    set: {device: {eta_charge: 0.9}}\n\n\nanalyses: {feeder: {}} # keep me\n\n"
                   "add_devices:\n  - share: 0.5\n    seed: 1\n    device: {device_id: b, type: battery}\n")


def test_the_forms_round_trip_a_study_and_name_what_is_wrong():
    import yaml

    from der_opt.ui.study_form import FormErrors, form_state, sections_from_form

    text = (REPO / "examples/scenarios/feeder_ev_heat_wave.yaml").read_text(encoding="utf-8")
    text += ("\noverrides:\n  tx_a_h02: {premise: {poi_limit_kw: 40}, devices: {ev: {soc_init_frac: 0.5}}}\n"
             "add_devices:\n  - where: {element_id: tx_b}\n    share: 0.5\n    seed: 3\n"
             "    device: {device_id: battery, type: battery, template: battery_13kwh, kw: 5, kwh: 13.5}\n")
    state = form_state(text, None, None, [])
    sections = sections_from_form(state)
    original = yaml.safe_load(text)
    assert sections["defaults"] == original["defaults"] and sections["add_devices"] == original["add_devices"]
    assert sections["overrides"] == {"tx_a_h02": {"premise": {"poi_limit_kw": 40}},
                                     "tx_a_h02/ev": {"device": {"soc_init_frac": 0.5}}}  # the same, in one form
    state["defaults"][1]["device"].append(["eta_charge", "{oops"])
    state["add_devices"][0]["device"] = [["type", "battery"]]
    state["overrides"].append({"target": "", "settings": [["x", "1"]]})
    with pytest.raises(FormErrors) as caught:
        sections_from_form(state)
    text_errors = " | ".join(caught.value.errors)
    assert "defaults[1] device: eta_charge" in text_errors and "isn't YAML" in text_errors
    assert "add_devices[0]" in text_errors and "device_id" in text_errors
    assert "name a premise, or premise/device" in text_errors


def test_study_settings_save_through_the_app(server, project):
    import json

    app, port = server
    path = "scenarios/files_study.yaml"
    status, page, _ = _get(port, f"/fragment/study-settings?path={path}")
    assert status == 200 and "study-form" in page and "inherited from the blocks" in page
    state = app.study_state(path)
    assert "home_06/battery" in state["suggest"]["targets"] and "battery_13kwh" in state["suggest"]["templates"]
    state["overrides"] = [{"target": "home_06/battery", "settings": [["soc_init_frac", "0.2"]]}]
    form = {k: state[k] for k in ("defaults", "overrides", "add_devices")}
    status, body, _ = _post(port, "/study-settings", {"path": path, "form": json.dumps(form)}, app.token)
    assert status == 200 and "Saved." in body and "Ready to run." in body
    written = (project / path).read_text(encoding="utf-8")
    assert "home_06/battery: {device: {soc_init_frac: 0.2}}" in written and "# Six homes" in written
    form["overrides"][0]["target"] = "home_99"
    body = _post(port, "/study-settings", {"path": path, "form": json.dumps(form)}, app.token)[1]
    assert "Saved." in body and "overrides a premise that" in body  # saved; the check names the mistake


def test_new_files_from_a_template_and_from_blocks(server, project):
    app, port = server
    data = REPO / "examples/data/spec_sample"
    shutil.copytree(data, project / "data")
    assert "New scenario or study" in _get(port, "/new")[1]
    form = {"template": "feeder-from-files", "target": "studies/sample.yaml", "premises": "data/premises.csv",
            "devices": "data/devices.csv", "network": "data/network.csv", "timeseries": "data/timeseries.csv"}
    _, _, headers = _post(port, "/new/template", form, app.token)
    assert headers.get("X-Location") == "/file?path=studies/sample.yaml#check"
    assert "population:" in (project / "studies/sample.yaml").read_text(encoding="utf-8")
    assert "exists" in _post(port, "/new/template", form, app.token)[1]  # never overwritten
    query = "block:time=time/2026-07-13T12_72x60min&block:value_streams=value_streams/bill"
    assert "needs a billing block" in _get(port, f"/fragment/blocks-check?{query}")[1]
    picks = {"target": "scenarios/home.yaml", "timeseries": "data/timeseries.csv", "premise": "home_06",
             "block:time": "time/2026-07-13T12_72x60min",
             "block:value_streams": "value_streams/pge_e_elec_2026-06_import", "block:devices": "devices/battery_13kwh"}
    _, body, headers = _post(port, "/new/blocks", picks, app.token)
    assert headers.get("X-Location") == "/file?path=scenarios/home.yaml#check", body
    written = (project / "scenarios/home.yaml").read_text(encoding="utf-8")
    assert "battery_13kwh.yaml" in written and "premise_id: home_06" in written
