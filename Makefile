.PHONY: up down build ingest eval logs test check-keys clean

VENV := ./venv/bin/python
API := http://localhost:8000

# ── Primary: docker-compose ───────────────────────────────────────────────────

## Start everything (build image first run, then start)
up:
	docker compose up -d --build

## Stop all containers
down:
	docker compose down

## Rebuild image only
build:
	docker compose build

## Tail logs
logs:
	docker compose logs -f api

## Restart API container
restart:
	docker compose restart api

# ── Local dev (no Docker) ─────────────────────────────────────────────────────

## Install deps in venv
install:
	python3 -m venv venv
	venv/bin/pip install --upgrade pip
	venv/bin/pip install --only-binary=:all: -r requirements.txt

## Run locally (dev mode)
dev:
	$(VENV) -m uvicorn app.server:app --reload --host 0.0.0.0 --port 8000

## Run ingestion (local venv)
ingest:
	$(VENV) -m app.ingest --all

## Ingest docs only (faster)
ingest-docs:
	$(VENV) -m app.ingest --docs

# ── Eval ─────────────────────────────────────────────────────────────────────

## Run full eval against running API
eval:
	$(VENV) -m eval.run --api $(API) --output eval/results.md
	@echo "Results: eval/results.md"

## Retrieval-only eval (zero LLM calls)
eval-retrieval:
	$(VENV) -m eval.run --api $(API) --retrieval-only

# ── Tests ─────────────────────────────────────────────────────────────────────

## Run tests
test:
	$(VENV) -m pytest tests/ -v

# ── Key check ────────────────────────────────────────────────────────────────

## Verify OpenRouter + Cohere keys
check-keys:
	$(VENV) scripts/check_keys.py

# ── Docker ingest (runs ingestion inside running container) ───────────────────

## Trigger reindex via API
reindex:
	curl -s -X POST $(API)/admin/reindex \
	  -H "Authorization: Bearer $$(grep ADMIN_TOKEN .env | cut -d= -f2)" \
	  | python3 -m json.tool

## Show stats
stats:
	curl -s $(API)/admin/stats \
	  -H "Authorization: Bearer $$(grep ADMIN_TOKEN .env | cut -d= -f2)" \
	  | python3 -m json.tool

# ── Cleanup ───────────────────────────────────────────────────────────────────

## Remove built artifacts
clean:
	rm -rf frontend/dist venv __pycache__ .pytest_cache

## Remove index (forces full re-ingest)
clean-index:
	rm -rf data/index/*.db data/index/*.index data/index/*.ids data/index/*.json
