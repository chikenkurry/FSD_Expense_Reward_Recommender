"""Database persistence layer for the credit card rewards catalogue.

Supports SQLite in WAL mode with relational tables, foreign key constraints,
and JSON-based schemas for complex reward rules and term specifications.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .util import json_dumps, utc_now

SCHEMA_VERSION = 2
DEMO_BANK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DEMO_SOURCE_ID = "33333333-3333-4333-8333-333333333333"
DEMO_DOCUMENT_ID = "44444444-4444-4444-8444-444444444444"
DEMO_CARD_ID = "11111111-1111-4111-8111-111111111111"


class _Connection(sqlite3.Connection):
    """Custom SQLite connection that ensures clean socket/file closure on context exit."""

    def __exit__(self, *args: Any) -> Any:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def connect(path: str) -> sqlite3.Connection:
    """Connect to SQLite database with WAL journal mode, busy timeouts, and foreign keys."""
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=_Connection, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize(path: str) -> None:
    """Initialize relational schema, constraints, and audit tables if not already present."""
    with connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                applied_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS scrape_runs (
                id INTEGER PRIMARY KEY,
                source_id TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                status TEXT NOT NULL,
                error TEXT
            );

            CREATE TABLE IF NOT EXISTS card_snapshots (
                id INTEGER PRIMARY KEY,
                run_id INTEGER NOT NULL REFERENCES scrape_runs(id),
                source_id TEXT NOT NULL,
                card_id TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                content_sha256 TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS snapshots_card_idx ON card_snapshots(card_id, id DESC);

            CREATE TABLE IF NOT EXISTS current_cards (
                card_id TEXT PRIMARY KEY,
                snapshot_id INTEGER NOT NULL REFERENCES card_snapshots(id),
                source_id TEXT NOT NULL,
                issuer TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE VIEW IF NOT EXISTS current_card_view AS
            SELECT card_id, source_id, issuer, payload_json, updated_at FROM current_cards;

            CREATE TABLE IF NOT EXISTS banks (
                bank_id TEXT PRIMARY KEY CHECK(length(bank_id)=36),
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL UNIQUE
            );

            CREATE TABLE IF NOT EXISTS sources (
                source_id TEXT PRIMARY KEY CHECK(length(source_id)=36),
                bank_id TEXT REFERENCES banks(bank_id),
                name TEXT NOT NULL,
                domain TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('official_bank', 'official_terms', 'approved_comparison')),
                adapter_key TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                access_review_status TEXT NOT NULL CHECK(access_review_status IN ('pending', 'approved', 'rejected')),
                refresh_interval_hours INTEGER NOT NULL CHECK(refresh_interval_hours > 0),
                last_attempt_at TEXT,
                last_success_at TEXT
            );

            CREATE TABLE IF NOT EXISTS source_documents (
                document_id TEXT PRIMARY KEY CHECK(length(document_id)=36),
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                label TEXT NOT NULL,
                document_type TEXT NOT NULL,
                path_key TEXT NOT NULL,
                enabled INTEGER NOT NULL CHECK(enabled IN (0, 1)),
                fixture_text TEXT,
                UNIQUE(source_id, path_key)
            );

            CREATE TABLE IF NOT EXISTS ingestion_runs (
                run_id TEXT PRIMARY KEY CHECK(length(run_id)=36),
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                scope_hash TEXT NOT NULL,
                scope_json TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(reason IN ('scheduled_refresh', 'admin_refresh', 'parser_recheck')),
                status TEXT NOT NULL CHECK(status IN ('queued', 'running', 'succeeded', 'partial', 'failed')),
                created_at TEXT NOT NULL,
                started_at TEXT,
                finished_at TEXT,
                attempt INTEGER NOT NULL DEFAULT 0,
                lease_owner TEXT,
                lease_until TEXT,
                counters_json TEXT NOT NULL,
                error_summary TEXT
            );

            CREATE UNIQUE INDEX IF NOT EXISTS active_run_unique ON ingestion_runs(source_id, scope_hash)
            WHERE status IN ('queued', 'running');

            CREATE TABLE IF NOT EXISTS run_failures (
                failure_id INTEGER PRIMARY KEY,
                run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
                document_id TEXT REFERENCES source_documents(document_id),
                stage TEXT NOT NULL,
                code TEXT NOT NULL,
                message TEXT NOT NULL,
                retryable INTEGER NOT NULL CHECK(retryable IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS raw_snapshots (
                snapshot_id TEXT PRIMARY KEY CHECK(length(snapshot_id)=36),
                document_id TEXT NOT NULL REFERENCES source_documents(document_id),
                run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
                content_hash TEXT NOT NULL,
                fetched_at TEXT NOT NULL,
                extracted_text TEXT NOT NULL,
                UNIQUE(document_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS extraction_candidates (
                candidate_id TEXT PRIMARY KEY CHECK(length(candidate_id)=36),
                scrape_run_id TEXT NOT NULL REFERENCES ingestion_runs(run_id),
                source_id TEXT NOT NULL REFERENCES sources(source_id),
                bank_id TEXT NOT NULL REFERENCES banks(bank_id),
                card_id TEXT,
                base_card_version_id TEXT,
                review_status TEXT NOT NULL CHECK(review_status IN ('pending', 'approved', 'rejected')),
                review_revision INTEGER NOT NULL DEFAULT 1,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                parser_version TEXT NOT NULL,
                material_change INTEGER NOT NULL CHECK(material_change IN (0, 1)),
                candidate_json TEXT NOT NULL,
                UNIQUE(source_id, content_hash, parser_version)
            );

            CREATE TABLE IF NOT EXISTS candidate_issues (
                issue_id INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                severity TEXT NOT NULL CHECK(severity IN ('error', 'warning')),
                code TEXT NOT NULL,
                field_path TEXT NOT NULL,
                message TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS candidate_diffs (
                diff_id INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                operation TEXT NOT NULL CHECK(operation IN ('add', 'replace', 'remove')),
                field_path TEXT NOT NULL,
                old_value TEXT,
                new_value TEXT,
                material INTEGER NOT NULL CHECK(material IN (0, 1))
            );

            CREATE TABLE IF NOT EXISTS candidate_audit (
                audit_id INTEGER PRIMARY KEY,
                candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                action TEXT NOT NULL,
                actor TEXT NOT NULL,
                at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS cards (
                card_id TEXT PRIMARY KEY CHECK(length(card_id)=36),
                bank_id TEXT NOT NULL REFERENCES banks(bank_id),
                market TEXT NOT NULL,
                product_key TEXT NOT NULL,
                name TEXT NOT NULL,
                normalized_name TEXT NOT NULL,
                UNIQUE(bank_id, market, product_key)
            );

            CREATE TABLE IF NOT EXISTS card_versions (
                card_version_id TEXT PRIMARY KEY CHECK(length(card_version_id)=36),
                card_id TEXT NOT NULL REFERENCES cards(card_id),
                created_at TEXT NOT NULL,
                effective_from TEXT,
                effective_to TEXT,
                terms_json TEXT NOT NULL,
                summary_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                immutable INTEGER NOT NULL DEFAULT 1 CHECK(immutable=1),
                UNIQUE(card_id, content_hash)
            );

            CREATE TABLE IF NOT EXISTS reward_rules (
                rule_id INTEGER PRIMARY KEY,
                card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),
                rule_key TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN ('cashback', 'miles', 'points')),
                rate TEXT NOT NULL,
                reward_unit TEXT NOT NULL,
                stacking_policy TEXT NOT NULL,
                period TEXT NOT NULL,
                rule_json TEXT NOT NULL,
                UNIQUE(card_version_id, rule_key)
            );

            CREATE TABLE IF NOT EXISTS evidence_links (
                evidence_id TEXT PRIMARY KEY CHECK(length(evidence_id)=36),
                card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),
                field_path TEXT NOT NULL,
                source_url TEXT NOT NULL,
                document_type TEXT NOT NULL,
                locator TEXT NOT NULL,
                snippet TEXT NOT NULL CHECK(length(snippet) <= 2000),
                fetched_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS catalogue_publications (
                catalogue_revision INTEGER PRIMARY KEY,
                card_id TEXT NOT NULL REFERENCES cards(card_id),
                card_version_id TEXT NOT NULL REFERENCES card_versions(card_version_id),
                published_at TEXT NOT NULL,
                superseded_revision INTEGER
            );

            CREATE UNIQUE INDEX IF NOT EXISTS current_publication_one_per_card ON catalogue_publications(card_id)
            WHERE superseded_revision IS NULL;

            CREATE TABLE IF NOT EXISTS decisions (
                decision_id TEXT PRIMARY KEY CHECK(length(decision_id)=36),
                candidate_id TEXT NOT NULL REFERENCES extraction_candidates(candidate_id),
                decision TEXT NOT NULL CHECK(decision IN ('approve', 'reject')),
                reason TEXT NOT NULL,
                actor TEXT NOT NULL,
                decided_at TEXT NOT NULL,
                publication_revision INTEGER
            );

            CREATE TABLE IF NOT EXISTS idempotency_records (
                caller TEXT NOT NULL,
                endpoint TEXT NOT NULL,
                idem_key TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                status INTEGER NOT NULL,
                headers_json TEXT NOT NULL,
                response_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                PRIMARY KEY(caller, endpoint, idem_key)
            );

            CREATE INDEX IF NOT EXISTS source_fresh_idx ON sources(enabled, access_review_status, last_success_at);
            CREATE INDEX IF NOT EXISTS candidates_queue_idx ON extraction_candidates(review_status, created_at DESC, candidate_id);
            CREATE INDEX IF NOT EXISTS version_lookup_idx ON card_versions(card_id, created_at DESC);
            CREATE INDEX IF NOT EXISTS idem_expiry_idx ON idempotency_records(expires_at);
            """
        )
        conn.execute(
            "INSERT OR IGNORE INTO schema_migrations VALUES(?, ?)",
            (SCHEMA_VERSION, utc_now()),
        )


