from __future__ import annotations

import argparse
import json
import sqlite3
import sys

from . import db
from .api import serve
from .config import ConfigError, load_config, selected_sources
from .exporter import render
from .scraper import scrape_sources


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m credit_card_service", description="Configured, auditable credit-card rewards scraper")
    commands = parser.add_subparsers(dest="command", required=True)
    init = commands.add_parser("init-db"); init.add_argument("--db", required=True)
    validate = commands.add_parser("validate-config"); validate.add_argument("--config", required=True)
    scrape = commands.add_parser("scrape"); scrape.add_argument("--config", required=True); scrape.add_argument("--db", required=True); scrape.add_argument("--source"); scrape.add_argument("--allow-private-hosts", action="store_true")
    export = commands.add_parser("export"); export.add_argument("--db", required=True); export.add_argument("--format", choices=("json", "csv"), default="json"); export.add_argument("--output")
    server = commands.add_parser("serve"); server.add_argument("--config", required=True); server.add_argument("--db", required=True); server.add_argument("--host", default="127.0.0.1"); server.add_argument("--port", type=int, default=8080); server.add_argument("--api-key"); server.add_argument("--admin-secret"); server.add_argument("--internal-secret"); server.add_argument("--allow-private-hosts", action="store_true")
    worker = commands.add_parser("worker"); worker.add_argument("--db", required=True); worker.add_argument("--once", action="store_true"); worker.add_argument("--worker-id", default="local-worker"); worker.add_argument("--interval", type=int, default=0, help="Continuous worker polling interval in seconds (0 = run once)")
    seed = commands.add_parser("seed-demo"); seed.add_argument("--db", required=True); seed.add_argument("--rate", default="0.015")
    discover = commands.add_parser("discover"); discover.add_argument("--url", required=True); discover.add_argument("--issuer", default="Bank"); discover.add_argument("--pattern"); discover.add_argument("--output"); discover.add_argument("--scrape-db"); discover.add_argument("--allow-private-hosts", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init-db":
            db.initialize(args.db); print(json.dumps({"status": "initialized", "db": args.db}, sort_keys=True)); return 0
        if args.command == "validate-config":
            config = load_config(args.config)
            print(json.dumps({"status": "valid", "source_count": len(config["sources"])}, sort_keys=True)); return 0
        if args.command == "scrape":
            if args.allow_private_hosts: print("WARNING: private-host access enabled only for trusted local development/testing", file=sys.stderr)
            config = load_config(args.config)
            results = scrape_sources(selected_sources(config, [args.source] if args.source else None), args.db, args.allow_private_hosts)
            print(json.dumps({"results": results}, sort_keys=True))
            return 0 if all(item["status"] == "success" for item in results) else 1
        if args.command == "export":
            content = render(args.db, args.format)
            if args.output:
                with open(args.output, "w", encoding="utf-8", newline="") as handle: handle.write(content)
                print(json.dumps({"status": "exported", "format": args.format, "output": args.output}, sort_keys=True))
            else:
                sys.stdout.write(content)
            return 0
        if args.command == "serve":
            if args.allow_private_hosts: print("WARNING: private-host access enabled only for trusted local development/testing", file=sys.stderr)
            serve(load_config(args.config), args.db, args.host, args.port, args.api_key, args.allow_private_hosts, admin_secret=args.admin_secret, internal_secret=args.internal_secret)
            return 0
        if args.command == "seed-demo":
            print(json.dumps(db.seed_demo(args.db, args.rate), sort_keys=True)); return 0
        if args.command == "worker":
            import signal
            import time

            interval = getattr(args, "interval", 0)
            if args.once or interval <= 0:
                result = db.process_one(args.db, args.worker_id)
                print(json.dumps({"status": "idle"} if result is None else result, sort_keys=True))
                return 0

            stop_requested = False

            def _sig_handler(signum, frame):
                nonlocal stop_requested
                stop_requested = True

            signal.signal(signal.SIGINT, _sig_handler)
            signal.signal(signal.SIGTERM, _sig_handler)

            print(json.dumps({"event": "worker_started", "worker_id": args.worker_id, "interval_sec": interval}, sort_keys=True))
            sys.stdout.flush()

            while not stop_requested:
                while not stop_requested:
                    res = db.process_one(args.db, args.worker_id)
                    if res is None:
                        break
                    print(json.dumps({"event": "task_completed", "worker_id": args.worker_id, "result": res}, sort_keys=True))
                    sys.stdout.flush()

                for _ in range(int(interval * 2)):
                    if stop_requested:
                        break
                    time.sleep(0.5)

            print(json.dumps({"event": "worker_stopped", "worker_id": args.worker_id}, sort_keys=True))
            return 0
        if args.command == "discover":
            from .scraper import Scraper
            scraper = Scraper(allow_private_hosts=args.allow_private_hosts)
            links = scraper.discover_links(args.url, args.pattern)
            sources = []
            for item in links:
                slug = item["slug"]
                source_id = f"{args.issuer.lower().replace(' ', '-')}-{slug}"
                sources.append({
                    "source_id": source_id,
                    "issuer": args.issuer,
                    "name": item["title"],
                    "card_id": source_id,
                    "page_url": item["url"],
                    "enabled": True,
                    "request_delay_seconds": 2,
                    "extraction_hints": {}
                })
            out_data = {"version": 1, "sources": sources}
            if args.output:
                with open(args.output, "w", encoding="utf-8") as f:
                    json.dump(out_data, f, indent=2)
            if args.scrape_db:
                results = scrape_sources(sources, args.scrape_db, args.allow_private_hosts)
                print(json.dumps({"discovered": len(links), "scraped": len(results), "results": results}, indent=2))
            else:
                print(json.dumps({"discovered_count": len(links), "sources": sources}, indent=2))
            return 0
    except (ConfigError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
