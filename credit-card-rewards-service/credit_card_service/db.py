"""Database persistence layer for the credit card rewards catalogue.

Supports SQLite in WAL mode and MySQL 8.0 with relational tables, foreign key constraints,
and JSON-based schemas for complex reward rules and term specifications.
"""

from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
import sqlite3
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

try:
    import pymysql
    import pymysql.cursors
except ImportError:
    pymysql = None

from .util import json_dumps, utc_now

SCHEMA_VERSION = 2
DEMO_BANK_ID = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
DEMO_SOURCE_ID = "33333333-3333-4333-8333-333333333333"
DEMO_DOCUMENT_ID = "44444444-4444-4444-8444-444444444444"
DEMO_CARD_ID = "11111111-1111-4111-8111-111111111111"


def is_mysql(path: str) -> bool:
    """Check if the path or environment DATABASE_URL targets a MySQL database."""
    target = path or os.environ.get("DATABASE_URL", "")
    return target.startswith("mysql://") or target.startswith("mysql+pymysql://")


def parse_mysql_url(url_str: str) -> dict[str, Any]:
    """Parse MySQL connection parameters from standard connection string."""
    if url_str.startswith("mysql+pymysql://"):
        url_str = "mysql://" + url_str[len("mysql+pymysql://"):]
    parsed = urlparse(url_str)
    return {
        "host": parsed.hostname or "127.0.0.1",
        "port": parsed.port or 3306,
        "user": parsed.username or "root",
        "password": parsed.password or "",
        "database": parsed.path.lstrip("/"),
    }


