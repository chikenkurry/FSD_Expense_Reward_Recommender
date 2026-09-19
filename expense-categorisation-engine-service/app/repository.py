import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.models import Category


@dataclass(frozen=True)
class CacheEntry:
    merchant_name: str
    category: Category


class CacheRepository:
    def __init__(self, database_path: str) -> None:
        self.database_path = database_path
        Path(database_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS merchant_category_cache (
                    merchant_name TEXT PRIMARY KEY,
                    assigned_category TEXT NOT NULL,
                    confidence_score TEXT NOT NULL DEFAULT '1.0',
                    source TEXT NOT NULL,
                    override_count INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.database_path)

    def find(self, merchant_name: str) -> CacheEntry | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT merchant_name, assigned_category FROM merchant_category_cache WHERE merchant_name = ?",
                (merchant_name,),
            ).fetchone()
        return CacheEntry(row[0], Category(row[1])) if row else None

    def upsert(self, merchant_name: str, category: Category, override: bool) -> bool:
        now = datetime.now(UTC).isoformat()
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT assigned_category FROM merchant_category_cache WHERE merchant_name = ?",
                (merchant_name,),
            ).fetchone()
            if existing and not override:
                return False
            connection.execute("""
                INSERT INTO merchant_category_cache
                    (merchant_name, assigned_category, source, override_count, created_at, updated_at)
                VALUES (?, ?, 'user_feedback', 0, ?, ?)
                ON CONFLICT(merchant_name) DO UPDATE SET
                    assigned_category = excluded.assigned_category,
                    source = excluded.source,
                    override_count = merchant_category_cache.override_count + 1,
                    updated_at = excluded.updated_at
            """, (merchant_name, category.value, now, now))
        return True