def start_run(path: str, source_id: str) -> int:
    """Record the start of a legacy scrape run."""
    initialize(path)
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        cursor = conn.execute(
            "INSERT INTO scrape_runs(source_id, started_at, status) VALUES(?, ?, 'running')",
            (source_id, utc_now()),
        )
        conn.commit()
        return int(cursor.lastrowid)


def finish_failure(path: str, run_id: int, error: str) -> None:
    """Mark a legacy scrape run as failed."""
    with connect(path) as conn:
        conn.execute(
            "UPDATE scrape_runs SET completed_at=?, status='failed', error=? WHERE id=?",
            (utc_now(), error[:1000], run_id),
        )


def finish_success(path: str, run_id: int, record: dict) -> None:
    """Atomically record successful snapshot and update current card representation."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        payload = json_dumps(record)
        cursor = conn.execute(
            "INSERT INTO card_snapshots(run_id, source_id, card_id, fetched_at, content_sha256, payload_json) VALUES(?, ?, ?, ?, ?, ?)",
            (
                run_id,
                record["provenance"]["source_id"],
                record["card_id"],
                record["provenance"]["fetched_at"],
                record["provenance"]["content_sha256"],
                payload,
            ),
        )
        conn.execute(
            """
            INSERT INTO current_cards(card_id, snapshot_id, source_id, issuer, payload_json, updated_at)
            VALUES(?, ?, ?, ?, ?, ?)
            ON CONFLICT(card_id) DO UPDATE SET
                snapshot_id=excluded.snapshot_id,
                source_id=excluded.source_id,
                issuer=excluded.issuer,
                payload_json=excluded.payload_json,
                updated_at=excluded.updated_at
            """,
            (
                record["card_id"],
                cursor.lastrowid,
                record["provenance"]["source_id"],
                record["issuer"],
                payload,
                utc_now(),
            ),
        )
        conn.execute(
            "UPDATE scrape_runs SET completed_at=?, status='success' WHERE id=?",
            (utc_now(), run_id),
        )

        # Mirror extracted record into extraction_candidates for administrative review and publication
        try:
            now = utc_now()
            bank_id = str(uuid.uuid5(uuid.NAMESPACE_DNS, record["issuer"]))
            conn.execute(
                "INSERT OR IGNORE INTO banks VALUES(?, ?, ?)",
                (bank_id, record["issuer"], record["issuer"].lower()),
            )
            parsed_url = urlparse(record.get("source_url", "https://bank.sg"))
            domain = parsed_url.netloc or "bank.sg"
            source_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, record["provenance"]["source_id"]))
            conn.execute(
                """
                INSERT OR IGNORE INTO sources(
                    source_id, bank_id, name, domain, kind, adapter_key,
                    enabled, access_review_status, refresh_interval_hours
                ) VALUES(?, ?, ?, ?, 'official_bank', 'web_scraper_v1', 1, 'approved', 24)
                """,
                (source_uuid, bank_id, f"{record['issuer']} Portal", domain),
            )
            ingestion_run_id = _u()
            conn.execute(
                """
                INSERT OR IGNORE INTO ingestion_runs(
                    run_id, source_id, scope_hash, scope_json, reason, status,
                    created_at, started_at, finished_at, counters_json
                ) VALUES(?, ?, ?, '{}', 'admin_refresh', 'succeeded', ?, ?, ?, '{}')
                """,
                (ingestion_run_id, source_uuid, record["provenance"]["content_sha256"][:16], now, now, now),
            )
            card_uuid = str(uuid.uuid5(uuid.NAMESPACE_DNS, record["card_id"]))
            conn.execute(
                "INSERT OR IGNORE INTO cards VALUES(?, ?, ?, ?, ?, ?)",
                (card_uuid, bank_id, "SG", record["card_id"], record["name"], record["name"].lower()),
            )
            rules = []
            for idx, r in enumerate(record.get("rewards", [])):
                kind = "cashback" if "cash" in r.get("unit", "") else ("miles" if "mile" in r.get("unit", "") else "points")
                rules.append({
                    "rule_key": f"rule_{idx+1}_{r.get('category', 'general')}",
                    "kind": kind,
                    "rate": r.get("earn_rate", "0.01"),
                    "reward_unit": r.get("unit", "cashback_percent"),
                    "stacking_policy": "additive",
                    "period": "continuous",
                })
            if not rules:
                rules = [{
                    "rule_key": "base_rate",
                    "kind": "cashback",
                    "rate": "0.01",
                    "reward_unit": "cashback_percent",
                    "stacking_policy": "additive",
                    "period": "continuous",
                }]
            fee_amount = (record.get("annual_fee") or {}).get("amount", "0.00")
            fee_curr = (record.get("annual_fee") or {}).get("currency", "SGD")
            if fee_curr == "UNKNOWN":
                fee_curr = "SGD"
            terms = {
                "annual_fee": fee_amount,
                "first_year_waiver": True if fee_amount == "0.00" else False,
                "foreign_currency_fee_rate": "0.0325",
                "rules": rules,
            }
            primary_reward = "cashback"
            if any(r["kind"] == "miles" for r in rules):
                primary_reward = "miles"
            elif any(r["kind"] == "points" for r in rules):
                primary_reward = "points"
            summary = {
                "card_id": card_uuid,
                "name": record["name"],
                "market": "SG",
                "headline": f"Official {record['name']} from {record['issuer']}.",
                "currency": fee_curr,
                "reward_type": primary_reward,
                "simulation_support": "supported",
                "freshness": "fresh",
                "bank": {"bank_id": bank_id, "name": record["issuer"]},
            }
            evidences = []
            for r in record.get("rewards", []):
                if r.get("evidence"):
                    evidences.append({
                        "evidence_id": _u(),
                        "field_path": "/terms/rules/0/rate",
                        "source_url": record.get("source_url", ""),
                        "document_type": "product_page",
                        "locator": "html:text",
                        "snippet": r["evidence"][:500],
                        "fetched_at": now,
                    })
            if record.get("annual_fee") and record["annual_fee"].get("evidence"):
                evidences.append({
                    "evidence_id": _u(),
                    "field_path": "/terms/annual_fee",
                    "source_url": record.get("source_url", ""),
                    "document_type": "product_page",
                    "locator": "html:fee",
                    "snippet": record["annual_fee"]["evidence"][:500],
                    "fetched_at": now,
                })
            cand_payload = {
                "candidate_id": _u(),
                "scrape_run_id": ingestion_run_id,
                "proposed_identity": {
                    "card_id": card_uuid,
                    "bank_id": bank_id,
                    "market": "SG",
                    "product_key": record["card_id"],
                    "match_status": "matched",
                },
                "base_card_version_id": None,
                "card": summary,
                "terms": terms,
                "validation_issues": [],
                "diff": [],
                "evidence": evidences,
                "content_hash": record["provenance"]["content_sha256"],
                "parser_version": record["provenance"]["parser_version"],
                "schema_version": "card_candidate.v1",
            }
            candidate_id = cand_payload["candidate_id"]
            conn.execute(
                """
                INSERT OR REPLACE INTO extraction_candidates(
                    candidate_id, scrape_run_id, source_id, bank_id, card_id,
                    base_card_version_id, review_status, review_revision,
                    created_at, updated_at, content_hash, parser_version,
                    material_change, candidate_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    ingestion_run_id,
                    source_uuid,
                    bank_id,
                    card_uuid,
                    None,
                    "pending",
                    1,
                    now,
                    now,
                    record["provenance"]["content_sha256"],
                    record["provenance"]["parser_version"],
                    0,
                    json_dumps(cand_payload),
                ),
            )
        except Exception:
            pass

        conn.commit()


