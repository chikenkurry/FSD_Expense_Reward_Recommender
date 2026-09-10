from __future__ import annotations

import json
import io
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sqlite3
import tempfile
import threading
import unittest
from contextlib import redirect_stderr
from urllib.error import HTTPError
from urllib.request import Request, urlopen
from types import SimpleNamespace
from unittest import mock

from credit_card_service import db
from credit_card_service.__main__ import main as cli_main
from credit_card_service.api import make_handler
from credit_card_service.config import ConfigError, load_config, selected_sources
from credit_card_service.extract import extract
from credit_card_service.exporter import render
from credit_card_service.scraper import Scraper, scrape_sources

FIXTURE = (Path(__file__).parent / "fixtures" / "synthetic_card.html").read_bytes()
SCRIPT_FIXTURE = (Path(__file__).parent / "fixtures" / "normal_script_only.html").read_text()


class FixtureHandler(BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_GET(self):
        if self.path == "/robots.txt":
            body, typ = b"User-agent: *\nAllow: /\n", "text/plain"
        elif self.path == "/card":
            body, typ = FIXTURE, "text/html; charset=utf-8"
        elif self.path == "/directory":
            body, typ = b'<html><body><a href="/card">Synthetic Card</a><a href="/faq">FAQ</a></body></html>', "text/html; charset=utf-8"
        elif self.path == "/large":
            body, typ = b"x" * 1_000_001, "text/html"
        elif self.path == "/json":
            body, typ = b"{}", "application/json"
        elif self.path == "/fail":
            self.send_response(503); self.end_headers(); return
        else:
            self.send_response(404); self.end_headers(); return
        self.send_response(200); self.send_header("Content-Type", typ); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)


class ServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True); cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.db_path = str(Path(self.tmp.name) / "records.sqlite")
        self.source = {"source_id": "synthetic", "issuer": "Synthetic Issuer", "name": "Synthetic Card", "card_id": "synthetic-card", "page_url": self.base + "/card", "enabled": True, "request_delay_seconds": 0, "extraction_hints": {}}

    def tearDown(self): self.tmp.cleanup()

    def test_private_targets_rejected_by_default(self):
        result = Scraper().scrape_source(self.source, self.db_path)
        self.assertEqual(result["status"], "failed")
        self.assertIn("private", result["error"])

    def test_extract_persist_dedup_jsonld_and_exports(self):
        result = scrape_sources([self.source], self.db_path, allow_private_hosts=True)
        self.assertEqual(result[0]["status"], "success", result)
        record = db.card(self.db_path, "synthetic-card")
        self.assertEqual(record["annual_fee"]["amount"], "196.2")
        self.assertEqual(len([r for r in record["rewards"] if r["unit"] == "cashback_percent"]), 1)
        self.assertTrue(any(r["unit"] == "miles_per_currency" for r in record["rewards"]))
        self.assertEqual(len(record["provenance"]["content_sha256"]), 64)
        self.assertIn("Welcome", record["welcome_offer"]["summary"])
        self.assertIn("synthetic-card", render(self.db_path, "json"))
        self.assertIn("reward_category", render(self.db_path, "csv"))

    def test_regular_script_text_is_not_visible_extraction_input(self):
        record = extract(self.source, SCRIPT_FIXTURE)
        self.assertEqual(record["rewards"], [])
        self.assertIsNone(record["annual_fee"])

    def test_bare_dollar_annual_fee_has_unknown_currency(self):
        record = extract(self.source, "<p>Annual fee $99.99.</p>")
        self.assertEqual(record["annual_fee"]["currency"], "UNKNOWN")

    def test_invalid_extraction_hints_are_rejected_at_config_load(self):
        invalid_hints = [
            {"unexpected": "value"},
            {"reward_patterns": [{"regex": "no capture", "unit": "points_per_currency"}]},
            {"reward_patterns": [{"regex": "(1)", "unit": "unsupported"}]},
            {"annual_fee_regex": "no capture"},
            {"welcome_offer_regex": "["},
            {"annual_fee_currency": ""},
        ]
        for hints in invalid_hints:
            config = {"version": 1, "sources": [{**self.source, "extraction_hints": hints}]}
            path = Path(self.tmp.name) / "invalid-config.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ConfigError, msg=str(hints)):
                load_config(str(path))

    def test_boolean_version_and_invalid_url_ports_are_rejected(self):
        path = Path(self.tmp.name) / "invalid-url-config.json"
        path.write_text(json.dumps({"version": True, "sources": []}), encoding="utf-8")
        with self.assertRaises(ConfigError):
            load_config(str(path))
        for port in ("0", "70000", "not-a-port"):
            config = {"version": 1, "sources": [{**self.source, "page_url": f"http://example.test:{port}/card"}]}
            path.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaises(ConfigError, msg=port):
                load_config(str(path))

    def test_request_pacing_is_per_host_and_response_is_closed(self):
        now = [0.0]
        sleeps: list[float] = []
        def sleeper(seconds: float) -> None:
            sleeps.append(seconds); now[0] += seconds
        class Response:
            status = 200
            headers = {"Content-Type": "text/html"}
            def __init__(self): self.closed = False
            def read(self, size): return b"ok"
            def close(self): self.closed = True
        responses: list[Response] = []
        def open_response(*args, **kwargs):
            response = Response(); responses.append(response); return response
        scraper = Scraper(allow_private_hosts=True, clock=lambda: now[0], sleeper=sleeper)
        scraper._opener = SimpleNamespace(open=open_response)
        scraper._request("http://local.test/robots.txt", request_delay=2)
        now[0] += 0.5
        scraper._request("http://local.test/card", request_delay=2)
        scraper._request("http://other.test/card", request_delay=2)
        self.assertEqual(sleeps, [1.5])
        self.assertTrue(all(response.closed for response in responses))

    def test_lowercase_transport_headers_support_redirect_and_content_type(self):
        class Response:
            def __init__(self, status, headers, body=b""):
                self.status, self.headers, self.body, self.closed = status, headers, body, False
            def read(self, size): return self.body
            def close(self): self.closed = True
        responses = [Response(302, {"location": "http://local.test/final"}), Response(200, {"content-type": "text/html"}, b"ok")]
        scraper = Scraper(allow_private_hosts=True)
        scraper._opener = SimpleNamespace(open=lambda *args, **kwargs: responses.pop(0))
        final_url, headers, body = scraper._fetch("http://local.test/start")
        self.assertEqual(final_url, "http://local.test/final")
        self.assertEqual(headers["content-type"], "text/html")
        self.assertEqual(body, b"ok")

    def test_last_good_record_is_preserved_after_failure(self):
        initial = scrape_sources([self.source], self.db_path, True)[0]
        self.assertEqual(initial["status"], "success", initial)
        good = db.card(self.db_path, "synthetic-card")
        failing = {**self.source, "page_url": self.base + "/fail"}
        self.assertEqual(scrape_sources([failing], self.db_path, True)[0]["status"], "failed")
        self.assertEqual(db.card(self.db_path, "synthetic-card")["provenance"]["content_sha256"], good["provenance"]["content_sha256"])
        self.assertEqual(db.runs(self.db_path)[0]["status"], "failed")

    def test_response_safety(self):
        for path, expected in (("/large", "size cap"), ("/json", "content type")):
            result = scrape_sources([{**self.source, "page_url": self.base + path}], self.db_path, True)[0]
            self.assertEqual(result["status"], "failed")
            self.assertIn(expected, result["error"])

    def test_api_read_and_authenticated_scrape(self):
        config = {"version": 1, "sources": [self.source]}
        api = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(config, self.db_path, "secret", True))
        thread = threading.Thread(target=api.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{api.server_port}"
        try:
            self.assertEqual(json.loads(urlopen(base + "/health").read())["status"], "ok")
            with self.assertRaises(HTTPError) as bad:
                urlopen(Request(base + "/v1/scrape", data=b"{}", method="POST"))
            self.assertEqual(bad.exception.code, 401)
            request = Request(base + "/v1/scrape", data=b'{"source_ids":["synthetic"]}', method="POST", headers={"Authorization": "Bearer secret", "Content-Type": "application/json"})
            self.assertEqual(json.loads(urlopen(request).read())["status"], "completed")
            self.assertEqual(len(json.loads(urlopen(base + "/v1/cards?issuer=Synthetic%20Issuer").read())["cards"]), 1)
        finally:
            api.shutdown(); api.server_close()

    def test_api_database_read_errors_are_json(self):
        api = ThreadingHTTPServer(("127.0.0.1", 0), make_handler({"version": 1, "sources": []}, self.db_path, None, True))
        thread = threading.Thread(target=api.serve_forever, daemon=True); thread.start()
        base = f"http://127.0.0.1:{api.server_port}"
        try:
            for endpoint, target in (("/v1/cards", "cards"), ("/v1/cards/test", "card"), ("/v1/scrape-runs", "runs")):
                with mock.patch(f"credit_card_service.api.db.{target}", side_effect=sqlite3.DatabaseError("private details")):
                    with self.assertRaises(HTTPError) as failed:
                        urlopen(base + endpoint)
                    self.assertEqual(failed.exception.code, 500)
                    payload = json.loads(failed.exception.read())
                    self.assertEqual(payload["error"]["message"], "database query failed")
        finally:
            api.shutdown(); api.server_close()

    def test_empty_api_key_is_rejected_at_handler_creation(self):
        with self.assertRaises(ValueError):
            make_handler({"version": 1, "sources": []}, self.db_path, "", True)

    def test_cli_rejects_explicit_empty_api_key_as_json_error(self):
        config_path = Path(self.tmp.name) / "config.json"
        config_path.write_text(json.dumps({"version": 1, "sources": [self.source]}), encoding="utf-8")
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            result = cli_main(["serve", "--config", str(config_path), "--db", self.db_path, "--api-key", ""])
        self.assertEqual(result, 2)
        self.assertIn("api_key must be nonempty", json.loads(stderr.getvalue())["error"])

    def test_only_configured_sources_selected(self):
        with self.assertRaises(Exception): selected_sources({"sources": [self.source]}, ["not-configured"])

    def test_directory_crawler_discovery_and_scrape(self):
        dir_source = {
            "source_id": "synthetic-dir",
            "issuer": "Synthetic Bank",
            "name": "Synthetic Bank Directory",
            "card_id": "synthetic-hub",
            "page_url": self.base + "/directory",
            "is_directory": True,
            "link_pattern": "/card$",
            "enabled": True,
            "request_delay_seconds": 0,
            "extraction_hints": {},
        }
        res = Scraper(allow_private_hosts=True).scrape_source(dir_source, self.db_path)
        self.assertEqual(res["status"], "success")
        self.assertEqual(res["discovered_count"], 1)
        self.assertIn("synthetic-hub-card", res["cards_scraped"])
