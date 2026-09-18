# NotifyHub — Product Requirements Document

## 1. Problem

Every product eventually needs to send email, SMS, and push
notifications, and almost every one builds this ad hoc, one provider
integration at a time, directly wired into whatever feature needed it
first. The result, predictably: swapping providers means touching every
call site; a provider outage means notifications silently stop instead
of failing over; a user gets forty separate emails for forty related
events because nobody built batching; and "did we actually respect this
person's unsubscribe" is a question nobody can answer with confidence,
because the check (if it exists at all) happened once, at some point
upstream, and was never revisited.

The genuinely hard part isn't sending a message — it's handling failure
correctly. A provider that times out has not told you it failed. It has
told you *nothing* — the request may have been received and processed
successfully on the other end, with the response simply lost on the way
back. Treating that the same way as a clear rejection (try the next
provider, or just retry blindly) is how notification systems end up
occasionally double-sending, and it's usually invisible until a user
complains about getting the same email three times.

## 2. Goal

Build a **unified notification gateway** where:
1. Providers are fully interchangeable behind a common interface —
   adding, removing, or reordering a channel's provider chain touches
   no call site anywhere else in the system.
2. Ambiguous failures (timeouts, connection errors) and definitive
   failures (explicit provider rejection, invalid recipient) are
   classified distinctly and handled differently — retry-same-provider
   for the former, failover-or-stop for the latter — because treating
   them the same is the specific, common mistake that causes avoidable
   duplicate sends.
3. Related events are batched into digests on a configurable window,
   using a mechanism whose actual correctness guarantee (at most one
   open window per user/category/channel) lives in the database, not in
   application-level coordination.
4. Preferences and suppressions are re-checked at the moment of actual
   send, not trusted from whenever the underlying event was first
   queued — a digest window spanning an hour must reflect a preference
   change made ten minutes before it flushes.
5. Retry-with-backoff scheduling is delegated to QueueLine, an existing
   job-execution system elsewhere in this portfolio, rather than rebuilt
   a third time.

## 3. Users

- **Product engineers integrating notifications**: call one API
  (`POST /v1/events`) regardless of which channel or provider actually
  ends up handling delivery.
- **End users receiving notifications**: configure per-category,
  per-channel preferences (on/off, immediate vs. digest), and can
  unsubscribe via a stateless, no-login-required link.
- **The platform itself**: the actual owner of the correctness
  guarantees this project exists to prove — bounded duplicate risk under
  provider failover, and suppression/preference decisions that are
  always current at send time, never stale.

## 4. Scope

### In scope (v1)
- A provider abstraction (the strategy pattern named explicitly in the
  roadmap brief) with a configurable, ordered provider chain per
  channel (email, SMS, push).
- Ambiguous-vs-definitive failure classification, with retry-same-
  provider on ambiguous outcomes (using a stable idempotency key) and
  failover-or-stop on definitive ones.
- Digest batching: a configurable per-user, per-category window
  (hourly/daily), backed by a Postgres partial unique index as the
  actual concurrency guarantee and a Postgres-timestamp-driven sweep as
  the actual flush trigger (never Redis key-expiry as an event signal —
  see `docs/ARCHITECTURE.md` §2).
- A preference center (per user, per category, per channel: on/off,
  batching mode).
- Suppression handling: hard bounces and complaints (reported via a
  provider bounce webhook, idempotently processed) and unsubscribes (via
  a stateless token link), with a real, documented distinction between
  what can be auto-cleared by a later preference change (unsubscribes)
  and what can't (hard bounces, complaints).
- Delegating retry/backoff scheduling for individual provider-attempt
  jobs to QueueLine.
- Mock provider services (one file each, running as their own tiny
  Docker services) configurable to simulate success, definitive
  rejection, definitive-invalid-recipient, and ambiguous timeout — the
  actual test harness for every correctness claim above.

### Explicitly out of scope (v1), and why
- **Rich message content** (HTML email builders, MMS/media attachments,
  rich push notification actions). This project's subject is delivery
  correctness and provider abstraction, not a content/design system —
  templates are simple (Jinja2, plain text or basic HTML), not a
  full-featured builder.
- **Real provider integrations.** Every provider in v1 is a mock,
  deliberately — the interface is designed so a real Twilio/SendGrid/FCM
  adapter is "one more file," per the roadmap brief, but writing one
  isn't part of proving this project's actual claims.
- **A full job queue.** Retry/backoff scheduling for individual send
  attempts is QueueLine's job, not rebuilt here — see
  `docs/ARCHITECTURE.md` §6, the same "recognize infrastructure you
  already built" discipline PyDataRex applies to the same system
  elsewhere in this portfolio.
