# Card Catalogue & Ingestion Service — API Reference & Startup Guide

This document provides a comprehensive reference for running the service, authentication mechanisms, and all available HTTP API endpoints with their exact input parameters and response formats.

---

## 1. Quick Start & Startup Guide

The service runs on Python 3.11+ powered by **FastAPI** + **Uvicorn** with a multi-engine persistence layer (**MySQL 8.0** in Docker Compose and zero-dependency **SQLite** fallback for local unit tests).

### 1.1 Local CLI Startup

From the `credit-card-rewards-service` directory:

```bash
# 1. Initialize database schema (SQLite or MySQL)
python3 -m credit_card_service init-db --db catalogue.sqlite

# 2. (Optional) Seed demo registered sources and 1.5% cashback fixtures
python3 -m credit_card_service seed-demo --db catalogue.sqlite

# 3. (Optional) Process queued scrape runs using the stateless worker
python3 -m credit_card_service worker --once --db catalogue.sqlite

# 4. Start the FastAPI HTTP API server with Uvicorn
python3 -m credit_card_service serve \
  --config config/sources.example.json \
  --db catalogue.sqlite \
  --host 127.0.0.1 \
  --port 8080 \
  --admin-secret "your-admin-secret" \
  --internal-secret "your-internal-secret"
```

*Note: Omitting `--admin-secret` and `--internal-secret` runs the server in no-auth mode for local development.*

### 1.2 Docker & Compose Startup (MySQL 8.0 + Load Balancer)

The service includes an enterprise-grade containerized topology in `compose.yaml`:
- **MySQL 8.0**: Relational persistence container with automated `mysqladmin ping` healthchecks and persistent storage.
- **API Nodes (`api-1`, `api-2`)**: Scaled FastAPI application instances connected to MySQL via `DATABASE_URL`.
- **Nginx (`load-balancer`)**: Reverse proxy distributing incoming traffic with healthcheck failover on port `8080`.

```bash
export CARD_ADMIN_BEARER="your-admin-secret"
export CARD_INTERNAL_BEARER="your-internal-secret"
# (Optional) Export your Gemini API key securely in the shell
export GEMINI_API_KEY="your-google-api-key"

docker compose up --build
```

### 1.3 Interactive Documentation & Dashboard

- **FastAPI Swagger UI**: Interactive API testing and schema explorer at `http://localhost:8080/docs`
- **FastAPI ReDoc**: High-performance API documentation at `http://localhost:8080/redoc`
- **Built-in Web UI Dashboard**: Visual card catalog, comparison view, rewards simulator, and administrative candidate review interface at `http://localhost:8080/` or `http://localhost:8080/ui`

---

## 2. Authentication & Common Standards

### 2.1 Security Schemes
- **Public Routes**: No authentication required.
- **Admin Routes**: Require `Authorization: Bearer <admin_secret>`.
- **Internal Routes**: Require `Authorization: Bearer <internal_secret>` (for recommendation engine callers).

### 2.2 Standard Error Response
All 4xx and 5xx errors return JSON in this envelope:
```json
{
  "error": {
    "code": "VALIDATION_ERROR",
    "message": "Detailed error message",
    "details": [],
    "request_id": "787bb7b5-2efc-44bf-80a5-f857eec03d20"
  }
}
```

### 2.3 Common Request Headers
- `Content-Type: application/json` (Required for all POST/PATCH bodies).
- `Idempotency-Key: <opaque-string>` (Required for state-changing admin operations like `POST /api/v1/admin/scrape-runs` and `POST .../decisions`).
- `If-None-Match: <etag>` (Supported on catalogue GET routes; returns `304 Not Modified` on cache hit).
- `X-Request-ID: <uuid>` (Echoed or generated on every response).

---

## 3. API Endpoints Overview

