# Testing NotifyHub

Same shape as the rest of the portfolio: none of this exists yet, and
neither does the code it would test. This document is the spec for the
test suite to build alongside `docs/CURSOR_CONTEXT.md`'s implementation.
This project's entire claim rests on §1; prove it before building
anything on top of `app/services/dispatch_service.py`.

## 1. Ambiguous vs. definitive failure classification — the single most important test in this repo

Requires the real mock provider service (`AMBIGUOUS_TIMEOUT` mode) —
this is not a property an in-process stub can prove, since the whole
point is a genuine network call whose outcome is genuinely unknown to
the caller.

- **Ambiguous failure retries the same provider, with a stable
  idempotency key:** configure a mock provider in
  `AMBIGUOUS_TIMEOUT` mode. Dispatch a notification against it. Confirm
  the resulting `notification_send_attempts` rows: multiple rows, all
  with the *same* `provider_name` and the *same* `idempotency_key`,
  `attempt_number` incrementing — **no row for any other provider until
  the retry budget for this one is exhausted.**
- **The mock provider actually only "sent" once, despite N retries:**
  after the sequence above, query the mock provider's own internal
  counter (expose a `GET /debug/send-count?idempotencyKey=...` endpoint
  on the mock for exactly this purpose) and assert it recorded exactly
  one distinct send, not N — this is the direct proof that the
  idempotency key is doing its job, not just being sent as an
  unused header.
- **Definitive provider failure fails over immediately, no retry against
  the failing provider:** configure the *first* provider in a channel's
  chain to `DEFINITIVE_REJECT`, the second to `NORMAL`. Dispatch.
  Confirm exactly **one** `notification_send_attempts` row for the first
  provider (no retries against it at all) before a row for the second
  provider appears, and the notification ends `SENT`.
- **Definitive recipient-invalid stops entirely, no failover attempted:**
  configure the *first* provider to `DEFINITIVE_INVALID_RECIPIENT`, the
  second to `NORMAL`. Dispatch. Confirm exactly one
  `notification_send_attempts` row total (for the first provider only),
  the notification ends `FAILED`, and the second provider's mock
  received **zero** requests (check its own request log/counter) — the
  direct proof that this classification genuinely stops the chain
  rather than just being labeled differently but behaving the same as
  a provider-level failure.
- **Retry budget exhaustion on an all-ambiguous provider triggers
  failover, not an infinite loop:** the first provider stays
  `AMBIGUOUS_TIMEOUT` forever; the second is `NORMAL`. Confirm the
  first provider is retried up to exactly its configured budget, then a
  request finally appears against the second provider, and the
  notification ends `SENT` — with the residual honest note (assert this
  is documented, not "fixed") that the first provider's own send-count
  debug endpoint may show it actually processed one or more of those
  "ambiguous" attempts internally, which is the real, accepted,
  narrow duplicate-risk window named in `docs/PRD.md` §7.

## 2. Digest window concurrency

Requires real Postgres.

- **Exactly one window under concurrent creation:** fire 20 concurrent
  `POST /v1/events` calls for the identical `(user_id, category,
  channel)` with no window currently open. Assert exactly one
  `digest_windows` row is created (`status = 'OPEN'`), and all 20
  `notification_events` rows point at it via `digest_window_id` — no
  event was dropped, and no second window was created.
- **Redis cache-miss fallback is correct:** the same test, but with
  Redis flushed mid-way through (simulating a cache miss for some
  requests after the window already exists in Postgres) — assert the
  fallback `SELECT` path still correctly finds the existing open window
  rather than attempting (and failing, safely, via the `ON CONFLICT`) to
  create a second one.
- **Flush claims exactly once under a concurrent sweep race:** simulate
  two sweep passes running at the same moment against the same due
  window (two concurrent calls to the claim step). Assert exactly one
  succeeds (`status` transitions `OPEN → FLUSHING`), the other's
  affected-row count is zero and it correctly skips.
