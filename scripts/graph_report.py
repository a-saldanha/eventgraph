#!/usr/bin/env python3
"""Structured report on a built graph: entity counts, entity-resolution quality
signals, duplicate label groups, and connectivity. Reads a saved bundle or builds
one fresh from a corpus directory.

    python -m scripts.graph_report                      # saved .cache/bundle.json
    python -m scripts.graph_report --build              # fresh heuristic build
    python -m scripts.graph_report --build --input processed_data --json

The report is a diagnostic used across phases, so it deliberately does not filter
out `unknown` people or chat-title "correspondents": those artifacts are what the
resolution work is meant to remove, and the report is how we watch that happen.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

_TYPES = ["person", "org", "location", "money", "document", "topic", "subevent"]


def _norm_label(s: str) -> str:
    """Case-fold and strip punctuation so near-identical labels collapse to one key."""
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def load_saved() -> Optional[dict]:
    from app.store import SNAPSHOT

    if not SNAPSHOT.exists():
        return None
    return json.loads(SNAPSHOT.read_text())


def build_fresh(input_dir: Path) -> dict:
    from app.ingest.markdown import parse_batch_file
    from app.pipeline.build import build_graph

    items = []
    for f in sorted(input_dir.glob("batch*.md")):
        items += parse_batch_file(f)
    if not items:
        raise SystemExit(f"no batch*.md files found under {input_dir}")
    b = build_graph(items, mode="heuristic")
    return {
        "items": [it.model_dump(mode="json") for it in b.items],
        "graph": b.graph.model_dump(mode="json"),
        "timeline": b.timeline,
        "stats": b.stats,
        "dedup": {
            "exact_groups": b.dedup.exact_groups,
            "near_clusters": b.dedup.near_clusters,
            "canonical": b.dedup.canonical,
        },
    }


def analyze(bundle: dict) -> dict[str, Any]:
    items = bundle.get("items", [])
    graph = bundle.get("graph", {})
    ents = graph.get("entities", [])
    edges = graph.get("edges", [])
    merges = graph.get("merges", [])
    review = graph.get("review_queue") or bundle.get("review_queue") or []
    dedup = bundle.get("dedup", {})
    stats = bundle.get("stats", {})

    src_by_item = {it["id"]: it.get("source_type") for it in items}
    n_exact, n_near = stats.get("exact_dupes"), stats.get("near_dupes")
    canonical = dedup.get("canonical", {})
    if canonical:
        unique = len({canonical.get(it["id"], it["id"]) for it in items})
    elif n_exact is not None and n_near is not None:
        unique = len(items) - n_exact - n_near
    else:
        unique = len(items)

    def mentions(e: dict) -> int:
        return len(e.get("mentions") or [])

    def channels(e: dict) -> list[str]:
        got = {src_by_item.get(m.get("item_id")) for m in (e.get("mentions") or [])}
        return sorted(c for c in got if c)

    def is_unknown(e: dict) -> bool:
        return (e.get("label") or "").strip().lower() in ("", "unknown")

    persons = [e for e in ents if e.get("type") == "person"]
    unknown_persons = [e for e in persons if is_unknown(e)]

    # owner: an entity explicitly flagged as owner (added in a later phase), else
    # the most-mentioned person as a heuristic stand-in.
    owner = next((e for e in persons if (e.get("attrs") or {}).get("owner")), None)
    owner_inferred = owner is None
    if owner is None and persons:
        owner = max(persons, key=mentions)

    correspondents = sorted(
        (e for e in persons if e is not owner), key=mentions, reverse=True
    )[:10]

    degree: Counter = Counter()
    for e in edges:
        degree[e["source"]] += 1
        degree[e["target"]] += 1
    isolated = [e for e in ents if degree.get(e["id"], 0) == 0]

    def dup_label_groups(entity_type: str) -> dict[str, list[str]]:
        groups: dict[str, list[str]] = defaultdict(list)
        for e in ents:
            if e.get("type") == entity_type:
                groups[_norm_label(e.get("label"))].append(e.get("label"))
        return {k: sorted(set(v)) for k, v in groups.items() if len(set(v)) > 1}

    return {
        "mode": stats.get("mode"),
        "items": len(items),
        "unique_after_dedup": unique,
        "exact_dupes": n_exact,
        "near_dupes": n_near,
        "entity_counts": {t: sum(1 for e in ents if e.get("type") == t) for t in _TYPES},
        "entities_total": len(ents),
        "persons_total": len(persons),
        "persons_unknown_or_empty": len(unknown_persons),
        "owner": {
            "label": owner.get("label") if owner else None,
            "mentions": mentions(owner) if owner else 0,
            "channels": channels(owner) if owner else [],
            "inferred_heuristically": owner_inferred,
        },
        "top_correspondents": [
            {"label": e.get("label"), "mentions": mentions(e), "channels": channels(e)}
            for e in correspondents
        ],
        "org_label_dup_groups": dup_label_groups("org"),
        "location_label_dup_groups": dup_label_groups("location"),
        "isolated_entities": len(isolated),
        "isolated_pct": round(100 * len(isolated) / len(ents), 1) if ents else 0.0,
        "merges": len(merges),
        "review_queue": len(review),
    }


def render_text(r: dict[str, Any]) -> str:
    L = []
    L.append(f"mode: {r['mode']}")
    L.append(f"items: {r['items']}  unique after dedup: {r['unique_after_dedup']} "
             f"(exact {r['exact_dupes']}, near {r['near_dupes']})")
    L.append("")
    L.append("entities by type: " + ", ".join(f"{t}={n}" for t, n in r["entity_counts"].items()))
    L.append(f"persons: {r['persons_total']}  unknown/empty: {r['persons_unknown_or_empty']}")
    o = r["owner"]
    tag = " (heuristic: most-mentioned person)" if o["inferred_heuristically"] else ""
    L.append(f"owner{tag}: {o['label']!r}  mentions={o['mentions']}  channels={o['channels']}")
    L.append("")
    L.append("top correspondents (person, by mentions):")
    for c in r["top_correspondents"]:
        L.append(f"  {c['mentions']:>5}  {c['label']!r}  {c['channels']}")
    L.append("")
    L.append(f"duplicate ORG label groups: {len(r['org_label_dup_groups'])}")
    for k, v in r["org_label_dup_groups"].items():
        L.append(f"  [{k}] {v}")
    L.append(f"duplicate LOCATION label groups: {len(r['location_label_dup_groups'])}")
    for k, v in r["location_label_dup_groups"].items():
        L.append(f"  [{k}] {v}")
    L.append("")
    L.append(f"isolated entities: {r['isolated_entities']} / {r['entities_total']} "
             f"({r['isolated_pct']}%)")
    L.append(f"merges: {r['merges']}   review queue: {r['review_queue']}")
    return "\n".join(L)


def main() -> None:
    ap = argparse.ArgumentParser(description="Report on a built EventGraph bundle.")
    ap.add_argument("--build", action="store_true", help="build fresh instead of loading the saved bundle")
    ap.add_argument("--input", type=Path, default=ROOT / "processed_data", help="corpus dir for --build")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of text")
    args = ap.parse_args()

    bundle = build_fresh(args.input) if args.build else load_saved()
    if bundle is None:
        raise SystemExit("no saved bundle found; pass --build to build one")

    result = analyze(bundle)
    print(json.dumps(result, indent=2) if args.json else render_text(result))


if __name__ == "__main__":
    main()