| Category | Method | Path | Auth | Description |
|---|---|---|---|---|
| **System** | `GET` | `/health` | Public | Database liveness & readiness check |
| **System** | `GET` | `/metrics` | Public | Service metrics summary |
| **System** | `GET` | `/metrics/prometheus` | Public | Prometheus text metrics |
| **System** | `GET` | `/` or `/ui` | Public | Built-in interactive HTML dashboard |
| **Catalogue** | `GET` | `/api/v1/cards` | Public | List current published cards with filters and pagination |
| **Catalogue** | `GET` | `/api/v1/cards/{card_id}` | Public | Detailed view of a card, rules, and terms |
| **Catalogue** | `POST` | `/api/v1/cards/compare` | Public | Compare 2–4 cards side-by-side |
| **Catalogue** | `POST` | `/api/v1/cards/simulate` | Public | Simulate monthly rewards across cards with min spend, caps & MCC exclusions |
| **Internal** | `GET` | `/api/v1/internal/catalogue-snapshots/current` | Internal | Complete version-consistent catalogue snapshot |
| **Internal** | `GET` | `/api/v1/internal/card-versions/{card_version_id}` | Internal | Immutable historical card terms version |
| **Admin** | `GET` | `/api/v1/admin/sources` | Admin | List registered card scraping sources |
| **Admin** | `GET` | `/api/v1/admin/config-sources` | Admin | List sources defined in JSON config |
| **Admin** | `POST` | `/api/v1/admin/scrape-runs` | Admin | Queue an asynchronous scrape run |
| **Admin** | `GET` | `/api/v1/admin/scrape-runs/{run_id}` | Admin | Get scrape run status, counters, and errors |
| **Admin** | `GET` | `/api/v1/admin/extraction-candidates` | Admin | List extracted card terms pending/completed review |
| **Admin** | `GET` | `/api/v1/admin/extraction-candidates/{candidate_id}` | Admin | Get extraction candidate details, diff, & evidence |
| **Admin** | `PATCH` | `/api/v1/admin/extraction-candidates/{candidate_id}` | Admin | Edit candidate fields before approval |
| **Admin** | `POST` | `/api/v1/admin/extraction-candidates/{candidate_id}/decisions` | Admin | Approve & publish or reject an extraction candidate |
| **Admin** | `POST` | `/api/v1/admin/discover` | Admin | Crawl bank directory URL to discover card links |
| **Admin** | `POST` | `/v1/scrape` or `/api/v1/admin/scrape` | Admin | Synchronous scrape of enabled sources |
| **Legacy** | `GET` | `/v1/cards` | Public | Legacy simple card listing |
| **Legacy** | `GET` | `/v1/cards/{card_id}` | Public | Legacy card lookup |
| **Legacy** | `GET` | `/v1/scrape-runs` | Public | Legacy scrape run history |

---

## 4. Endpoint Details (Inputs & Outputs)

### 4.1 System Endpoints

#### `GET /health`
- **Auth**: Public
- **Input**: None
- **Output (`200 OK`)**:
  ```json
  {
    "status": "ok",
    "database": "ready"
  }
  ```

#### `GET /metrics`
- **Auth**: Public
- **Input**: None
- **Output (`200 OK`)**:
  ```json
  {
    "service": "card_catalogue",
    "mode": "sqlite",
    "note": "Prometheus text is available at /metrics/prometheus"
  }
  ```

---

### 4.2 Public Catalogue Endpoints

#### `GET /api/v1/cards`
List published cards with optional filtering and cursor pagination.
- **Auth**: Public
- **Input (Query Parameters)**:
  - `q` *(string, optional, max 100 chars)*: Free text search across title and issuer.
  - `bank_id` *(UUID string, optional)*: Filter by issuer bank UUID.
  - `reward_type` *(enum string, optional)*: `"cashback"` | `"miles"` | `"points"` | `"mixed"`.
  - `simulation_support` *(enum string, optional)*: `"supported"` | `"unsupported"`.
  - `freshness` *(enum string, optional)*: `"fresh"` | `"stale"`.
  - `limit` *(integer, optional, range 1–100, default 20)*: Page size.
  - `cursor` *(string, optional)*: Opaque pagination cursor.
- **Input (Headers)**:
  - `If-None-Match` *(string, optional)*: ETag value for conditional caching.
- **Output (`200 OK`)**:
  ```json
  {
    "catalogue_revision": 4,
    "items": [
      {
        "card_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "name": "Standard Cash Card",
        "issuer": "Synthetic Bank",
        "bank_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "reward_type": "cashback",
        "headline_benefit": "1.5% cashback on all spend",
        "simulation_support": "supported",
        "freshness": "fresh",
        "currency": "SGD"
      }
    ],
    "next_cursor": "eyJmIjp7InJld2FyZF90eXBlIjoiY2FzaGJhY2sifSwiciI6NCwibyI6MjAsImUiOjE3NzM2NDMwMDB9.signature"
  }
  ```

#### `GET /api/v1/cards/{card_id}`
Fetch full card details, structured reward rules, and audit evidence.
- **Auth**: Public
- **Input (Path Parameters)**:
  - `card_id` *(UUID string, required)*: The unique ID of the card.