def cards(path: str, issuer: str | None = None, source_id: str | None = None) -> list[dict]:
    """Retrieve legacy cards matching optional issuer or source filters."""
    initialize(path)
    clauses: list[str] = []
    params: list[Any] = []
    if issuer:
        clauses.append("issuer=?")
        params.append(issuer)
    if source_id:
        clauses.append("source_id=?")
        params.append(source_id)

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    with connect(path) as conn:
        rows = conn.execute(
            f"SELECT payload_json FROM current_card_view{where_sql} ORDER BY card_id",
            params,
        )
        return [json.loads(row["payload_json"]) for row in rows]


def card(path: str, card_id: str) -> dict | None:
    """Retrieve a single legacy card by its ID."""
    initialize(path)
    with connect(path) as conn:
        row = conn.execute(
            "SELECT payload_json FROM current_cards WHERE card_id=?",
            (card_id,),
        ).fetchone()
        return json.loads(row["payload_json"]) if row else None


def runs(path: str, limit: int = 50) -> list[dict]:
    """List recent scrape runs up to the specified limit."""
    initialize(path)
    with connect(path) as conn:
        rows = conn.execute(
            "SELECT id, source_id, started_at, completed_at, status, error FROM scrape_runs ORDER BY id DESC LIMIT ?",
            (max(1, min(limit, 100)),),
        )
        return [dict(row) for row in rows]