def sqlite_to_mysql_query(sql: str) -> str:
    """Translate SQLite-specific SQL idioms and parameter placeholders to MySQL syntax."""
    # 1. Translate INSERT OR IGNORE and INSERT OR REPLACE
    sql = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT IGNORE INTO", sql, flags=re.IGNORECASE)
    sql = re.sub(r"INSERT\s+OR\s+REPLACE\s+INTO", "REPLACE INTO", sql, flags=re.IGNORECASE)
    # 2. Translate SQLite date/time expressions to MySQL equivalents
    sql = re.sub(r"datetime\('now',\s*'\+60 seconds'\)", "DATE_ADD(UTC_TIMESTAMP(), INTERVAL 60 SECOND)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"datetime\('now',\s*'-14 days'\)", "DATE_SUB(UTC_TIMESTAMP(), INTERVAL 14 DAY)", sql, flags=re.IGNORECASE)
    sql = re.sub(r"datetime\(([^)]+)\)", r"\1", sql, flags=re.IGNORECASE)
    # 3. Replace ? parameter placeholders with %s outside single-quoted strings
    sql = re.sub(r"\?(?=(?:[^']*'[^']*')*[^']*$)", "%s", sql)
    return sql


class MySQLCursorWrapper:
    """Wrapper around PyMySQL cursor matching SQLite Row and Cursor ergonomics."""

    def __init__(self, cursor: Any) -> None:
        self._cursor = cursor

    def fetchone(self) -> dict[str, Any] | None:
        return self._cursor.fetchone()

    def fetchall(self) -> list[dict[str, Any]]:
        return self._cursor.fetchall()

    def __iter__(self):
        return iter(self.fetchall())

    @property
    def lastrowid(self) -> int | None:
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        return self._cursor.rowcount

    def close(self) -> None:
        self._cursor.close()


class MySQLConnectionWrapper:
    """PyMySQL connection wrapper with automatic transaction management and dialect adaptation."""

    def __init__(self, raw_conn: Any) -> None:
        self._raw_conn = raw_conn

    def execute(self, sql: str, params: tuple | list = ()) -> MySQLCursorWrapper:
        clean_sql = sql.strip()
        upper_sql = clean_sql.upper()
        if upper_sql in ("BEGIN IMMEDIATE", "BEGIN"):
            try:
                self._raw_conn.begin()
            except Exception:
                pass
            class _DummyCursor:
                lastrowid = None
                rowcount = 0
                def fetchone(self): return None
                def fetchall(self): return []
                def __iter__(self): return iter([])
                def close(self): pass
            return MySQLCursorWrapper(_DummyCursor())

        transformed = sqlite_to_mysql_query(sql)
        cursor = self._raw_conn.cursor()
        cursor.execute(transformed, params or ())
        return MySQLCursorWrapper(cursor)

    def executemany(self, sql: str, seq_of_params: list | tuple) -> MySQLCursorWrapper:
        transformed = sqlite_to_mysql_query(sql)
        cursor = self._raw_conn.cursor()
        cursor.executemany(transformed, seq_of_params)
        return MySQLCursorWrapper(cursor)

    def executescript(self, script: str) -> None:
        for stmt in script.split(";"):
            cleaned = stmt.strip()
            if cleaned and not cleaned.startswith("--"):
                self.execute(cleaned)

    def commit(self) -> None:
        self._raw_conn.commit()

    def rollback(self) -> None:
        try:
            self._raw_conn.rollback()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self._raw_conn.close()
        except Exception:
            pass

    def __enter__(self) -> MySQLConnectionWrapper:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        try:
            if exc_type is not None:
                self.rollback()
            else:
                self.commit()
        finally:
            self.close()


class _Connection(sqlite3.Connection):
    """Custom SQLite connection that ensures clean socket/file closure on context exit."""

    def __exit__(self, *args: Any) -> Any:
        try:
            return super().__exit__(*args)
        finally:
            self.close()


def _init_mysql(conn: Any) -> None:
    """Initialize MySQL 8.0 schema tables and indexes with InnoDB and UTF-8 collation."""
    ddl_statements = [
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INT PRIMARY KEY,
            applied_at VARCHAR(100) NOT NULL
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INT AUTO_INCREMENT PRIMARY KEY,
            source_id VARCHAR(64) NOT NULL,
            started_at VARCHAR(100) NOT NULL,
            completed_at VARCHAR(100),
            status VARCHAR(64) NOT NULL,
            error TEXT
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS card_snapshots (
            id INT AUTO_INCREMENT PRIMARY KEY,
            run_id INT NOT NULL,
            source_id VARCHAR(64) NOT NULL,
            card_id VARCHAR(64) NOT NULL,
            fetched_at VARCHAR(100) NOT NULL,
            content_sha256 VARCHAR(64) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES scrape_runs(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS current_cards (
            card_id VARCHAR(64) PRIMARY KEY,
            snapshot_id INT NOT NULL,
            source_id VARCHAR(64) NOT NULL,
            issuer VARCHAR(255) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            updated_at VARCHAR(100) NOT NULL,
            FOREIGN KEY (snapshot_id) REFERENCES card_snapshots(id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE OR REPLACE VIEW current_card_view AS
        SELECT card_id, source_id, issuer, payload_json, updated_at FROM current_cards
        """,
        """
        CREATE TABLE IF NOT EXISTS banks (
            bank_id VARCHAR(36) PRIMARY KEY,
            name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL UNIQUE
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS sources (
            source_id VARCHAR(36) PRIMARY KEY,
            bank_id VARCHAR(36),
            name VARCHAR(255) NOT NULL,
            domain VARCHAR(255) NOT NULL,
            kind VARCHAR(64) NOT NULL,
            adapter_key VARCHAR(64) NOT NULL,
            enabled TINYINT NOT NULL,
            access_review_status VARCHAR(64) NOT NULL,
            refresh_interval_hours INT NOT NULL,
            last_attempt_at VARCHAR(100),
            last_success_at VARCHAR(100),
            FOREIGN KEY (bank_id) REFERENCES banks(bank_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS source_documents (
            document_id VARCHAR(36) PRIMARY KEY,
            source_id VARCHAR(36) NOT NULL,
            label VARCHAR(255) NOT NULL,
            document_type VARCHAR(64) NOT NULL,
            path_key VARCHAR(255) NOT NULL,
            enabled TINYINT NOT NULL,
            fixture_text LONGTEXT,
            UNIQUE KEY uq_source_doc (source_id, path_key),
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS ingestion_runs (
            run_id VARCHAR(36) PRIMARY KEY,
            source_id VARCHAR(36) NOT NULL,
            scope_hash VARCHAR(64) NOT NULL,
            scope_json LONGTEXT NOT NULL,
            reason VARCHAR(64) NOT NULL,
            status VARCHAR(64) NOT NULL,
            created_at VARCHAR(100) NOT NULL,
            started_at VARCHAR(100),
            finished_at VARCHAR(100),
            attempt INT NOT NULL DEFAULT 0,
            lease_owner VARCHAR(255),
            lease_until VARCHAR(100),
            counters_json LONGTEXT NOT NULL,
            error_summary LONGTEXT,
            FOREIGN KEY (source_id) REFERENCES sources(source_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS run_failures (
            failure_id INT AUTO_INCREMENT PRIMARY KEY,
            run_id VARCHAR(36) NOT NULL,
            document_id VARCHAR(36),
            stage VARCHAR(64) NOT NULL,
            code VARCHAR(64) NOT NULL,
            message LONGTEXT NOT NULL,
            retryable TINYINT NOT NULL,
            FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id),
            FOREIGN KEY (document_id) REFERENCES source_documents(document_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS raw_snapshots (
            snapshot_id VARCHAR(36) PRIMARY KEY,
            document_id VARCHAR(36) NOT NULL,
            run_id VARCHAR(36) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            fetched_at VARCHAR(100) NOT NULL,
            extracted_text LONGTEXT NOT NULL,
            UNIQUE KEY uq_doc_hash (document_id, content_hash),
            FOREIGN KEY (document_id) REFERENCES source_documents(document_id),
            FOREIGN KEY (run_id) REFERENCES ingestion_runs(run_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS extraction_candidates (
            candidate_id VARCHAR(36) PRIMARY KEY,
            scrape_run_id VARCHAR(36) NOT NULL,
            source_id VARCHAR(36) NOT NULL,
            bank_id VARCHAR(36) NOT NULL,
            card_id VARCHAR(64),
            base_card_version_id VARCHAR(36),
            review_status VARCHAR(64) NOT NULL,
            review_revision INT NOT NULL DEFAULT 1,
            created_at VARCHAR(100) NOT NULL,
            updated_at VARCHAR(100) NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            parser_version VARCHAR(64) NOT NULL,
            material_change TINYINT NOT NULL,
            candidate_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_src_hash_parser (source_id, content_hash, parser_version),
            FOREIGN KEY (scrape_run_id) REFERENCES ingestion_runs(run_id),
            FOREIGN KEY (source_id) REFERENCES sources(source_id),
            FOREIGN KEY (bank_id) REFERENCES banks(bank_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS candidate_issues (
            issue_id INT AUTO_INCREMENT PRIMARY KEY,
            candidate_id VARCHAR(36) NOT NULL,
            severity VARCHAR(64) NOT NULL,
            code VARCHAR(64) NOT NULL,
            field_path VARCHAR(255) NOT NULL,
            message LONGTEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES extraction_candidates(candidate_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS candidate_diffs (
            diff_id INT AUTO_INCREMENT PRIMARY KEY,
            candidate_id VARCHAR(36) NOT NULL,
            operation VARCHAR(64) NOT NULL,
            field_path VARCHAR(255) NOT NULL,
            old_value LONGTEXT,
            new_value LONGTEXT,
            material TINYINT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES extraction_candidates(candidate_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS candidate_audit (
            audit_id INT AUTO_INCREMENT PRIMARY KEY,
            candidate_id VARCHAR(36) NOT NULL,
            action VARCHAR(64) NOT NULL,
            actor VARCHAR(255) NOT NULL,
            at VARCHAR(100) NOT NULL,
            payload_json LONGTEXT NOT NULL,
            FOREIGN KEY (candidate_id) REFERENCES extraction_candidates(candidate_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS cards (
            card_id VARCHAR(64) PRIMARY KEY,
            bank_id VARCHAR(36) NOT NULL,
            market VARCHAR(64) NOT NULL,
            product_key VARCHAR(255) NOT NULL,
            name VARCHAR(255) NOT NULL,
            normalized_name VARCHAR(255) NOT NULL,
            UNIQUE KEY uq_bank_mkt_prod (bank_id, market, product_key),
            FOREIGN KEY (bank_id) REFERENCES banks(bank_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS card_versions (
            card_version_id VARCHAR(36) PRIMARY KEY,
            card_id VARCHAR(64) NOT NULL,
            created_at VARCHAR(100) NOT NULL,
            effective_from VARCHAR(100),
            effective_to VARCHAR(100),
            terms_json LONGTEXT NOT NULL,
            summary_json LONGTEXT NOT NULL,
            content_hash VARCHAR(64) NOT NULL,
            immutable TINYINT NOT NULL DEFAULT 1,
            UNIQUE KEY uq_card_hash (card_id, content_hash),
            FOREIGN KEY (card_id) REFERENCES cards(card_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS reward_rules (
            rule_id INT AUTO_INCREMENT PRIMARY KEY,
            card_version_id VARCHAR(36) NOT NULL,
            rule_key VARCHAR(255) NOT NULL,
            kind VARCHAR(64) NOT NULL,
            rate VARCHAR(64) NOT NULL,
            reward_unit VARCHAR(64) NOT NULL,
            stacking_policy VARCHAR(64) NOT NULL,
            period VARCHAR(64) NOT NULL,
            rule_json LONGTEXT NOT NULL,
            UNIQUE KEY uq_ver_rule (card_version_id, rule_key),
            FOREIGN KEY (card_version_id) REFERENCES card_versions(card_version_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS evidence_links (
            evidence_id VARCHAR(36) PRIMARY KEY,
            card_version_id VARCHAR(36) NOT NULL,
            field_path VARCHAR(255) NOT NULL,
            source_url VARCHAR(1000) NOT NULL,
            document_type VARCHAR(64) NOT NULL,
            locator VARCHAR(255) NOT NULL,
            snippet TEXT NOT NULL,
            fetched_at VARCHAR(100) NOT NULL,
            FOREIGN KEY (card_version_id) REFERENCES card_versions(card_version_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS catalogue_publications (
            catalogue_revision INT AUTO_INCREMENT PRIMARY KEY,
            card_id VARCHAR(64) NOT NULL,
            card_version_id VARCHAR(36) NOT NULL,
            published_at VARCHAR(100) NOT NULL,
            superseded_revision INT,
            FOREIGN KEY (card_id) REFERENCES cards(card_id),
            FOREIGN KEY (card_version_id) REFERENCES card_versions(card_version_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS decisions (
            decision_id VARCHAR(36) PRIMARY KEY,
            candidate_id VARCHAR(36) NOT NULL,
            decision VARCHAR(64) NOT NULL,
            reason LONGTEXT NOT NULL,
            actor VARCHAR(255) NOT NULL,
            decided_at VARCHAR(100) NOT NULL,
            publication_revision INT,
            FOREIGN KEY (candidate_id) REFERENCES extraction_candidates(candidate_id)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
        """
        CREATE TABLE IF NOT EXISTS idempotency_records (
            caller VARCHAR(255) NOT NULL,
            endpoint VARCHAR(255) NOT NULL,
            idem_key VARCHAR(255) NOT NULL,
            body_hash VARCHAR(64) NOT NULL,
            status INT NOT NULL,
            headers_json LONGTEXT NOT NULL,
            response_json LONGTEXT NOT NULL,
            created_at VARCHAR(100) NOT NULL,
            expires_at VARCHAR(100) NOT NULL,
            PRIMARY KEY (caller, endpoint, idem_key)
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
        """,
    ]
    for stmt in ddl_statements:
        conn.execute(stmt)

    index_statements = [
        "CREATE INDEX snapshots_card_idx ON card_snapshots(card_id, id)",
        "CREATE INDEX active_run_unique ON ingestion_runs(source_id, scope_hash, status)",
        "CREATE INDEX current_pub_idx ON catalogue_publications(card_id, superseded_revision)",
        "CREATE INDEX source_fresh_idx ON sources(enabled, access_review_status, last_success_at)",
        "CREATE INDEX candidates_queue_idx ON extraction_candidates(review_status, created_at, candidate_id)",
        "CREATE INDEX version_lookup_idx ON card_versions(card_id, created_at)",
        "CREATE INDEX idem_expiry_idx ON idempotency_records(expires_at)",
    ]
    for idx_sql in index_statements:
        try:
            conn.execute(idx_sql)
        except Exception:
            pass


def connect(path: str) -> Any:
    """Connect to database (MySQL if path/DATABASE_URL is MySQL, else SQLite)."""
    if is_mysql(path):
        target = path if (path.startswith("mysql://") or path.startswith("mysql+pymysql://")) else os.environ.get("DATABASE_URL", path)
        if pymysql is None:
            raise RuntimeError("PyMySQL is required for MySQL connections. Run pip install pymysql.")
        cfg = parse_mysql_url(target)
        raw_conn = pymysql.connect(
            host=cfg["host"],
            port=cfg["port"],
            user=cfg["user"],
            password=cfg["password"],
            database=cfg["database"],
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            autocommit=True,
            connect_timeout=10,
        )
        return MySQLConnectionWrapper(raw_conn)

    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, factory=_Connection, timeout=5, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def initialize(path: str) -> None:
    """Initialize relational schema, constraints, and audit tables if not already present."""
    if is_mysql(path):
        with connect(path) as conn:
            _init_mysql(conn)
            conn.execute(
                "INSERT IGNORE INTO schema_migrations VALUES(?, ?)",
                (SCHEMA_VERSION, utc_now()),
            )
        return

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
                "schema_version": "card_terms.v1",
                "currency": fee_curr,
                "annual_fee": fee_amount,
                "annual_fee_status": "known",
                "first_year_waiver": record.get("first_year_waiver", True if fee_amount == "0.00" else False),
                "foreign_currency_fee_rate": record.get("foreign_currency_fee_rate", "0.0325"),
                "minimum_monthly_spend": record.get("minimum_monthly_spend"),
                "cap_groups": record.get("cap_groups", []),
                "excluded_mccs": record.get("excluded_mccs", ["9399", "6540", "6300", "4900"]),
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
            except (sqlite3.IntegrityError, pymysql.IntegrityError if pymysql else sqlite3.IntegrityError):
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


def simulate_card_rewards(card_detail: dict, monthly_spend: list[dict]) -> dict:
    """Simulate rewards earned on a credit card for a monthly transaction basket.

    Enforces Singapore credit card rule mechanics:
    - Minimum monthly spend thresholds (downgrades to base rate if unfulfilled)
    - MCC exclusion lists (e.g. government 9399, e-wallets 6540, utilities 4900)
    - Category bonus matching
    - Cap group limits (monthly or category caps)
    """
    card = card_detail.get("card", {})
    terms = card_detail.get("terms", {})
    rules = terms.get("rules", [])

    total_spend = sum(float(tx["amount"]) for tx in monthly_spend)

    min_spend = None
    if terms.get("minimum_monthly_spend"):
        try:
            min_spend = float(terms["minimum_monthly_spend"])
        except (ValueError, TypeError):
            pass

    excluded_mccs = set(str(mcc) for mcc in terms.get("excluded_mccs", []))

    # Cap groups mapping: cap_key -> max_amount
    cap_limits: dict[str, float] = {}
    for cap in terms.get("cap_groups", []):
        if isinstance(cap, dict) and cap.get("cap_key") and cap.get("amount"):
            try:
                cap_limits[cap["cap_key"]] = float(cap["amount"])
            except (ValueError, TypeError):
                pass

    # Determine base rate
    base_rate = 0.01
    for r in rules:
        if r.get("rule_key") in ("base_rate", "base_cashback") or not r.get("match", {}).get("category_keys"):
            try:
                base_rate = float(r.get("rate", "0.01"))
            except (ValueError, TypeError):
                pass
            break

    min_spend_met = True if min_spend is None else (total_spend >= min_spend)
    cap_accumulated: dict[str, float] = {k: 0.0 for k in cap_limits}

    breakdown = []
    total_reward = 0.0

    for tx in monthly_spend:
        amt = float(tx["amount"])
        mcc = str(tx.get("mcc", "")).strip()
        cat = str(tx.get("category", "general")).lower().strip()
        desc = tx.get("description", f"{cat.title()} spend")

        if mcc and mcc in excluded_mccs:
            breakdown.append({
                "description": desc,
                "amount": f"{amt:.2f}",
                "category": cat,
                "mcc": mcc,
                "rate": "0.0000",
                "reward_earned": "0.00",
                "status": "excluded",
                "reason": f"Excluded by MCC {mcc} (e.g. government/e-wallet/utilities)",
            })
            continue

        matched_rule = None
        for r in rules:
            rule_key = r.get("rule_key", "").lower()
            match_cats = [c.lower() for c in r.get("match", {}).get("category_keys", [])]
            if cat in rule_key or cat in match_cats:
                matched_rule = r
                break

        applied_rate = base_rate
        rule_name = "Base rate"
        cap_key = None

        if matched_rule:
            rule_min = None
            if matched_rule.get("minimum_spend"):
                try:
                    rule_min = float(matched_rule["minimum_spend"])
                except (ValueError, TypeError):
                    pass

            rule_threshold_met = (rule_min is None or total_spend >= rule_min)

            if min_spend_met and rule_threshold_met:
                try:
                    applied_rate = float(matched_rule.get("rate", base_rate))
                    rule_name = f"Bonus '{cat}' rate"
                    caps = matched_rule.get("cap_group_keys", [])
                    if caps:
                        cap_key = caps[0]
                    elif matched_rule.get("cap_group_key"):
                        cap_key = matched_rule["cap_group_key"]
                except (ValueError, TypeError):
                    applied_rate = base_rate
            else:
                threshold_needed = min_spend if min_spend is not None else rule_min
                rule_name = f"Base rate (min spend ${threshold_needed:.2f} not reached)"
        else:
            if not min_spend_met:
                rule_name = f"Base rate (min spend ${min_spend:.2f} not reached)"

        raw_reward = round(amt * applied_rate, 4)
        capped_reward = raw_reward

        if cap_key and cap_key in cap_limits:
            limit = cap_limits[cap_key]
            current = cap_accumulated[cap_key]
            if current >= limit:
                capped_reward = 0.0
                rule_name += f" (Capped: max ${limit:.2f} reached for {cap_key})"
            elif current + raw_reward > limit:
                capped_reward = limit - current
                rule_name += f" (Partially Capped: hit ${limit:.2f} limit for {cap_key})"
                cap_accumulated[cap_key] = limit
            else:
                cap_accumulated[cap_key] += raw_reward

        total_reward += capped_reward
        breakdown.append({
            "description": desc,
            "amount": f"{amt:.2f}",
            "category": cat,
            "mcc": mcc,
            "rate": f"{applied_rate:.4f}",
            "reward_earned": f"{capped_reward:.2f}",
            "status": "applied",
            "reason": rule_name,
        })

    effective_rate = f"{(total_reward / total_spend * 100):.2f}%" if total_spend > 0 else "0.00%"

    return {
        "card_id": card.get("card_id"),
        "card_name": card.get("name", "Unknown Card"),
        "issuer": card.get("bank", {}).get("name") if isinstance(card.get("bank"), dict) else card.get("issuer", "Bank"),
        "reward_type": card.get("reward_type", "cashback"),
        "currency": card.get("currency", "SGD"),
        "total_monthly_spend": f"{total_spend:.2f}",
        "total_reward_earned": f"{total_reward:.2f}",
        "effective_reward_rate": effective_rate,
        "minimum_spend_met": min_spend_met,
        "minimum_monthly_spend": f"{min_spend:.2f}" if min_spend is not None else None,
        "breakdown": breakdown,
    }


def simulate_rewards_all(path: str, card_ids: list[str] | None, monthly_spend: list[dict]) -> dict:
    """Run reward simulation across cards and return ranked recommendations."""
    initialize(path)
    cards_detail: list[dict] = []

    if card_ids:
        for cid in card_ids:
            detail = current_detail(path, cid)
            if detail:
                cards_detail.append(detail)
    else:
        _, published = published_cards(path)
        for c in published:
            detail = current_detail(path, c["card_id"])
            if detail:
                cards_detail.append(detail)

    simulated = [simulate_card_rewards(detail, monthly_spend) for detail in cards_detail]
    simulated.sort(key=lambda x: float(x["total_reward_earned"]), reverse=True)

    for idx, item in enumerate(simulated, 1):
        item["rank"] = idx

    total_spend = sum(float(tx["amount"]) for tx in monthly_spend)
    return {
        "total_monthly_spend": f"{total_spend:.2f}",
        "currency": "SGD",
        "transactions_count": len(monthly_spend),
        "ranked_cards": simulated,
    }

