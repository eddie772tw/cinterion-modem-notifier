"""Durable local SMS inbox; this module never deletes modem SMS."""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class SmsInbox:
    """Archive complete SMS payloads locally before any future cleanup policy."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._initialise()
        self.path.chmod(0o600)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA busy_timeout=5000")
        return connection

    def _initialise(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS sms_messages (
                    identity TEXT PRIMARY KEY,
                    modem_path TEXT NOT NULL,
                    sender TEXT NOT NULL,
                    timestamp TEXT NOT NULL,
                    storage TEXT NOT NULL,
                    text TEXT NOT NULL,
                    data TEXT NOT NULL,
                    archived_at REAL NOT NULL,
                    notified_at REAL
                )
                """
            )
            connection.execute("CREATE INDEX IF NOT EXISTS sms_messages_path ON sms_messages(modem_path)")
            connection.execute("CREATE INDEX IF NOT EXISTS sms_messages_archived ON sms_messages(archived_at DESC)")
        self.path.chmod(0o600)

    def archive(
        self,
        identity: str,
        modem_path: str,
        fields: dict[str, str],
        already_notified: bool = False,
    ) -> None:
        now = time.time()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO sms_messages
                    (identity, modem_path, sender, timestamp, storage, text, data, archived_at, notified_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(identity) DO UPDATE SET
                    modem_path=excluded.modem_path,
                    sender=excluded.sender,
                    timestamp=excluded.timestamp,
                    storage=excluded.storage,
                    text=excluded.text,
                    data=excluded.data,
                    notified_at=COALESCE(sms_messages.notified_at, excluded.notified_at)
                """,
                (
                    identity,
                    modem_path,
                    fields.get("from", "--"),
                    fields.get("timestamp", "--"),
                    fields.get("storage", "--"),
                    fields.get("text", "--"),
                    fields.get("data", ""),
                    now,
                    now if already_notified else None,
                ),
            )

    def mark_notified(self, identity: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "UPDATE sms_messages SET notified_at=COALESCE(notified_at, ?) WHERE identity=?",
                (time.time(), identity),
            )

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT identity, modem_path, sender, timestamp, storage, text, data,
                       archived_at, notified_at
                FROM sms_messages ORDER BY archived_at DESC LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [self._row(row) for row in rows]

    def cleanup_candidates(self, current_paths: set[str], limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        if not current_paths:
            return []
        placeholders = ",".join("?" for _ in current_paths)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT identity, modem_path, sender, timestamp, storage, text, data,
                       archived_at, notified_at
                FROM sms_messages
                WHERE notified_at IS NOT NULL AND modem_path IN ({placeholders})
                ORDER BY archived_at ASC LIMIT ?
                """,
                [*sorted(current_paths), limit],
            ).fetchall()
        return [self._row(row) for row in rows]

    @staticmethod
    def _row(row: tuple[Any, ...]) -> dict[str, Any]:
        identity, modem_path, sender, timestamp, storage, text, data, archived_at, notified_at = row
        return {
            "identity": identity,
            "modem_path": modem_path,
            "from": sender,
            "timestamp": timestamp,
            "storage": storage,
            "text": text,
            "data": data,
            "archived_at": archived_at,
            "notified_at": notified_at,
        }