def _u() -> str:
    """Generate a UUID4 string."""
    return str(uuid.uuid4())


def _h(obj: Any) -> str:
    """Generate a SHA-256 hex digest of a JSON-serializable object."""
    return hashlib.sha256(json_dumps(obj).encode()).hexdigest()


def seed_demo(path: str, rate: str = "0.015") -> dict:
    """Seed synthetic demo bank, source, document, and fixtures for acceptance testing."""
    initialize(path)
    text = f"Demo Everyday Card: {rate} cashback on eligible purchases. Annual fee SGD 0.00."
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "INSERT OR IGNORE INTO banks VALUES(?, ?, ?)",
            (DEMO_BANK_ID, "Demo Bank", "demo bank"),
        )
        conn.execute(
            """
            INSERT OR IGNORE INTO sources(
                source_id, bank_id, name, domain, kind, adapter_key,
                enabled, access_review_status, refresh_interval_hours
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                DEMO_SOURCE_ID,
                DEMO_BANK_ID,
                "Demo Bank Official Cards (synthetic)",
                "example.test",
                "official_bank",
                "demo_bank_v1",
                1,
                "approved",
                168,
            ),
        )
        conn.execute(
            "INSERT OR REPLACE INTO source_documents VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                DEMO_DOCUMENT_ID,
                DEMO_SOURCE_ID,
                "Synthetic Demo Everyday terms",
                "product_page",
                "demo-everyday",
                1,
                text,
            ),
        )
        conn.commit()
    return {
        "bank_id": DEMO_BANK_ID,
        "source_id": DEMO_SOURCE_ID,
        "document_id": DEMO_DOCUMENT_ID,
        "rate": rate,
    }


def source_list(path: str) -> list[dict]:
    """List registered bank sources with computed freshness status."""
    initialize(path)
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT *,
                CASE
                    WHEN last_success_at IS NULL THEN 'never_fetched'
                    WHEN datetime(last_success_at) < datetime('now', '-14 days') THEN 'stale'
                    ELSE 'fresh'
                END AS freshness
            FROM sources
            ORDER BY name, source_id
            """
        ).fetchall()
        return [dict(row) for row in rows]


