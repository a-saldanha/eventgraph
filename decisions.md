# decisions.md

## Brief

**Problem.** A queryable knowledge graph of one real event — an accepted paper's
journey from acceptance to conference presentation — reconstructed from a noisy,
multi-source, multi-format personal archive (email, WhatsApp, an Excel-style
calendar, ticket/visa/receipt PDFs). For someone drowning in scattered
correspondence who needs to ask "what did this cost / what did I have to do / what's
the status of X" and get an answer they can trust back to the source.

**The hard part.** The archive is noisy, redundant, and heterogeneous. The same
real-world entity surfaces across channels in different forms (the trip's owner
appears as `R0h@n`, `Rohan Menezes`, `Menezes Rohan`, and **four** email addresses),
and the same fact is duplicated (a registration email exists both as a standalone
`.eml` and inside the Gmail mbox). Meanwhile most of the corpus is *not* about this
event (other work, group chatter, illness). A naive "embed everything + vector
retrieve" pipeline double-counts duplicates, fragments entities, and lets noise
corrupt answers. The interesting work is turning dirty input into a clean event
graph: **relevance scoping + cross-source entity resolution + dedup**, with every
fact grounded to its source.

**The slice we ship.** Ingest all sources → normalize to `SourceItem` (raw
retained) → dedup → distill conversations into provenance-preserving units →
extract a temporal entity/relation graph → answer a fixed set of cross-source
**join / aggregation / timeline** questions in an auth-gated web app that shows the
answer, the contributing sub-graph, and the exact source span behind each fact.

**One failure handled.** Duplicate / forwarded / noisy input: the "trip cost" query
a forwarded invoice would inflate, and the "visa timeline" query with illness
messages interleaved.

**Why this instead of a fixed prompt.** It is a harder reading of prompt #1 (messy →
queryable) where the mess is *multi-source and adversarially noisy*, forcing entity
resolution + relevance scoping + cross-channel joins that a single-document
extractor or flat RAG structurally cannot do.

The corpus is real (mine), redacted for privacy (see D2); at message grain it is
**1,482 items** (1,139 WhatsApp, 296 calendar rows, 40 emails, 7 PDFs).

---

## Decisions

### D1 — Evaluate NVIDIA `context-aware-rag`; borrow concepts, reject wholesale
**Chose:** build a focused, lightweight pipeline; take only ideas from NVIDIA's repo
(Neo4j as graph store; ingestion/retrieval separation as a *concept*; structured
graph-extraction prompting; optional OpenTelemetry).
**Alternatives:** adopt the blueprint as-is (its GraphRAG + services).
**Reasoning:** it's the VSS (video) context-aware RAG generalized — 8+ services
(Neo4j + Milvus + Elasticsearch + Kibana + OTel + Phoenix + Prometheus + 2 app
services), GPU-oriented, with *streaming/video-shaped* ingestion (`stream_id`,
`chunk_idx`) and *video-timestamp* temporality. Three reasons to reject wholesale:
(1) the round explicitly discourages rebuilding a known reference implementation —
adopting it buries the hard sub-problem; (2) 8 services + GPU is not reliably
deployable/demoable solo in 5 days (breadth, not depth); (3) its generic
entity/relation extraction has **no** notion of relevance scoping or cross-source
entity resolution — the actual hard parts here. Apache-2.0, so borrowing is fine.
**Cut:** Milvus/Elasticsearch/Phoenix/Prometheus/Kibana, GPU serving, microservices.

### D2 — Redact via an LLM pass that preserves the mess
**Chose:** pseudonymize identity in a separate Claude chat, but **retain duplicates,
noise, surface-form variation, typos, and timestamps** (dates shifted by a constant
offset).
**Alternatives:** deterministic local regex scrub; no redaction (auth-gate only).
**Reasoning:** the privacy risk is real (deployed URL), but a redactor that "tidies"
the data would *pre-solve* dedup/relevance/ER — the exact things being graded. So the
redaction prompt is explicit: never merge, filter, or normalize. Deployment is
additionally **auth-gated** (D5).
**Cut:** using the redactor's `notes:` field in the pipeline — it's kept as an **eval
oracle only** (it annotates some duplicates/system-messages), never an input.