- **Input (Headers)**:
  - `If-None-Match` *(string, optional)*: ETag value.
- **Output (`200 OK`)**:
  ```json
  {
    "catalogue_revision": 4,
    "card": {
      "card_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "name": "Standard Cash Card",
      "issuer": "Synthetic Bank",
      "bank_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
      "reward_type": "cashback",
      "headline_benefit": "1.5% cashback on all spend",
      "simulation_support": "supported",
      "freshness": "fresh",
      "currency": "SGD"
    },
    "terms": {
      "schema_version": "card_terms.v1",
      "currency": "SGD",
      "annual_fee": "192.60",
      "annual_fee_status": "known",
      "rules": [
        {
          "rule_key": "base_spend",
          "kind": "cashback",
          "rate": "0.015",
          "reward_unit": "SGD",
          "match": { "type": "all" }
        }
      ],
      "cap_groups": [],
      "eligibility": {
        "min_age": 21,
        "min_income": "30000.00"
      }
    },
    "effective_from": "2026-01-01",
    "effective_to": null,
    "evidence": [
      {
        "evidence_id": "e4b52bb3-1b9b-46bf-811c-d7f027fc64a5",
        "field_path": "terms.rules[0].rate",
        "source_url": "https://example.invalid/cards/cash-terms.pdf",
        "document_type": "pdf",
        "locator": "Page 2 paragraph 1",
        "snippet": "Earn 1.5% cashback on eligible retail purchases.",
        "fetched_at": "2026-09-16T00:00:00Z"
      }
    ]
  }
  ```

#### `POST /api/v1/cards/compare`
Side-by-side comparison of 2 to 4 credit cards with computed differences.
- **Auth**: Public
- **Input (JSON Body)**:
  ```json
  {
    "card_ids": [
      "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
      "8a0ceb3c-2a6c-4c9c-8acc-1a9c6a2cba5c"
    ]
  }
  ```
- **Output (`200 OK`)**:
  ```json
  {
    "catalogue_revision": 4,
    "cards": [
      /* Card detail objects */
    ],
    "differences": [
      {
        "field": "annual_fee",
        "values": [
          {
            "card_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
            "display_value": "\"192.60\""
          },
          {
            "card_id": "8a0ceb3c-2a6c-4c9c-8acc-1a9c6a2cba5c",
            "display_value": "\"0.00\""
          }
        ]
      }
    ]
  }
  ```

#### `POST /api/v1/cards/simulate`
Simulate and rank rewards earned across credit cards for a monthly basket of transactions.
Enforces realistic Singapore banking rules:
- Minimum monthly qualifying spend thresholds (e.g. S$600 or S$800; falls back to base rate if unmet).
- Standard Singapore MCC exclusions (e.g. MCC 9399 government/taxes, 6540 e-wallet top-ups like GrabPay, 6300 insurance, 4900 utilities).
- Bonus category matching (e.g. 6% dining vs 0.3% base).
- Cap group enforcement (monthly or category rebate limits, e.g. S$70.00/month).

- **Auth**: Public
- **Input (JSON Body)**:
  ```json
  {
    "card_ids": ["9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"], // Optional: omit or null to simulate all published cards
    "monthly_spend": [
      {
        "amount": "400.00",
        "category": "dining",
        "mcc": "5812",
        "description": "Restaurants & Cafes"
      },
      {
        "amount": "300.00",
        "category": "groceries",
        "mcc": "5411",
        "description": "Supermarket Groceries"
      },
      {
        "amount": "150.00",
        "category": "utilities",
        "mcc": "4900",
        "description": "Electricity bill (MCC 4900 excluded)"
      }
    ]
  }
  ```