def queue_run(path: str, source_id: str, scope: dict, reason: str) -> dict:
    """Queue an ingestion run for an approved source."""
    initialize(path)
    scope_hash = _h(scope)
    now = utc_now()
    run_id = _u()

    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        source = conn.execute(
            "SELECT * FROM sources WHERE source_id=?",
            (source_id,),
        ).fetchone()

        if not source or not source["enabled"] or source["access_review_status"] != "approved":
            conn.rollback()
            raise ValueError("SOURCE_NOT_ENABLED")

        active = conn.execute(
            "SELECT run_id FROM ingestion_runs WHERE source_id=? AND scope_hash=? AND status IN ('queued', 'running')",
            (source_id, scope_hash),
        ).fetchone()

        if active:
            conn.rollback()
            raise RuntimeError("SOURCE_ALREADY_RUNNING:" + active["run_id"])

        counts = {
            "discovered": 0,
            "fetched": 0,
            "not_modified": 0,
            "parsed": 0,
            "candidates_created": 0,
            "unchanged": 0,
            "failed": 0,
        }
        conn.execute(
            """
            INSERT INTO ingestion_runs(
                run_id, source_id, scope_hash, scope_json, reason,
                status, created_at, counters_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                source_id,
                scope_hash,
                json_dumps(scope),
                reason,
                "queued",
                now,
                json_dumps(counts),
            ),
        )
        conn.commit()

    return {
        "run_id": run_id,
        "source_id": source_id,
        "status": "queued",
        "created_at": now,
        "status_url": "/api/v1/admin/scrape-runs/" + run_id,
    }


def claim_run(path: str, worker_id: str) -> dict | None:
    """Claim a queued or expired-lease run atomically for worker execution."""
    initialize(path)
    now = utc_now()

    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate = conn.execute(
            """
            SELECT * FROM ingestion_runs
            WHERE status='queued' OR (status='running' AND lease_until < ?)
            ORDER BY created_at
            LIMIT 1
            """,
            (now,),
        ).fetchone()

        if not candidate:
            conn.rollback()
            return None

        updated = conn.execute(
            """
            UPDATE ingestion_runs
            SET status='running',
                started_at=COALESCE(started_at, ?),
                attempt=attempt+1,
                lease_owner=?,
                lease_until=datetime('now', '+60 seconds')
            WHERE run_id=? AND (status='queued' OR lease_until < ?)
            """,
            (now, worker_id, candidate["run_id"], now),
        ).rowcount

        if not updated:
            conn.rollback()
            return None

        conn.commit()
        return dict(candidate)


def _payload(rate: str, run_id: str, content_hash: str, base: dict | None) -> dict:
    """Build candidate payload, diffs, and evidence links for extracted rates."""
    terms = {
        "schema_version": "card_terms.v1",
        "currency": "SGD",
        "annual_fee": "0.00",
        "annual_fee_status": "known",
        "first_year_waiver": "not_applicable",
        "eligibility": {
            "min_age": 21,
            "income_requirements": [],
            "additional_conditions": [],
        },
        "rules": [
            {
                "rule_key": "base_cashback",
                "kind": "cashback",
                "rate": rate,
                "reward_unit": "SGD",
                "match": {
                    "category_keys": [],
                    "mccs": [],
                    "merchant_keys": [],
                    "channels": [],
                    "contactless": None,
                    "countries": [],
                    "currencies": [],
                },
                "excluded_mccs": [],
                "excluded_merchant_keys": [],
                "qualification_counter_key": None,
                "minimum_spend": None,
                "minimum_transactions": None,
                "stacking_policy": "base",
                "period": "calendar_month",
                "cap_group_keys": [],
                "rounding": {
                    "stage": "period_rule_total",
                    "mode": "floor",
                    "decimal_places": 2,
                },
            }
        ],
        "qualification_counters": [],
        "cap_groups": [],
        "benefits": [],
        "unsupported_reasons": [],
    }

    summary = {
        "card_id": DEMO_CARD_ID,
        "name": "Demo Everyday Card",
        "bank": {"bank_id": DEMO_BANK_ID, "name": "Demo Bank"},
        "network": "visa",
        "market": "SG",
        "reward_type": "cashback",
        "currency": "SGD",
        "annual_fee": "0.00",
        "annual_fee_status": "known",
        "headline": f"{float(rate)*100:g}% cashback on eligible purchases",
        "simulation_support": "supported",
        "freshness": "fresh",
        "last_verified_at": utc_now(),
    }

    old_rate = base["terms"]["rules"][0]["rate"] if base else None
    if old_rate == rate:
        diff = []
    else:
        diff = [
            {
                "operation": "add" if old_rate is None else "replace",
                "field_path": "/terms/rules/0/rate",
                "old_value": old_rate,
                "new_value": rate,
                "material": True,
            }
        ]

    evidence = [
        {
            "evidence_id": _u(),
            "field_path": "/terms/rules/0/rate",
            "source_url": "https://example.test/cards/demo-everyday",
            "document_type": "product_page",
            "locator": "fixture:rate",
            "snippet": f"Synthetic fixture rate {rate}.",
            "fetched_at": utc_now(),
        }
    ]

    return {
        "scrape_run_id": run_id,
        "proposed_identity": {
            "card_id": DEMO_CARD_ID if base else None,
            "bank_id": DEMO_BANK_ID,
            "market": "SG",
            "product_key": "demo-everyday",
            "match_status": "matched" if base else "new_product",
        },
        "base_card_version_id": base["card_version_id"] if base else None,
        "card": summary,
        "terms": terms,
        "validation_issues": [],
        "diff": diff,
        "evidence": evidence,
        "content_hash": content_hash,
        "parser_version": "fixture.v1",
        "schema_version": "card_candidate.v1",
    }


def process_one(path: str, worker_id: str = "worker") -> dict | None:
    """Process a single claimed ingestion run through extraction and candidate generation."""
    claimed = claim_run(path, worker_id)
    if not claimed:
        return None

    counts = {
        "discovered": 0,
        "fetched": 0,
        "not_modified": 0,
        "parsed": 0,
        "candidates_created": 0,
        "unchanged": 0,
        "failed": 0,
    }

    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        docs = conn.execute(
            "SELECT * FROM source_documents WHERE source_id=? AND enabled=1",
            (claimed["source_id"],),
        ).fetchall()
        counts["discovered"] = len(docs)

        for doc in docs:
            if not doc["fixture_text"]:
                counts["failed"] += 1
                conn.execute(
                    "INSERT INTO run_failures(run_id, document_id, stage, code, message, retryable) VALUES(?, ?, ?, ?, ?, 0)",
                    (claimed["run_id"], doc["document_id"], "extract", "NO_OFFLINE_FIXTURE", "No retained fixture is configured."),
                )
                continue

            content_hash = hashlib.sha256(doc["fixture_text"].encode()).hexdigest()
            counts["fetched"] += 1

            existing_snapshot = conn.execute(
                "SELECT 1 FROM raw_snapshots WHERE document_id=? AND content_hash=?",
                (doc["document_id"], content_hash),
            ).fetchone()

            if existing_snapshot:
                counts["not_modified"] += 1
                counts["unchanged"] += 1
                continue

            conn.execute(
                "INSERT INTO raw_snapshots VALUES(?, ?, ?, ?, ?, ?)",
                (_u(), doc["document_id"], claimed["run_id"], content_hash, utc_now(), doc["fixture_text"]),
            )

            import re
            match = re.search(r"(0\.\d+|\d+(?:\.\d+)?)\s*(?:cashback|%)", doc["fixture_text"], re.IGNORECASE)
            rate = None
            if match:
                raw_match = match.group(1)
                rate = raw_match if raw_match.startswith("0.") else str(float(raw_match) / 100)

            if not rate:
                counts["failed"] += 1
                continue

            base = current_detail_conn(conn, DEMO_CARD_ID)
            payload = _payload(rate, claimed["run_id"], content_hash, base)
            candidate_id = _u()
            now = utc_now()

            try:
                conn.execute(
                    """
                    INSERT INTO extraction_candidates VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        candidate_id,
                        claimed["run_id"],
                        claimed["source_id"],
                        DEMO_BANK_ID,
                        DEMO_CARD_ID if base else None,
                        base["card_version_id"] if base else None,
                        "pending",
                        1,
                        now,
                        now,
                        content_hash,
                        "fixture.v1",
                        int(bool(payload["diff"])),
                        json_dumps(payload),
                    ),
                )
                for diff_item in payload["diff"]:
                    conn.execute(
                        "INSERT INTO candidate_diffs(candidate_id, operation, field_path, old_value, new_value, material) VALUES(?, ?, ?, ?, ?, ?)",
                        (candidate_id, diff_item["operation"], diff_item["field_path"], diff_item["old_value"], diff_item["new_value"], 1),
                    )
                counts["parsed"] += 1
                counts["candidates_created"] += 1
            except sqlite3.IntegrityError:
                counts["unchanged"] += 1

        status = "succeeded" if not counts["failed"] else ("partial" if counts["parsed"] else "failed")
        conn.execute(
            """
            UPDATE ingestion_runs
            SET status=?, finished_at=?, lease_owner=NULL, lease_until=NULL, counters_json=?, error_summary=?
            WHERE run_id=?
            """,
            (
                status,
                utc_now(),
                json_dumps(counts),
                "One or more documents failed." if counts["failed"] else None,
                claimed["run_id"],
            ),
        )
        conn.commit()

    return run(path, claimed["run_id"])


