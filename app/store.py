"""Durable snapshot of the built graph.

Saves the whole Bundle (items + graph + timeline + stats) to disk after every
ingest and reloads it on startup — so a restart never loses your data and never
re-runs (or re-charges for) the LLM. Complements the per-batch extraction cache:
that avoids re-calling the model; this avoids re-uploading and re-building at all.

A JSON file is the pragmatic store for a prototype; Postgres/Neo4j is the later step.
"""
from __future__ import annotations

import json
from pathlib import Path

from .graph_model import EventGraph
from .ingest.dedup import DedupResult
from .pipeline.build import Bundle
from .schema import SourceItem

SNAPSHOT = Path(__file__).resolve().parents[1] / ".cache" / "bundle.json"


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


def load_bundle() -> Bundle | None:
    if not SNAPSHOT.exists():
        return None
    try:
        data = json.loads(SNAPSHOT.read_text())
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
        return None  # corrupt/old snapshot — fall back to a fresh build
