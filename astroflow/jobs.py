"""Background jobs: every long task (analysis, stacking, export...) runs here.

The UI never waits on a long task. It starts a job, then polls its progress,
time estimate and memory use, and can cancel it at any time. Job functions
call ``job.update(...)`` regularly; ``update`` raises ``Cancelled`` once the
user has pressed cancel, so cancellation is quick and clean.
"""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from typing import Any, Callable

log = logging.getLogger("astroflow.jobs")


class Cancelled(Exception):
    """Raised inside a job when the user cancels it."""


def _process_memory() -> int:
    try:
        import psutil
        return psutil.Process().memory_info().rss
    except Exception:
        return 0


def plain(v: Any) -> Any:
    """Convert numpy numbers/arrays (and tuples, paths) inside a task result to plain JSON-friendly values."""
    if isinstance(v, dict):
        return {str(k): plain(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set)):
        return [plain(x) for x in v]
    if isinstance(v, float):
        return v if v == v and v not in (float("inf"), float("-inf")) else None   # JSON has no NaN/inf
    if isinstance(v, (str, int, bool)) or v is None:
        return v
    try:
        import numpy as np
        if isinstance(v, np.ndarray):
            return plain(v.tolist()) if v.size <= 100_000 else f"<array {v.shape}>"
        if isinstance(v, np.generic):
            return plain(v.item())
    except ImportError:
        pass
    return str(v)


class Job:
    def __init__(self, kind: str, title: str):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.title = title
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.result: Any = None
        self.error: str | None = None
        self.error_detail: str | None = None
        self.log: list[str] = []
        self._cancel = threading.Event()
        self._last_check = 0.0

    # Called from inside the job function ------------------------------------
    def update(self, progress: float | None = None, message: str | None = None) -> None:
        if progress is not None:
            self.progress = float(min(max(progress, 0.0), 1.0))
        if message is not None and message != self.message:
            self.message = message
            self.log.append(f"{time.strftime('%H:%M:%S')} {message}")
            del self.log[:-200]
        self.check()

    def check(self) -> None:
        if self._cancel.is_set():
            raise Cancelled()

    def note(self, message: str) -> None:
        self.log.append(f"{time.strftime('%H:%M:%S')} {message}")

    # Called from the outside -------------------------------------------------
    def cancel(self) -> None:
        self._cancel.set()

    @property
    def cancelled(self) -> bool:
        return self._cancel.is_set()

    def eta_seconds(self) -> float | None:
        if self.status != "running" or not self.started or self.progress <= 0.01:
            return None
        elapsed = time.time() - self.started
        return max(0.0, elapsed / self.progress - elapsed)

    def to_dict(self, include_result: bool = True) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title, "status": self.status,
            "progress": round(self.progress, 4), "message": self.message,
            "eta_s": self.eta_seconds(), "created": self.created, "started": self.started,
            "finished": self.finished, "error": self.error, "log": self.log[-30:],
            "elapsed_s": (self.finished or time.time()) - self.started if self.started else 0,
        }
        if include_result:
            d["result"] = self.result
        return d


class JobManager:
    """Runs jobs in threads. Heavy jobs are limited so the PC stays usable."""

    def __init__(self, max_heavy: int = 1):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._heavy = threading.Semaphore(max_heavy)

    def submit(self, kind: str, title: str, fn: Callable[..., Any], *args, heavy: bool = True, **kwargs) -> Job:
        job = Job(kind, title)
        with self._lock:
            self._jobs[job.id] = job
            # Keep the list bounded.
            done = [j for j in self._jobs.values() if j.finished]
            for old in sorted(done, key=lambda j: j.finished or 0)[:-50]:
                self._jobs.pop(old.id, None)

        def runner():
            if heavy:
                job.message = "Waiting for another task to finish..."
                while not self._heavy.acquire(timeout=0.25):
                    if job.cancelled:
                        job.status = "cancelled"
                        job.finished = time.time()
                        return
            try:
                job.status = "running"
                job.started = time.time()
                log.info("Job %s started: %s", job.id, title)
                job.result = plain(fn(job, *args, **kwargs))
                job.progress = 1.0
                job.status = "done"
                log.info("Job %s finished in %.1fs", job.id, time.time() - job.started)
            except Cancelled:
                job.status = "cancelled"
                job.message = "Cancelled"
                log.info("Job %s cancelled", job.id)
            except Exception as e:  # noqa: BLE001 - we report every failure to the user
                job.status = "error"
                job.error = f"{type(e).__name__}: {e}"
                job.error_detail = traceback.format_exc()
                log.error("Job %s failed: %s\n%s", job.id, job.error, job.error_detail)
            finally:
                job.finished = time.time()
                if heavy:
                    self._heavy.release()

        t = threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True)
        t.start()
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job and not job.finished:
            job.cancel()
            return True
        return False

    def active(self) -> list[Job]:
        return [j for j in self.list() if j.status in ("queued", "running")]

    def wait(self, job_id: str, timeout: float = 600) -> Job:
        """Block until a job ends (used by tests and scripts)."""
        end = time.time() + timeout
        while time.time() < end:
            j = self.get(job_id)
            if j and j.finished:
                return j
            time.sleep(0.05)
        raise TimeoutError(job_id)

    @staticmethod
    def memory_bytes() -> int:
        return _process_memory()


jobs = JobManager()


class ProgressSplitter:
    """Maps sub-step progress (0..1) into a slice of the overall job progress."""

    def __init__(self, job: Job | None, start: float, end: float):
        self.job, self.start, self.end = job, start, end

    def __call__(self, frac: float, message: str | None = None) -> None:
        if self.job is not None:
            self.job.update(self.start + (self.end - self.start) * frac, message)


class NullJob(Job):
    """Stand-in when running algorithms outside the job system (tests, scripts)."""

    def __init__(self):
        super().__init__("inline", "inline")
        self.status = "running"
        self.started = time.time()
