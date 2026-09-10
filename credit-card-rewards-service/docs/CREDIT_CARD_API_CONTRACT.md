# Card Catalogue & Ingestion Service — API & Integration Contract

**Version:** Proposed v1.0  
**Date:** 7 September 2026  
**Audience:** Catalogue-service, Recommendation Engine, frontend, admin frontend, auth, and testing teammates  
**Status:** Design contract for implementation and mocks; these endpoints do not exist yet  
**Related design:** [Card Catalogue & Ingestion Service Project Plan](CREDIT_CARD_SYSTEM_PLAN.md)

## 1. Service responsibility

### Inputs

- Registered, allowlisted card-information sources.
- Retrieved HTML pages and terms documents.
- Administrator review decisions.

### Outputs

- A public catalogue of current published cards.
- Structured card details and comparisons.
- Version-consistent `CardTermsV1` snapshots for the Recommendation Engine.
- Historical immutable card versions.
- Scrape status, extraction candidates, validation results, and evidence for administrators.

This service owns scraping, source snapshots, extraction, normalization, identity resolution, consolidation, validation, review, card-term versioning, and catalogue publication.

It does **not** own expenses, spending profiles, reward simulation, personalized ranking, user reward preferences, or saved recommendation runs. Those belong to the Expense Service and Recommendation Engine.

```text
Approved sources ── fetch/extract/consolidate ──▶ Catalogue Service
Admin frontend ───────── review/publish ───────▶ Catalogue Service
Public frontend ◀──────── cards/details ─────── Catalogue Service
Recommendation Engine ◀── versioned terms ──── Catalogue Service
```

## 2. Endpoint summary

All paths are relative to `/api/v1`.

| Method | Path | Input | Output | Access |
|---|---|---|---|---|
| `GET` | `/cards` | Filters and pagination | Current card summaries | Public |
| `GET` | `/cards/{card_id}` | Product ID | Current published card details | Public |
| `POST` | `/cards/compare` | 2–4 product IDs | Structured product differences | Public |
| `GET` | `/internal/catalogue-snapshots/current` | Schema/support filters | Version-consistent full catalogue | Recommendation Engine |
| `GET` | `/internal/card-versions/{card_version_id}` | Immutable version ID | Historical published card version | Recommendation Engine |
| `GET` | `/admin/sources` | Filters and pagination | Registered sources and freshness | Admin |
| `POST` | `/admin/scrape-runs` | Registered source and scope | Queued ingestion job | Admin |
| `GET` | `/admin/scrape-runs/{run_id}` | Job ID | Progress, counters, and failures | Admin |
| `GET` | `/admin/extraction-candidates` | Review filters and pagination | Candidate payloads, evidence, and diffs | Admin |
| `PATCH` | `/admin/extraction-candidates/{candidate_id}` | Expected revision and edits | Updated candidate | Admin |
| `POST` | `/admin/extraction-candidates/{candidate_id}/decisions` | Expected revision and decision | Review decision and optional publication | Admin |

## 3. Common conventions

### 3.1 Requests, identity, and authorization

- JSON bodies use `Content-Type: application/json`. GET requests have no body.
- Public catalogue endpoints do not require authentication unless the application gateway applies a broader policy.
- Admin endpoints require `Authorization: Bearer <access-token>` with an admin role supplied by the shared auth component.
- Internal endpoints require service authentication. For the MVP this may be a dedicated bearer token accepted only from the Recommendation Engine; production deployment should prefer the team's standard workload identity or mTLS.
- Never authorize an internal request using a caller-controlled header alone.
- Unknown request fields return `422 VALIDATION_ERROR`. Clients must tolerate additional response fields within v1.
- Every response includes an `X-Request-ID` header. Services should propagate an incoming valid request ID or generate one.

### 3.2 IDs, money, rates, and dates

- IDs are UUID strings.
- Money is a decimal string such as `"196.20"`; never send money as a JSON floating-point number.
- Rates are decimal-fraction strings such as `"0.015"` for 1.5%.
- Counts and revisions are JSON integers.
- Dates use `YYYY-MM-DD`.
- Timestamps use UTC ISO 8601.
- Effective intervals are half-open: `[effective_from, effective_to)`. A null `effective_to` is open-ended, not proof that terms are permanent.
- Unknown, zero, unlimited, and not applicable are distinct states.
- Singapore reward periods use `Asia/Singapore` unless the published terms specify another policy.

### 3.3 Pagination

List endpoints accept:

| Field | Type | Default / validation |
|---|---|---|
| `limit` | integer | Default 20; range 1–100. Candidate review defaults to 10 |
| `cursor` | opaque string | Optional |