- **Output (`200 OK`)**:
  ```json
  {
    "total_monthly_spend": "850.00",
    "currency": "SGD",
    "transactions_count": 3,
    "ranked_cards": [
      {
        "rank": 1,
        "card_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
        "card_name": "Standard Cash Card",
        "issuer": "Synthetic Bank",
        "reward_type": "cashback",
        "currency": "SGD",
        "total_monthly_spend": "850.00",
        "total_reward_earned": "42.00",
        "effective_reward_rate": "4.94%",
        "minimum_spend_met": true,
        "minimum_monthly_spend": "600.00",
        "breakdown": [
          {
            "description": "Restaurants & Cafes",
            "amount": "400.00",
            "category": "dining",
            "mcc": "5812",
            "rate": "0.0600",
            "reward_earned": "24.00",
            "status": "applied",
            "reason": "Bonus 'dining' rate"
          },
          {
            "description": "Supermarket Groceries",
            "amount": "300.00",
            "category": "groceries",
            "mcc": "5411",
            "rate": "0.0600",
            "reward_earned": "18.00",
            "status": "applied",
            "reason": "Bonus 'groceries' rate"
          },
          {
            "description": "Electricity bill (MCC 4900 excluded)",
            "amount": "150.00",
            "category": "utilities",
            "mcc": "4900",
            "rate": "0.0000",
            "reward_earned": "0.00",
            "status": "excluded",
            "reason": "Excluded by MCC 4900 (e.g. government/e-wallet/utilities)"
          }
        ]
      }
    ]
  }
  ```

---

### 4.3 Internal Recommendation Engine Endpoints

#### `GET /api/v1/internal/catalogue-snapshots/current`
Consistent snapshot of all active cards and simulation-ready rules for the Recommendation Engine.
- **Auth**: `Authorization: Bearer <internal_secret>`
- **Input (Query Parameters)**:
  - `schema_version` *(string, optional, default `"card_terms.v1"`)*: Required to be `"card_terms.v1"`.
  - `simulation_support` *(enum string, optional, default `"supported"`)*: `"supported"` | `"all"`.
  - `market` *(string, optional, default `"SG"`)*.
  - `reward_type` *(enum string, optional)*: Filter by reward kind.
- **Output (`200 OK`)**:
  ```json
  {
    "schema_version": "card_catalogue_snapshot.v1",
    "card_terms_schema_version": "card_terms.v1",
    "catalogue_revision": 4,
    "generated_at": "2026-09-16T01:00:00Z",
    "cards": [
      {
        "card": { "card_id": "...", "name": "...", "issuer": "...", ... },
        "terms": { "schema_version": "card_terms.v1", "currency": "SGD", "rules": [ ... ], ... },
        "effective_from": "2026-01-01",
        "effective_to": null,
        "evidence": [ ... ]
      }
    ]
  }
  ```

#### `GET /api/v1/internal/card-versions/{card_version_id}`
Fetch immutable terms for a specific historical card version.
- **Auth**: `Authorization: Bearer <internal_secret>`
- **Input (Path Parameters)**:
  - `card_version_id` *(UUID string, required)*: The immutable version ID.
- **Output (`200 OK`)**:
  ```json
  {
    "published_in_catalogue_revision": 2,
    "superseded_in_catalogue_revision": 4,
    "card": { ... },
    "terms": { ... },
    "effective_from": "2026-01-01",
    "effective_to": "2026-06-30",
    "evidence": [ ... ]
  }
  ```

---

### 4.4 Admin & Ingestion Endpoints

#### `GET /api/v1/admin/sources`
List all registered card scraping sources and their status.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (Query Parameters)**:
  - `kind` *(string, optional)*
  - `access_review_status` *(string, optional)*
  - `freshness` *(string, optional)*
  - `limit` *(integer, optional, default 20)*
  - `cursor` *(string, optional)*
- **Output (`200 OK`)**:
  ```json
  {
    "items": [
      {
        "source_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "bank_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "name": "Synthetic Bank Main Page",
        "page_url": "https://example.invalid/cards",
        "kind": "bank_directory",
        "access_review_status": "approved",
        "freshness": "fresh"
      }
    ],
    "next_cursor": null
  }
  ```

#### `GET /api/v1/admin/config-sources`
List sources configured in the active configuration file.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Output (`200 OK`)**:
  ```json
  {
    "sources": [
      {
        "source_id": "operator-maintained-example",
        "issuer": "Illustrative Issuer",
        "name": "Illustrative Card",
        "card_id": "illustrative-card",
        "page_url": "https://example.invalid/operator-must-replace",
        "enabled": false,
        "request_delay_seconds": 2,
        "extraction_hints": {}
      }
    ]
  }
  ```

#### `POST /api/v1/admin/scrape-runs`
Queue an asynchronous scraping run for a registered source.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Headers**: `Idempotency-Key: <unique-key>` *(required)*
- **Input (JSON Body)**:
  ```json
  {
    "source_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "scope": {
      "type": "all"
    },
    "reason": "admin_refresh"
  }
  ```
  *(Supported reasons: `"scheduled_refresh"`, `"admin_refresh"`, `"parser_recheck"`)*