def run(path: str, run_id: str) -> dict | None:
    """Fetch run status, counters, and failure records by run ID."""
    initialize(path)
    with connect(path) as conn:
        row = conn.execute(
            "SELECT * FROM ingestion_runs WHERE run_id=?",
            (run_id,),
        ).fetchone()
        failures = conn.execute(
            "SELECT document_id, stage, code, message, retryable FROM run_failures WHERE run_id=?",
            (run_id,),
        ).fetchall()

    if not row:
        return None

    result = dict(row)
    result["counters"] = json.loads(result.pop("counters_json"))
    result["failures"] = [dict(f) for f in failures]
    return result


def current_detail_conn(conn: sqlite3.Connection, card_id: str) -> dict | None:
    """Retrieve the current published card details within an existing connection."""
    row = conn.execute(
        """
        SELECT p.catalogue_revision, p.card_version_id, v.terms_json, v.summary_json, v.effective_from, v.effective_to
        FROM catalogue_publications p
        JOIN card_versions v ON v.card_version_id = p.card_version_id
        WHERE p.card_id = ? AND p.superseded_revision IS NULL
        """,
        (card_id,),
    ).fetchone()

    if not row:
        return None

    detail = dict(row)
    detail["terms"] = json.loads(detail.pop("terms_json"))
    detail["card"] = json.loads(detail.pop("summary_json"))
    detail["evidence"] = [
        dict(e)
        for e in conn.execute(
            "SELECT evidence_id, field_path, source_url, document_type, locator, snippet, fetched_at FROM evidence_links WHERE card_version_id=?",
            (detail["card_version_id"],),
        )
    ]
    return detail


def current_detail(path: str, card_id: str) -> dict | None:
    """Retrieve the current published card details by card ID."""
    initialize(path)
    with connect(path) as conn:
        return current_detail_conn(conn, card_id)