Responses return `items` and `next_cursor`, which is null on the final page. Cursors bind the filters and listing revision. Reusing a cursor with different filters returns `422 CURSOR_FILTER_MISMATCH`; using an expired or replaced revision returns `409 CURSOR_EXPIRED`.

Empty lists return `200` with `items: []`.

### 3.4 Idempotency and concurrency

Require `Idempotency-Key` on:

- `POST /admin/scrape-runs`
- `POST /admin/extraction-candidates/{candidate_id}/decisions`

Retain keys for at least 24 hours, scoped to caller and endpoint, together with the request hash and original response.

- Same key and same body: replay the original status, headers, and body.
- Same key and different body: `409 IDEMPOTENCY_CONFLICT`.
- Replays remain valid after a candidate has been published.

Candidate updates and decisions include `expected_review_revision`. A stale revision returns `409 REVISION_CONFLICT`.

### 3.5 Conditional catalogue reads

Catalogue responses include:

```text
ETag: "catalogue-7"
```

A GET with `If-None-Match: "catalogue-7"` returns `304 Not Modified` with no body if the selected representation has not changed. The ETag must include representation-affecting filters, schema version, and catalogue revision; the literal example is illustrative.

### 3.6 Error envelope

```json
{
  "error": {
    "code": "PUBLICATION_VALIDATION_FAILED",
    "message": "The candidate contains unresolved critical fields.",
    "details": [
      {"field": "/terms/rules/0/rate", "reason": "conflicting_official_sources"}
    ],
    "request_id": "d0000000-0000-4000-8000-000000000001"
  }
}
```

| HTTP | Meaning / example codes |
|---|---|
| `400` | Malformed JSON: `MALFORMED_REQUEST` |
| `401` | Missing or invalid identity: `UNAUTHENTICATED` |
| `403` | Valid identity without required role: `FORBIDDEN` |
| `404` | Missing, unpublished, or unavailable resource: `NOT_FOUND` |
| `409` | `REVISION_CONFLICT`, `IDEMPOTENCY_CONFLICT`, `SOURCE_ALREADY_RUNNING`, `CURSOR_EXPIRED`, `CANDIDATE_NOT_PENDING` |
| `415` | Unsupported content type: `UNSUPPORTED_MEDIA_TYPE` |
| `422` | `VALIDATION_ERROR`, `CURSOR_FILTER_MISMATCH`, `UNSUPPORTED_SCHEMA_VERSION`, `PUBLICATION_VALIDATION_FAILED` |
| `429` | `RATE_LIMITED`; include `Retry-After` |
| `503` | `STORAGE_UNAVAILABLE`, `SERVICE_UNAVAILABLE` |
| `500` | `INTERNAL_ERROR`; never expose a stack trace |

## 4. Shared catalogue objects

### 4.1 BankSummaryV1

```json
{
  "bank_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
  "name": "Demo Bank"
}
```

### 4.2 CardSummaryV1

```json
{
  "card_id": "11111111-1111-4111-8111-111111111111",
  "card_version_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  "name": "Demo Everyday Card",
  "bank": {
    "bank_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    "name": "Demo Bank"
  },
  "network": "visa",
  "market": "SG",
  "reward_type": "cashback",
  "currency": "SGD",
  "annual_fee": "0.00",
  "annual_fee_status": "known",
  "headline": "1.5% cashback on eligible purchases",
  "simulation_support": "supported",
  "freshness": "fresh",
  "last_verified_at": "2026-09-07T01:00:00Z"
}
```

Enums:

- `network = visa | mastercard | american_express | unionpay | other | unknown`
- `reward_type = cashback | miles | points | mixed`
- `annual_fee_status = known | unknown`
- `simulation_support = supported | unsupported`
- `freshness = fresh | stale`

`headline` is display text. Consumers must calculate from typed terms, never from the headline.

### 4.3 MatchV1

```json
{
  "category_keys": ["dining"],
  "mccs": [],
  "merchant_keys": [],
  "channels": ["online"],
  "contactless": null,
  "countries": [],
  "currencies": []
}
```

Semantics:

- Fields combine with AND.
- Entries inside one array combine with OR.
- An empty array imposes no restriction.
- Null `contactless` imposes no contactless restriction.
- MCC is a four-digit string.
- Semantic category and bank MCC are separate dimensions.
- Supported category keys for v1 are `dining, groceries, transport, fuel, shopping, travel, entertainment, utilities, other, unknown`.
- Supported channels are `online, in_store`.

### 4.4 RewardRuleV1