- **The stuck-`FLUSHING` recovery pass works:** manually set a window to
  `FLUSHING` with an old `updated_at` (simulating a crashed flush
  worker). Run the reconciliation pass from
  `docs/CURSOR_CONTEXT.md` §4. Assert it resets to `OPEN` with a fresh
  `flush_at`, and the next normal sweep pass then flushes it correctly
  — proving the crash-recovery backstop actually recovers, not just
  detects.

## 3. Freshness — preferences and suppressions checked at send time

- **A digest window respects a preference change made after it opened:**
  open a digest window (first event arrives), then, before `flush_at`,
  disable the matching preference. Let the window flush. Assert every
  event in it ends `SUPPRESSED`, the window `FLUSHED` with no
  `notification_id` — not sent under the preference that was true when
  the window opened.
- **An immediate notification respects a suppression added between
  ingestion and dispatch:** create an immediate notification (dispatch
  not yet attempted — pause the worker or insert directly for test
  speed), add a suppression for that user/channel, then run dispatch.
  Assert the notification ends `SUPPRESSED`, not `SENT` — proving the
  check in `DispatchService.dispatch` isn't redundant with the
  ingestion-time check, it's the one that actually matters.
- **Re-enabling a preference clears the matching `UNSUBSCRIBE`
  suppression, and only that one:** unsubscribe a user from
  `(category=A, channel=EMAIL)`, add a *manual* suppression for
  `(category=B, channel=EMAIL)` too. Re-enable
  `(category=A, channel=EMAIL)`. Assert category A's suppression is
  cleared (`cleared_at` set) and category B's manual suppression is
  **untouched** — proving the clearing is scoped exactly, not broadly.
- **A hard bounce is never cleared by a preference change:** record a
  `HARD_BOUNCE` suppression, then toggle the matching preference off and
  back on. Assert the suppression's `cleared_at` remains `NULL`
  throughout — the direct proof of `docs/ARCHITECTURE.md` §4's claim.

## 4. Idempotent bounce webhook processing

- Send the identical bounce webhook payload (same `eventId`) twice.
  Assert exactly one `suppressions` row exists afterward, not two.
- Send two *different* bounce events for the same user/channel (genuine
  distinct bounces, different `eventId`s). Assert two separate
  `suppressions` rows exist — proving the dedup is scoped to the event
  id specifically, not accidentally deduping distinct real bounces.

## 5. Provider chain configurability

- Reorder a channel's provider chain purely via configuration (no code
  change). Dispatch a notification against a scenario where the
  (newly) first provider fails definitively and the (newly) second
  succeeds. Assert the attempt order in `notification_send_attempts`
  reflects the *new* configuration — the direct proof of
  `docs/PRD.md` success criterion #1.

## 6. End-to-end flow test

Drive the full HTTP API: set a user's preference to `DIGEST_HOURLY` for
one category, submit several events for it, confirm they're absorbed
into one open window (not sent individually), advance time (or shorten
the window for test speed) past `flush_at`, run the sweep, confirm one
digest notification is dispatched through the configured provider
chain and reaches `SENT`, then submit an immediate-mode event for a
different category and confirm it dispatches directly without ever
touching `digest_windows`. Unsubscribe via the token link, submit
another event for the unsubscribed category, confirm it's suppressed
immediately at ingestion.

## Known gaps to plan for once the base suite exists

- No load test characterizing ingestion throughput under a large number
  of concurrent digest-window-joining events across many different
  users (as opposed to §2's deliberately adversarial single-tuple
  contention test).
- No test yet for QueueLine's own retry/backoff timing interacting with
  `MAX_CONCURRENCY_RETRIES`-style budgets — confirm the exact contract
  once `app/services/queueline_client.py` is implemented against
  QueueLine's real, confirmed worker API.
- No test for push-notification-specific failure conventions (expired
  device tokens) beyond mapping them onto the existing
  `DEFINITIVE_RECIPIENT_INVALID` category — `docs/PRD.md` §7 names this
  as a simplification; add dedicated tests if push gets a fuller
  implementation later.
