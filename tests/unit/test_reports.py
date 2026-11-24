"""report.html (docs/REPORTS.md section 2): one page per output folder, sharing one
copy of plotly.js per output tree, rebuilt by `der-opt report`."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd
from typer.testing import CliRunner

from der_opt.cli.main import app
from der_opt.reports.page import ASSETS, PLOTLY, Card, Page, assets_for

REPO = Path(__file__).parent.parent.parent
FIXTURE = REPO / "tests" / "fixtures" / "scenario_basic.yaml"
runner = CliRunner()


def _data(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(re.search(r'<script type="application/json" id="report-data">(.*?)</script>', text, re.DOTALL)[1])


def test_a_page_sends_a_time_axis_once_and_escapes_what_it_shows(tmp_path):
    page = Page("a <b>", "sub", "scenario")
    section = page.section("s", "Section")
    index = pd.date_range("2026-07-13", periods=48, freq="h")
    axis = page.time_axis("t", index)
    page.chart(section, [{"type": "scatter", "x": axis, "y": list(range(48))},
                         {"type": "scatter", "x": axis, "y": list(range(48))}], zoom=True)
    page.cards(section, [Card("cost", "$1", "</script>")])
    page.table(section, pd.DataFrame({"name": ["<x>"], "kw": [1.5]}))
    text = page.render("_assets/plotly.min.js")
    assert "<h1>a &lt;b&gt;</h1>" in text and "&lt;x&gt;" in text and "&lt;/script&gt;" in text
    data = json.loads(re.search(r'id="report-data">(.*?)</script>', text, re.DOTALL)[1])
    assert data["axes"]["t"] == {"start": "2026-07-13T00:00:00", "minutes": 60, "n": 48}
    assert all(t["x"] == {"axis": "t"} for t in data["charts"][0]["traces"])
    assert '<td data-sort="1.5" class="num">1.50</td>' in text


def test_assets_are_found_above_or_put_beside(tmp_path):
    study = tmp_path / "study"
    (study / ASSETS).mkdir(parents=True)
    (study / ASSETS / PLOTLY).write_text("//", encoding="utf-8")
    run = study / "portfolio" / "home_a"
    run.mkdir(parents=True)
    assert assets_for(run) == f"../../{ASSETS}/{PLOTLY}"
    alone = tmp_path / "alone"
    alone.mkdir()
    assert assets_for(alone) == f"{ASSETS}/{PLOTLY}" and (alone / ASSETS / PLOTLY).stat().st_size > 1_000_000


def test_a_single_home_run_writes_its_report(tmp_path):
    scenario = tmp_path / "cf.yaml"
    scenario.write_text(FIXTURE.read_text() + "\ncounterfactual:\n  policies:\n    battery: {type: self_consumption}\n")
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(scenario), "-o", str(out)])
    assert result.exit_code == 0 and f"Report: {out / 'report.html'}" in result.output
    text = (out / "report.html").read_text(encoding="utf-8")
    assert '<script src="_assets/plotly.min.js">' in text and (out / ASSETS / PLOTLY).exists()
    for section in ("summary", "value", "dispatch", "energy", "checks", "settings"):
        assert f'<section id="{section}">' in text, section
    assert 'class="case-pick"' in text and ">Optimization</button>" in text  # the value of optimization's pair
    (out / "report.html").unlink()
    again = runner.invoke(app, ["report", str(out)])
    assert again.exit_code == 0 and (out / "report.html").exists()
    assert runner.invoke(app, ["report", str(tmp_path)]).exit_code != 0  # no summary.json there


def test_a_study_links_its_feeder_and_shares_one_plotly(tmp_path):
    out = tmp_path / "out"
    result = runner.invoke(app, ["run", str(REPO / "examples/scenarios/feeder_coordination.yaml"), "-o", str(out),
                                 "--no-counterfactual"])
    assert result.exit_code == 0, result.output
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob(PLOTLY)) == [f"{ASSETS}/{PLOTLY}"]
    study, feeder = (out / "report.html").read_text(encoding="utf-8"), (out / "feeder" / "report.html").read_text(
        encoding="utf-8")
    assert 'href="feeder/report.html"' in study and "% of ideal" in study
    assert f'<script src="../{ASSETS}/{PLOTLY}">' in feeder and 'href="../report.html"' in feeder
    for section in ("loading", "premises", "coordination"):
        assert f'<section id="{section}">' in feeder, section
    charts = _data(out / "feeder" / "report.html")["charts"]
    assert any(t.get("name") == "envelopes" for c in charts for t in c["traces"])
    # a page per premise: linked from the feeder's, with every run, its envelope and its path's prices
    assert 'class="premise-pick"' in feeder and 'href="premises/home_01.html"' in feeder
    page = out / "feeder" / "premises" / "home_01.html"
    text = page.read_text(encoding="utf-8")
    assert f'<script src="../../{ASSETS}/{PLOTLY}">' in text and 'href="../report.html"' in text
    for section in ("summary", "costs", "meter", "devices", "prices"):
        assert f'<section id="{section}">' in text, section
    names = {t.get("name") for c in _data(page)["charts"] for t in c["traces"]}
    assert {"coordinated", "independent", "envelopes: import envelope",
            "congestion price on its path (coordinated)"} <= names
    devices = next(c for c in _data(page)["charts"] if c["layout"].get("updatemenus"))
    assert [b["label"] for b in devices["layout"]["updatemenus"][0]["buttons"]][:2] == ["coordinated", "independent"]


def test_time_charts_zoom_together_and_hover_with_units(tmp_path):
    page = Page("p")
    section = page.section("s", "S")
    axis = page.time_axis("t", pd.date_range("2026-07-13", periods=48, freq="h"))
    page.chart(section, [{"type": "scatter", "x": axis, "y": [1.0] * 48}], zoom=True, unit="kW")
    page.chart(section, [{"type": "scatter", "x": axis, "y": [1.0] * 48, "yaxis": "y2"}], zoom=True, unit2="kWh")
    page.chart(section, [{"type": "bar", "x": ["a"], "y": [1]}])
    text = page.render("x.js")
    data = json.loads(re.search(r'id="report-data">(.*?)</script>', text, re.DOTALL)[1])
    assert [c["sync"] for c in data["charts"]] == [True, True, False]
    assert data["charts"][0]["traces"][0]["hovertemplate"] == "%{y:,.2f} kW"
    assert data["charts"][1]["traces"][0]["hovertemplate"] == "%{y:,.2f} kWh"
    assert "linkZoom" in text and "hoverlabel:{bgcolor" in text


def test_trc_and_rim_lead_the_summary(tmp_path):
    from der_opt.reports import scenario

    summary = {"name": "home", "time_grid": {"start": "2026-01-01T00:00:00", "end": "2027-01-01T00:00:00", "steps": 8760,
                                             "resolution_minutes": 60},
               "costs": {"optimal": {"bill": 10.0, "total": 10.0}}, "devices": [], "checks": {},
               "cost_tests": [{"test": t, "openbca_ratio": r, "net_benefits": n}
                              for t, r, n in (("PCT", 2.0, 5), ("RIM", 0.8, -30), ("TRC", 1.4, 200), ("PAC", 1.1, 9))]}
    text = scenario.build(tmp_path, summary).render("x.js")
    summary_section = text[text.index('<section id="summary">'):text.index("</section>")]
    assert summary_section.index("TRC ratio") < summary_section.index("RIM ratio") < summary_section.index("Optimal cost")
    assert summary_section.count('class="card good feature"') == 1 and summary_section.count('class="card bad feature"') == 1
    assert "PAC ratio" not in summary_section
