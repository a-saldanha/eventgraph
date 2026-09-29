# EventGraph — status & pending work

_Snapshot of what's done, what's left, and how close we are to a deployed submission._
Companion to `docs/rebuild_tasks.md` (the full per-phase playbook). Run everything from
`knowledge-graph/` with `.venv/bin/python`.

## Done (committed, 44 tests green, demo live locally on :8000 / :3000)

| Phase | What shipped |
|---|---|
| 0 | Baseline report (`scripts/graph_report.py`), hygiene, one project name, ports, tz fix |
| 1 | Typed `Participant` model + parsers (email/WhatsApp/pdf/csv), placeholder/role/group classification, phone E.164 |
| 2 | Identity must-links, structural owner inference, dedup applied over canonicals |
| 3 | Chunked LLM extraction with verbatim-surface verification, `build_graph.py`/`check_llm.py` CLIs |
| 4 | Profiles → blocking → per-block LLM resolution + review queue |
| 5 | Query-time scoping via topics, parameterized query types, `/api/ask` 503; deleted `relevance.py` (last stale pseudonyms) |
| +fix | Deterministic name-collapse (fixed the 1,557→27 people sprawl) + restored `affiliated_with` |

**Current graph:** 111 entities · 48 edges (10 relation types) · 27 people · owner unified · reproducible from `build_graph.py`.

---

## Deployment readiness: ~65%

The hard part (the ER pipeline) is done and the app runs. What's missing is **packaging
for a public deploy** — the brief's deliverable is a live URL.

| Requirement for a deployed URL | Status | Phase |
|---|---|---|
| Working app + clean graph | ✅ done | 1–5 |
| **Committed bundle a fresh clone can serve with no API key** | ❌ `.cache/bundle.json` is gitignored → clone rebuilds (needs key) | 6 |
| Read-only demo mode (uploads don't clobber the shared graph) | ❌ single global graph, persisted | 8 |
| Rate limiting + spend guard on `/api/ask` | ❌ open/unmetered | 8 |
| Health endpoint, CORS from env | ❌ | 8 |
| Dockerfile + one-command run + deploy config | ❌ no deploy files | 8 |
| Hosted URL (backend host + Vercel) | ❌ needs your accounts | 8 |
| Frontend loading/error/empty states | ⚠️ partial | 8/9 |

**Critical path to a live URL (smallest set):** commit a redacted `data/bundle.json`
(6-lite) → `DEMO_READONLY` + rate limit + health + Dockerfile (8) → deploy (needs your
Vercel/Railway/Render accounts). Estimate: **~2 focused sessions** to a working
read-only public demo. Full submission polish (eval, UX, docs) is another 2–3.

---

## Pending tasks (detailed)

Deploy-critical items are marked 🚀. Do those first if the goal is a live URL.

### 🚀 Phase 6-lite — commit a servable bundle (do this first)
The app must serve with **no API key** on a fresh clone. Today `.cache/bundle.json` is
gitignored, so a clone rebuilds (and needs a key).
- Create `data/`. Write the current good bundle to `data/bundle.json` (reuse
  `build_graph.py`'s store serializer, or `cp .cache/bundle.json data/bundle.json`).
- Edit `app/store.py`: load `data/bundle.json` first, then fall back to `.cache`, then
  rebuild; log which was used.
- Also write `data/corpus.redacted.jsonl` (the canonical `SourceItem`s) for
  reproducibility. Stop gitignoring `data/`; keep `.cache/` ignored.
- **Verify:** `git stash` the key, fresh `.venv`, `uvicorn app.api:app` serves the graph.
- **Commit:** "Phase 6-lite: commit servable redacted bundle under data/".

_Full Phase 6 (graph-driven redaction from `raw/` + leak-check + invariance test) is only
needed to regenerate the redacted corpus from scratch; the committed redacted data already
satisfies the privacy policy, so it can come later._

### 🚀 Phase 8 — deploy, sessions, robustness
- **State:** `DEMO_READONLY=1` serves the committed bundle read-only. Uploads build a
  **session-scoped** in-memory bundle (cookie session id, LRU + TTL, never persisted,
  never replaces the demo). Currency resolutions per session.
- **Limits:** `/api/ask` per-IP + global in-memory token-bucket rate limit, max question
  length, daily call cap → clear 429. CORS origins from env.
- **Health:** `GET /api/health` → bundle source, counts, llm availability.
- **Package:** `Dockerfile` (backend), frontend build config, `Makefile`
  (`setup dev test build-graph eval`), one-command `make dev`; `API_BASE` from env only.
- **Deploy notes:** backend on a container host (Railway/Render/Fly), frontend on Vercel.
  **Stop and ask the user** before anything needing their accounts/secrets.
- **Tests:** read-only rejects demo writes; two sessions don't cross-contaminate; rate
  limit → 429.
- **Commit:** "Phase 8: session state, rate limits, health, deploy scaffolding".

### Phase 7 — evaluation + agnosticism proofs (scoring, not deploy-blocking)
- `scripts/eval_er.py` + `tests/gold/entities.template.yaml` (pairwise P/R/F1 vs a small
  gold set; missing file → say so).
- `tests/gold/dedup.yaml` from the markdown `notes`; a test that fails if any `app/`
  module reads `notes`.
- `tests/fixtures/holdout/` — a small invented archive (different domain/people) +
  recorded `FakeLLM`; full pipeline runs with no code changes and asserts clusters.
- `tests/test_agnostic.py` + `tests/corpus_terms.txt` — fail if any real archive term
  appears in `app/`/`frontend/`/prompts. (`app/` is already clean; this locks it.)
- README results table generated by a script.
- **Commit:** "Phase 7: ER/dedup eval, hold-out archive, agnosticism test".

### Phase 9 — UX
- Replace `OverviewView` with a first-answer screen (run `money` over the top topic:
  line items + citations + flagged amounts resolvable inline + link into the graph; one
  sentence on what the tool is; no hero/emoji/roadmap/vanity metrics).
- Resolution tab: surfaces + evidence + confidence per merge; review-queue confirm/reject
  (session-scoped); eval/leak numbers with one-line explanations.
- Graph: hide isolated nodes behind a toggle with a count; entity search; click → evidence.
- Queries: starter questions from the bundle; `/ask` errors show the API message.
- Empty states name working file types.
- **Commit:** "Phase 9: answer-first UX, review queue, graph isolate toggle".

### Phase 10 — documentation
- Rewrite `README.md` (what it is in 2–3 sentences; live URL; one-command setup;
  build-from-your-own-exports flow with `--dry-run`/`--confirm`; one Mermaid diagram +
  a paragraph; the Phase-7 results table; privacy policy in plain words; known limits).
- Restructure `decisions.md` per the brief's decision list + "Mistakes" + "Not built".
- Delete `docs/baseline.md` if folded in.
- **Commit:** "Phase 10: README + decisions.md rewrite".

### Optional — Sonnet extraction pass (richer edges)
Current graph used cached **Haiku** extraction (48 edges). A **Sonnet** extraction pass
(`LLM_EXTRACT_MODEL=claude-sonnet-4-6`, ~$1–2, a few min, then rebuild) surfaces more
relations → denser edges. Not required; the graph is already clean and connected.

---

## Suggested order
1. 🚀 Phase 6-lite (servable bundle) — unblocks any deploy.
2. 🚀 Phase 8 (deploy scaffolding + read-only + limits) → then deploy with your accounts.
3. Phase 10 (docs) — needed for the submission.
4. Phase 7 (eval/agnosticism) — strengthens grading.
5. Phase 9 (UX polish) — last.
6. Optional Sonnet extraction pass anytime.
