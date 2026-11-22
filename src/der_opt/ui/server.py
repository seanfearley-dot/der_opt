"""`der-opt ui`: the local app (docs/REPORTS.md section 3).

A standard-library HTTP server on 127.0.0.1. It reads and writes the project's own
files: scenario and study YAML, and output folders under out/. Every POST carries a
token from the page, and requests must name localhost, so another site in the browser
can't drive it.
"""

from __future__ import annotations

import mimetypes
import secrets
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from der_opt.ui import forms, views
from der_opt.ui.jobs import JobQueue
from der_opt.ui.project import OutsideProject, Project


class App:
    def __init__(self, root: str | Path, library: str | Path | None = None):
        self.project = Project(root)
        self.jobs = JobQueue(self.project.root)
        self.token = secrets.token_urlsafe(24)
        self.library = library

    def blocks(self):
        """The library's blocks (read each time, so edits to them show)."""
        from der_opt.scenarios.library import read_library
        from der_opt.settings import library_dir

        folder, _ = library_dir(self.library)
        return folder, read_library(folder)

    def study_state(self, relative: str) -> dict:
        from der_opt.population.compile import _template_index
        from der_opt.scenarios.compose import CompositionError, compose_scenario
        from der_opt.ui.study_form import form_state

        path = self.project.inside(relative)
        try:
            composed = compose_scenario(path).data
        except CompositionError:
            composed = None
        try:
            resolved = views.resolved(self.project, relative)
        except Exception:  # noqa: BLE001 -- the forms work without suggestions from a study that doesn't resolve
            resolved = None
        folders = ((composed or {}).get("population") or {}).get("templates")
        try:
            templates = sorted(_template_index(folders))
        except Exception:  # noqa: BLE001
            templates = []
        return form_state(path.read_text(encoding="utf-8"), composed, resolved, templates)


