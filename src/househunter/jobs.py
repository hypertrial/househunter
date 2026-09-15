from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Literal

from .acquisition import download_optional_bea
from .build import build_snapshot
from .chrr import download_chrr
from .config import RuntimePaths
from .contracts import JobStatus
from .download import download_fema, download_fema_counties
from .errors import HouseHunterError
from .locking import exclusive_lock

JobKind = Literal["download", "build", "prepare"]


@dataclass
class _Job:
    status: JobStatus
    cancel: threading.Event


class JobManager:
    def __init__(self, paths: RuntimePaths) -> None:
        self.paths = paths
        self._jobs: dict[str, _Job] = {}
        self._guard = threading.Lock()

    def start(self, kind: JobKind, *, state: str | None = None) -> JobStatus:
        self.paths.ensure()
        with self._guard:
            if any(job.status.state in {"queued", "running"} for job in self._jobs.values()):
                raise HouseHunterError("Another mutating job is already running")
            job_id = uuid.uuid4().hex
            status = JobStatus(
                job_id=job_id,
                kind=kind,
                state="queued",
                progress=0,
                message="Queued",
                created_at=datetime.now(UTC),
            )
            job = _Job(status=status, cancel=threading.Event())
            self._jobs[job_id] = job
            threading.Thread(
                target=self._run, args=(job, state), daemon=True, name=f"househunter-{job_id[:8]}"
            ).start()
            return status.model_copy(deep=True)

    def get(self, job_id: str) -> JobStatus:
        with self._guard:
            job = self._jobs.get(job_id)
            if not job:
                raise HouseHunterError(f"Job not found: {job_id}")
            return job.status.model_copy(deep=True)

    def cancel(self, job_id: str) -> JobStatus:
        with self._guard:
            job = self._jobs.get(job_id)
            if not job:
                raise HouseHunterError(f"Job not found: {job_id}")
            if job.status.state in {"queued", "running"}:
                job.cancel.set()
                job.status.message = "Cancellation requested"
            return job.status.model_copy(deep=True)

    def _update(self, job: _Job, progress: int, message: str) -> None:
        with self._guard:
            job.status.progress = progress
            job.status.message = message

    def _run(self, job: _Job, state: str | None) -> None:
        try:
            with exclusive_lock(self.paths.job_lock):
                with self._guard:
                    job.status.state = "running"
                    job.status.started_at = datetime.now(UTC)
                    job.status.message = "Starting"
                if job.cancel.is_set():
                    raise InterruptedError("Job cancelled")

                def progress(value: int, message: str) -> None:
                    if job.cancel.is_set():
                        raise InterruptedError("Job cancelled")
                    self._update(job, value, message)

                if job.status.kind == "download":
                    download_fema(
                        self.paths,
                        progress=lambda value, message: progress(value * 35 // 100, message),
                        cancelled=job.cancel.is_set,
                    )
                    download_fema_counties(
                        self.paths,
                        progress=lambda value, message: progress(35 + value * 25 // 100, message),
                        cancelled=job.cancel.is_set,
                    )
                    download_chrr(
                        self.paths,
                        progress=lambda value, message: progress(60 + value * 25 // 100, message),
                        cancelled=job.cancel.is_set,
                    )
                    download_optional_bea(
                        self.paths,
                        progress=lambda value, message: progress(85 + value * 15 // 100, message),
                        cancelled=job.cancel.is_set,
                    )
                elif job.status.kind == "build":
                    build_snapshot(
                        self.paths, state=state, progress=progress, cancelled=job.cancel.is_set
                    )
                else:
                    from .run import prepare_runtime

                    prepare_runtime(
                        self.paths,
                        state=state,
                        progress=progress,
                        cancelled=job.cancel.is_set,
                        hold_lock=False,
                    )
                with self._guard:
                    job.status.state = "succeeded"
                    job.status.progress = 100
                    job.status.message = "Complete"
        except InterruptedError:
            with self._guard:
                job.status.state = "cancelled"
                job.status.message = "Cancelled"
        except Exception as exc:
            with self._guard:
                job.status.state = "failed"
                job.status.message = "Failed"
                job.status.error = str(exc)
        finally:
            with self._guard:
                job.status.finished_at = datetime.now(UTC)