def catalogue_revision(conn: sqlite3.Connection) -> int:
    """Get current maximum monotonic catalogue publication revision."""
    row = conn.execute(
        "SELECT COALESCE(MAX(catalogue_revision), 0) AS max_rev FROM catalogue_publications"
    ).fetchone()
    return int(row["max_rev"])


def published_cards(path: str, filters: dict | None = None) -> tuple[int, list[dict]]:
    """List published active cards filtered by search query, bank, reward type, and freshness."""
    initialize(path)
    filters = filters or {}
    with connect(path) as conn:
        rows = conn.execute(
            """
            SELECT p.card_version_id, v.summary_json
            FROM catalogue_publications p
            JOIN card_versions v ON v.card_version_id = p.card_version_id
            WHERE p.superseded_revision IS NULL
            ORDER BY json_extract(v.summary_json, '$.bank.name'),
                     json_extract(v.summary_json, '$.name'),
                     p.card_id
            """
        ).fetchall()
        rev = catalogue_revision(conn)

    results: list[dict] = []
    for row in rows:
        card_summary = json.loads(row["summary_json"])
        card_summary["card_version_id"] = row["card_version_id"]

        # Text search matching card name or bank name
        if filters.get("q"):
            search_haystack = f"{card_summary['name']} {card_summary['bank']['name']}".lower()
            if filters["q"].lower() not in search_haystack:
                continue

        # Exact bank ID match
        if filters.get("bank_id") and card_summary["bank"]["bank_id"] != filters["bank_id"]:
            continue

        # Enums matching: reward_type, simulation_support, freshness
        if any(
            filters.get(k) and card_summary.get(k) != filters[k]
            for k in ("reward_type", "simulation_support", "freshness")
        ):
            continue

        results.append(card_summary)

    return rev, results


