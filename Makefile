.PHONY: up down migrate dev worker digest-sweep test test-integration lint

up:
	docker compose up -d
	@until docker compose exec -T postgres pg_isready -U notifyhub >/dev/null 2>&1; do sleep 1; done
	@until docker compose exec -T redis redis-cli ping >/dev/null 2>&1; do sleep 1; done
	@$(MAKE) migrate

migrate:
	PGPASSWORD=notifyhub psql -h localhost -U notifyhub -d notifyhub -f migrations/0001_init.sql

down:
	docker compose down

# Runs app/main.py — does not exist yet, see docs/CURSOR_CONTEXT.md.
dev:
	uvicorn app.main:app --reload --port 8000

# Runs app/worker.py (the QueueLine-consuming dispatch worker) — does
# not exist yet.
worker:
	python -m app.worker

# Runs the digest flush sweep as its own process — does not exist yet.
digest-sweep:
	python -m app.digest_sweep

# Pure-logic unit tests. No Docker required once these exist.
test:
	pytest tests/ -m "not integration"

# The classification, digest-concurrency, and freshness suite in
# tests/integration — requires `make up` running.
test-integration:
	pytest tests/integration -v

lint:
	ruff check app/
