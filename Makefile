.PHONY: setup dev dev-backend dev-frontend test build-graph eval

# ── setup ──────────────────────────────────────────────────────────────────────

setup:  ## Create .venv, install deps, install frontend packages
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -r requirements.txt
	cd frontend && npm install

# ── development ────────────────────────────────────────────────────────────────

API_BASE ?= http://localhost:8000

dev: ## Start backend (:8000) and frontend (:3000) in parallel (GNU make -j2)
	$(MAKE) -j2 dev-backend dev-frontend

dev-backend:  ## Start the FastAPI backend with auto-reload
	.venv/bin/uvicorn app.api:app --reload --port 8000

dev-frontend:  ## Start the Next.js frontend (reads API_BASE from env)
	cd frontend && NEXT_PUBLIC_API_BASE=$(API_BASE) npm run dev

# ── testing ────────────────────────────────────────────────────────────────────

test:  ## Run the full test suite
	.venv/bin/pytest tests/ -v

# ── graph build ────────────────────────────────────────────────────────────────

build-graph:  ## Rebuild the knowledge graph from processed_data/ (requires API key)
	.venv/bin/python -m scripts.build_graph --dry-run
	@echo ""
	@echo "Re-run with: make build-graph-confirm"

build-graph-confirm:  ## Execute graph build (makes LLM calls, costs money)
	.venv/bin/python -m scripts.build_graph --confirm

# ── eval ───────────────────────────────────────────────────────────────────────

eval:  ## Run ER/dedup evaluation (Phase 7 — not yet implemented)
	@echo "Phase 7 eval not yet implemented. See docs/STATUS.md."
