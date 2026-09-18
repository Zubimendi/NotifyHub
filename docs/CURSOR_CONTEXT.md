# Agent context: building NotifyHub

Read this file first, then `docs/ARCHITECTURE.md`, then `docs/PRD.md`,
before writing anything. **This document is the spec — the actual
implementation instructions — for code that doesn't exist yet,
including the mock provider services.** Follow `docs/ARCHITECTURE.md`'s
decisions exactly, especially §1 (the three-way failure classification)
and §6 (delegating retry scheduling to QueueLine) — both are easy to
accidentally simplify away, and both are the actual point of this
project.

## Project identity

- **Name:** NotifyHub — unified notification gateway.
- **Stack:** Python 3.12, FastAPI, PostgreSQL 16 (SQLAlchemy 2.0 async,
  hand-written migrations as the source of truth), Redis (cache-aside
  for digest-window lookups only), Jinja2 (templates), Docker Compose.
- **Purpose:** see `docs/PRD.md` for the full brief and
  `docs/ARCHITECTURE.md` for why every decision below is made the way
  it's made.
- **Companion projects:** QueueLine (Project 1) handles all retry/
  backoff scheduling for individual provider-attempt jobs — NotifyHub
  is a QueueLine client, not a second job queue. PyDataRex applies the
  identical "don't rebuild QueueLine" discipline to scheduled task
  execution; read its `docs/ARCHITECTURE.md` §6 for the same reasoning
  applied to a different problem shape. Dispatcher's and QueueLine's own
  "at-least-once, the receiver must tolerate a duplicate" posture is
  inherited directly, named explicitly in `docs/PRD.md` §7.

## Current state

### Done
- `docs/PRD.md`, `docs/ARCHITECTURE.md`, `docs/TESTING.md`,
  `docs/STORY.md`, this file, `README.md`.
- Schema: `migrations/0001_init.sql` — preferences, suppressions (with
  the auto-clearable-vs-not distinction), digest windows (partial
  unique index as the real concurrency guarantee), events, templates,
  notifications, and the per-attempt audit trail with its stable
  idempotency key.
- `docker-compose.yml` (Postgres + Redis + four mock provider service
  stubs), `Dockerfile` (will not build until `app/main.py` exists),
  `Makefile`, `requirements.txt` / `requirements-dev.txt`,
  `.env.example`.

### Not done — this entire document is the task list

Every file below — including every mock provider — is empty except for
a `.gitkeep`. Build in the order listed; later files depend on earlier
ones.

---

#### 1. `app/config.py`, `app/db/session.py`, `app/db/models.py`

Same shape as SearchCraft's/FormFlow's equivalents: Pydantic `Settings`
reading every `.env.example` variable, async SQLAlchemy engine/session,
ORM models mirroring `migrations/0001_init.sql` exactly (the migration
is the source of truth).

#### 2. `app/services/provider_client.py` — the strategy interface

An abstract `NotificationProvider` with one method:
`async def send(self, recipient: str, subject: str, body: str,
idempotency_key: str) -> ProviderResult`, where `ProviderResult` is a
small dataclass: `{outcome: Literal["SUCCESS", "AMBIGUOUS_FAILURE",
"DEFINITIVE_PROVIDER_FAILURE", "DEFINITIVE_RECIPIENT_INVALID"],
provider_message_id: str | None, error_detail: str | None}`.

A concrete `HttpProviderClient(NotificationProvider)` implementation
that POSTs `{recipient, subject, body, idempotencyKey}` to a configured
base URL (one instance per configured mock/real provider) with a
bounded timeout (`PROVIDER_TIMEOUT_MS`), and maps the HTTP outcome to
`ProviderResult`:
- A connection error, timeout, or 5xx with no parseable body →
  `AMBIGUOUS_FAILURE`.
- A clean 2xx → `SUCCESS`, `provider_message_id` from the response body.
- A 422/400 with a body indicating `"reason": "invalid_recipient"` →
  `DEFINITIVE_RECIPIENT_INVALID`.
