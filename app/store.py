"""Durable snapshot of the built graph.

Saves the whole Bundle to disk after every ingest and reloads it on startup —
so a restart never loses data and never re-runs (or re-charges for) LLM calls.

Load priority:
  1. $DATA_DIR/bundle.json — committed or volume-mounted; preferred in production.
  2. .cache/bundle.json    — local cache written after a real ingest.
  (The first one found wins; the source is recorded in LAST_SOURCE.)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
import os

from .graph_model import EventGraph
from .ingest.dedup import DedupResult
from .pipeline.build import Bundle
from .schema import SourceItem

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]

DATA_DIR = Path(os.getenv("DATA_DIR", "")).resolve() if os.getenv("DATA_DIR") else _ROOT / "data"
DATA_BUNDLE = DATA_DIR / "bundle.json"
SNAPSHOT = _ROOT / ".cache" / "bundle.json"

# Set by load_bundle() so health can report what was actually loaded.
LAST_SOURCE: str = "empty"

_SCHEMA_VERSION = 1


def save_bundle(bundle: Bundle) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "schema_version": _SCHEMA_VERSION,
        "items": [it.model_dump(mode="json") for it in bundle.items],
        "graph": bundle.graph.model_dump(mode="json"),
        "timeline": bundle.timeline,
        "stats": bundle.stats,
        "review_queue": bundle.review_queue,
        "item_topics": bundle.item_topics,
        "dedup": {
            "exact_groups": bundle.dedup.exact_groups,
            "near_clusters": bundle.dedup.near_clusters,
            "canonical": bundle.dedup.canonical,
        },
    }
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, sort_keys=True))
    tmp.replace(SNAPSHOT)


def _parse_bundle(path: Path) -> Bundle | None:
    try:
        data = json.loads(path.read_text())
        items = [SourceItem(**d) for d in data["items"]]
        graph = EventGraph(**data["graph"])
        dd = data.get("dedup", {})
        dedup = DedupResult(
            exact_groups=dd.get("exact_groups", []),
            near_clusters=dd.get("near_clusters", []),
            canonical=dd.get("canonical", {}),
        )
        return Bundle(
            items=items,
            dedup=dedup,
            graph=graph,
            timeline=data.get("timeline", []),
            stats=data.get("stats", {}),
            review_queue=data.get("review_queue", []),
            item_topics=data.get("item_topics", {}),
        )
    except Exception:
        log.warning("Failed to parse bundle at %s", path, exc_info=True)
        return None


def load_bundle() -> Bundle | None:
    global LAST_SOURCE
    if DATA_BUNDLE.exists():
        bundle = _parse_bundle(DATA_BUNDLE)
        if bundle is not None:
            log.info("Loaded bundle from %s", DATA_BUNDLE)
            LAST_SOURCE = "data_dir"
            return bundle
        log.warning("bundle.json in DATA_DIR corrupt — falling back to .cache/")

    if SNAPSHOT.exists():
        bundle = _parse_bundle(SNAPSHOT)
        if bundle is not None:
            log.info("Loaded bundle from local cache: %s", SNAPSHOT)
            LAST_SOURCE = "local_cache"
            return bundle
        log.warning("Local cache bundle corrupt — will rebuild from corpus")

    return None
