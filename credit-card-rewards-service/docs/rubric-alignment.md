# Rubric alignment

| Theme | Evidence | Boundary |
|---|---|---|
| SQL/data design | `credit_card_service/db.py`, foreign keys, checks and indexes | SQLite local mode only |
| Service/worker scale | durable leases and `worker` CLI | no multi-replica claim |
| Review/publication | candidates, diffs, evidence, immutable versions | fixture adapter is deterministic |
| Secure REST | `api.py`, separated auth, idempotency, cursor, ETag | bearer secrets are MVP auth |
| Container/CI | Dockerfile, compose, workflow | compose is local only |
| Documentation | architecture/database/deployment/testing docs | no unmeasured load claim |
| Frontend | N/A | explicitly excluded: backend-only |

No performance number is asserted without a reproducible environment. Measure p95 GET `/api/v1/cards` with hardware, concurrency, fixture size, and SQLite mode recorded.
