.PHONY: up down migrate dev worker digest-sweep test test-integration lint

up:
	docker-compose up -d --build
	@until docker-compose exec -T postgres pg_isready -U notifyhub >/dev/null 2>&1; do sleep 1; done
	@# Redis may already be running on the host (:6379); Compose redis is optional.
	@$(MAKE) migrate

migrate:
	docker-compose exec -T postgres psql -U notifyhub -d notifyhub -f - < migrations/0001_init.sql
	docker-compose exec -T postgres psql -U notifyhub -d notifyhub -f - < migrations/0002_seed.sql

down:
	docker-compose down

# Runs app/main.py
dev:
	uvicorn app.main:app --reload --port 8000

# Runs app/worker.py (the QueueLine-consuming dispatch worker)
worker:
	python -m app.worker

# Runs the digest flush sweep as its own process
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
