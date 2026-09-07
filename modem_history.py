"""Private local event history for the modem notifier."""
from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any


class EventHistory:
    """Store the latest observation for each event fingerprint locally."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.exists():
            self.path.chmod(0o600)
        self._initialise()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    fingerprint TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    first_seen REAL NOT NULL,
                    last_seen REAL NOT NULL,
                    observations INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS events_last_seen ON events(last_seen DESC)")
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS metrics (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at REAL NOT NULL,
                    name TEXT NOT NULL,
                    value REAL NOT NULL,
                    unit TEXT NOT NULL,
                    dimensions TEXT NOT NULL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS metrics_name_time ON metrics(name, observed_at DESC)")
        self.path.chmod(0o600)

    def record_metric(
        self,
        name: str,
        value: float,
        unit: str = "",
        dimensions: dict[str, Any] | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO metrics (observed_at, name, value, unit, dimensions) VALUES (?, ?, ?, ?, ?)",
                (time.time(), name, float(value), unit, json.dumps(dimensions or {}, sort_keys=True)),
            )

    def recent_metrics(self, limit: int = 100, name: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        query = "SELECT observed_at, name, value, unit, dimensions FROM metrics"
        parameters: list[Any] = []
        if name:
            query += " WHERE name = ?"
            parameters.append(name)
        query += " ORDER BY observed_at DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for observed_at, metric_name, value, unit, dimensions in rows:
            try:
                parsed_dimensions = json.loads(dimensions)
            except json.JSONDecodeError:
                parsed_dimensions = {"error": "corrupt metric dimensions"}
            result.append(
                {
                    "observed_at": observed_at,
                    "name": metric_name,
                    "value": value,
                    "unit": unit,
                    "dimensions": parsed_dimensions,
                }
            )
        return result

    def observe(self, event: Any) -> None:
        fingerprint = event.fingerprint()
        payload = json.dumps(event.fields, ensure_ascii=False, sort_keys=True)
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO events (fingerprint, kind, title, payload, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(fingerprint) DO UPDATE SET
                    payload=excluded.payload,
                    last_seen=excluded.last_seen,
                    observations=events.observations + 1
                """,
                (fingerprint, event.kind, event.title, payload, now, now),
            )

    def recent(self, limit: int = 20, kind: str | None = None) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        query = "SELECT fingerprint, kind, title, payload, first_seen, last_seen, observations FROM events"
        parameters: list[Any] = []
        if kind:
            query += " WHERE kind LIKE ?"
            parameters.append(f"{kind}%")
        query += " ORDER BY last_seen DESC LIMIT ?"
        parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        result = []
        for fingerprint, event_kind, title, payload, first_seen, last_seen, observations in rows:
            try:
                fields = json.loads(payload)
            except json.JSONDecodeError:
                fields = {"error": "corrupt event payload"}
            result.append(
                {
                    "fingerprint": fingerprint,
                    "kind": event_kind,
                    "title": title,
                    "fields": fields,
                    "first_seen": first_seen,
                    "last_seen": last_seen,
                    "observations": observations,
                }
            )
        return result