```json
{
  "rule_key": "base_cashback",
  "kind": "cashback",
  "rate": "0.015",
  "reward_unit": "SGD",
  "match": {
    "category_keys": [],
    "mccs": [],
    "merchant_keys": [],
    "channels": [],
    "contactless": null,
    "countries": [],
    "currencies": []
  },
  "excluded_mccs": [],
  "excluded_merchant_keys": [],
  "qualification_counter_key": null,
  "minimum_spend": null,
  "minimum_transactions": null,
  "stacking_policy": "base",
  "period": "calendar_month",
  "cap_group_keys": [],
  "rounding": {
    "stage": "period_rule_total",
    "mode": "floor",
    "decimal_places": 2
  }
}
```

Contract rules:

- `kind = cashback | miles | points`.
- `reward_unit` must agree with `kind`.
- `stacking_policy = base | add_to_base | replace_base` in v1.
- `period = calendar_month` is numerically supported in the MVP. Other values may be represented only with an `unsupported_reason` until the consumer implements them.
- A non-null threshold requires a `qualification_counter_key` that resolves within the same `CardTermsV1`.
- MVP permits one base rule. Overlapping bonuses require deterministic stacking or make the card unsupported.
- V1 rounding stage is `period_rule_total`; modes are `floor | half_up`.
- Rules that depend on transaction-level rounding, earn blocks, statement periods, or quarters must be marked unsupported unless fully represented by a later schema version.

### 4.5 QualificationCounterV1

```json
{
  "counter_key": "eligible_purchase_spend",
  "match": {
    "category_keys": [],
    "mccs": [],
    "merchant_keys": [],
    "channels": [],
    "contactless": null,
    "countries": [],
    "currencies": []
  },
  "excluded_mccs": [],
  "excluded_merchant_keys": [],
  "period": "calendar_month",
  "refund_policy": "linked_same_period"
}
```

The counter defines both qualifying amount and qualifying purchase count. It does not itself award rewards.

### 4.6 CapGroupV1

```json
{
  "cap_key": "monthly_dining_bonus",
  "mode": "limited",
  "amount": "15.00",
  "unit": "SGD",
  "period": "calendar_month",
  "scope": "reward"
}
```

- `mode = limited | unlimited | unknown`.
- Limited requires a nonnegative amount.
- Unlimited and unknown require `amount: null`.
- Unknown referenced caps prevent numerical simulation.
- MVP supports reward caps. Spend caps require an explicit later schema extension.

### 4.7 EligibilityV1

```json
{
  "min_age": 21,
  "income_requirements": [
    {"residency": "citizen", "minimum_annual_income": "30000.00"},
    {"residency": "permanent_resident", "minimum_annual_income": "30000.00"},
    {"residency": "foreigner", "minimum_annual_income": "45000.00"}
  ],
  "additional_conditions": []
}
```

Nullable minimums mean unknown. An empty requirements array means no documented requirement of that type; it must not replace failed extraction. Conditions that are captured only as text appear in `additional_conditions` and may require downstream conditional handling.

### 4.8 BenefitV1

```json
{
  "type": "lounge_access",
  "description": "Two complimentary visits per membership year.",
  "conditions": ["Registration is required."]
}
```

Benefits are descriptive and have no automatic monetary value.

### 4.9 EvidenceV1

```json
{
  "evidence_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
  "field_path": "/terms/rules/0/rate",
  "source_url": "https://example.test/cards/demo-everyday",
  "document_type": "product_page",
  "locator": "section:cashback-details",
  "snippet": "Earn 1.5% cashback on eligible purchases.",
  "fetched_at": "2026-09-07T00:30:00Z"
}
```

`field_path` is a JSON Pointer into the returned card document. Public responses expose source links and short snippets, never raw-storage keys.

### 4.10 CardTermsV1

```json
{
  "schema_version": "card_terms.v1",
  "currency": "SGD",
  "annual_fee": "0.00",
  "annual_fee_status": "known",
  "first_year_waiver": "not_applicable",
  "eligibility": {
    "min_age": 21,
    "income_requirements": [
      {"residency": "citizen", "minimum_annual_income": "30000.00"}
    ],
    "additional_conditions": []
  },
  "rules": [
    {
      "rule_key": "base_cashback",
      "kind": "cashback",
      "rate": "0.015",
      "reward_unit": "SGD",
      "match": {
        "category_keys": [],
        "mccs": [],
        "merchant_keys": [],
        "channels": [],
        "contactless": null,
        "countries": [],
        "currencies": []
      },
      "excluded_mccs": [],
      "excluded_merchant_keys": [],
      "qualification_counter_key": null,
      "minimum_spend": null,
      "minimum_transactions": null,
      "stacking_policy": "base",
      "period": "calendar_month",
      "cap_group_keys": [],
      "rounding": {"stage": "period_rule_total", "mode": "floor", "decimal_places": 2}
    }
  ],
  "qualification_counters": [],
  "cap_groups": [],
  "benefits": [],
  "unsupported_reasons": []
}
```

