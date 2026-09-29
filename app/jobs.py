"""In-process ingestion jobs with streamed progress.

Each job stores all events in a list so the frontend can poll at any time.
The SSE endpoint remains for local/dev use but the UI uses polling.
Finished jobs are evicted after JOB_EVICT_SECS; the map is capped at JOB_MAX.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from .ingest.adapters import UnsupportedUpload, parse_upload
from .pipeline.build import build_graph
from .schema import SourceItem
from .limits import JOB_EVICT_SECS, JOB_MAX

_SENTINEL = object()


@dataclass
class Job:
    id: str
    # All events ever emitted — append-only. The polling endpoint returns the full list.
    events: list[dict] = field(default_factory=list)
    # Internal queue used only by the SSE stream. Populated alongside events.
    _sse_queue: "queue.Queue" = field(default_factory=queue.Queue)
    status: str = "queued"          # queued | running | done | failed
    error: Optional[str] = None
    result_stats: Optional[dict] = None
    finished_at: Optional[float] = None


class JobManager:
    def __init__(self, on_bundle: Callable):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._on_bundle = on_bundle

    def start(
        self,
        files: list[tuple[str, bytes]],
        mode: str = "heuristic",
        session_id: Optional[str] = None,
    ) -> Job:
        self._evict()
        with self._lock:
            if len(self._jobs) >= JOB_MAX:
                # Evict the oldest finished job to make room.
                for jid, j in list(self._jobs.items()):
                    if j.status in ("done", "failed"):
                        del self._jobs[jid]
                        break
        job = Job(id=str(uuid.uuid4()))
        with self._lock:
            self._jobs[job.id] = job
        threading.Thread(
            target=self._run, args=(job, files, mode, session_id), daemon=True
        ).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def _evict(self) -> None:
        cutoff = time.time() - JOB_EVICT_SECS
        with self._lock:
            stale = [jid for jid, j in self._jobs.items()
                     if j.finished_at is not None and j.finished_at < cutoff]
            for jid in stale:
                del self._jobs[jid]

    def _emit(self, job: Job, stage: str, message: str) -> None:
        event = {"stage": stage, "message": message, "ts": time.time()}
        job.events.append(event)
        job._sse_queue.put(event)

    def _run(
        self,
        job: Job,
        files: list[tuple[str, bytes]],
        mode: str = "heuristic",
        session_id: Optional[str] = None,
    ) -> None:
        job.status = "running"
        try:
            items: list[SourceItem] = []
            self._emit(job, "parsing", f"Reading {len(files)} file(s)…")
            for name, data in files:
                try:
                    parsed = parse_upload(name, data)
                    items.extend(parsed)
                    self._emit(job, "parsing", f"  {name}: {len(parsed)} items")
                except UnsupportedUpload as e:
                    self._emit(job, "skipped", f"  {name}: {e}")
                except Exception as e:
                    self._emit(job, "skipped", f"  {name}: failed ({type(e).__name__})")

            if not items:
                job.error = "No parseable items in the uploaded files."
                job.status = "failed"
                self._emit(job, "error", job.error)
                return

            self._emit(job, "building", f"Parsed {len(items)} items. Deduplicating…")
            self._emit(
                job, "building",
                f"Mode: {mode}. Extracting and resolving entities…"
                + (" (LLM — calls the model per batch)" if mode == "llm" else ""),
            )
            bundle = build_graph(
                items, mode=mode,
                progress=lambda stage, msg: self._emit(job, stage, msg),
            )
            self._on_bundle(bundle, session_id)
            job.result_stats = bundle.stats
            job.status = "done"
            self._emit(
                job, "done",
                f"Done — {bundle.stats['entities']} entities, "
                f"{bundle.stats['edges']} edges, {bundle.stats['merges']} merges "
                f"from {bundle.stats['items']} items.",
            )
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
            job.status = "failed"
            self._emit(job, "error", job.error)
        finally:
            job.finished_at = time.time()
            if job.status == "running":
                job.status = "failed"
            job._sse_queue.put(_SENTINEL)

    def stream(self, job_id: str):
        """SSE generator — kept for local dev. UI uses polling (/api/jobs/{id})."""
        import json

        job = self.get(job_id)
        if not job:
            yield {"event": "error", "data": json.dumps({"message": "unknown job"})}
            return

        # Replay any events already emitted before the client connected.
        for evt in list(job.events):
            yield {"event": "progress", "data": json.dumps(evt)}

        if job.status in ("done", "failed"):
            yield {"event": "done", "data": json.dumps({"error": job.error, "stats": job.result_stats})}
            return

        while True:
            item = job._sse_queue.get()
            if item is _SENTINEL:
                break
            yield {"event": "progress", "data": json.dumps(item)}
        yield {"event": "done", "data": json.dumps({"error": job.error, "stats": job.result_stats})}
