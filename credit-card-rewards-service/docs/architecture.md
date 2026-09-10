# Architecture

```mermaid
flowchart LR
  Admin[Admin client] --> API[Stateless API]
  Engine[Recommendation engine] --> API
  API --> DB[(SQLite WAL: local mode)]
  Worker[Separate worker] --> DB
  Worker --> Sources[Registered documents only]
```

```mermaid
sequenceDiagram
  Admin->>API: queue source + idempotency key
  API->>DB: durable queued run
  Worker->>DB: atomic claim/lease
  Worker->>DB: snapshot, candidate, diff
  Admin->>API: optimistic review decision
  API->>DB: immutable version + evidence + publication transaction
```

```mermaid
erDiagram
  BANKS ||--o{ SOURCES : owns
  SOURCES ||--o{ INGESTION_RUNS : queues
  INGESTION_RUNS ||--o{ EXTRACTION_CANDIDATES : creates
  CARDS ||--o{ CARD_VERSIONS : has
  CARD_VERSIONS ||--o{ REWARD_RULES : normalizes
  CARD_VERSIONS ||--o{ EVIDENCE_LINKS : proves
  CARD_VERSIONS ||--o{ CATALOGUE_PUBLICATIONS : publishes
```

API and worker are intentionally separate. The worker claims a queued row under a short write transaction; its lease makes duplicate processing observable and recoverable. Publication is one transaction, so a failed version/rule/evidence write cannot replace a current card. Last-good public versions are never removed by partial ingestion failures.

SQLite is selected for a dependency-free local baseline. WAL, foreign keys, busy timeout, indexed candidate queues, source freshness, idempotency expiry, and card-version lookup are implemented. It is not a multi-host or multi-replica deployment: use a real PostgreSQL backend before such deployment. Bounded request/evidence sizes, pagination, cursor expiry, response ETags, and one-shot workers constrain resource use.