- **Output (`202 Accepted`)**:
  ```json
  {
    "run_id": "c1f72ef1-95ad-4d43-9ce7-59f7dfb08db9",
    "source_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "status": "queued",
    "requested_at": "2026-09-16T01:00:00Z",
    "status_url": "/api/v1/admin/scrape-runs/c1f72ef1-95ad-4d43-9ce7-59f7dfb08db9"
  }
  ```

#### `GET /api/v1/admin/scrape-runs/{run_id}`
Check status, counters, and errors of a scrape run.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (Path Parameters)**:
  - `run_id` *(UUID string, required)*
- **Output (`200 OK`)**:
  ```json
  {
    "run_id": "c1f72ef1-95ad-4d43-9ce7-59f7dfb08db9",
    "source_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
    "status": "succeeded",
    "started_at": "2026-09-16T01:00:01Z",
    "completed_at": "2026-09-16T01:00:05Z",
    "counters": {
      "pages_discovered": 1,
      "pages_fetched": 1,
      "candidates_extracted": 1,
      "unchanged": 0
    },
    "failures": []
  }
  ```

#### `GET /api/v1/admin/extraction-candidates`
List extracted card terms waiting for administrative review.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (Query Parameters)**:
  - `review_status` *(enum string, optional)*: `"pending"` | `"approved"` | `"rejected"`.
  - `source_id` *(UUID string, optional)*
  - `scrape_run_id` *(UUID string, optional)*
  - `bank_id` *(UUID string, optional)*
  - `material_change` *(boolean string, optional)*: `"true"` | `"false"`.
  - `limit` *(integer, optional, default 10)*
  - `cursor` *(string, optional)*
- **Output (`200 OK`)**:
  ```json
  {
    "items": [
      {
        "candidate_id": "184b25b6-7647-49d7-8e6f-402dc450b284",
        "review_status": "pending",
        "review_revision": 1,
        "source_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
        "scrape_run_id": "c1f72ef1-95ad-4d43-9ce7-59f7dfb08db9",
        "created_at": "2026-09-16T01:00:04Z"
      }
    ],
    "next_cursor": null
  }
  ```

#### `GET /api/v1/admin/extraction-candidates/{candidate_id}`
Retrieve candidate payload, validation issues, differences vs published, and audit evidence.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (Path Parameters)**:
  - `candidate_id` *(UUID string, required)*
- **Output (`200 OK`)**:
  ```json
  {
    "candidate_id": "184b25b6-7647-49d7-8e6f-402dc450b284",
    "review_status": "pending",
    "review_revision": 1,
    "card_summary": { ... },
    "extracted_terms": { ... },
    "validation_issues": [
      {
        "severity": "warning",
        "code": "MISSING_ANNUAL_FEE_WAIVER",
        "message": "First year waiver clause not explicitly detected."
      }
    ],
    "diff_against_current": [],
    "evidence": [ ... ]
  }
  ```

#### `PATCH /api/v1/admin/extraction-candidates/{candidate_id}`
Apply manual corrections to extracted terms before approving.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (Path Parameters)**:
  - `candidate_id` *(UUID string, required)*
- **Input (JSON Body)**:
  ```json
  {
    "expected_review_revision": 1,
    "edits": [
      {
        "field_path": "terms.rules[0].rate",
        "value": "0.020"
      }
    ],
    "reason": "Corrected rate based on paragraph 4.2 of terms PDF"
  }
  ```
- **Output (`200 OK`)**: Full updated candidate object with incremented `review_revision: 2`.

#### `POST /api/v1/admin/extraction-candidates/{candidate_id}/decisions`
Approve (which immediately publishes a new catalogue version) or reject a candidate.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Headers**: `Idempotency-Key: <unique-key>` *(required)*
- **Input (Path Parameters)**:
  - `candidate_id` *(UUID string, required)*
- **Input (JSON Body)**:
  ```json
  {
    "expected_review_revision": 1,
    "decision": "approve",
    "reason": "Verified against official PDF fee schedule",
    "acknowledged_warning_codes": [
      "MISSING_ANNUAL_FEE_WAIVER"
    ]
  }
  ```
  *(Set `"decision": "reject"` to decline publication).*
- **Output (`201 Created` / `200 OK`)**:
  ```json
  {
    "candidate_id": "184b25b6-7647-49d7-8e6f-402dc450b284",
    "decision": "approve",
    "publication": {
      "card_version_id": "e93f3d79-22a8-4e5a-a309-84724838634e",
      "catalogue_revision": 5,
      "published_at": "2026-09-16T01:10:00Z"
    }
  }
  ```

