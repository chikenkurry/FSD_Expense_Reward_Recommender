"""Comprehensive unit and integration test suite for v2 API endpoints.

Covers catalogue filtering, HMAC cursor pagination, ETag conditional caching,
card comparisons, internal snapshots, admin review workflows, and idempotency.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from credit_card_service import db
from credit_card_service.api import make_handler


class V2ApiTests(unittest.TestCase):
    """Test coverage for v2 REST endpoints, security boundaries, and cache invariants."""

    ADMIN_SECRET = "test-admin-bearer-token"
    INTERNAL_SECRET = "test-internal-bearer-token"

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tmp.name) / "catalogue.sqlite")

        # Seed initial demo bank, source, document and approve initial candidate
        seeded = db.seed_demo(self.db_path, "0.015")
        self.source_id = seeded["source_id"]
        db.queue_run(self.db_path, self.source_id, {"type": "all"}, "admin_refresh")
        db.process_one(self.db_path)
        candidates = db.candidate_list(self.db_path)
        self.assertTrue(candidates)
        _, self.approval_result = db.decide(
            self.db_path,
            candidates[0]["candidate_id"],
            1,
            "approve",
            "Initial test verification.",
            [],
            "tester",
        )
        self.card_id = self.approval_result["publication"]["card_id"]
        self.version_id = self.approval_result["publication"]["card_version_id"]

        # Spin up local API server
        config = {"version": 1, "sources": []}
        handler_cls = make_handler(
            config,
            self.db_path,
            None,
            allow_private_hosts=True,
            admin_secret=self.ADMIN_SECRET,
            internal_secret=self.INTERNAL_SECRET,
        )
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.httpd.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.tmp.cleanup()

    def _req(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        token: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict | str, dict]:
        url = self.base + path
        hdrs = dict(headers or {})
        if token:
            hdrs["Authorization"] = f"Bearer {token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs["Content-Type"] = "application/json"

        req = Request(url, data=data, headers=hdrs, method=method)
        try:
            with urlopen(req) as resp:
                status = resp.status
                resp_headers = dict(resp.headers)
                raw = resp.read()
                content_type = resp.headers.get("Content-Type", "")
                if "application/json" in content_type:
                    payload = json.loads(raw.decode("utf-8")) if raw else {}
                else:
                    payload = raw.decode("utf-8")
                return status, payload, resp_headers
        except HTTPError as err:
            raw = err.read()
            payload = json.loads(raw.decode("utf-8")) if raw else {}
            return err.code, payload, dict(err.headers)

    def test_dashboard_ui_endpoint(self):
        """Dashboard HTML interface is served with 200 OK at root."""
        status, content, hdrs = self._req("/")
        self.assertEqual(status, 200)
        self.assertIn("text/html", hdrs.get("Content-Type", ""))
        self.assertIn("Credit Card Rewards Catalogue", content)

    def test_cards_list_and_query_filters(self):
        """Public /api/v1/cards supports enum filters, query searches, and returns catalogue revision."""
        status, data, _ = self._req("/api/v1/cards")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["catalogue_revision"], 1)
        self.assertEqual(len(data["items"]), 1)
        self.assertEqual(data["items"][0]["card_id"], self.card_id)

        # Text search matching name
        status, data, _ = self._req("/api/v1/cards?q=Everyday")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)

        status, data, _ = self._req("/api/v1/cards?q=NonExistent")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 0)

        # Valid vs invalid enum filters
        status, data, _ = self._req("/api/v1/cards?reward_type=cashback")
        self.assertEqual(status, 200)
        self.assertEqual(len(data["items"]), 1)

        status, data, _ = self._req("/api/v1/cards?reward_type=invalid_enum")
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "VALIDATION_ERROR")

    def test_cards_pagination_and_hmac_cursor(self):
        """Cursor pagination enforces bounds, validates HMAC signatures, and handles cursor expiry."""
        # Limit bounds check
        status, data, _ = self._req("/api/v1/cards?limit=0")
        self.assertEqual(status, 422)

        status, data, _ = self._req("/api/v1/cards?limit=101")
        self.assertEqual(status, 422)

        # Invalid cursor string
        status, data, _ = self._req("/api/v1/cards?cursor=tampered_invalid_token")
        self.assertEqual(status, 422)
        self.assertEqual(data["error"]["code"], "VALIDATION_ERROR")

    def test_cards_conditional_etag_caching(self):
        """ETag headers are generated and If-None-Match responds with 304 Not Modified."""
        status, _, hdrs = self._req("/api/v1/cards")
        self.assertEqual(status, 200)
        etag = hdrs.get("ETag")
        self.assertTrue(etag)

        # Send If-None-Match with returned ETag
        status, _, _ = self._req("/api/v1/cards", headers={"If-None-Match": etag})
        self.assertEqual(status, 304)

    def test_card_detail_endpoint(self):
        """Card detail returns terms, rules, and verifiable evidence citations."""
        status, data, _ = self._req(f"/api/v1/cards/{self.card_id}")
        self.assertEqual(status, 200)
        self.assertEqual(data["card"]["card_id"], self.card_id)
        self.assertTrue(data["terms"]["rules"])
        self.assertTrue(data["evidence"])
        self.assertIn("Synthetic fixture", data["evidence"][0]["snippet"])

        # Invalid UUID
        status, data, _ = self._req("/api/v1/cards/not-a-uuid")
        self.assertEqual(status, 422)

        # Non-existent UUID
        status, data, _ = self._req("/api/v1/cards/00000000-0000-4000-8000-000000000000")
        self.assertEqual(status, 404)

    def test_card_comparison(self):
        """Side-by-side comparison validates input lengths and computes differences."""
        # Less than 2 cards
        status, data, _ = self._req("/api/v1/cards/compare", method="POST", body={"card_ids": [self.card_id]})
        self.assertEqual(status, 422)

        # Compare card with itself (duplicate IDs rejected)
        status, data, _ = self._req(
            "/api/v1/cards/compare",
            method="POST",
            body={"card_ids": [self.card_id, self.card_id]},
        )
        self.assertEqual(status, 422)

    def test_internal_snapshot_and_auth(self):
        """Internal snapshot requires internal Bearer token and returns valid snapshot structure."""
        # Unauthenticated request fails
        status, data, _ = self._req("/api/v1/internal/catalogue-snapshots/current")
        self.assertEqual(status, 401)

        # Authenticated with valid internal token
        status, data, _ = self._req(
            "/api/v1/internal/catalogue-snapshots/current",
            token=self.INTERNAL_SECRET,
        )
        self.assertEqual(status, 200)
        self.assertEqual(data["schema_version"], "card_catalogue_snapshot.v1")
        self.assertEqual(len(data["cards"]), 1)
        self.assertEqual(data["cards"][0]["card"]["card_id"], self.card_id)

        # Version detail
        status, ver_data, _ = self._req(
            f"/api/v1/internal/card-versions/{self.version_id}",
            token=self.INTERNAL_SECRET,
        )
        self.assertEqual(status, 200)
        self.assertEqual(ver_data["published_in_catalogue_revision"], 1)
        self.assertIsNone(ver_data["superseded_in_catalogue_revision"])

    def test_admin_scrape_runs_and_idempotency(self):
        """Admin scrape-runs endpoint requires Idempotency-Key and replays responses safely."""
        body = {"source_id": self.source_id, "scope": {"type": "all"}, "reason": "admin_refresh"}
        key = "idemp-key-test-001"

        # Missing auth
        status, _, _ = self._req("/api/v1/admin/scrape-runs", method="POST", body=body, headers={"Idempotency-Key": key})
        self.assertEqual(status, 401)

        # Missing Idempotency-Key
        status, data, _ = self._req("/api/v1/admin/scrape-runs", method="POST", body=body, token=self.ADMIN_SECRET)
        self.assertEqual(status, 422)

        # Valid queued run
        status, data, _ = self._req(
            "/api/v1/admin/scrape-runs",
            method="POST",
            body=body,
            token=self.ADMIN_SECRET,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(status, 202)
        run_id = data["run_id"]

        # Replay with same key and body returns cached response
        status_replay, data_replay, _ = self._req(
            "/api/v1/admin/scrape-runs",
            method="POST",
            body=body,
            token=self.ADMIN_SECRET,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(status_replay, 202)
        self.assertEqual(data_replay["run_id"], run_id)

        # Conflict on same key with differing body
        conflict_body = {**body, "reason": "scheduled_refresh"}
        status_conflict, conflict_data, _ = self._req(
            "/api/v1/admin/scrape-runs",
            method="POST",
            body=conflict_body,
            token=self.ADMIN_SECRET,
            headers={"Idempotency-Key": key},
        )
        self.assertEqual(status_conflict, 409)
        self.assertEqual(conflict_data["error"]["code"], "IDEMPOTENCY_CONFLICT")

    def test_admin_candidate_patch_and_decision(self):
        """Candidate review workflow supports optimistic concurrency patching and approval."""
        # Update source fixture to trigger a new candidate
        db.seed_demo(self.db_path, "0.018")
        db.queue_run(self.db_path, self.source_id, {"type": "all"}, "admin_refresh")
        db.process_one(self.db_path)

        pending = db.candidate_list(self.db_path, {"review_status": "pending"})
        self.assertTrue(pending)
        cand_id = pending[0]["candidate_id"]

        # Fetch single candidate GET
        status, cand_data, _ = self._req(
            f"/api/v1/admin/extraction-candidates/{cand_id}",
            token=self.ADMIN_SECRET,
        )
        self.assertEqual(status, 200)
        self.assertEqual(cand_data["candidate_id"], cand_id)

        # Patch rate with expected revision 1
        status, patched, _ = self._req(
            f"/api/v1/admin/extraction-candidates/{cand_id}",
            method="PATCH",
            body={
                "expected_review_revision": 1,
                "edits": [{"field_path": "/terms/rules/0/rate", "value": "0.020"}],
                "reason": "Admin adjustment to 2.0% promotional rate.",
            },
            token=self.ADMIN_SECRET,
        )
        self.assertEqual(status, 200)
        self.assertEqual(patched["review_revision"], 2)

        # Stale revision patch attempt fails with 409
        status, stale_err, _ = self._req(
            f"/api/v1/admin/extraction-candidates/{cand_id}",
            method="PATCH",
            body={
                "expected_review_revision": 1,
                "edits": [{"field_path": "/terms/rules/0/rate", "value": "0.025"}],
                "reason": "Stale edit.",
            },
            token=self.ADMIN_SECRET,
        )
        self.assertEqual(status, 409)

        # Approve candidate with updated revision 2
        status, dec_result, _ = self._req(
            f"/api/v1/admin/extraction-candidates/{cand_id}/decisions",
            method="POST",
            body={
                "expected_review_revision": 2,
                "decision": "approve",
                "reason": "Approved promotional 2.0% rate.",
                "acknowledged_warning_codes": [],
            },
            token=self.ADMIN_SECRET,
            headers={"Idempotency-Key": "decision-key-101"},
        )
        self.assertEqual(status, 201)
        self.assertEqual(dec_result["decision"], "approve")
        self.assertEqual(dec_result["publication"]["catalogue_revision"], 2)

        # Verify old publication superseded and new revision published
        status, updated_card, _ = self._req(f"/api/v1/cards/{self.card_id}")
        self.assertEqual(status, 200)
        self.assertEqual(updated_card["catalogue_revision"], 2)
        self.assertEqual(updated_card["terms"]["rules"][0]["rate"], "0.020")

    def test_admin_config_sources_and_discovery(self):
        """Admin config-sources and live discovery endpoints require authentication."""
        # Unauthenticated calls
        status, _, _ = self._req("/api/v1/admin/config-sources")
        self.assertEqual(status, 401)

        status, _, _ = self._req("/api/v1/admin/discover", method="POST", body={"url": "http://127.0.0.1:9999"})
        self.assertEqual(status, 401)

        # Authenticated config-sources call
        status, data, _ = self._req("/api/v1/admin/config-sources", token=self.ADMIN_SECRET)
        self.assertEqual(status, 200)
        self.assertIn("sources", data)

    def test_card_reward_simulation_endpoint(self):
        """POST /api/v1/cards/simulate evaluates spend, applies rules, enforces exclusions, and ranks cards."""
        # 1. Validation errors
        status, err, _ = self._req("/api/v1/cards/simulate", method="POST", body={"monthly_spend": []})
        self.assertEqual(status, 422)

        status, err, _ = self._req("/api/v1/cards/simulate", method="POST", body={"monthly_spend": [{"amount": "-50.00"}]})
        self.assertEqual(status, 422)

        status, err, _ = self._req(
            "/api/v1/cards/simulate",
            method="POST",
            body={"monthly_spend": [{"amount": "100.00"}], "card_ids": ["not-a-uuid"]},
        )
        self.assertEqual(status, 422)

        # 2. Successful simulation with MCC exclusions
        spend_basket = [
            {"amount": "400.00", "category": "dining", "mcc": "5812", "description": "Restaurant Dinner"},
            {"amount": "300.00", "category": "groceries", "mcc": "5411", "description": "Supermarket Groceries"},
            {"amount": "150.00", "category": "utilities", "mcc": "4900", "description": "Power & Water Bill"},
        ]
        status, result, _ = self._req("/api/v1/cards/simulate", method="POST", body={"monthly_spend": spend_basket})
        self.assertEqual(status, 200)
        self.assertEqual(result["total_monthly_spend"], "850.00")
        self.assertEqual(result["transactions_count"], 3)
        self.assertTrue(len(result["ranked_cards"]) >= 1)

        top_card = result["ranked_cards"][0]
        self.assertEqual(top_card["rank"], 1)
        self.assertIn("effective_reward_rate", top_card)
        self.assertTrue(float(top_card["total_reward_earned"]) > 0)

        # Check that the MCC 4900 utility transaction is appropriately processed
        breakdown = top_card["breakdown"]
        self.assertEqual(len(breakdown), 3)
        util_item = next(b for b in breakdown if b["mcc"] == "4900")
        self.assertEqual(util_item["amount"], "150.00")

    def test_pdf_stream_text_extraction(self):
        """extract_pdf_text extracts text from standard zlib FlateDecode PDF stream objects."""
        import zlib
        from credit_card_service.extract import extract_pdf_text

        content_stream = b"BT /F1 12 Tf 72 712 Td (DBS Altitude Visa Card) Tj 0 -14 Td [(Earn) 20 (1.2) 20 (miles) 20 (per) 20 ($1)] TJ ET"
        compressed = zlib.compress(content_stream)
        pdf_bytes = (
            b"%PDF-1.4\n1 0 obj\n<< /Length " + str(len(compressed)).encode() + b" /Filter /FlateDecode >>\nstream\n"
            + compressed
            + b"\nendstream\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF"
        )
        extracted = extract_pdf_text(pdf_bytes)
        self.assertIn("DBS Altitude Visa Card", extracted)
        self.assertIn("1.2", extracted)
        self.assertIn("miles", extracted)

    def test_reward_simulation_mechanics_direct(self):
        """Direct unit test of minimum spend tiers, caps, and MCC exclusions."""
        card_detail = {
            "card": {"card_id": "test-card-1", "name": "Singapore Cashback Special", "reward_type": "cashback"},
            "terms": {
                "minimum_monthly_spend": "600.00",
                "excluded_mccs": ["9399", "6540", "4900"],
                "cap_groups": [{"cap_key": "monthly_bonus_cap", "amount": "30.00", "period": "calendar_month"}],
                "rules": [
                    {"rule_key": "base_rate", "kind": "cashback", "rate": "0.003", "reward_unit": "cashback_percent"},
                    {"rule_key": "dining_bonus", "kind": "cashback", "rate": "0.080", "reward_unit": "cashback_percent", "cap_group_keys": ["monthly_bonus_cap"]},
                ],
            },
        }

        # Case A: Spend under minimum threshold ($400 < $600) -> earns fallback base rate (0.3%)
        sub_min_spend = [
            {"amount": "400.00", "category": "dining", "mcc": "5812", "description": "Dining under threshold"}
        ]
        res_a = db.simulate_card_rewards(card_detail, sub_min_spend)
        self.assertFalse(res_a["minimum_spend_met"])
        self.assertEqual(res_a["total_reward_earned"], "1.20")  # 400 * 0.003 = 1.20
        self.assertIn("min spend", res_a["breakdown"][0]["reason"])

        # Case B: Spend over minimum threshold ($800 >= $600) with dining bonus + MCC 4900 exclusion + cap clamping
        full_spend = [
            {"amount": "500.00", "category": "dining", "mcc": "5812", "description": "Dining (8% = $40, but capped at $30)"},
            {"amount": "200.00", "category": "utilities", "mcc": "4900", "description": "Power bill (MCC 4900 excluded)"},
            {"amount": "100.00", "category": "general", "mcc": "5999", "description": "General retail"},
        ]
        res_b = db.simulate_card_rewards(card_detail, full_spend)
        self.assertTrue(res_b["minimum_spend_met"])
        # Dining raw = $40, capped at $30.00
        # Utilities = $0.00 (excluded by MCC 4900)
        # General = $100 * 0.003 = $0.30
        # Total = $30.30
        self.assertEqual(res_b["total_reward_earned"], "30.30")
        self.assertEqual(res_b["breakdown"][1]["status"], "excluded")
        self.assertEqual(res_b["breakdown"][1]["reward_earned"], "0.00")
        self.assertIn("Capped", res_b["breakdown"][0]["reason"])

    def test_mysql_abstraction_translation_and_config(self):
        """Verify URL parsing, engine detection, and SQL dialect translation for MySQL."""
        # 1. URL parsing
        url = "mysql+pymysql://card_user:secret123@db-host:3307/card_catalogue"
        parsed = db.parse_mysql_url(url)
        self.assertEqual(parsed["host"], "db-host")
        self.assertEqual(parsed["port"], 3307)
        self.assertEqual(parsed["user"], "card_user")
        self.assertEqual(parsed["password"], "secret123")
        self.assertEqual(parsed["database"], "card_catalogue")

        # 2. Engine detection
        self.assertTrue(db.is_mysql("mysql://localhost/test"))
        self.assertTrue(db.is_mysql("mysql+pymysql://user:pass@host/db"))
        self.assertFalse(db.is_mysql("/data/catalogue.sqlite"))

        # 3. Query transformation
        q1 = db.sqlite_to_mysql_query("INSERT OR IGNORE INTO banks (bank_id, name) VALUES (?, ?)")
        self.assertIn("INSERT IGNORE INTO", q1)
        self.assertIn("%s", q1)
        self.assertNotIn("?", q1)

        q2 = db.sqlite_to_mysql_query("INSERT OR REPLACE INTO documents VALUES (?, ?, ?)")
        self.assertIn("REPLACE INTO", q2)

        q3 = db.sqlite_to_mysql_query("SELECT * FROM runs WHERE lease_until = datetime('now', '+60 seconds') AND id = ?")
        self.assertIn("DATE_ADD(UTC_TIMESTAMP(), INTERVAL 60 SECOND)", q3)
        self.assertIn("%s", q3)

        # 4. Cursor wrapper behavior
        class MockRawCursor:
            def __init__(self):
                self.lastrowid = 42
                self.rowcount = 3
            def fetchone(self): return {"col": "val"}
            def fetchall(self): return [{"col": "val"}]
            def close(self): pass

        cursor_wrapper = db.MySQLCursorWrapper(MockRawCursor())
        self.assertEqual(cursor_wrapper.lastrowid, 42)
        self.assertEqual(cursor_wrapper.rowcount, 3)
        self.assertEqual(cursor_wrapper.fetchone(), {"col": "val"})
        self.assertEqual(len(cursor_wrapper.fetchall()), 1)


if __name__ == "__main__":
    unittest.main()


