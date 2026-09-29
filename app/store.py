"""Durable snapshot of the built graph.

Saves the whole Bundle (items + graph + timeline + stats) to disk after every
ingest and reloads it on startup — so a restart never loses your data and never
re-runs (or re-charges for) the LLM. Complements the per-batch extraction cache:
that avoids re-calling the model; this avoids re-uploading and re-building at all.

A JSON file is the pragmatic store for a prototype; Postgres/Neo4j is the later step.

Load priority on startup:
  1. data/bundle.json  — committed redacted bundle; works with no API key
  2. .cache/bundle.json — local cache written after a real ingest
  3. rebuild from processed_data (needs LLM key)
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from .graph_model import EventGraph
from .ingest.dedup import DedupResult
from .pipeline.build import Bundle
from .schema import SourceItem

log = logging.getLogger(__name__)

_ROOT = Path(__file__).resolve().parents[1]
# Committed redacted bundle — always present on a fresh clone.
DATA_BUNDLE = _ROOT / "data" / "bundle.json"
# Local cache written after a real ingest — takes priority over the committed bundle
# so a user's own data is served after they upload.
SNAPSHOT = _ROOT / ".cache" / "bundle.json"


def save_bundle(bundle: Bundle) -> None:
    SNAPSHOT.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "items": [it.model_dump(mode="json") for it in bundle.items],
        "graph": bundle.graph.model_dump(mode="json"),
        "timeline": bundle.timeline,
        "stats": bundle.stats,
        "dedup": {
            "exact_groups": bundle.dedup.exact_groups,
            "near_clusters": bundle.dedup.near_clusters,
            "canonical": bundle.dedup.canonical,
        },
    }
    tmp = SNAPSHOT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(SNAPSHOT)  # atomic


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
        return Bundle(items=items, dedup=dedup, graph=graph,
                      timeline=data["timeline"], stats=data["stats"])
    except Exception:
        log.warning("Failed to parse bundle at %s", path, exc_info=True)
        return None


def load_bundle() -> Bundle | None:
    """Load the bundle from the best available source, logging which was used."""
    if SNAPSHOT.exists():
        bundle = _parse_bundle(SNAPSHOT)
        if bundle is not None:
            log.info("Loaded bundle from local cache: %s", SNAPSHOT)
            return bundle
        log.warning("Local cache bundle corrupt — falling back to data/bundle.json")

    if DATA_BUNDLE.exists():
        bundle = _parse_bundle(DATA_BUNDLE)
        if bundle is not None:
            log.info("Loaded committed bundle from %s (no API key needed)", DATA_BUNDLE)
            return bundle
        log.warning("data/bundle.json corrupt — will rebuild from corpus")

    return None  # caller will rebuild from processed_data
