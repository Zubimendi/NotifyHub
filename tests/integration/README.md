# Integration tests

Requires Docker services from `make up` (Postgres, Redis, mock providers).
Optionally QueueLine at `QUEUELINE_BASE_URL` for full E2E dispatch tests.

```bash
make up
pytest tests/integration -v
```
