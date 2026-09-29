#!/usr/bin/env python3
"""Build the knowledge graph from a corpus directory using LLM extraction.

    python -m scripts.build_graph --dry-run
    python -m scripts.build_graph --dry-run --input processed_data
    python -m scripts.build_graph --confirm --out .cache/bundle.json

Flags:
  --input DIR       Corpus directory (default: processed_data).
  --out PATH        Output bundle path (default: .cache/bundle.json).
  --dry-run         Print chunk count + estimated token usage + cache hits.
                    No LLM calls are made.
  --confirm         Required before any uncached paid LLM call is made.
                    Without --confirm, the script stops after the dry-run
                    estimate and asks you to re-run with --confirm.
  --model MODEL     Override LLM_EXTRACT_MODEL for this run.
  --concurrency N   Thread concurrency for LLM calls (default 5).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

CORPUS_DEFAULT = ROOT / "processed_data"
BUNDLE_DEFAULT = ROOT / ".cache" / "bundle.json"

# Honour DATA_DIR for the LLM cache (same logic as app/llm/extract.py).
import os as _os
_DATA_DIR = _os.getenv("DATA_DIR")
CACHE_DIR = Path(_DATA_DIR) / "cache" / "llm" if _DATA_DIR else ROOT / ".cache" / "llm"

# Approximate output tokens per chunk (mentions + relations + topics).
_EST_OUTPUT_TOKENS_PER_CHUNK = 800
# Claude Haiku-4-5 pricing (as of 2025, per million tokens).
_INPUT_PRICE_PER_MT = 0.80
_OUTPUT_PRICE_PER_MT = 4.00


def load_items(input_dir: Path):
    """Load SourceItems from a directory.

    Accepts two layouts:
    - Legacy batch*.md files (the redacted markdown format used in processed_data/)
    - Native exports: .mbox, .eml, .txt, .csv, .tsv, .xlsx, .pdf (raw/ on the volume)

    Native files are parsed with the same parse_upload() used by the upload endpoint,
    so the pipeline is identical to what a user experiences when replaying the upload.
    """
    from app.ingest.markdown import parse_batch_file
    from app.ingest.adapters import parse_upload, UnsupportedUpload

    items = []

    batch_files = sorted(input_dir.glob("batch*.md"))
    if batch_files:
        for f in batch_files:
            items += parse_batch_file(f)
        return items

    # Native export files
    native_exts = {".mbox", ".eml", ".txt", ".csv", ".tsv", ".xlsx", ".pdf"}
    native_files = sorted(f for f in input_dir.iterdir()
                          if f.is_file() and f.suffix.lower() in native_exts)
    for f in native_files:
        try:
            parsed, fmt = parse_upload(f.name, f.read_bytes())
            items += parsed
            print(f"  {f.name}: {len(parsed)} items ({fmt})")
        except UnsupportedUpload as e:
            print(f"  {f.name}: skipped — {e}", file=sys.stderr)
        except Exception as e:
            print(f"  {f.name}: error — {e}", file=sys.stderr)

    return items


def count_cache_hits(chunks, model: str) -> int:
    from app.llm.extract import _chunk_cache_key
    hits = 0
    for chunk in chunks:
        key = _chunk_cache_key(chunk, model)
        if (CACHE_DIR / f"{key}.json").exists():
            hits += 1
    return hits


def dry_run(items, model: str) -> None:
    from app.llm.chunking import chunk_items

    print(f"\n{'='*60}")
    print(f"DRY-RUN — no LLM calls will be made")
    print(f"{'='*60}")
    print(f"Items loaded:     {len(items)}")

    # Filter canonicals for chunking (same as build_graph does).
    from app.ingest.dedup import find_duplicates
    from app.pipeline.build import _mark_duplicates
    from app.ingest.participants import mark_shared_mailboxes
    mark_shared_mailboxes(items)
    dedup = find_duplicates(items)
    _mark_duplicates(items, dedup)
    canon_items = [it for it in items if not it.duplicate_of]

    print(f"Canonical items:  {len(canon_items)}")
    print(f"Duplicates:       {len(items) - len(canon_items)}")

    chunks = chunk_items(canon_items)
    total_input_tokens = sum(c.estimated_input_tokens for c in chunks)
    total_output_tokens = _EST_OUTPUT_TOKENS_PER_CHUNK * len(chunks)
    cache_hits = count_cache_hits(chunks, model)
    uncached = len(chunks) - cache_hits

    print(f"\nChunk stats:")
    print(f"  Total chunks:     {len(chunks)}")
    print(f"  Cache hits:       {cache_hits}")
    print(f"  Uncached chunks:  {uncached}")
    print(f"\nToken estimates (uncached only):")
    from app.llm.extract import _chunk_cache_key
    uncached_input = sum(
        c.estimated_input_tokens for c in chunks
        if not (CACHE_DIR / f"{_chunk_cache_key(c, model)}.json").exists()
    )
    print(f"  Est. input tokens:  {total_input_tokens:,}  (all chunks)")
    print(f"  Est. input tokens:  {uncached_input:,}  (uncached only)")
    print(f"  Est. output tokens: {uncached * _EST_OUTPUT_TOKENS_PER_CHUNK:,}  (uncached only)")
    input_cost = (uncached_input / 1_000_000) * _INPUT_PRICE_PER_MT
    output_cost = (uncached * _EST_OUTPUT_TOKENS_PER_CHUNK / 1_000_000) * _OUTPUT_PRICE_PER_MT
    total_cost = input_cost + output_cost
    print(f"\nEstimated cost ({model}):")
    print(f"  Input:  ${input_cost:.4f}")
    print(f"  Output: ${output_cost:.4f}")
    print(f"  Total:  ${total_cost:.4f}")
    print(f"\n{'='*60}")
    if uncached > 0:
        print(f"Re-run with --confirm to execute {uncached} uncached chunk(s).")
    else:
        print("All chunks are cached — re-run with --confirm to rebuild the bundle from cache.")
    print(f"{'='*60}\n")


def run_full(items, model: str, out_path: Path, concurrency: int) -> None:
    from app.pipeline.build import build_graph
    import os
    os.environ["LLM_EXTRACT_MODEL"] = model

    print(f"Building graph (LLM mode, model={model})...")
    bundle = build_graph(items, mode="llm")

    print(f"\nGraph built:")
    for k, v in bundle.stats.items():
        print(f"  {k}: {v}")

    out_path.parent.mkdir(parents=True, exist_ok=True)

    # Serialize in the store format the app loads (items + nested graph + dedup),
    # so the built bundle can be served directly by app/store.py::load_bundle.
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
    out_path.write_text(json.dumps(data, default=str))
    print(f"\nBundle written to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Build the knowledge graph from corpus.")
    parser.add_argument("--input", type=Path, default=CORPUS_DEFAULT,
                        help="Corpus directory (default: processed_data)")
    parser.add_argument("--out", type=Path, default=BUNDLE_DEFAULT,
                        help="Output bundle path")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print token estimate; no LLM calls.")
    parser.add_argument("--confirm", action="store_true",
                        help="Required to execute uncached LLM calls.")
    parser.add_argument("--model", type=str, default=None,
                        help="Override LLM_EXTRACT_MODEL")
    parser.add_argument("--concurrency", type=int, default=5,
                        help="Thread concurrency (default 5)")
    args = parser.parse_args()

    import os
    model = args.model or os.getenv("LLM_EXTRACT_MODEL") or os.getenv("LLM_MODEL") or "claude-haiku-4-5"

    if not args.input.exists():
        print(f"ERROR: input directory not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    items = load_items(args.input)
    if not items:
        print(f"ERROR: no batch*.md files found in {args.input}", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(items)} items from {args.input}")

    if args.dry_run or not args.confirm:
        dry_run(items, model)
        if not args.confirm:
            return  # stop here; user must re-run with --confirm

    if args.confirm:
        run_full(items, model, args.out, args.concurrency)


if __name__ == "__main__":
    main()
