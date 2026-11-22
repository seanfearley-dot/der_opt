"""The app's run queue: `der-opt run` in a subprocess, one at a time (solves use every
core they get), each with its log kept as it's written."""

from __future__ import annotations

import itertools
import queue
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

RUNNER = "from der_opt.cli.main import app; app()"


@dataclass
class Job:
    id: int
    scenario: Path  # relative to the project
    output: Path  # relative to the project
    options: list[str]
    status: str = "queued"  # queued, running, done, failed, cancelled
    log: list[str] = field(default_factory=list)
    queued_at: float = field(default_factory=time.time)
    started_at: float | None = None
    finished_at: float | None = None
    returncode: int | None = None
    process: subprocess.Popen | None = None

    @property
    def command(self) -> list[str]:
        return ["der-opt", "run", str(self.scenario), "-o", str(self.output), *self.options]

    @property
    def elapsed(self) -> float | None:
        if self.started_at is None:
            return None
        return (self.finished_at or time.time()) - self.started_at


class JobQueue:
    def __init__(self, root: Path):
        self.root = root
        self.jobs: dict[int, Job] = {}
        self._ids = itertools.count(1)
        self._pending: queue.Queue[int] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._work, daemon=True)
        self._worker.start()

    def submit(self, scenario: Path, output: Path, options: list[str]) -> Job:
        with self._lock:
            job = Job(next(self._ids), scenario, output, options)
            self.jobs[job.id] = job
        self._pending.put(job.id)
        return job

    def cancel(self, job_id: int) -> None:
        job = self.jobs.get(job_id)
        if job is None:
            return
        if job.status == "queued":
            job.status = "cancelled"
        elif job.status == "running" and job.process is not None:
            job.process.terminate()
            job.status = "cancelled"

    def ordered(self) -> list[Job]:
        return sorted(self.jobs.values(), key=lambda j: j.id, reverse=True)

    def _work(self) -> None:
        while True:
            job = self.jobs[self._pending.get()]
            if job.status == "cancelled":
                continue
            job.status, job.started_at = "running", time.time()
            job.log.append("$ " + " ".join(job.command))
            try:
                job.process = subprocess.Popen(
                    [sys.executable, "-c", RUNNER, "run", str(job.scenario), "-o", str(job.output), *job.options],
                    cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                for line in job.process.stdout:
                    job.log.append(line.rstrip("\n"))
                job.returncode = job.process.wait()
            except OSError as exc:
                job.log.append(f"could not start: {exc}")
                job.returncode = -1
            job.finished_at = time.time()
            if job.status != "cancelled":
                job.status = "done" if job.returncode == 0 else "failed"
            job.log.append(f"[{job.status}, exit code {job.returncode}, {job.elapsed:.1f} s]")
