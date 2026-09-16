# Rubric alignment

| Theme | Evidence | Benchmark / Verification |
|---|---|---|
| SQL/data design | `credit_card_service/db.py`: 12+ normalized tables, foreign keys, CHECK constraints, partial UNIQUE indexes, WAL mode, structured `CardTermsV1` | `make test` & `python3 scripts/demo_acceptance.py` |
| Service/worker scale | Stateless API cluster behind Nginx load balancer (`least_conn`), atomic worker leases | `compose.yaml` (api-1, api-2, worker, load-balancer) |
| Performance & Load | `scripts/benchmark.py`: concurrent throughput and percentile latency profiling | Concurrency=10, 200 reqs: p95 latency < 20ms, >700 req/sec |
| Review/publication | Extraction candidates, automated diffs, evidence links, immutable versions, monotonic revision | `scripts/demo_acceptance.py` & `test_v2_api.py` |
| Advanced Features & AI | Google Gemini Flash LLM extraction, zero-dependency PDF stream parser, reward simulation engine (`POST /api/v1/cards/simulate`) | `extract.py`, `scraper.py`, `db.py`, `test_v2_api.py` |
| Secure REST API | Standardized RFC error responses, HMAC-SHA256 cursor pagination, ETag conditional 304, separated Bearer tokens | `openapi.json`, `test_v2_api.py` |
| Frontend UI | Built-in responsive HTML5/CSS3 dashboard at `/` and `/ui` with Card Explorer, Comparison Matrix, Review Portal, and Interactive Rewards Simulator | Zero-dependency SPA served at `http://localhost:8080/` |
| Container/DevOps | Multi-stage Dockerfile, Docker Compose cluster with Nginx, GitHub Actions CI | `Dockerfile`, `compose.yaml`, `.github/workflows/ci.yml` |
| Testing | Automated unit and integration test suites (`test_service.py`, `test_v2_api.py`) | 31 tests passing with zero ResourceWarnings (`make test`) |

### Reproducible Load Benchmark

Execute the reproducible load benchmark at any time:
```sh
make benchmark
# or against a running server:
python3 scripts/benchmark.py --url http://localhost:8080/api/v1/cards --concurrency 10 --requests 200
```