- Any other clean, parseable rejection (429 rate limit, 401/403 account
  issue, or an explicit `"reason": "provider_rejected"`) →
  `DEFINITIVE_PROVIDER_FAILURE`.

**This mapping is the single most important piece of logic in this
file — get the boundary between "ambiguous" and "definitive" exactly
right, since `docs/ARCHITECTURE.md` §1's entire guarantee depends on
it.** A provider that returns an unclear or malformed error body should
be classified `AMBIGUOUS_FAILURE`, not guessed at — when genuinely
uncertain, treat it as ambiguous, since that's the safer failure mode
(a same-provider retry with a stable idempotency key, rather than a
premature failover).

`app/services/provider_registry.py` — configuration-driven, not
hardcoded: a `ProviderChain` per channel, an ordered list of
`HttpProviderClient` instances built from config
(`EMAIL_PROVIDER_CHAIN`, `SMS_PROVIDER_CHAIN`, `PUSH_PROVIDER_CHAIN` —
comma-separated base URLs in priority order, per `.env.example`).
Adding, removing, or reordering a provider is an env var change, full
stop — no code anywhere else references a specific provider by name.

#### 3. `app/services/suppression_service.py` and `preference_service.py`

`SuppressionService.is_suppressed(user_id, channel, category) -> bool`
— checks `suppressions WHERE user_id = $1 AND channel = $2 AND
(category = $3 OR category IS NULL) AND cleared_at IS NULL`. Called
unconditionally by `DispatchService` immediately before rendering and
sending (§6's freshness check), never cached, never skipped because "it
was already checked earlier."

`SuppressionService.record_bounce(provider, source_event_id, user_id,
channel, reason) -> bool` (returns whether a new row was actually
inserted, for observability) — `INSERT INTO suppressions (...) VALUES
(...) ON CONFLICT (source_event_id) WHERE source_event_id IS NOT NULL
DO NOTHING`, the idempotent-webhook mechanism from
`docs/ARCHITECTURE.md` §5.

`PreferenceService.get(user_id, category, channel) -> Preference`
(defaults to `enabled=true, batching_mode=IMMEDIATE` if no row exists —
document this default explicitly, since it means an unconfigured
category/channel pair is opted in by default, a real product decision
worth stating plainly rather than leaving implicit).
`PreferenceService.set(...)` — and, per `docs/ARCHITECTURE.md` §4:
**setting `enabled=true` for a `(user_id, category, channel)` also
clears any `UNSUBSCRIBE`-reason suppression for that exact tuple**
(`UPDATE suppressions SET cleared_at = now() WHERE user_id = $1 AND
channel = $2 AND category = $3 AND reason = 'UNSUBSCRIBE' AND
cleared_at IS NULL`) — and never touches a `HARD_BOUNCE` or `COMPLAINT`
row, under any circumstance. Put this exact behavior in one place
(inside `PreferenceService.set`, not scattered across API handlers) so
it can't be bypassed by a future code path that updates preferences a
different way.

#### 4. `app/services/digest_service.py`

`get_or_create_open_window(user_id, category, channel, window_duration)
-> digest_window_id`:
1. Check Redis (`GET digest:{user_id}:{category}:{channel}`). If found,
   return it directly — no Postgres round trip for the common case.
2. On a cache miss: `INSERT INTO digest_windows (user_id, category,
   channel, flush_at, status) VALUES (..., now() + :duration, 'OPEN')
   ON CONFLICT (user_id, category, channel) WHERE status = 'OPEN' DO
   NOTHING RETURNING id`. If a row came back, this call created it — set
   the Redis key (`SET ... EX :duration`) and return the new id. If no
   row came back, someone else won the race — `SELECT id FROM
   digest_windows WHERE user_id = $1 AND category = $2 AND channel = $3
   AND status = 'OPEN'`, populate Redis from that, return it.

**`DigestFlushWorker`** (a scheduled loop, `DIGEST_SWEEP_INTERVAL_MS`):
`SELECT id FROM digest_windows WHERE status = 'OPEN' AND flush_at <=
now()`, and for each:
1. Claim it: `UPDATE digest_windows SET status = 'FLUSHING' WHERE id =
   $1 AND status = 'OPEN'` — check affected-row count (the same
   claim-before-acting discipline as every other background sweep in
   this portfolio); if zero, another sweep pass already claimed it,
   skip.
2. Delete the Redis pointer immediately after claiming (so no new event
   can join a window that's about to flush).
3. Load every `notification_events` row with this `digest_window_id`.
4. **Re-check suppression and preference right now** (§6's freshness
   principle) — if the user is currently suppressed or has since turned
   the category off, mark every event `SUPPRESSED`, the window
   `FLUSHED` with no `notification_id`, and stop.
5. Otherwise: render the digest template (§5), create one `notifications`
   row (`kind = 'DIGEST'`), mark every event `BATCHED` →
   `notification_id` set, window `FLUSHED` with `notification_id` set,
   and enqueue the first provider-attempt QueueLine job (§7).

**Resolving the PRD's open question — a window stuck `FLUSHING` after a
crash:** decide explicitly (this is currently open in `docs/PRD.md`
§7): the sweep's `WHERE status = 'OPEN'` clause means a `FLUSHING` row
left behind by a crashed worker is never picked up again by the normal
sweep. Add a second, longer-interval reconciliation pass —
`WHERE status = 'FLUSHING' AND updated_at < now() - FLUSHING_STUCK_TIMEOUT`
— that resets such rows back to `OPEN` (with a fresh `flush_at = now()`)
so the next normal sweep pass picks them up and actually flushes them.
Document this second pass clearly as the crash-recovery backstop,
distinct from the primary flush trigger.

#### 5. `app/services/template_service.py`

`render_immediate(category, channel, event: NotificationEvent) ->
(subject, body)` and `render_digest(category, channel, events:
list[NotificationEvent]) -> (subject, body)` — both look up the
matching `notification_templates` row (`kind = 'IMMEDIATE'` or
`'DIGEST'`) and render via Jinja2, passing `template_data` (single
event) or a list of `template_data` dicts (digest) into the template
context. Missing template → a clear, specific error (`ErrTemplateNotFound`),
not a silently-empty message.

#### 6. `app/services/dispatch_service.py` — where everything composes

`DispatchService.dispatch(notification_id)`, called by the QueueLine
worker (§7) for a specific `(notification_id, provider_name,
attempt_number)` job:
1. Load the `notifications` row; if its `status` is already terminal
   (`SENT`/`FAILED`/`SUPPRESSED`), this job is stale (a race with a
   prior attempt already resolving it) — no-op, report success to
   QueueLine.
2. **Re-check suppression and preference** — if now suppressed, set
   `status = 'SUPPRESSED'`, `suppression_reason`, stop, report success
   to QueueLine (this is a correct, final outcome, not a failure).
3. Compute `idempotency_key = hash(notification_id, provider_name)` —
   recomputed identically every call, never stored and reused from a
   prior attempt's row, so it's naturally stable across retries without
   needing to look anything up.
4. Call the provider (`provider_client.send(...)`), insert a
   `notification_send_attempts` row recording the outcome.
5. On `SUCCESS`: `notifications.status = 'SENT'`. Report success to
   QueueLine.
6. On `AMBIGUOUS_FAILURE`: report failure to QueueLine **with a
   same-job retry request** — QueueLine's own retry/backoff re-invokes
   this exact job (same `provider_name`, same `attempt_number` sequence
   from QueueLine's perspective, though this service's own
   `attempt_number` column increments per actual call for audit
   purposes — reconcile this distinction clearly in the code: QueueLine
   tracks its own retry count for job-level backoff; NotifyHub tracks
   its own `attempt_number` for the audit trail, and the two are related
   but not required to be numerically identical).
7. On `DEFINITIVE_RECIPIENT_INVALID`: `notifications.status = 'FAILED'`.
   Report success to QueueLine (the job itself completed correctly — it
   determined, definitively, that this can't be sent; that's not a job
   failure, it's a correct terminal outcome) — **do not enqueue any
   further provider attempt**.
8. On `DEFINITIVE_PROVIDER_FAILURE`: look up the next provider in this
   channel's chain after `provider_name`. If one exists, enqueue a new
   QueueLine job for it (`attempt_number` resets to 1 for the new
   provider). If none remain (this was the last provider in the chain),
   `notifications.status = 'FAILED'`. Either way, report success to
   QueueLine for *this* job (it correctly determined this provider
   won't work).

`app/services/queueline_client.py` — thin wrapper: `enqueue(queue,
payload) -> job_id`, consistent with PyDataRex's own
`queueline_client.py` shape elsewhere in this portfolio. **Confirm
QueueLine's exact worker-registration/claim API against its own docs
before implementing the consumer side** — this spec assumes a standard
claim-job/report-outcome contract without prescribing QueueLine's
internals, the same caution PyDataRex names for its own QueueLine
integration.

#### 7. `app/worker.py` — the QueueLine-consuming dispatch worker

A standalone process (`python -m app.worker`) that claims jobs from the
`notify-dispatch` queue and calls `DispatchService.dispatch(...)` for
each, reporting the outcome back per §6's rules. Graceful shutdown
finishes an in-flight dispatch before exiting.

#### 8. `app/api/*.py`

- `POST /v1/events` `{userId, category, channel, templateData}` →
  preference check #1; `IMMEDIATE` → create `notifications` row +
  enqueue; `DIGEST_*` → `digest_service.get_or_create_open_window` +
  insert the `notification_events` row against it. `OFF`/disabled →
  mark the event `SUPPRESSED` immediately, no window, no notification.
- `PUT /v1/users/{id}/preferences` → `PreferenceService.set`.
- `GET /v1/users/{id}/preferences`.
- `POST /v1/users/{id}/unsubscribe?token=...` → verify the stateless
  HMAC unsubscribe token (§9), then `PreferenceService.set(enabled=false)`
  for the token's `(category, channel)` **and** insert an
  `UNSUBSCRIBE`-reason suppression explicitly (setting the preference
  alone isn't enough — the suppression row is what
  `DispatchService`'s freshness check actually reads; keep both
  updated together, in one transaction).
- `POST /v1/webhooks/{provider}/bounce` `{eventId, userId, channel,
  reason}` → `SuppressionService.record_bounce`.
- `GET /v1/users/{id}/notifications` — history, paginated.
- `GET /health/live`, `GET /health/ready` (Postgres, Redis, and
  QueueLine reachability), `GET /metrics`.

#### 9. `app/services/unsubscribe_token.py`

Same deliberately minimal, stateless HMAC-signed token family as
Switchboard's `wsauth`, FormFlow's admin key, Tribunal's actor tokens,
and CatalogSync's actor tokens elsewhere in this portfolio — here
carrying `{userId, category, channel}`, no expiry needed (an
unsubscribe link in an old email should still work), `hmac.compare`
for verification, no database lookup.

#### 10. `mock_provider/app.py` — one generic file, parameterized

A single small FastAPI (or plain `http.server`) script, run as multiple
Docker services with different env vars (`SIMULATE_MODE=NORMAL|
DEFINITIVE_REJECT|DEFINITIVE_INVALID_RECIPIENT|AMBIGUOUS_TIMEOUT`,
`PROVIDER_NAME` for logging). `POST /send {recipient, subject, body,
idempotencyKey}`:
- `NORMAL` → `200 {"messageId": "<uuid>"}`, and record the
  `idempotencyKey` in an in-memory set; a *second* request with the
  same key returns the *same* `messageId` without "sending" again
  (increment a separate counter so a test can assert "received exactly
  one distinct send" even after several retries with the same key).
- `DEFINITIVE_REJECT` → `429 {"reason": "provider_rejected"}`.
- `DEFINITIVE_INVALID_RECIPIENT` → `422 {"reason": "invalid_recipient"}`.
- `AMBIGUOUS_TIMEOUT` → sleep past the caller's expected timeout, then
  (this is the important, realistic part) **actually complete the send
  successfully on the server side** — record it exactly like `NORMAL`
  would — so a test can prove the caller genuinely never learns whether
  it succeeded from the response alone, and can only find out via a
  second request bearing the same idempotency key.

#### 11. `app/observability.py` and `app/main.py`

Metrics per `docs/ARCHITECTURE.md` §9. `main.py` wires routers,
`/health/*`, `/metrics`.

---

## Design decisions already made — don't relitigate without reason

1. **Three failure categories, never conflated**: ambiguous (retry same
   provider), definitive-provider (failover), definitive-recipient
   (stop entirely). See `docs/ARCHITECTURE.md` §1.
2. **The idempotency key excludes `attempt_number`** — it must stay
   identical across retries against the same provider. Don't
   "improve" it by making it unique per attempt; that defeats its
   entire purpose.
3. **Digest-window concurrency is a Postgres partial unique index; Redis
   is a cache in front of it, never the source of truth.** Don't let a
   future change make Redis the actual coordination mechanism.
4. **The flush trigger is a Postgres-timestamp-polled sweep, never
   Redis TTL/keyspace-notification expiry.** See
   `docs/ARCHITECTURE.md` §2.
5. **Suppression and preference are re-checked at send time,
   unconditionally, even for immediate (non-digest) notifications**,
   not trusted from the ingestion-time check alone.
6. **`UNSUBSCRIBE` suppressions auto-clear on re-enabling the matching
   preference; `HARD_BOUNCE` and `COMPLAINT` never do.** See
   `docs/ARCHITECTURE.md` §4.
7. **Retry/backoff scheduling for provider attempts is QueueLine's job.**
   Don't build a retry loop with its own backoff/delay logic inside
   NotifyHub itself.
8. **Mock providers run as real, separate network services**, not
   in-process stubs — this is what makes a genuine ambiguous-timeout
   test possible at all.

## Suggested build order (restated as a checklist)

1. `app/config.py`, `app/db/session.py`, `app/db/models.py`.
2. `mock_provider/app.py` — build this early; almost everything else
   needs a real mock provider to test against.
3. `app/services/provider_client.py`, `provider_registry.py` — write
   the ambiguous-vs-definitive classification tests
   (`docs/TESTING.md` §1) immediately after. This is the project's
   centerpiece.
4. `app/services/suppression_service.py`, `preference_service.py`.
5. `app/services/digest_service.py` — write the concurrent-window-
   creation test (`docs/TESTING.md` §2) right after.
6. `app/services/template_service.py`, `unsubscribe_token.py`.
7. `app/services/dispatch_service.py`, `queueline_client.py` — write the
   freshness-check test (`docs/TESTING.md` §3) right after.
8. `app/worker.py`.
9. `app/api/*.py`, `app/observability.py`, `app/main.py`.
10. The idempotent-bounce-webhook test and the full end-to-end flow test
    (`docs/TESTING.md` §4, §6).

## How to give a fresh agent session everything it needs

Point it at, in this order: this file → `docs/ARCHITECTURE.md` →
`docs/PRD.md`. Tell it explicitly: "nothing in `app/` or
`mock_provider/` is written yet — this file's numbered sections are the
actual spec, build in the order listed, and do not conflate ambiguous
and definitive provider failures anywhere in the dispatch path without
first re-reading why the distinction is the entire point in
`docs/ARCHITECTURE.md` §1."
