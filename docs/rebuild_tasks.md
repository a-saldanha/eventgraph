# Rebuild task breakdown (for independent agent sessions)

The rebuild follows the phased brief. This file decomposes the remaining work into
tasks small enough that a fresh agent session can pick up any one of them without the
context of the others.

## How to run a task in a fresh session
Give the agent: (1) the phase brief, (2) this repo, (3) the task block below. Each task
lists its goal, the files it touches, what it depends on, the brief section, and how to
check it's done. Run `.venv/bin/python -m pytest -q` and
`.venv/bin/python -m scripts.graph_report --build` before and after.

**Ground rules for every task** (from the brief §0, §5):
- One phase at a time; stop for approval at a phase boundary.
- No archive-specific strings in `app/` (Phase 7 enforces this). Invented data in tests.
- Never fabricate a metric; if something fails, report it failed.
- Small commits; never `git push`. Update `decisions.md` per phase.
- Ask before spending API credits beyond a one-token model check (paid runs are approved
  by the user for this rebuild, but confirm scope before a full extraction).

Environment: `knowledge-graph/.venv` (uv). Backend :8000, frontend :3000.

---

## Phase 2 — remaining (one coupled session)
Status: `identity.py` and `owner.py` are done and committed. The rest is coupled through
`build.py`, so it is one session, not parallel tasks.

### P2-int — apply dedup + wire identity/owner into people resolution
- **Goal:** (a) mark each item `duplicate_of` and run extraction/counts/money over
  canonical items only, keeping all copies as provenance; (b) make the owner's email
  identity + leet handle + calendar-self collapse into one owner person entity, using
  `app/resolve/identity.py` and `app/resolve/owner.py`.
- **Files:** `app/schema.py` (add `duplicate_of`), `app/pipeline/build.py`,
  `app/pipeline/entities.py`, maybe a small `app/resolve/people.py` bridge.
- **Depends on:** identity.py, owner.py (done).
- **Brief:** Phase 2, tasks 3–4.
- **Done when:** `graph_report --build` shows one owner entity across all channels and
  duplicates no longer inflate counts (unique-after-dedup drives extraction).

### P2-test — Phase 2 tests
- **Goal:** invented-data tests: two emails differing only in case merge; two phone
  formats merge; a role mailbox used by several names does not merge them; owner
  inference picks the right identity in a 3-channel archive with a leet handle and
  returns "uncertain" on a tie; a forwarded invoice (same amounts + a note) does not
  double a money total.
- **Files:** `tests/test_identity.py`, `tests/test_owner.py`, `tests/test_dedup_apply.py`.
- **Depends on:** P2-int.
- **Brief:** Phase 2, task 5.

### P2-demo — verify + rebuild the demo (paid)
- **Goal:** run the full pipeline in `llm` mode to regenerate `.cache/bundle.json`;
  restart backend/frontend; confirm the demo is at least as good as before via
  `graph_report`. Confirm token estimate first.
- **Depends on:** P2-int, P2-test.
- **Brief:** Phase 2 acceptance.

---

## Phases 3–10 — one session per phase (gated)
Each phase is its own session and stops for approval. Sub-tasks that are parallel-safe
(disjoint files) are marked ‖; otherwise treat the phase as sequential.

### Phase 3 — LLM mention extraction
- P3-chunk ‖ `app/llm/chunking.py` (conversation chunking, short ids, boilerplate strip).
- P3-extract — rewrite `app/llm/extract.py` (tool-use schema, `EXTRACT_V1` prompt),
  `app/llm/prompts.py`, `app/llm/verify.py` (verbatim-surface checks). Depends on P3-chunk.
- P3-cli ‖ `scripts/build_graph.py` (`--dry-run`, `--confirm`), `scripts/check_llm.py`.
- P3-test — `FakeLLM` tests (hallucination rejected, cache replay, retry policy).
- **Brief:** Phase 3. Checkpoint: `--dry-run` token estimate before any paid run.

### Phase 4 — profiles, blocking, LLM resolution
- P4-profiles `app/resolve/profiles.py` → P4-block `app/resolve/blocking.py` →
  P4-resolve `app/resolve/llm_resolve.py` (`RESOLVE_V1`) → P4-apply (cannot-link,
  union-find, review queue, `MergeRecord`). Sequential. Delete `_group_llm`,
  `_domain_orgs`, gazetteers, leet-merge.
- **Brief:** Phase 4. Checkpoint: before/after graph report on a paid run.

### Phase 5 — remove corpus-specific logic; query types
- P5-topics ‖ topic-based relevance + consolidation (replaces `relevance.py`).
- P5-queries — parameterized query types replacing `queries.py`; `/api/ask` 503 path.
- **Brief:** Phase 5.

### Phase 6 — redaction from the resolved graph (local, uses `raw/`)
- P6-plan/map/apply `app/redact/…`; P6-leak `scripts/leak_check.py`; P6-invariance
  `tests/test_invariance.py`. Wire `raw/` from the parent `zamp/` exports (gitignored).
- **Brief:** Phase 6. Checkpoint: leak report + invariance before committing redacted data.

### Phase 7 — evaluation and agnosticism proofs
- P7-eval ‖ `scripts/eval_er.py`, gold templates. P7-holdout ‖ invented `tests/fixtures/holdout/`.
  P7-agnostic ‖ `tests/test_agnostic.py` + `tests/corpus_terms.txt`. Mostly parallel.
- **Brief:** Phase 7.

### Phase 8 — API, deploy, robustness
- P8-state (session-scoped uploads, `DEMO_READONLY`) ‖ P8-limits (rate limits, health) ‖
  P8-deploy (Dockerfile, Makefile). Frontend loading/error states.
- **Brief:** Phase 8.

### Phase 9 — UX
- Replace Overview with a first-answer screen; resolution/review UI; graph isolates
  toggle; starter questions. **Brief:** Phase 9.

### Phase 10 — documentation
- Rewrite README + restructure decisions.md; results table generated by a script.
- **Brief:** Phase 10.