Additional semantics:

- `first_year_waiver = guaranteed | conditional | none | not_applicable | unknown`.
- Unknown annual fee is not zero.
- A partial rule array cannot be published with `simulation_support = supported`.
- Critical terms include rates, caps, minimum spend/count, exclusions, eligibility, dates, fees, rounding, period, and stacking.
- Promotions and acquisition gifts are separate records and are not included in recurring `CardTermsV1` v1.

### 4.11 CardDetailV1

```text
{
  "catalogue_revision": integer,
  "card": CardSummaryV1,
  "terms": CardTermsV1,
  "effective_from": date | null,
  "effective_to": date | null,
  "evidence": EvidenceV1[]
}
```

## 5. Public catalogue APIs

### 5.1 GET /cards

Optional query parameters:

| Field | Type | Default / validation |
|---|---|---|
| `q` | string | Omitted; case-insensitive name/bank search, max 100 characters |
| `bank_id` | UUID | Omitted |
| `reward_type` | enum | Omitted; enum from CardSummaryV1 |
| `simulation_support` | enum | Omitted; `supported | unsupported` |
| `freshness` | enum | Omitted; `fresh | stale` |
| `limit`, `cursor` | pagination | Section 3.3 |

Example:

```http
GET /api/v1/cards?reward_type=cashback&simulation_support=supported&limit=20
```

**Output: `200`**

```json
{
  "catalogue_revision": 7,
  "items": [
    {
      "card_id": "11111111-1111-4111-8111-111111111111",
      "card_version_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      "name": "Demo Everyday Card",
      "bank": {"bank_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "name": "Demo Bank"},
      "network": "visa",
      "market": "SG",
      "reward_type": "cashback",
      "currency": "SGD",
      "annual_fee": "0.00",
      "annual_fee_status": "known",
      "headline": "1.5% cashback on eligible purchases",
      "simulation_support": "supported",
      "freshness": "fresh",
      "last_verified_at": "2026-09-07T01:00:00Z"
    }
  ],
  "next_cursor": null
}
```

Only currently published products appear. Items are ordered by normalized bank name, card name, and card ID. Invalid filters return `422`.

### 5.2 GET /cards/{card_id}

**Input:** Product UUID in the path; no body.

**Output: `200 CardDetailV1`**

```json
{
  "catalogue_revision": 7,
  "card": {
    "card_id": "11111111-1111-4111-8111-111111111111",
    "card_version_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "name": "Demo Everyday Card",
    "bank": {"bank_id": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb", "name": "Demo Bank"},
    "network": "visa",
    "market": "SG",
    "reward_type": "cashback",
    "currency": "SGD",
    "annual_fee": "0.00",
    "annual_fee_status": "known",
    "headline": "1.5% cashback on eligible purchases",
    "simulation_support": "supported",
    "freshness": "fresh",
    "last_verified_at": "2026-09-07T01:00:00Z"
  },
  "terms": {
    "schema_version": "card_terms.v1",
    "currency": "SGD",
    "annual_fee": "0.00",
    "annual_fee_status": "known",
    "first_year_waiver": "not_applicable",
    "eligibility": {
      "min_age": 21,
      "income_requirements": [
        {"residency": "citizen", "minimum_annual_income": "30000.00"}
      ],
      "additional_conditions": []
    },
    "rules": [
      {
        "rule_key": "base_cashback",
        "kind": "cashback",
        "rate": "0.015",
        "reward_unit": "SGD",
        "match": {
          "category_keys": [],
          "mccs": [],
          "merchant_keys": [],
          "channels": [],
          "contactless": null,
          "countries": [],
          "currencies": []
        },
        "excluded_mccs": [],
        "excluded_merchant_keys": [],
        "qualification_counter_key": null,
        "minimum_spend": null,
        "minimum_transactions": null,
        "stacking_policy": "base",
        "period": "calendar_month",
        "cap_group_keys": [],
        "rounding": {"stage": "period_rule_total", "mode": "floor", "decimal_places": 2}
      }
    ],
    "qualification_counters": [],
    "cap_groups": [],
    "benefits": [],
    "unsupported_reasons": []
  },
  "effective_from": "2026-09-01",
  "effective_to": null,
  "evidence": [
    {
      "evidence_id": "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
      "field_path": "/terms/rules/0/rate",
      "source_url": "https://example.test/cards/demo-everyday",
      "document_type": "product_page",
      "locator": "section:cashback-details",
      "snippet": "Earn 1.5% cashback on eligible purchases.",
      "fetched_at": "2026-09-07T00:30:00Z"
    }
  ]
}
```

