# Expense Categorisation Engine Service

Standalone FastAPI microservice for categorising transactions.

## Run locally

```powershell
cd expense-categorisation-engine-service
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e .
uvicorn app.main:app --reload
```

The API is available at `http://localhost:8000`. OpenAPI documentation is at
`/docs`.

The default `LLM_PROVIDER=none` is intentional: cache misses return
`Uncategorized` with `source=fallback` until an LLM adapter is configured.