### D3 — Compression = distillation over retained raw, not lossy summarization
**Chose:** keep every raw `SourceItem`; "compress" conversations only by **episode
segmentation + structured extraction that carries provenance pointers back to raw**.
**Alternatives:** summarize each conversation into prose at ingestion (the intuitive
"compress at input").
**Reasoning:** the token-blowup concern is real (1,139 WhatsApp messages). But lossy
summarization destroys the three things the project is graded on: provenance (a fact
citing "the summary" isn't grounded), ER signal (summaries normalize `Rohin/Rohan/
R0h@n` away), and relevance signal (summaries drop the noise we must *detect*). Raw
text is tiny (~700 KB total), so we retain all of it and compress only the *working
representation* the graph stages consume. Tradeoff: more storage + a two-layer model
(raw + distilled), accepted because it's the only way to keep calibrated ER and
click-through citations.
**Cut:** in-place mutation/summarization of source items.

### D4 — One `SourceItem` schema; deterministic ingestion before any LLM
**Chose:** every source normalizes to `SourceItem` (raw body, sender/recipients,
timestamp, conversation_id, provenance, content_hash); parsing + exact/near dedup are
**pure deterministic code**, unit-tested, run before the LLM touches anything.
**Reasoning:** keeps the LLM boundary small (cheaper, testable, reproducible) and
makes dedup explainable. Near-dup uses token-shingle Jaccard within source-type
blocks; it already catches a registration email duplicated across the `.eml` export
*and* the Gmail mbox — cross-source duplication, deterministically.
**Cut (for now):** perfect near-dup recall — the `[RESEND]`/`[URGENT]` rebuttal pair
differs enough in framing text to fall below threshold; logged as a known limit,
tuned later or deferred to the ER stage.

### D5 — Storage: Postgres (+pgvector) + Neo4j; auth-gated deploy *(planned)*
**Chose (planned):** Postgres + pgvector for items/provenance/embeddings in one
store; Neo4j for the graph (traversal + visualization); deployment behind auth.
**Alternatives:** three datastores like NVIDIA (Milvus + ES + Neo4j); graph-as-edges
in Postgres only.
**Reasoning:** a bounded single-event graph doesn't justify three datastores; one
relational store covers items/provenance/vector, and Neo4j earns its place only
because the queries are traversal/aggregation-shaped *and* it gives a graph we can
render in the UI (showing the ER/relevance work is half the point). Auth-gating lets
the deployed demo use real (redacted) data safely.
**Cut:** NL-to-SQL/Cypher generation — a deterministic query planner over a fixed
question taxonomy is more predictable and honest for a demo.

---

## Edge cases / notes discovered while building
- Corpus is **1,482 items at message grain**, not the ~50 threads first estimated —
  so the LLM stages must be cost/rate-aware (cheap prefilter, batching, caching,
  backoff). Scale is now a real "handled the real world" dimension.
- The same person carries **4 email addresses** + 3 name forms — genuine ER, not a
  toy.
- WhatsApp export has out-of-order timestamps and invisible bidi marks (U+200E etc.);
  kept in raw, stripped only for hashing/matching.

---

# Rebuild log

The sections above predate the current rebuild and are kept for now; they are
restructured in Phase 10. Entries below are added per phase.

## Phase 0 — baseline and hygiene

### D0.1 — One diagnostic report drives every phase
**Chose:** `scripts/graph_report.py`, reporting entity counts, unknown-person count,
the owner entity, top correspondents, duplicate label groups, and isolated-entity
percentage, over a saved bundle or a fresh build. Baseline saved in `docs/baseline.md`.
**Alternatives:** ad-hoc queries per phase; assertions only in tests.
**Reasoning:** the later phases target measurable ER/dedup defects; a single report run
before and after each phase is how those numbers are tracked without re-deriving them.
**Cut:** nothing.

### D0.2 — Resolve named timezone abbreviations with a fixed map
**Chose:** a `_TZINFOS` map (EET, EEST, CET, IST, US zones, …) passed to dateutil, and
removal of the blanket `warnings.filterwarnings("ignore")` that hid the failure.
**Alternatives:** keep suppressing the warning; store naive datetimes.
**Reasoning:** a header reading `14:22 EET` was parsed to a naive datetime, so ordering
across channels was off by the offset. The map makes those timestamps aware; a numeric
offset in the header is still parsed directly. Ambiguous abbreviations (IST) resolve to
the corpus region.
**Cut:** full IANA abbreviation coverage; only observed zones are mapped.

### D0.3 — One project name, keys and ports from the environment
**Chose:** the name EventGraph across the app; the Anthropic and LlamaParse keys read
from the environment or this project's `.env` only; backend on 8000 and the frontend on
`NEXT_PUBLIC_API_BASE` (default 8000).
**Alternatives:** the previous fallback to a sibling project's `.env`.
**Reasoning:** the fallback coupled the app to an unrelated local directory and leaked
into the docs. Removing it makes the project self-contained.
**Cut:** the sibling `.env` fallback; the one-off `restore_*` scripts and `.backups/`.

### Mistakes and what I changed
- Dead `reasons` dict in `resolve_people` was written and never read; removed.
- `scripts/ingest_stats.py` still probed for pre-rebuild pseudonyms that no longer occur
  in the data; removed that block.
