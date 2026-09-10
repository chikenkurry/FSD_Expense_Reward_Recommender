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
    worker = commands.add_parser("worker"); worker.add_argument("--db", required=True); worker.add_argument("--once", action="store_true"); worker.add_argument("--worker-id", default="local-worker")
    seed = commands.add_parser("seed-demo"); seed.add_argument("--db", required=True); seed.add_argument("--rate", default="0.015")
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
            result = db.process_one(args.db, args.worker_id)
            print(json.dumps({"status": "idle"} if result is None else result, sort_keys=True)); return 0
    except (ConfigError, OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
