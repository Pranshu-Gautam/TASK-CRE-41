"""Durable response cache.

Two purposes: it keeps repeat runs off the public endpoints (a hard
requirement of the Nominatim usage policy) and it makes a re-run of the
pipeline deterministic and auditable.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any


def cache_key(namespace: str, payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{namespace}|{blob}".encode("utf-8")).hexdigest()


class SqliteCache:
    """Minimal key/value cache. Safe to use from a single process."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path))
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS response_cache (
                key         TEXT PRIMARY KEY,
                namespace   TEXT NOT NULL,
                request     TEXT NOT NULL,
                response    TEXT NOT NULL,
                fetched_at  TEXT NOT NULL
            )
            """
        )
        self._conn.commit()
        self.hits = 0
        self.misses = 0
        self.writes = 0

    def get(self, key: str) -> Any | None:
        row = self._conn.execute(
            "SELECT response FROM response_cache WHERE key = ?", (key,)
        ).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(row[0])

    def put(self, key: str, namespace: str, request: Any, response: Any) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO response_cache "
            "(key, namespace, request, response, fetched_at) VALUES (?, ?, ?, ?, ?)",
            (
                key,
                namespace,
                json.dumps(request, sort_keys=True, ensure_ascii=False, default=str),
                json.dumps(response, ensure_ascii=False, default=str),
                dt.datetime.now(dt.timezone.utc).isoformat(),
            ),
        )
        self._conn.commit()
        self.writes += 1

    def stats(self) -> dict[str, int]:
        return {"hits": self.hits, "misses": self.misses, "writes": self.writes}

    def close(self) -> None:
        self._conn.close()
