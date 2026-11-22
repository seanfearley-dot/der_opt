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

from der_opt.ui import views
from der_opt.ui.jobs import JobQueue
from der_opt.ui.project import OutsideProject, Project


class App:
    def __init__(self, root: str | Path):
        self.project = Project(root)
        self.jobs = JobQueue(self.project.root)
        self.token = secrets.token_urlsafe(24)


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
            params = {k: v[0] for k, v in parse_qs(url.query).items()}
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
                    return self._send(200, views.where_fragment(project, params["path"], params.get("where", "")))
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
            form = {k: v[0] for k, v in parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True).items()}
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
                if route == "/cancel":
                    app.jobs.cancel(int(form["id"]))
                    return self._send(200, "", headers={"X-Location": f"/jobs/{form['id']}"})
                return self._error(404, "nothing here")
            except OutsideProject as exc:
                return self._error(403, str(exc))
            except (KeyError, ValueError) as exc:
                return self._error(400, f"bad request: {exc}")

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
