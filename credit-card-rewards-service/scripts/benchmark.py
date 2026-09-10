"""Concurrent load testing and latency percentile benchmark script.

Measures p50, p90, p95, p99 latencies, throughput (requests/second),
and error rates under concurrent load for public catalogue endpoints.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import statistics
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from credit_card_service import db
from credit_card_service.api import make_handler


def run_benchmark(
    target_url: str,
    concurrency: int = 10,
    total_requests: int = 200,
) -> dict:
    """Execute concurrent requests against target_url and compute latency percentiles."""
    latencies: list[float] = []
    errors = 0
    start_time = time.perf_counter()

    def make_call() -> None:
        nonlocal errors
        t0 = time.perf_counter()
        try:
            req = Request(target_url, headers={"User-Agent": "Catalogue-Benchmark/1.0"})
            with urlopen(req, timeout=5) as resp:
                if resp.status == 200:
                    latencies.append((time.perf_counter() - t0) * 1000.0)
                else:
                    errors += 1
        except Exception:
            errors += 1

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = [pool.submit(make_call) for _ in range(total_requests)]
        concurrent.futures.wait(futures)

    duration = time.perf_counter() - start_time
    throughput = len(latencies) / duration if duration > 0 else 0

    if not latencies:
        return {"status": "failed", "errors": errors, "duration_seconds": duration}

    latencies.sort()
    p50 = statistics.median(latencies)
    p90 = latencies[int(len(latencies) * 0.90)]
    p95 = latencies[int(len(latencies) * 0.95)]
    p99 = latencies[int(len(latencies) * 0.99)]

    return {
        "status": "success",
        "concurrency": concurrency,
        "total_requests": total_requests,
        "successful_requests": len(latencies),
        "errors": errors,
        "duration_seconds": round(duration, 3),
        "throughput_req_per_sec": round(throughput, 2),
        "latency_ms": {
            "min": round(min(latencies), 2),
            "p50": round(p50, 2),
            "p90": round(p90, 2),
            "p95": round(p95, 2),
            "p99": round(p99, 2),
            "max": round(max(latencies), 2),
            "mean": round(statistics.mean(latencies), 2),
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Performance benchmark for credit card catalogue")
    parser.add_argument("--url", help="Target URL (if running standalone server)")
    parser.add_argument("--concurrency", type=int, default=10, help="Number of concurrent client workers")
    parser.add_argument("--requests", type=int, default=200, help="Total requests to execute")
    args = parser.parse_args()

    if args.url:
        results = run_benchmark(args.url, args.concurrency, args.requests)
        print(json.dumps(results, indent=2))
        return

    # Self-contained standalone benchmark with temporary SQLite WAL database
    print(f"Starting benchmark: concurrency={args.concurrency}, requests={args.requests}...")
    with tempfile.TemporaryDirectory() as tmp_dir:
        db_path = str(Path(tmp_dir) / "bench.sqlite")
        seeded = db.seed_demo(db_path, "0.015")
        db.queue_run(db_path, seeded["source_id"], {"type": "all"}, "admin_refresh")
        db.process_one(db_path)
        candidates = db.candidate_list(db_path)
        db.decide(db_path, candidates[0]["candidate_id"], 1, "approve", "Benchmark seed.", [], "bench")

        handler_cls = make_handler({"version": 1, "sources": []}, db_path, None, True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        port = httpd.server_port
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        try:
            results = run_benchmark(f"http://127.0.0.1:{port}/api/v1/cards", args.concurrency, args.requests)
            print(json.dumps(results, indent=2))
        finally:
            httpd.shutdown()
            httpd.server_close()


if __name__ == "__main__":
    main()
