# Multi-stage-ish build for NotifyHub's API and dispatch worker. Not
# required for local dev (see Makefile's `dev`/`worker`/`digest-sweep`
# targets, which run against the host Python venv against Dockerized
# Postgres/Redis/mock providers) — present for deploying the app as
# containers once app/main.py and app/worker.py exist.
#
# NOTE: this Dockerfile will not build successfully until those exist —
# see docs/CURSOR_CONTEXT.md.

FROM python:3.12-slim AS base
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
COPY migrations ./migrations

FROM base AS api
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]

FROM base AS worker
CMD ["python", "-m", "app.worker"]
