# Card Catalogue & Ingestion Service

Backend-only, Python 3.11+ card catalogue and review service. All included bank/card data is fictional synthetic fixture data, never current financial-product information. It has no frontend by design.

## Local quick start

```sh
python3 -m credit_card_service init-db --db catalogue.sqlite
python3 -m credit_card_service seed-demo --db catalogue.sqlite
python3 -m credit_card_service worker --once --db catalogue.sqlite
python3 -m credit_card_service serve --config config/sources.example.json --db catalogue.sqlite --admin-secret "$CARD_ADMIN_BEARER" --internal-secret "$CARD_INTERNAL_BEARER"
```

`seed-demo` adds an approved registered source and offline 1.5% fixture. Queue it through `POST /api/v1/admin/scrape-runs`, then use the worker. Existing command interfaces remain: `init-db`, `validate-config`, `scrape`, `export`, and `serve`; `worker --once` is the stateless worker entrypoint.

Public `/api/v1` catalogue reads are unauthenticated. Admin and internal routes require separately configured nonempty bearer secrets; `--api-key` remains an admin alias only for legacy callers. Requests are bounded, unknown JSON fields are rejected, and errors never contain exception details. Registered source scraping retains the original robots/SSRF/private-address/redirect/pacing safeguards; the ingestion API never accepts arbitrary URLs.

SQLite/WAL is the implemented offline/local backend. It supports one API/worker coordination domain through short transactions and leases; it is **not** claimed to be multi-replica safe. PostgreSQL is not currently selectable at runtime, so this repository intentionally does not claim a production database compose stack.

## Verification

```sh
python3 -m unittest discover -s tests -v
python3 -W error::ResourceWarning -m unittest discover -s tests -v
python3 -m compileall -q credit_card_service tests scripts
make verify
python3 scripts/demo_acceptance.py
```

See [architecture](docs/architecture.md), [database](docs/database.md), [deployment](docs/deployment.md), [testing](docs/testing.md), and [rubric alignment](docs/rubric-alignment.md). `openapi.json` and `schemas/` are committed integration artifacts.
