"""In-process ingestion jobs with streamed progress (for the bulk-upload demo).

A job takes uploaded files, runs the full pipeline, and swaps the live graph — while
streaming each stage over SSE so the UI narrates 'the system approaching the problem'.
Single-process; a real deploy swaps this for a task queue behind the same interface.
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

_SENTINEL = object()


@dataclass
class Job:
    id: str
    events: "queue.Queue" = field(default_factory=queue.Queue)
    done: bool = False
    error: Optional[str] = None
    result_stats: Optional[dict] = None


class JobManager:
    def __init__(self, on_bundle: Callable):
        self._jobs: dict[str, Job] = {}
        # on_bundle(bundle, session_id) — session_id is None in non-readonly mode
        self._on_bundle = on_bundle

    def start(
        self,
        files: list[tuple[str, bytes]],
        mode: str = "heuristic",
        session_id: Optional[str] = None,
    ) -> Job:
        job = Job(id=str(uuid.uuid4()))
        self._jobs[job.id] = job
        threading.Thread(
            target=self._run, args=(job, files, mode, session_id), daemon=True
        ).start()
        return job

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def _emit(self, job: Job, stage: str, message: str):
        job.events.put({"stage": stage, "message": message, "ts": time.time()})

    def _run(
        self,
        job: Job,
        files: list[tuple[str, bytes]],
        mode: str = "heuristic",
        session_id: Optional[str] = None,
    ):
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
                except Exception as e:  # one bad file shouldn't sink the batch
                    self._emit(job, "skipped", f"  {name}: failed ({type(e).__name__})")

            if not items:
                job.error = "No parseable items in the uploaded files."
                self._emit(job, "error", job.error)
                return

            self._emit(job, "building", f"Parsed {len(items)} items. Deduplicating…")
            self._emit(job, "building",
                       f"Mode: {mode}. Scoping relevance, extracting & resolving entities…"
                       + (" (LLM — this calls the model per batch)" if mode == "llm" else ""))
            bundle = build_graph(items, mode=mode,
                                 progress=lambda stage, msg: self._emit(job, stage, msg))
            self._on_bundle(bundle, session_id)
            job.result_stats = bundle.stats
            self._emit(job, "done",
                       f"Done — {bundle.stats['entities']} entities, "
                       f"{bundle.stats['edges']} edges, {bundle.stats['merges']} identity merges "
                       f"from {bundle.stats['items']} items.")
        except Exception as e:
            job.error = f"{type(e).__name__}: {e}"
            self._emit(job, "error", job.error)
        finally:
            job.done = True
            job.events.put(_SENTINEL)

    def stream(self, job_id: str):
        import json

        job = self.get(job_id)
        if not job:
            yield {"event": "error", "data": json.dumps({"message": "unknown job"})}
            return
        while True:
            item = job.events.get()
            if item is _SENTINEL:
                break
            yield {"event": "progress", "data": json.dumps(item)}
        yield {"event": "done", "data": json.dumps({"error": job.error, "stats": job.result_stats})}