#### `POST /api/v1/admin/discover`
Crawl a public bank directory page to discover individual credit card URLs.
- **Auth**: `Authorization: Bearer <admin_secret>`
- **Input (JSON Body)**:
  ```json
  {
    "url": "https://example.invalid/personal/cards",
    "issuer": "Example Bank",
    "pattern": ".*personal/cards/.*",
    "scrape_now": false
  }
  ```
- **Output (`200 OK`)**:
  ```json
  {
    "status": "success",
    "discovered_count": 3,
    "cards": [
      {
        "title": "Example Platinum Card",
        "url": "https://example.invalid/personal/cards/platinum",
        "slug": "platinum"
      }
    ],
    "scraped_count": 0,
    "results": []
  }
  ```

#### `POST /v1/scrape` (or `POST /api/v1/admin/scrape`)
Execute a synchronous on-demand scrape of configured sources.
- **Auth**: `Authorization: Bearer <admin_secret>` (if configured)
- **Input (JSON Body)**:
  ```json
  {
    "source_ids": ["operator-maintained-example"]
  }
  ```
  *(Or `{}` to scrape all enabled sources in config).*
- **Output (`200 OK`)**:
  ```json
  {
    "status": "completed",
    "results": [
      {
        "source_id": "operator-maintained-example",
        "status": "success",
        "fetched_at": "2026-09-16T01:15:00Z"
      }
    ]
  }
  ```

---

### 4.5 Legacy Compatibility Endpoints

Kept for backwards compatibility with earlier integrations:

- `GET /v1/cards?issuer=&source_id=`: Returns `{"cards": [...]}`.
- `GET /v1/cards/{card_id}`: Returns card JSON or `404 NOT_FOUND`.
- `GET /v1/scrape-runs?limit=50`: Returns `{"scrape_runs": [...]}`.

---

## 5. Advanced Intelligence & Singapore Domain Modeling

### 5.1 Google Gemini Flash LLM Extraction
The service includes a hybrid extraction engine in [`credit_card_service/extract.py`](file:///Users/cled/Desktop/FSD/credit-card-rewards-service/credit_card_service/extract.py):
- **Deterministic Regex/Heuristics Engine**: Zero-network offline fallback that extracts base cashback, miles, annual fees, and standard MCC exclusions.
- **Gemini Flash LLM Engine**: Connected to `https://generativelanguage.googleapis.com/v1beta/models/gemini-flash-latest:generateContent`.
  - Triggered when `use_llm: true` or `use_gemini: true` is configured in the source, or if `GEMINI_API_KEY` is present.
  - Automatically parses complex multi-sentence terms from HTML or bank PDFs into structured `CardTermsV1`.
  - Extracts minimum monthly qualifying spend thresholds (e.g. S$600, S$800).
  - Extracts monthly rebate/miles cap groups (e.g. S$70 per calendar month).
  - Extracts fine-print MCC exclusions and exact evidence quote snippets.

### 5.2 Zero-Dependency PDF Text Extraction
Bank credit card product pages often link to terms & conditions and fee schedules formatted as PDF files.
[`extract_pdf_text`](file:///Users/cled/Desktop/FSD/credit-card-rewards-service/credit_card_service/extract.py) provides a pure Python standard library PDF parser:
- Decompresses `FlateDecode` streams using built-in `zlib`.
- Parses PDF `BT ... ET` text drawing operators (`Tj`, `TJ`).
- Decodes character strings without requiring `pypdf`, `pdfplumber`, or other external dependencies.

### 5.3 Singapore Credit Card Rule Fidelity
1. **Minimum Spend Requirements**: Real cards (e.g. UOB One, OCBC 365, DBS Live Fresh) require reaching a monthly spend threshold to unlock higher rebate rates. If not met, the engine automatically falls back to base spend rates.
2. **Cap Groups**: Tracks monthly and category bonus caps, clamping accumulated rewards at the cap limit.
3. **MCC Exclusions**: Enforces exclusion lists for transactions that do not earn rewards in Singapore:
   - `MCC 9399`: Government Services, Tax Payments & Town Councils
   - `MCC 6540`: Stored Value Cards & E-Wallet Top-ups (GrabPay, ShopeePay)
   - `MCC 6300`: Insurance Sales, Underwriting & Premiums
   - `MCC 4900`: Utilities (Electricity, Gas, Water)

