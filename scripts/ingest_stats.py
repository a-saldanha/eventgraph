#!/usr/bin/env python3
"""Parse the redacted batches and print an ingestion report.

Usage:  python -m scripts.ingest_stats [../processed_data]
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.ingest.dedup import find_duplicates
from app.ingest.markdown import parse_batch_file


def main() -> None:
    data_dir = Path(sys.argv[1] if len(sys.argv) > 1 else "../processed_data")
    items = []
    for f in sorted(data_dir.glob("batch*.md")):
        items += parse_batch_file(f)

    print(f"Parsed {len(items)} items from {data_dir}")
    print("  by source_type:", dict(Counter(i.source_type.value for i in items)))
    print("  with timestamp:", sum(1 for i in items if i.timestamp))
    print("  conversations :", len({i.conversation_id for i in items}))

    res = find_duplicates(items)
    print(f"\nDedup:")
    print(f"  exact groups : {len(res.exact_groups)}  (redundant copies {res.n_exact_dupes})")
    print(f"  near clusters: {len(res.near_clusters)}  (redundant copies {res.n_near_dupes})")
    unique = len(items) - res.n_exact_dupes - res.n_near_dupes
    print(f"  ~unique items after dedup: {unique}")


if __name__ == "__main__":
    main()