def make_handler(app: App, port: int):
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "der-opt-ui"

        def log_message(self, format, *args):  # quiet: the terminal shows the URL, not every request
            return

        def _send(self, status: int, body: str | bytes, content_type: str = "text/html; charset=utf-8",
                  headers: dict | None = None) -> None:
            data = body.encode("utf-8") if isinstance(body, str) else body
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for key, value in (headers or {}).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(data)

        def _error(self, status: int, message: str) -> None:
            self._send(status, f'<p class="status bad">{views.esc(message)}</p>')

        def _allowed(self) -> bool:
            if self.headers.get("Host") not in hosts:
                self._error(HTTPStatus.FORBIDDEN, "der-opt ui answers only on localhost")
                return False
            return True

        def do_GET(self):
            if not self._allowed():
                return
            url = urlparse(self.path)
            lists = parse_qs(url.query, keep_blank_values=True)
            params = {k: v[0] for k, v in lists.items()}
            project, jobs, token = app.project, app.jobs, app.token
            try:
                route = url.path
                if route == "/":
                    return self._send(200, views.home(project, jobs.ordered(), token))
                if route == "/file":
                    return self._send(200, views.file_page(project, params["path"], token))
                if route == "/fragment/check":
                    return self._send(200, views.check_fragment(project, params["path"]))
                if route == "/fragment/population":
                    return self._send(200, views.population_fragment(project, params["path"]))
                if route == "/fragment/where":
                    return self._send(200, views.where_fragment(project, params["path"], params.get("where", ""),
                                                                compact=bool(params.get("compact"))))
                if route == "/fragment/study-settings":
                    return self._send(200, forms.study_settings_fragment(params["path"], app.study_state(params["path"])))
                if route == "/new":
                    folder, blocks = app.blocks()
                    return self._send(200, forms.new_page(project, token, folder, blocks))
                if route == "/fragment/blocks-check":
                    _, blocks = app.blocks()
                    picks = [v for k, values in lists.items() if k.startswith("block:") for v in values if v]
                    return self._send(200, forms.blocks_check(blocks, picks, params.get("timeseries", ""),
                                                              params.get("premise", ""), project))
                if route == "/fragment/merged":
                    return self._send(200, views.merged_fragment(project, params["path"]))
                if route == "/jobs":
                    return self._send(200, views.jobs_page(project, jobs.ordered(), token))
                if route == "/fragment/jobs":
                    return self._send(200, views.jobs_table(jobs.ordered()))
                if route.startswith("/jobs/"):
                    job = jobs.jobs.get(int(route.rsplit("/", 1)[1]))
                    if job is None:
                        return self._error(404, "no such run")
                    return self._send(200, views.job_page(project, job, token))
                if route == "/fragment/log":
                    job = jobs.jobs.get(int(params["id"]))
                    if job is None:
                        return self._error(404, "no such run")
                    return self._send(200, views.log_fragment(project, job))
                if route.startswith("/files/"):
                    return self._file(route[len("/files/"):])
                return self._error(404, "nothing here")
            except OutsideProject as exc:
                return self._error(403, str(exc))
            except (KeyError, ValueError) as exc:
                return self._error(400, f"bad request: {exc}")
            except FileNotFoundError as exc:
                return self._error(404, str(exc))

        def _file(self, relative: str) -> None:
            from urllib.parse import unquote

            path = app.project.inside(unquote(relative))
            if path.is_dir():
                path = path / "report.html"
            if not path.is_file():
                return self._error(404, f"{relative} isn't there")
            content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
            if content_type.startswith("text/") or content_type in ("application/javascript", "application/json"):
                content_type += "; charset=utf-8"
            return self._send(200, path.read_bytes(), content_type)

        def do_POST(self):
            if not self._allowed():
                return
            if not secrets.compare_digest(self.headers.get("X-Token", ""), app.token):
                return self._error(HTTPStatus.FORBIDDEN, "missing or stale token: reload the page")
            length = int(self.headers.get("Content-Length") or 0)
            lists = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True)
            form = {k: v[0] for k, v in lists.items()}
            project = app.project
            try:
                route = urlparse(self.path).path
                if route == "/save":
                    path = project.inside(form["path"])
                    if path.suffix not in (".yaml", ".yml"):
                        return self._error(400, "only scenario and study YAML files are edited here")
                    path.write_text(form["content"].replace("\r\n", "\n"), encoding="utf-8")
                    return self._send(200, '<p class="status ok">Saved.</p>'
                                      + views.check_fragment(project, form["path"]))
                if route == "/run":
                    scenario = project.relative(project.inside(form["path"]))
                    output = project.relative(project.inside(form.get("output") or project.default_output(scenario)))
                    options = [flag for key, flag in (("no_counterfactual", "--no-counterfactual"),
                                                      ("cost_tests", "--cost-tests"),
                                                      ("keep_openbca", "--keep-openbca")) if form.get(key)]
                    job = app.jobs.submit(scenario, output, options)
                    return self._send(200, "", headers={"X-Location": f"/jobs/{job.id}"})
                if route == "/study-settings":
                    return self._send(200, self._study_settings(form))
                if route == "/new/template":
                    return self._new_template(form)
                if route == "/new/blocks":
                    picks = [v for k, values in lists.items() if k.startswith("block:") for v in values if v]
                    return self._new_blocks(form, picks)
                if route == "/cancel":
                    app.jobs.cancel(int(form["id"]))
                    return self._send(200, "", headers={"X-Location": f"/jobs/{form['id']}"})
                return self._error(404, "nothing here")
            except OutsideProject as exc:
                return self._error(403, str(exc))
            except (KeyError, ValueError) as exc:
                return self._error(400, f"bad request: {exc}")

        def _study_settings(self, form: dict) -> str:
            import json

            from der_opt.ui.study_form import FormErrors, apply_form

            path = app.project.inside(form["path"])
            try:
                text = apply_form(path.read_text(encoding="utf-8"), json.loads(form["form"]))
            except FormErrors as exc:
                return ('<p class="status bad">Not saved: fix these first.</p><ul>'
                        + "".join(f"<li>{views.esc(e)}</li>" for e in exc.errors) + "</ul>")
            path.write_text(text, encoding="utf-8")
            return ('<p class="status ok">Saved.</p>' + views.check_fragment(app.project, form["path"])
                    + f'<template id="newtext">{views.esc(text)}</template>')

        def _target(self, form: dict) -> tuple[Path, str]:
            relative = (form.get("target") or "").strip()
            if not relative.endswith((".yaml", ".yml")):
                raise ValueError("the file to write ends in .yaml")
            path = app.project.inside(relative)
            if path.exists():
                raise ValueError(f"{relative} exists; pick another name (nothing was written)")
            return path, str(app.project.relative(path))

        def _new_template(self, form: dict) -> None:
            from der_opt.scenarios.templates import Options, write_template

            try:
                path, relative = self._target(form)
                files = {k: app.project.inside(form[k]) if form.get(k, "").strip() else None
                         for k in ("timeseries", "tariff", "export_rates", "avoided_costs", "price", "premises",
                                   "devices", "network")}
                missing = [f"{k}: {form[k]}" for k, v in files.items() if v is not None and not v.exists()]
                if missing:
                    raise ValueError("these aren't there: " + "; ".join(missing))
                year = int(form["year"]) if form.get("year", "").strip() else None
                opts = Options(premise=form.get("premise") or None, program=form.get("program") or "nbt",
                               baseline_territory=form.get("baseline_territory") or None, year=year, **files)
                written = write_template(form["template"], path, opts)
            except (ValueError, FileExistsError) as exc:
                return self._send(200, f'<p class="status bad">{views.esc(exc)}</p>')
            note = f" ({written.todos} TODO to fill in)" if written.todos else ""
            return self._send(200, f"written{note}", headers={"X-Location": forms.file_link(relative) + "#check"})

        def _new_blocks(self, form: dict, picks: list[str]) -> None:
            from der_opt.scenarios.library import find, home_data, write_picked

            try:
                path, relative = self._target(form)
                folder, blocks = app.blocks()
                chosen = [find(blocks, name) for name in picks]
                series = form.get("timeseries", "").strip()
                home = home_data(app.project.inside(series) if series else None, form.get("premise") or None)
                write_picked(path, chosen, home, folder)
            except (ValueError, FileExistsError) as exc:
                text = views.esc(exc).replace("\n", "<br>")
                return self._send(200, f'<p class="status bad">{text}</p>')
            return self._send(200, "written", headers={"X-Location": forms.file_link(relative) + "#check"})

    return Handler


def serve(root: str | Path, port: int = 8700, open_browser: bool = True) -> None:
    app = App(root)
    server = ThreadingHTTPServer(("127.0.0.1", port), make_handler(app, port))
    url = f"http://localhost:{port}/"
    print(f"der-opt ui: {app.project.root} at {url} (Ctrl+C stops it)")
    if open_browser:
        import webbrowser

        threading.Timer(0.5, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("stopped")
    finally:
        server.server_close()