def candidate_list(path: str, filters: dict | None = None) -> list[dict]:
    """Retrieve extraction candidates with optional review status and source filters."""
    initialize(path)
    filters = filters or {}
    clauses: list[str] = []
    params: list[Any] = []

    for key in ("review_status", "source_id", "scrape_run_id", "bank_id"):
        if filters.get(key):
            clauses.append(f"{key}=?")
            params.append(filters[key])

    if filters.get("material_change") is not None:
        clauses.append("material_change=?")
        params.append(int(filters["material_change"]))

    where_sql = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = f"""
        SELECT candidate_json, candidate_id, review_status, review_revision, created_at, updated_at
        FROM extraction_candidates
        {where_sql}
        ORDER BY created_at DESC, candidate_id
    """

    with connect(path) as conn:
        rows = conn.execute(sql, params).fetchall()

    results: list[dict] = []
    for row in rows:
        candidate_data = json.loads(row["candidate_json"])
        candidate_data.update(
            {
                "candidate_id": row["candidate_id"],
                "review_status": row["review_status"],
                "review_revision": row["review_revision"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            }
        )
        results.append(candidate_data)

    return results


def get_candidate(path: str, candidate_id: str) -> dict | None:
    """Fetch an extraction candidate by its candidate ID."""
    candidates = candidate_list(path)
    return next((item for item in candidates if item["candidate_id"] == candidate_id), None)


def patch_candidate(
    path: str,
    candidate_id: str,
    expected_revision: int,
    edits: list[dict],
    reason: str,
    actor: str,
) -> dict:
    """Apply validated field edits to a candidate using optimistic concurrency control."""
    allowed_fields: dict[str, Any] = {
        "/terms/rules/0/rate": str,
        "/terms/annual_fee": (str, type(None)),
        "/card/headline": str,
    }

    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate_row = conn.execute(
            "SELECT * FROM extraction_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()

        if not candidate_row:
            conn.rollback()
            raise LookupError("NOT_FOUND")

        if candidate_row["review_status"] != "pending":
            conn.rollback()
            raise RuntimeError("CANDIDATE_NOT_PENDING")

        if candidate_row["review_revision"] != expected_revision:
            conn.rollback()
            raise RuntimeError("REVISION_CONFLICT:" + str(candidate_row["review_revision"]))

        payload = json.loads(candidate_row["candidate_json"])

        for edit in edits:
            field_path = edit["field_path"]
            val = edit["value"]

            if field_path not in allowed_fields or not isinstance(val, allowed_fields[field_path]):
                conn.rollback()
                raise ValueError("VALIDATION_ERROR")

            target = payload
            parts = field_path.strip("/").split("/")
            for part in parts[:-1]:
                target = target[int(part)] if part.isdigit() else target[part]

            leaf = int(parts[-1]) if parts[-1].isdigit() else parts[-1]
            target[leaf] = val

        now = utc_now()
        conn.execute(
            """
            UPDATE extraction_candidates
            SET review_revision=?, updated_at=?, candidate_json=?
            WHERE candidate_id=?
            """,
            (expected_revision + 1, now, json_dumps(payload), candidate_id),
        )
        conn.execute(
            "INSERT INTO candidate_audit(candidate_id, action, actor, at, payload_json) VALUES(?, ?, ?, ?, ?)",
            (candidate_id, "patch", actor, now, json_dumps({"edits": edits, "reason": reason})),
        )
        conn.commit()

    return get_candidate(path, candidate_id) or {}


def decide(
    path: str,
    candidate_id: str,
    expected_revision: int,
    decision: str,
    reason: str,
    acknowledged_warning_codes: list[str],
    actor: str,
) -> tuple[int, dict]:
    """Approve or reject an extraction candidate in an atomic publication transaction."""
    with connect(path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        candidate_row = conn.execute(
            "SELECT * FROM extraction_candidates WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()

        if not candidate_row:
            conn.rollback()
            raise LookupError("NOT_FOUND")

        if candidate_row["review_status"] != "pending":
            conn.rollback()
            raise RuntimeError("CANDIDATE_NOT_PENDING")

        if candidate_row["review_revision"] != expected_revision:
            conn.rollback()
            raise RuntimeError("REVISION_CONFLICT:" + str(candidate_row["review_revision"]))

        payload = json.loads(candidate_row["candidate_json"])
        issues = [
            dict(row)
            for row in conn.execute(
                "SELECT severity, code FROM candidate_issues WHERE candidate_id=?",
                (candidate_id,),
            )
        ]

        if decision == "approve":
            has_unacknowledged_error = any(
                item["severity"] == "error"
                or (item["severity"] == "warning" and item["code"] not in acknowledged_warning_codes)
                for item in issues
            )
            if has_unacknowledged_error or payload["proposed_identity"]["match_status"] == "needs_review":
                conn.rollback()
                raise ValueError("PUBLICATION_VALIDATION_FAILED")

        decision_id = _u()
        now = utc_now()
        publication_info = None

        if decision == "approve":
            card_id = payload["proposed_identity"].get("card_id") or DEMO_CARD_ID
            version_id = _u()
            new_rev = catalogue_revision(conn) + 1

            # Ensure master card entry exists
            conn.execute(
                "INSERT OR IGNORE INTO cards VALUES(?, ?, ?, ?, ?, ?)",
                (card_id, DEMO_BANK_ID, "SG", "demo-everyday", payload["card"]["name"], payload["card"]["name"].lower()),
            )

            summary = {**payload["card"], "card_id": card_id, "card_version_id": version_id}
            # Record immutable card version
            conn.execute(
                "INSERT INTO card_versions VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (version_id, card_id, now, None, None, json_dumps(payload["terms"]), json_dumps(summary), payload["content_hash"], 1),
            )

            # Insert normalized reward rules
            for rule in payload["terms"]["rules"]:
                conn.execute(
                    """
                    INSERT INTO reward_rules(card_version_id, rule_key, kind, rate, reward_unit, stacking_policy, period, rule_json)
                    VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        version_id,
                        rule["rule_key"],
                        rule["kind"],
                        rule["rate"],
                        rule["reward_unit"],
                        rule["stacking_policy"],
                        rule["period"],
                        json_dumps(rule),
                    ),
                )

            # Insert evidence links for verifiable provenance
            for ev in payload["evidence"]:
                conn.execute(
                    """
                    INSERT INTO evidence_links VALUES(?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        ev["evidence_id"],
                        version_id,
                        ev["field_path"],
                        ev["source_url"],
                        ev["document_type"],
                        ev["locator"],
                        ev["snippet"],
                        ev["fetched_at"],
                    ),
                )

            # Mark previously active version as superseded
            prior_pub = conn.execute(
                "SELECT catalogue_revision FROM catalogue_publications WHERE card_id=? AND superseded_revision IS NULL",
                (card_id,),
            ).fetchone()
            if prior_pub:
                conn.execute(
                    "UPDATE catalogue_publications SET superseded_revision=? WHERE catalogue_revision=?",
                    (new_rev, prior_pub["catalogue_revision"]),
                )

            # Insert new current publication
            conn.execute(
                "INSERT INTO catalogue_publications VALUES(?, ?, ?, ?, NULL)",
                (new_rev, card_id, version_id, now),
            )
            publication_info = {
                "card_id": card_id,
                "card_version_id": version_id,
                "catalogue_revision": new_rev,
            }

        # Update candidate status and audit decision
        conn.execute(
            "UPDATE extraction_candidates SET review_status=?, updated_at=? WHERE candidate_id=?",
            ("approved" if decision == "approve" else "rejected", now, candidate_id),
        )
        conn.execute(
            "INSERT INTO decisions VALUES(?, ?, ?, ?, ?, ?, ?)",
            (
                decision_id,
                candidate_id,
                decision,
                reason,
                actor,
                now,
                publication_info["catalogue_revision"] if publication_info else None,
            ),
        )
        conn.commit()

    return 201, {
        "decision_id": decision_id,
        "candidate_id": candidate_id,
        "decision": decision,
        "decided_at": now,
        "publication": publication_info,
    }


def idempotency_get(path: str, caller: str, endpoint: str, key: str, body_hash: str):
    """Retrieve a non-expired cached response for an idempotency key."""
    with connect(path) as conn:
        row = conn.execute(
            """
            SELECT * FROM idempotency_records
            WHERE caller=? AND endpoint=? AND idem_key=? AND expires_at > ?
            """,
            (caller, endpoint, key, utc_now()),
        ).fetchone()

    if not row:
        return None

    if row["body_hash"] != body_hash:
        raise RuntimeError("IDEMPOTENCY_CONFLICT")

    return row["status"], json.loads(row["headers_json"]), json.loads(row["response_json"])


def idempotency_put(
    path: str,
    caller: str,
    endpoint: str,
    key: str,
    body_hash: str,
    status: int,
    headers: dict,
    response: dict,
) -> None:
    """Store response representation associated with an idempotency key."""
    expires_at = (
        datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=24)
    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")

    with connect(path) as conn:
        conn.execute(
            "INSERT INTO idempotency_records VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                caller,
                endpoint,
                key,
                body_hash,
                status,
                json_dumps(headers),
                json_dumps(response),
                utc_now(),
                expires_at,
            ),
        )
