# JSON HTTP API

> [!NOTE]
> For the complete, modern specification of all `/api/v1` public, internal recommendation snapshot, and admin endpoints with request/response schemas, see [API_REFERENCE.md](API_REFERENCE.md).

The server defaults to `127.0.0.1:8080`; responses are JSON with UTF-8 content type. Errors are `{ "error": { "status": integer, "message": string } }`.

| Method and path | Result |
|---|---|
| `GET /health` | `{status:"ok"}` if SQLite is available |
| `GET /v1/cards?issuer=&source_id=` | `{cards:[current card records]}`; optional exact filters |
| `GET /v1/cards/{card_id}` | one record, or 404 |
| `GET /v1/scrape-runs?limit=50` | newest runs, clamped to 1–100 |
| `POST /v1/scrape` | synchronously runs configured enabled sources, or configured `source_ids` only |

POST body is `{}` (all enabled sources) or `{ "source_ids": ["configured-id"] }`. It never accepts a URL. A successful completed response is `{ "status":"completed", "results":[...] }`; individual failures remain in `results` and the audit table. With a nonempty `--api-key KEY`, POST requires `Authorization: Bearer KEY`, compared constant-time; GET stays readable. An explicitly empty API key is rejected at startup, while omitting the option is the deliberate no-auth mode. Invalid body/source id returns 400, bad auth 401, unknown paths 404, and database health failure 503.