Use the complete Section 4 fixtures for mocks and contract tests. Missing or unpublished products return `404`. A malformed UUID returns `422`.

### 5.3 POST /cards/compare

**Input:** 2–4 distinct product UUIDs.

```json
{
  "card_ids": [
    "11111111-1111-4111-8111-111111111111",
    "22222222-2222-4222-8222-222222222222"
  ]
}
```

**Output: `200`**

```text
{
  "catalogue_revision": integer,
  "cards": CardDetailV1[],
  "differences": [
    {
      "field": string,
      "values": [{"card_id": UUID, "display_value": string}]
    }
  ]
}
```

Return cards in request order under one catalogue revision. Difference fields are `annual_fee, first_year_waiver, eligibility, rewards, caps, benefits`. Display values are presentation-only; consumers use typed terms.

If any card is unavailable, return `404` for the request. Duplicate IDs or an invalid count return `422`.

## 6. Recommendation Engine integration

### 6.1 GET /internal/catalogue-snapshots/current

This is the primary Recommendation Engine contract. It avoids an N+1 request per card and guarantees one consistent catalogue revision.

Query parameters:

| Field | Required | Default / validation |
|---|---|---|
| `schema_version` | No | `card_terms.v1`; unsupported versions return `422` |
| `simulation_support` | No | `supported`; may be `supported | all` |
| `reward_type` | No | Omitted; optional reward-type filter |
| `market` | No | `SG` for MVP |

**Output: `200 CardCatalogueSnapshotV1`**

```text
{
  "schema_version": "card_catalogue_snapshot.v1",
  "card_terms_schema_version": "card_terms.v1",
  "catalogue_revision": integer,
  "generated_at": timestamp,
  "cards": [
    {
      "card": CardSummaryV1,
      "terms": CardTermsV1,
      "effective_from": date | null,
      "effective_to": date | null,
      "evidence": EvidenceV1[]
    }
  ]
}
```

Rules:

- All entries are selected under one committed catalogue revision.
- The default response includes only fresh, published, supported cards.
- `simulation_support=all` includes unsupported and stale products with reasons/statuses.
- Cards are ordered by `card_id`; consumers must not infer rank from order.
- The Recommendation Engine records `catalogue_revision`, `card_version_id`, and its own algorithm version with every result.
- A later publication does not alter this response in transit or mutate historical versions.
- Use `ETag` and `If-None-Match` to avoid repeatedly transferring unchanged catalogues.

### 6.2 GET /internal/card-versions/{card_version_id}

Returns one immutable published version for a trusted service consumer.

**Output: `200`**

```text
{
  "published_in_catalogue_revision": integer,
  "superseded_in_catalogue_revision": integer | null,
  "card": CardSummaryV1,
  "terms": CardTermsV1,
  "effective_from": date | null,
  "effective_to": date | null,
  "evidence": EvidenceV1[]
}
```

The response never substitutes the current version. Unknown or unpublished version IDs return `404`.

## 7. Admin source and ingestion APIs

### 7.1 SourceSummaryV1

```json
{
  "source_id": "33333333-3333-4333-8333-333333333333",
  "name": "Demo Bank Official Cards",
  "domain": "example.test",
  "kind": "official_bank",
  "adapter_key": "demo_bank_v1",
  "enabled": true,
  "access_review_status": "approved",
  "refresh_interval_hours": 168,
  "last_attempt_at": "2026-09-07T00:00:00Z",
  "last_success_at": "2026-09-07T00:02:00Z",
  "freshness": "fresh"
}
```

Enums:

- `kind = official_bank | official_terms | approved_comparison`
- `access_review_status = pending | approved | rejected`
- `freshness = fresh | stale | never_fetched`

Credentials, raw storage locations, and unrestricted URL patterns are not returned.

### 7.2 GET /admin/sources

Optional filters: `enabled`, `kind`, `access_review_status`, `freshness`, `limit`, and `cursor`.

**Output: `200`**

