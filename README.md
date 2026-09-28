# EventGraph — a queryable knowledge graph from a messy multi-source archive

Reconstructs a **queryable temporal knowledge graph of one real event** (an accepted
paper's journey from acceptance → conference presentation) out of a noisy, redundant,
multi-format personal archive — email, WhatsApp, a spreadsheet-style calendar, and
ticket/visa/receipt PDFs. Every fact is grounded back to its source span.

The hard problem is not "embed everything and retrieve." It is turning dirty input into
a clean graph: **relevance scoping + cross-source entity resolution + dedup** — e.g. the
trip owner surfaces as `R0h@n`, `Rohan Menezes`, `Menezes Rohan` and **four** email
addresses, and the same registration email exists both as a standalone `.eml` and inside
a Gmail mbox. See [decisions.md](decisions.md) for the full design rationale.

Corpus (redacted): **1,482 items** — 1,139 WhatsApp, 296 calendar rows, 40 emails, 7 PDFs.

---

## Layout

```
app/                 FastAPI backend (importable as `app.api:app`)
  ingest/            deterministic parsing + exact/near dedup (pre-LLM)
  pipeline/          relevance, entity extraction, ER, relations, timeline, currency
  llm/               Anthropic client + cached batch extraction (MockLLM fallback)
  queries.py         5 canned cross-source queries + source citations
  query_llm.py       free-form NL question answering over the graph
  store.py           JSON snapshot persistence (.cache/bundle.json)
frontend/            Next.js UI (corpus / graph / timeline / queries / ingest views)
processed_data/      redacted source batches (seed data, loaded on first boot)
tests/               ingestion + dedup unit tests
scripts/             ingest_stats.py CLI
.cache/bundle.json   prebuilt graph snapshot — boots instantly, no LLM needed
```

## Storage

Prototype persistence is a JSON snapshot at `.cache/bundle.json` (rebuilt from
`processed_data/` if absent). Postgres + Neo4j and an auth-gated deploy are planned
(decision D5), not yet implemented.

---

## Run it

This is a self-contained project. It ships with a prebuilt graph, so the backend serves
answers immediately — **no API key required** for the default (already-built) bundle.

### Backend

```bash
cd knowledge-graph
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional; only needed to re-run LLM extraction
uvicorn app.api:app --host 127.0.0.1 --port 8021
```

### Frontend

```bash
cd frontend
npm install
NEXT_PUBLIC_API_BASE=http://localhost:8021 npm run dev -- --port 3002
```

> **Port note.** A copy of this app may already be running from the original
> `../eventgraph` on **3001** (frontend) / **8020** (backend). This standalone copy uses
> **8021 / 3002** above to avoid a clash — change them freely.

### Tests

```bash
source .venv/bin/activate
python -m pytest -q          # ingestion + dedup spine
```

---

## LLM configuration (optional)

The default `.cache/bundle.json` was built from the redacted corpus, so the app runs
with **no credentials**. To re-run extraction on new data, set a key in `.env`
(`LLM_API_KEY=sk-ant-...`). If unset, the key is read from `../backend/.env` when present;
with no key anywhere, the pipeline falls back to the deterministic heuristic path
(`MockLLM`). Ingesting raw PDFs additionally needs `LLAMA_CLOUD_API_KEY`.

- **Extraction:** `claude-sonnet-4-6` (batched + cached in `.cache/llm/`)
- **NL query:** `claude-opus-4-8`

## Privacy

The archive is real and redacted. Files holding un-redacted identities
(`*.private.md`, `ENTITY_TRANSITION_MAP.txt`, `bundle.real.local.json`, `.cache.backup/`)
are intentionally **not** part of this project and are gitignored. Only redacted
`processed_data/` and the redacted `.cache/bundle.json` are included.
