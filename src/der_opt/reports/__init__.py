"""report.html for every output folder (docs/REPORTS.md section 2): built from the
folder's summary.json and CSVs, so `der-opt report OUT` can rebuild it without solving
again."""

from __future__ import annotations

from pathlib import Path

from der_opt.reports.page import ASSETS, Page, assets_for, ensure_assets
from der_opt.results.summary import read_summary

REPORT = "report.html"


def write_report(folder: str | Path, crumbs: list[tuple[str, str]] | None = None) -> Path | None:
    """The folder's report.html, from its summary.json; None if it has no summary."""
    from der_opt.reports import feeder, scenario, study

    folder = Path(folder)
    summary = read_summary(folder)
    if summary is None:
        return None
    builder = {"scenario": scenario.build, "feeder": feeder.build, "study": study.build}[summary["kind"]]
    page = builder(folder, summary)
    page.links = crumbs or []
    path = folder / REPORT
    path.write_text(page.render(assets_for(folder)), encoding="utf-8")
    return path


def write_reports(folder: str | Path) -> list[Path]:
    """The folder's report, and for a study its feeder's and each portfolio premise's,
    sharing one copy of plotly.js in the study's _assets/."""
    folder = Path(folder)
    summary = read_summary(folder)
    if summary is None:
        return []
    ensure_assets(folder)
    written = []
    if summary["kind"] == "study":
        back = [(summary["name"], "../report.html")]
        if summary.get("feeder"):
            written.append(write_report(folder / summary["feeder"]["folder"], back))
        for item in (summary.get("portfolio") or {}).get("premises", []):
            if item.get("folder") and (folder / item["folder"]).exists():
                written.append(write_report(folder / item["folder"], [(summary["name"], "../../report.html")]))
    written.append(write_report(folder))
    return [p for p in written if p is not None]


__all__ = ["ASSETS", "REPORT", "Page", "write_report", "write_reports"]