```json
{
  "items": [
    {
      "source_id": "33333333-3333-4333-8333-333333333333",
      "name": "Demo Bank Official Cards",
      "domain": "example.test",
      "kind": "official_bank",
      "adapter_key": "demo_bank_v1",
      "enabled": true,
      "access_review_status": "approved",
      "refresh_interval_hours": 168,
      "last_attempt_at": null,
      "last_success_at": null,
      "freshness": "never_fetched"
    }
  ],
  "next_cursor": null
}
```

### 7.3 POST /admin/scrape-runs

Requires admin authentication and `Idempotency-Key`.

**Input:**

```json
{
  "source_id": "33333333-3333-4333-8333-333333333333",
  "scope": {
    "type": "all"
  },
  "reason": "scheduled_refresh"
}
```

Scope is either:

```json
{"type": "all"}
```

or:

```json
{
  "type": "documents",
  "document_ids": ["44444444-4444-4444-8444-444444444444"]
}
```

`reason = scheduled_refresh | admin_refresh | parser_recheck`. A parser recheck uses retained snapshots and should be a distinct internal worker mode; it must not silently perform live fetching.

**Output: `202`**

```json
{
  "run_id": "55555555-5555-4555-8555-555555555555",
  "source_id": "33333333-3333-4333-8333-333333333333",
  "status": "queued",
  "created_at": "2026-09-07T02:00:00Z",
  "status_url": "/api/v1/admin/scrape-runs/55555555-5555-4555-8555-555555555555"
}
```

Reject disabled or unapproved sources with `422 SOURCE_NOT_ENABLED`. Unknown documents or documents outside the source return `422`. An active job for the same source/scope returns `409 SOURCE_ALREADY_RUNNING` and includes its run ID in `details`.

### 7.4 GET /admin/scrape-runs/{run_id}

**Output: `200 ScrapeRunV1`**

```json
{
  "run_id": "55555555-5555-4555-8555-555555555555",
  "source_id": "33333333-3333-4333-8333-333333333333",
  "status": "partial",
  "reason": "admin_refresh",
  "created_at": "2026-09-07T02:00:00Z",
  "started_at": "2026-09-07T02:00:02Z",
  "finished_at": "2026-09-07T02:02:00Z",
  "attempt": 1,
  "counters": {
    "discovered": 3,
    "fetched": 3,
    "not_modified": 0,
    "parsed": 2,
    "candidates_created": 2,
    "unchanged": 0,
    "failed": 1
  },
  "failures": [
    {
      "document_id": "44444444-4444-4444-8444-444444444444",
      "stage": "extract",
      "code": "REQUIRED_SECTION_MISSING",
      "message": "The reward-rate table was not found.",
      "retryable": false
    }
  ],
  "error_summary": "One document requires adapter review."
}
```

`status = queued | running | succeeded | partial | failed`. A partial or failed job does not deactivate, delete, or replace current published cards.

## 8. Admin candidate and publication APIs

### 8.1 ValidationIssueV1

```json
{
  "severity": "error",
  "code": "MISSING_EVIDENCE",
  "field_path": "/terms/rules/0/rate",
  "message": "A supported reward rate requires field evidence."
}
```

`severity = error | warning`. Errors block approval. Warnings require explicit acknowledgement in the approval decision.

### 8.2 CandidateDiffV1

```json
{
  "operation": "replace",
  "field_path": "/terms/rules/0/rate",
  "old_value": "0.015",
  "new_value": "0.016",
  "material": true
}
```

`operation = add | replace | remove`. Diffs are generated server-side against the indicated base version.

### 8.3 ExtractionCandidateV1

```text
{
  "candidate_id": UUID,
  "scrape_run_id": UUID,
  "review_status": "pending" | "approved" | "rejected",
  "review_revision": integer,
  "created_at": timestamp,
  "updated_at": timestamp,
  "proposed_identity": {
    "card_id": UUID | null,
    "bank_id": UUID,
    "market": string,
    "product_key": string,
    "match_status": "matched" | "new_product" | "needs_review"
  },
  "base_card_version_id": UUID | null,
  "card": CardSummaryV1-like candidate fields,
  "terms": CardTermsV1,
  "validation_issues": ValidationIssueV1[],
  "diff": CandidateDiffV1[],
  "evidence": EvidenceV1[],
  "content_hash": string,
  "parser_version": string,
  "schema_version": "card_candidate.v1"
}
```

Candidate evidence may include non-winning source observations and conflicts that are not exposed on public card details.

### 8.4 GET /admin/extraction-candidates

Optional filters:

| Field | Type | Default / validation |
|---|---|---|
| `review_status` | enum | `pending` |
| `source_id` | UUID | Omitted |
| `scrape_run_id` | UUID | Omitted |
| `bank_id` | UUID | Omitted |
| `has_errors` | boolean | Omitted |
| `material_change` | boolean | Omitted |
| `limit`, `cursor` | pagination | Default limit 10 |

