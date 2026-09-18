# tests/integration

Nothing here yet — this directory holds the Postgres-and-Redis-backed
integration suite, most importantly the ambiguous-vs-definitive failure
classification tests that prove this project's central claim
(`docs/ARCHITECTURE.md` §1). These tests need real mock provider
services running (`make up`), not in-process stubs — see
`docs/ARCHITECTURE.md` §8 for why that's a deliberate requirement, not
a convenience.

**Full spec for every test that belongs here: see `../../docs/TESTING.md`.**
This file is just the pointer; that document is the source of truth for
what to build and why.

**Build order** (matching `docs/TESTING.md`'s numbering and
`docs/CURSOR_CONTEXT.md`'s build order):

1. `test_failure_classification.py` (`docs/TESTING.md` §1) — **the
   single most important test in this repo.**
2. `test_digest_concurrency.py` (`docs/TESTING.md` §2).
3. `test_freshness.py` (`docs/TESTING.md` §3).
4. `test_bounce_webhook_idempotency.py` (`docs/TESTING.md` §4).
5. `test_provider_chain_config.py` (`docs/TESTING.md` §5).
6. `test_end_to_end_flow.py` (`docs/TESTING.md` §6).
