# Architecture: principles → code

Same format as every project in this portfolio: each section names a
principle, what it means for a unified notification gateway
specifically, and exactly where `docs/CURSOR_CONTEXT.md` specifies it
needs to be implemented.

## System shape

```
  POST /v1/events
        │
        ▼
 ┌──────────────┐
 │ ingestion       │  preference check #1 (cheap, early)
 │ (ohHOLDING for   │  ── IMMEDIATE ──▶ create `notifications` row ──▶ enqueue QueueLine job
 │  batching_mode)   │  ── DIGEST ──▶ join/open a digest_window (Postgres unique index + Redis cache)
 └──────────────┘
                              digest_windows.flush_at, polled by a sweep ──▶ render digest ──▶
                              preference/suppression check #2 (freshness) ──▶ create `notifications` row
                                                                                       │
                                                                                       ▼
                                                                        ┌──────────────────────┐
                                                                        │ QueueLine job: attempt   │
                                                                        │ (notification, provider,  │
                                                                        │  attempt N)                 │
                                                                        └──────────┬───────────┘
                                                                                   ▼
                                                                        ambiguous ──▶ QueueLine retries
                                                                        (same provider, same idempotency key)
                                                                                   │
                                                                        definitive-provider ──▶ enqueue
                                                                        NEXT provider's job
                                                                                   │
                                                                        definitive-recipient-invalid ──▶
                                                                        stop entirely, notification FAILED
```

---

## 1. Ambiguous vs. definitive failure — the centerpiece

**The failure mode this exists to prevent:** a provider call that times
out or drops the connection has told you nothing about whether the
message was actually sent — the request might have been fully processed
on the other end, with only the response lost in transit. Treating that
the same way as an explicit rejection (retry blindly, or immediately try
a different provider) is the single most common way notification
systems produce avoidable duplicate sends, and it's almost always
invisible until a user notices they got the same email twice.

**The mechanism:** every provider outcome is classified into exactly
three categories, never conflated:
- **`AMBIGUOUS_FAILURE`** (timeout, connection error, an unclear 5xx) —
  retry the *same* provider, using the *same* idempotency key
  (`hash(notification_id, provider_name)`, deliberately excluding
  `attempt_number` — see `docs/CURSOR_CONTEXT.md` §2), so a provider
  that honors idempotency keys recognizes the retry as the same logical
  request rather than a new send. Only after the retry budget against
  *this* provider is exhausted does the system move to the next
  provider in the chain.
- **`DEFINITIVE_PROVIDER_FAILURE`** (the provider explicitly rejects the
  request for a reason specific to itself — rate-limited, account
  issue, temporary outage reported clearly) — no point retrying the
  same provider; move to the next one in the chain immediately.