**Output: `200`**

```text
{
  "items": ExtractionCandidateV1[],
  "next_cursor": string | null
}
```

Order by creation time descending, then candidate ID. The endpoint returns complete candidate records so the initial admin frontend does not require a separate detail endpoint. Keep the default page small.

### 8.5 PATCH /admin/extraction-candidates/{candidate_id}

Requires admin authentication. Only pending candidates can be edited.

**Input:**

```json
{
  "expected_review_revision": 2,
  "edits": [
    {
      "field_path": "/terms/rules/0/rate",
      "value": "0.016"
    }
  ],
  "reason": "Corrected from the official terms table."
}
```

Rules:

- `edits` contains 1–50 entries.
- `field_path` is a JSON Pointer in an allowlist of editable candidate fields.
- Identity, IDs, audit fields, source snapshots, evidence text, content hash, and review status cannot be edited through this endpoint.
- The server applies all edits, reruns normalization/validation/diff generation, increments `review_revision`, and records an append-only audit patch.
- Values must use the target field's JSON type. Money and rates remain strings.
- Removing a nullable/optional value uses `value: null`; removing array entries requires replacing the complete array.

**Output: `200 ExtractionCandidateV1`**

A stale revision returns `409 REVISION_CONFLICT` with the current revision. Non-pending candidates return `409 CANDIDATE_NOT_PENDING`.

### 8.6 POST /admin/extraction-candidates/{candidate_id}/decisions

Requires admin authentication and `Idempotency-Key`.

**Approve input:**

```json
{
  "expected_review_revision": 3,
  "decision": "approve",
  "reason": "Verified against the official product page and terms document.",
  "acknowledged_warning_codes": ["EFFECTIVE_FROM_UNCONFIRMED"]
}
```

**Reject input:**

```json
{
  "expected_review_revision": 3,
  "decision": "reject",
  "reason": "The page describes a discontinued product variant.",
  "acknowledged_warning_codes": []
}
```

`reason` is required and limited to 1,000 characters. Approval is rejected when:

- Any validation error remains.
- A warning has not been acknowledged.
- Product identity still needs review.
- Critical fields lack evidence.
- The candidate claims supported simulation with incomplete rules.
- The effective interval would overlap another current version for the same product/cohort.

**Approve output: `201`**

```json
{
  "decision_id": "66666666-6666-4666-8666-666666666666",
  "candidate_id": "77777777-7777-4777-8777-777777777777",
  "decision": "approve",
  "decided_at": "2026-09-07T03:00:00Z",
  "publication": {
    "card_id": "11111111-1111-4111-8111-111111111111",
    "card_version_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "catalogue_revision": 7
  }
}
```

**Reject output: `201`**

```json
{
  "decision_id": "66666666-6666-4666-8666-666666666666",
  "candidate_id": "77777777-7777-4777-8777-777777777777",
  "decision": "reject",
  "decided_at": "2026-09-07T03:00:00Z",
  "publication": null
}
```

Approval creates the immutable card version, rules, caps, counters, benefits, evidence links, publication record, and catalogue-revision increment in one database transaction.

## 9. Consolidation behavior exposed by the contract

Consumers should understand these rules because they determine published values:

1. Product identity is based on bank, market, and stable product key—not display-name similarity alone.
2. Applicable official terms/amendments take precedence over official product pages, which take precedence over approved comparison sources.
3. Source precedence is evaluated per field, effective date, and customer cohort.
4. Losing observations and conflicts remain visible to administrators.
5. Unresolved conflicts in critical fields block supported publication.
6. A newer fetch timestamp does not automatically mean newer applicable terms.
7. Unknown fee is not zero; unknown cap is not unlimited; blank source text is not proof of absence.
8. Promotions are kept separate from recurring card terms.

Examples:

| Source wording / condition | Normalized behavior |
|---|---|
| “Up to 6% cashback” | Display-only claim until all qualifying/base/bonus rules are resolved |
| “Minimum S$800 spend” | Amount, currency, qualifying counter, period, and unlocked component |
| “Capped at S$50” | Limit, reward/spend unit, scope, period, and affected rules |
| “No cap” | `mode = unlimited`; distinct from unknown |
| Blank annual fee | `annual_fee = null`, `annual_fee_status = unknown` |
| “First year waived” | Ordinary annual fee plus an explicit waiver status/condition |
| “Online dining” | Separate category and channel conditions |
| “4 miles per dollar” | `kind = miles` and explicit unit; never converted to 4% cashback |