- **Analytics/engagement tracking** (open rates, click tracking). A
  real, valuable feature for a notification platform, genuinely
  orthogonal to this project's actual subject.
- **Cross-channel deduplication** ("don't also SMS someone if their
  email digest already covers this"). A real, interesting feature;
  scoped out to keep this project's focus on within-channel delivery
  correctness rather than cross-channel orchestration.

## 5. Success criteria

1. Configuring a channel's provider chain (adding, removing, or
   reordering providers) requires no change to any code that calls
   `POST /v1/events` or anything in the ingestion path — proven by
   swapping the email chain's order in configuration alone and
   confirming dispatch behavior changes accordingly with zero code
   edits elsewhere.
2. A provider that fails ambiguously (simulated timeout) on every
   attempt is retried against *itself*, using the same idempotency key
   every time, up to its configured retry budget, before the system
   ever attempts a different provider — proven by inspecting
   `notification_send_attempts` directly and confirming every row before
   the failover shares one `provider_name` and one `idempotency_key`.
3. A provider that returns a definitive, recipient-specific rejection
   (invalid address) results in the notification failing immediately,
   with **no** attempt made against any other provider in the chain —
   proven directly, since trying another provider against a
   structurally invalid address wastes a request and cannot succeed.
4. A digest window's content, at flush time, is dispatched using the
   user's preference and suppression state *as of the flush moment*, not
   as of when the window was opened — proven by opening a window,
   changing the user's preference or adding a suppression before the
   window flushes, and confirming the flush respects the *new* state.
5. Two concurrent events for the same user, category, and channel,
   arriving within milliseconds of each other while no window is
   currently open, result in **exactly one** digest window being
   created, not two — proven under real concurrent load against
   Postgres, not just architecturally argued from the partial unique
   index.
6. A duplicate bounce webhook delivery (the same provider event id,
   redelivered — a documented, common behavior for real ESPs like
   SendGrid) results in exactly one `suppressions` row, not two.

## 6. Non-functional requirements

- **Correctness of failure classification, digest concurrency, and
  suppression freshness is the whole point** — this project exists to
  get right the specific things a naive notification gateway gets
  subtly wrong, not to maximize message throughput.
- **Zero paid dependencies.** PostgreSQL + Redis + QueueLine (already
  part of this portfolio) via Docker Compose; every provider is a
  locally-running mock.
- **Idempotency-key support varies by real provider, and this project's
  design must not assume every provider has it.** Some real ESPs
  (Resend, Brevo, Moov) support client-supplied idempotency keys on
  send; others (SendGrid, notably) do not. NotifyHub always computes
  and records its own idempotency key regardless, but a provider
  adapter without native support gets a weaker guarantee against
  duplicate sends on ambiguous failure than one with support — named
  explicitly, not glossed over (see `docs/ARCHITECTURE.md` §1).

## 7. Risks / open questions

- **At-least-once is the actual guarantee, not exactly-once.** Even with
  the ambiguous/definitive distinction and idempotency keys, a
  first-attempt success on a provider that lacks idempotency-key
  support, followed by every subsequent status check somehow still
  reading as ambiguous, followed by exhausting retries and failing over
  — is a narrow, real, residual path to a genuine duplicate send. Named
  honestly as an accepted risk, the same "at-least-once; the receiver
  must tolerate a duplicate" posture Dispatcher and QueueLine both
  document elsewhere in this portfolio, applied here to a
  human-facing message where "the receiver must dedupe" isn't really an
  option — which is exactly why this project works harder on the
  sending side to keep actual duplicates rare, even though it can't
  reach a stronger guarantee than bounded-and-rare.
- **Digest window duration is a product decision this document doesn't
  prescribe** — an hour, a day, configurable per category — v1 supports
  configuring it, not a specific recommended default beyond what's
  reasonable for local testing.
- **What happens to a digest window's events if the window is stuck
  `FLUSHING` because the flush worker crashed mid-flush** is a real gap
  to close explicitly in `docs/CURSOR_CONTEXT.md` — flagged here as a
  question, resolved there with reasoning, not left ambiguous.
- **Push notification "providers" (APNs/FCM-style) don't really have
  the same bounce/complaint webhook conventions email does** — v1's
  suppression model is designed around email's conventions most
  directly; a push-specific failure mode (an expired/invalid device
  token) maps onto the same `DEFINITIVE_RECIPIENT_INVALID` outcome
  category, but the schema and mock provider for push are simplified
  relative to what a full push-notification integration would need.
