# NotifyHub

A unified notification gateway, built in Python/FastAPI. Week 8–9,
Project 16 of the backend roadmap — email, SMS, and push through
multiple interchangeable providers, with automatic failover, digest
batching, and a preference/suppression center that's actually checked
at the moment a message is about to go out, not just when it was first
queued.

## The problem this exists to solve

"Retry on failure, fail over to a backup provider" sounds simple until
you ask a sharper question: what happens when a provider times out and
you genuinely don't know whether it already sent the message? Retry the
same provider, and you might double-send if it actually succeeded.
Fail over to a different provider immediately, and you're *more* likely
to double-send, not less — a fresh provider has no way to know the first
one might already have delivered it. Most notification systems answer
this by not asking the question at all: any failure, ambiguous or not,
triggers the same retry-or-failover logic, and duplicate sends are a
"sometimes it just happens" cost nobody investigated closely enough to
notice was avoidable.

NotifyHub separates ambiguous failures (timeouts, connection errors —
retry the *same* provider, using an idempotency key so the retry is
safe even when the first attempt actually landed) from definitive ones
(a provider explicitly rejects it — move to the next provider in the
chain; a provider says the recipient address itself is invalid — stop
entirely, since no other provider will fare better). It also refuses to
treat a preference check at enqueue time as good enough — a digest batch
built over an hour is re-checked against the user's *current*
preferences and suppression status the moment it's actually about to
send, not whatever was true when the first event in it arrived. See
`docs/ARCHITECTURE.md` for the full mechanism.

## What's built (the architecture skeleton)

- **Schema** (`migrations/0001_init.sql`) — preferences, suppressions
  (bounce/complaint/unsubscribe, with a real distinction between what's
  auto-clearable and what isn't), digest windows (a partial unique index
  is the actual concurrency guarantee — Redis is a cache in front of it,
  never the source of truth), the raw event log, and a per-attempt audit
  trail (`notification_send_attempts`) recording every provider attempt
  with its own stable idempotency key.
- Docker Compose (Postgres + Redis + four mock provider services —
  two for email, specifically so the failover chain has something real
  to fail over to), Dockerfile skeleton, Makefile, `requirements.txt`,
  `.env.example`.
- Full documentation: `docs/PRD.md`, `docs/ARCHITECTURE.md`,
  `docs/TESTING.md`, `docs/STORY.md`, `docs/CURSOR_CONTEXT.md`.


## Quickstart

```bash
git clone <your-fork-url> notifyhub && cd notifyhub
cp .env.example .env
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
make up               # Postgres + Redis + mock providers + migrations
make dev                # API on :8000
make worker                # separate terminal: the QueueLine-consuming dispatcher
make digest-sweep             # separate terminal: the digest flush sweep
```

```bash
curl -X PUT localhost:8000/v1/users/<id>/preferences \
  -d '{"category":"comments","channel":"EMAIL","batchingMode":"DIGEST_HOURLY"}'

curl -X POST localhost:8000/v1/events \
  -d '{"userId":"<id>","category":"comments","channel":"EMAIL","templateData":{"commenter":"Alex"}}'
# five more of these within the hour get batched into one digest, not six emails

curl localhost:8000/v1/users/<id>/notifications   # history
```

## Documentation

`docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`,
`docs/STORY.md`.

## License

MIT — see `LICENSE`.
