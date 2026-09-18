-- 0001_init.sql
-- Schema only — see docs/CURSOR_CONTEXT.md for the Python code that
-- reads and writes these tables, none of which exists yet.

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE users (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    email      TEXT,
    phone      TEXT,
    push_token TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ============================================================
-- PREFERENCES — checked at ingestion (cheap early filter) AND again
-- at the moment of actual send (the freshness principle — see
-- docs/ARCHITECTURE.md §3). Never trusted as a one-time check.
-- ============================================================

CREATE TABLE notification_preferences (
    user_id       UUID NOT NULL REFERENCES users(id),
    category      TEXT NOT NULL,
    channel       TEXT NOT NULL,   -- EMAIL | SMS | PUSH
    enabled       BOOLEAN NOT NULL DEFAULT true,
    batching_mode TEXT NOT NULL DEFAULT 'IMMEDIATE',  -- IMMEDIATE | DIGEST_HOURLY | DIGEST_DAILY
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, category, channel)
);

-- ============================================================
-- SUPPRESSIONS — a hard bounce or complaint always wins over a
-- preference, unconditionally. An UNSUBSCRIBE is the one reason that
-- can be cleared (by re-enabling the matching preference); HARD_BOUNCE
-- and COMPLAINT are never auto-cleared, on purpose — see
-- docs/ARCHITECTURE.md §5 for why these two reasons are NOT
-- interchangeable even though both end in "don't send."
-- ============================================================

CREATE TABLE suppressions (
    id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    UUID NOT NULL REFERENCES users(id),
    channel    TEXT NOT NULL,
    reason     TEXT NOT NULL,   -- HARD_BOUNCE | COMPLAINT | UNSUBSCRIBE | MANUAL
    -- NULL = suppresses every category on this channel (always true for
    -- HARD_BOUNCE/COMPLAINT — a broken address is broken for everything).
    -- A specific category = scoped suppression (how UNSUBSCRIBE usually
    -- works — a user opts out of one kind of notification, not all of
    -- them, unless they explicitly unsubscribe from everything).
    category   TEXT,
    source_event_id TEXT,   -- the provider's own bounce/complaint event id, for idempotent webhook processing
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    cleared_at TIMESTAMPTZ   -- NULL = currently active. Only ever set for UNSUBSCRIBE-reason rows.
);

CREATE INDEX idx_suppressions_active ON suppressions (user_id, channel, category) WHERE cleared_at IS NULL;
-- Idempotent bounce-webhook processing, the same sg_event_id-style
-- pattern SendGrid's own webhook documentation recommends, applied
-- generically across every mock/real provider this project talks to.
CREATE UNIQUE INDEX idx_suppressions_source_event ON suppressions (source_event_id) WHERE source_event_id IS NOT NULL;

-- ============================================================
-- DIGEST WINDOWS — the batching mechanism. The partial unique index
-- is the actual concurrency guarantee (at most one OPEN window per
-- user+category+channel, ever); Redis is a read-cache in front of the
-- common-case "does a window already exist" check, never the source
-- of truth for whether one does. See docs/ARCHITECTURE.md §2.
-- ============================================================

CREATE TABLE digest_windows (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         UUID NOT NULL REFERENCES users(id),
    category        TEXT NOT NULL,
    channel         TEXT NOT NULL,
    opens_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- The flush sweep's trigger condition. Never Redis TTL expiry —
    -- Redis key expiration is not a reliable "do something now" signal
    -- (no guaranteed delivery of the expiry event); this timestamp,
    -- polled by a Postgres-backed sweep, is the actual backstop, the
    -- same discipline as every TTL-based correctness mechanism
    -- elsewhere in this portfolio (SlotForge's cron backstop,
    -- Switchboard's presence TTL, CatalogSync's saga sweep).
    flush_at        TIMESTAMPTZ NOT NULL,
    status          TEXT NOT NULL DEFAULT 'OPEN',  -- OPEN | FLUSHING | FLUSHED
    notification_id UUID,   -- set once flushed, points at the resulting notifications row
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX idx_digest_windows_open ON digest_windows (user_id, category, channel) WHERE status = 'OPEN';
-- The flush sweep's exact query shape.
CREATE INDEX idx_digest_windows_due ON digest_windows (flush_at) WHERE status = 'OPEN';

-- ============================================================
-- EVENTS — the raw trigger. May be dispatched immediately or absorbed
-- into a digest window, depending on the user's current preference.
-- ============================================================

CREATE TABLE notification_events (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id          UUID NOT NULL REFERENCES users(id),
    category         TEXT NOT NULL,
    channel          TEXT NOT NULL,
    template_data    JSONB NOT NULL DEFAULT '{}'::jsonb,
    status           TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | BATCHED | DISPATCHED | SUPPRESSED
    digest_window_id UUID REFERENCES digest_windows(id),
    notification_id  UUID,   -- set once part of a dispatched notification (immediate or flushed digest)
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_notification_events_window ON notification_events (digest_window_id);
CREATE INDEX idx_notification_events_user ON notification_events (user_id, created_at);

CREATE TABLE notification_templates (
    category         TEXT NOT NULL,
    channel          TEXT NOT NULL,
    kind             TEXT NOT NULL,  -- IMMEDIATE | DIGEST
    subject_template TEXT NOT NULL DEFAULT '',
    body_template    TEXT NOT NULL,
    PRIMARY KEY (category, channel, kind)
);

-- ============================================================
-- NOTIFICATIONS — the actual outbound send record. One row per
-- immediate event or per flushed digest window.
-- ============================================================

CREATE TABLE notifications (
    id                 UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id            UUID NOT NULL REFERENCES users(id),
    channel            TEXT NOT NULL,
    category           TEXT NOT NULL,
    kind               TEXT NOT NULL,  -- IMMEDIATE | DIGEST
    digest_window_id   UUID REFERENCES digest_windows(id),
    recipient_address  TEXT NOT NULL,
    rendered_subject   TEXT NOT NULL DEFAULT '',
    rendered_body      TEXT NOT NULL,
    status             TEXT NOT NULL DEFAULT 'PENDING',  -- PENDING | DISPATCHING | SENT | FAILED | SUPPRESSED
    suppression_reason TEXT,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX idx_notifications_user ON notifications (user_id, created_at);

-- Every attempt against every provider, in order. idempotency_key is
-- STABLE across retries against the same provider (computed as
-- hash(notification_id, provider_name), deliberately excluding
-- attempt_number) so a provider that supports idempotency keys can
-- recognize a retry as the same logical request — see
-- docs/ARCHITECTURE.md §1 for which real providers actually honor this
-- and, importantly, which (SendGrid, notably) do not.
CREATE TABLE notification_send_attempts (
    id                  BIGSERIAL PRIMARY KEY,
    notification_id     UUID NOT NULL REFERENCES notifications(id),
    provider_name       TEXT NOT NULL,
    attempt_number      INT NOT NULL,
    idempotency_key     TEXT NOT NULL,
    -- NULL while in flight.
    outcome             TEXT,  -- SUCCESS | AMBIGUOUS_FAILURE | DEFINITIVE_PROVIDER_FAILURE | DEFINITIVE_RECIPIENT_INVALID
    provider_message_id TEXT,
    error_detail        TEXT,
    queueline_job_id     TEXT,
    started_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at          TIMESTAMPTZ,
    UNIQUE (notification_id, provider_name, attempt_number)
);

CREATE INDEX idx_send_attempts_notification ON notification_send_attempts (notification_id);
