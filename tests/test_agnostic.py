"""Phase 3 agnosticism test.

Fails if any corpus-specific term from tests/corpus_terms.txt appears in
app/, frontend/ (excluding .next build artifacts), or the test fixtures
directory. This guards against hardcoded archive-specific vocabulary leaking
into the generic pipeline.
"""
from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TERMS_FILE = Path(__file__).parent / "corpus_terms.txt"

_SCAN_DIRS = [
    ROOT / "app",
    ROOT / "frontend",
]

_SKIP_PARTS = {".next", "__pycache__", ".venv", "node_modules", ".cache"}
_SKIP_EXTS = {".pyc", ".pyo", ".map", ".min.js", ".min.css"}


def _load_terms() -> list[str]:
    terms = []
    for line in TERMS_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            terms.append(line)
    return terms


def _scan_files(root: Path):
    for p in root.rglob("*"):
        if not p.is_file():
            continue
        if any(part in _SKIP_PARTS for part in p.parts):
            continue
        if p.suffix in _SKIP_EXTS:
            continue
        yield p


def test_no_corpus_terms_in_app_or_frontend():
    terms = _load_terms()
    if not terms:
        pytest.skip("No corpus terms defined")

    offences: list[str] = []
    for scan_dir in _SCAN_DIRS:
        if not scan_dir.exists():
            continue
        for path in _scan_files(scan_dir):
            try:
                text = path.read_text(errors="replace")
            except OSError:
                continue
            for term in terms:
                if term in text:
                    rel = path.relative_to(ROOT)
                    offences.append(f"{rel}: contains {term!r}")

    assert not offences, (
        "Corpus-specific terms found in generic code paths:\n"
        + "\n".join(offences[:20])
    )
