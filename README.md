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

Corpus: **1,482 items** — 1,139 WhatsApp, 296 calendar rows, 40 emails, 7 PDFs.

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

### Backend (default port 8000)

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # optional; only needed to re-run LLM extraction
uvicorn app.api:app --host 127.0.0.1 --port 8000
```

### Frontend (default port 3000)

```bash
cd frontend
npm install
npm run dev                   # reads NEXT_PUBLIC_API_BASE, defaults to http://localhost:8000
```

### Tests

```bash
source .venv/bin/activate
python -m pytest -q          # ingestion + dedup spine
```

---

## LLM configuration (optional)

The default `.cache/bundle.json` was built from the redacted corpus, so the app runs
with **no credentials**. To re-run extraction on new data, set a key in `.env`
(`LLM_API_KEY=sk-ant-...`) or the environment. With no key, the pipeline falls back to
the deterministic heuristic path (`MockLLM`). Ingesting raw PDFs additionally needs
`LLAMA_CLOUD_API_KEY`.

- **Extraction:** `claude-sonnet-4-6` (batched + cached in `.cache/llm/`)
- **NL query:** `claude-opus-4-8`

## Data

The archive is the real, unredacted export from the event. It is served behind a site
password and never committed to this repo. Raw files live on the Railway volume under
`$DATA_DIR/raw/`; the built graph is at `$DATA_DIR/bundle.json`.