## 10. Optional publication event

HTTP bulk reads are the MVP integration. If the team later adds event delivery, publish only after the catalogue transaction commits.

### CataloguePublishedV1

```json
{
  "event_id": "88888888-8888-4888-8888-888888888888",
  "event_type": "catalogue.published.v1",
  "occurred_at": "2026-09-07T03:00:00Z",
  "catalogue_revision": 7,
  "changes": [
    {
      "card_id": "11111111-1111-4111-8111-111111111111",
      "card_version_id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
      "change_type": "updated"
    }
  ]
}
```

`change_type = added | updated | deactivated`. Consumers deduplicate by `event_id`, ignore older catalogue revisions, and retrieve the authoritative snapshot through the internal GET endpoint. Events are invalidation notifications, not the authoritative card payload.

## 11. Security and logging contract

- Admin and internal responses use `Cache-Control: no-store` unless explicitly safe to cache.
- Public card responses may be cached against their ETag and catalogue revision.
- Raw HTML/PDF storage paths are never exposed through APIs.
- Evidence snippets are plain text or safely escaped; never render fetched markup directly.
- Logs include request, source, document, run, candidate, and version IDs where relevant.
- Logs must not include bearer tokens, credentials, full raw documents, or arbitrary source query strings.
- Scraping accepts only registered sources and document IDs. These APIs never accept a public arbitrary URL.
- Redirects and resolved addresses are checked against the source allowlist and private/loopback/link-local networks.

## 12. Teammate ownership and handoff

| Teammate/service | Owns or calls | Handoff required |
|---|---|---|
| Catalogue backend | Implements all 11 HTTP endpoints and publication transaction | OpenAPI, DTO models, migrations, fixtures |
| Ingestion worker | Fetches registered sources and creates validated candidates | Adapter interface, job lease contract, source fixtures |
| Recommendation Engine | Calls current snapshot and historical-version endpoints | Supported `CardTermsV1` schema versions and ETag handling |
| Public frontend | Calls cards/detail/compare endpoints | Generated types, loading/empty/stale/unsupported states |
| Admin frontend | Calls source/job/candidate/decision endpoints | Candidate/diff/evidence schemas and role handling |
| Auth owner | Supplies trusted admin and service identities | Token validation middleware or workload identity |
| QA/integration | Verifies fixtures, errors, and state transitions | Seed source, raw fixtures, expected candidates/publications |

The Recommendation Engine must not access catalogue tables directly. The catalogue service does not need expense schemas or user transactions.

## 13. Shared integration fixture and acceptance checks

Use fictional values so development and CI do not depend on a live bank site.

1. Seed an approved `Demo Bank Official Cards` source with one allowlisted product document.
2. Run ingestion with a fixture page describing Demo Everyday Card at 1.5% flat cashback and zero annual fee.
3. Assert that the raw snapshot, field evidence, normalized candidate, and product identity are created.
4. Approve the candidate and assert catalogue revision 7 with immutable card version A.
5. `GET /cards` returns the card; current internal snapshot returns version A and `card_terms.v1`.
6. Repeat the scrape with unchanged content and assert that no duplicate candidate/version is created.
7. Process a second fixture containing a 1.6% rate. Assert a material candidate diff from `0.015` to `0.016`.
8. Approve it and assert catalogue revision 8 with immutable version B.
9. Current catalogue endpoints return version B; `GET /internal/card-versions/{version_A}` still returns 1.5%.
10. Replay the original decision idempotency key and receive the original revision-7 response without another publication.
11. Run a fixture where one document fails extraction and assert `partial` status while the published catalogue remains intact.

Also verify:

- Malformed UUIDs and decimal types.
- Empty catalogue and filter combinations.
- Unknown versus zero/unlimited semantics.
- Missing evidence and unresolved conflicts blocking approval.
- Similar card names that must remain separate.
- Stale review revisions and duplicate scrape requests.
- Admin role denial and invalid service identity.
- Cursor/ETag behavior.
- URL/redirect allowlist enforcement.
- Atomic rollback during publication failure.

## 14. Contract completion criteria

The contract is complete when:

- Every endpoint has a handler, typed request/response models, documented statuses, and error behavior.
- The generated OpenAPI document matches this contract.
- `CardTermsV1` and `CardCatalogueSnapshotV1` are exported as JSON Schema for the Recommendation Engine.
- Frontend and engine mocks use the same committed fixtures as backend contract tests.
- Every published supported critical field has evidence.
- Candidate approval is atomic and versioned.
- Historical card versions remain available after later publication.
- Fixture-mode integration tests pass without network access.