- **`DEFINITIVE_RECIPIENT_INVALID`** (the address/number/token itself
  is malformed or doesn't exist) — stop entirely. No other provider in
  the chain will succeed against a recipient that doesn't exist; trying
  one wastes a request and delays the inevitable failure notification.

**Being honest about what idempotency keys can and can't fix:** not
every real provider supports them at all. Resend, Brevo, and Moov
accept a client-supplied idempotency key on send; SendGrid, notably,
does not. NotifyHub always computes and records its own key regardless
— it's part of `notification_send_attempts` unconditionally — but for a
provider adapter without native support, a retry against that specific
provider carries a real, higher duplicate risk than the architecture's
happy-path story implies. This is named directly in
`docs/PRD.md` §7, not smoothed over: **the actual guarantee this project
reaches is at-least-once delivery with bounded, rare duplicate risk —
not exactly-once** — the same honest posture Dispatcher and QueueLine
both take elsewhere in this portfolio for their own delivery guarantees,
applied here to a channel (a person reading an email) where "the
receiver must dedupe" isn't a workable answer, which is exactly why the
sending side has to work this much harder to keep duplicates rare in
the first place.

## 2. Digest windows: the database is the truth, Redis is the cache, and neither is the trigger you'd expect

**Where:** `digest_windows` has a partial unique index —
`(user_id, category, channel) WHERE status = 'OPEN'` — which is the
*entire* concurrency guarantee: at most one open window per person, per
category, per channel, ever, enforced by the database regardless of how
many concurrent ingestion requests are racing to create one. Redis holds
a fast-lookup pointer (`digest:{user_id}:{category}:{channel}` →
window id) populated by whichever request's Postgres insert actually
won, so the overwhelmingly common case — a second, third, fourth event
joining an *already-open* window — never has to touch Postgres for
existence checking at all, only for the actual event insert.

**The trigger for flushing a window is deliberately not Redis key
expiry.** Redis TTL expiration is a convenient mental model but not a
reliable "do something now" mechanism — there's no guaranteed delivery
of the expiry event to anything listening for it, only a best-effort
notification if that feature is even enabled. The actual flush trigger
is a scheduled sweep polling `digest_windows WHERE status = 'OPEN' AND
flush_at <= now()` directly against Postgres — the same TTL-driven,
timestamp-polled correctness backstop used throughout this portfolio
(SlotForge's expired-hold sweep, Switchboard's presence TTL,
CatalogSync's saga timeout sweep), recognized here for the fifth time
as the right tool whenever "eventually, reliably, without depending on
anything else noticing" is the actual requirement.

## 3. Freshness: a decision made an hour ago is not the same as a decision made now

**Where:** every notification — immediate or digest — is checked against
`notification_preferences` and `suppressions` **twice**: once, cheaply,
at ingestion (so an obviously-off category never even creates a digest
window or wastes work), and again, unconditionally, at the moment
`DispatchService` is about to actually render and send it. A digest
window can span an hour; a user's preference or suppression status can
change at any point during that hour, and the second check is what
makes the system honor whichever state was true *when it mattered* —
the moment of send — rather than whatever was true when the first event
in the batch happened to arrive. This is the same "never trust a
decision made against state that could have gone stale" discipline as
CatalogSync's refusal to let a checkout read from its cached catalog
view, recognized here in a different shape: not a separate read model
going stale, but time itself passing between two points that both
matter.

## 4. Not every suppression reason means the same thing

**Where:** `suppressions.reason` distinguishes `HARD_BOUNCE`,
`COMPLAINT`, `UNSUBSCRIBE`, and `MANUAL` — and only `UNSUBSCRIBE`-reason
rows are ever auto-cleared (by the user re-enabling the matching
preference; `docs/CURSOR_CONTEXT.md` §4 specifies exactly where this
clearing happens). `HARD_BOUNCE` and `COMPLAINT` are never
automatically cleared by anything. The reasoning: an unsubscribe is a
statement about *preference* — the address works fine, the person just
doesn't want this category of message, and that preference can
legitimately change back. A hard bounce is a statement about
*deliverability* — the address itself doesn't work — and re-enabling a
preference toggle does nothing to fix a broken address; treating the two
the same would either keep silently failing to deliver to a dead
address (if bounces auto-cleared) or permanently re-annoy someone who
explicitly opted out the moment they changed an unrelated setting (if
unsubscribes never cleared). They're superficially similar ("don't send
to this person") and semantically unrelated, and conflating them is
exactly the kind of near-miss mistake worth naming precisely rather than
discovering later.

## 5. Idempotent bounce-webhook processing

**Where:** `suppressions.source_event_id`, uniquely indexed (partial,
`WHERE source_event_id IS NOT NULL`), is the same `sg_event_id`-style
deduplication pattern SendGrid's own webhook documentation recommends —
real ESPs are explicit that webhook redelivery on anything short of a
clean `2xx` response is common and expected, not a rare edge case, over
retry windows that can run up to 24 hours. `POST
/v1/webhooks/{provider}/bounce` inserts a `suppressions` row keyed on
the provider's own event id; a redelivered webhook hits the unique
index and is recognized as already-processed rather than creating a
second suppression row (which would be harmless here, since a
suppression is a suppression regardless of duplicates — but getting this
right is the same discipline this project asks of every other write
path, applied consistently rather than selectively).

## 6. Delegate retry/backoff scheduling to QueueLine — don't rebuild a job queue a third time

**Where:** an individual provider-attempt (`notification_id`,
`provider_name`, `attempt_number`) is submitted as one QueueLine job.
An `AMBIGUOUS_FAILURE` outcome tells QueueLine to retry *that same job*
— QueueLine's own retry/backoff policy handles the "try the same
provider again after a delay" case natively, since a retry-with-backoff
is exactly the shape QueueLine already solves well for arbitrary job
execution. A `DEFINITIVE_PROVIDER_FAILURE` outcome tells QueueLine the
current job is done (successfully, from QueueLine's own perspective —
its job was "attempt this provider," and it did, definitively) and
NotifyHub's own worker code separately enqueues a *new* QueueLine job
for the next provider in the chain. This is the same "recognize
infrastructure you already built rather than re-solving it" discipline
PyDataRex applies to the same system elsewhere in this portfolio (there,
for scheduled task execution; here, for notification-provider retry
scheduling) — NotifyHub's own code stays focused on the
notification-domain-specific logic (classification, provider chain
ordering, suppression/preference checks), not on reimplementing worker
pools, backoff curves, or dead-lettering.

## 7. The provider abstraction — the strategy pattern, concretely

**Where:** every provider (mock or eventually real) implements one
interface: `send(recipient, subject, body, idempotency_key) ->
ProviderResult` where `ProviderResult` is one of the three outcome
categories from §1 plus an optional `provider_message_id`. A channel's
provider chain is ordered configuration (`docs/CURSOR_CONTEXT.md` §5),
not code — reordering, adding, or removing a provider for a channel is a
configuration change, touching zero call sites anywhere in the
ingestion or dispatch path, which is exactly `docs/PRD.md` success
criterion #1. A future real provider (Twilio, SendGrid, FCM) is "one
more file implementing this interface, plus a config entry," per the
roadmap's own framing — not built in v1, but the interface is shaped so
that adding one doesn't require touching anything else.

## 8. Mock providers are real network services, not in-process stubs

**Where:** each mock provider (`mock_provider/`) runs as its own tiny
Docker service, called over real HTTP by NotifyHub's worker — the same
way a real Twilio or SendGrid integration would be called. This is
deliberate: an in-process stub can't produce a genuine ambiguous
timeout (a real network call that genuinely doesn't return in time,
with the request's actual fate on the other end genuinely unknown to
the caller) — only a real network hop can. Each mock provider is
configurable (via env var or request parameter) to simulate `NORMAL`,
`DEFINITIVE_REJECT`, `DEFINITIVE_INVALID_RECIPIENT`, or `AMBIGUOUS_TIMEOUT`
behavior, and internally tracks idempotency keys it's already seen (an
in-memory set, reset on restart — sufficient for demonstrating and
testing provider-side deduplication behavior, not meant to simulate a
production-grade provider's own persistence).

## 9. Observability

**Where:** Prometheus metrics track dispatch outcomes broken out by
classification (`success` / `ambiguous_retry` / `failover` /
`definitive_stop`, per channel and provider — a rising `ambiguous_retry`
rate against one specific provider is a real, actionable signal that
provider is degraded, distinct from a rising `definitive_stop` rate,
which points at bad recipient data instead), digest window counts
(open/flushing/flushed, and flush latency relative to `flush_at` — how
late is the sweep actually running), and suppression counts by reason
(a spike in `HARD_BOUNCE` specifically is worth alerting on separately
from a spike in `UNSUBSCRIBE`, since they mean very different things
operationally).

## What's out of scope, and why

- **Real provider integrations, rich content, analytics/engagement
  tracking, cross-channel deduplication.** See `docs/PRD.md` §4 for the
  full reasoning on each.
- **A full job queue.** QueueLine's job, not rebuilt here — §6.
