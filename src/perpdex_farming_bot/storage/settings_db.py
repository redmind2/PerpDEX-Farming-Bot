from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from perpdex_farming_bot.security.secrets import assert_no_plaintext_secrets


DEFAULT_SETTINGS_DB = "data/bot_settings.sqlite"


@dataclass(frozen=True)
class SettingRow:
    namespace: str
    key: str
    value_json: str
    updated_at_utc: str


class SettingsDB:
    """Local SQLite store for non-secret bot settings.

    This intentionally stores JSON-encoded values while the project migrates away
    from repo JSON config files. Actual secrets still belong only in `.env`.
    """

    def __init__(self, path: str | Path = DEFAULT_SETTINGS_DB) -> None:
        self.path = Path(path)

    def init(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS settings (
                    namespace TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value_json TEXT NOT NULL,
                    updated_at_utc TEXT NOT NULL,
                    PRIMARY KEY (namespace, key)
                );

                CREATE TABLE IF NOT EXISTS setting_imports (
                    import_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_path TEXT NOT NULL,
                    namespace TEXT NOT NULL,
                    imported_at_utc TEXT NOT NULL
                );
                """
            )

    def set_value(self, namespace: str, key: str, value: Any) -> None:
        assert_no_plaintext_secrets(value)
        now = datetime.now(timezone.utc).isoformat()
        value_json = json.dumps(value, ensure_ascii=False, sort_keys=True)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO settings (namespace, key, value_json, updated_at_utc)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(namespace, key) DO UPDATE SET
                    value_json = excluded.value_json,
                    updated_at_utc = excluded.updated_at_utc
                """,
                (namespace, key, value_json, now),
            )

    def get_value(self, namespace: str, key: str, default: Any = None) -> Any:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT value_json
                FROM settings
                WHERE namespace = ? AND key = ?
                """,
                (namespace, key),
            ).fetchone()
        if row is None:
            return default
        return json.loads(str(row["value_json"]))

    def list_namespace(self, namespace: str) -> list[SettingRow]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT namespace, key, value_json, updated_at_utc
                FROM settings
                WHERE namespace = ?
                ORDER BY key
                """,
                (namespace,),
            ).fetchall()
        return [
            SettingRow(
                namespace=str(row["namespace"]),
                key=str(row["key"]),
                value_json=str(row["value_json"]),
                updated_at_utc=str(row["updated_at_utc"]),
            )
            for row in rows
        ]

    def import_json_file(self, path: str | Path, namespace: str) -> int:
        source_path = Path(path)
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        assert_no_plaintext_secrets(payload)
        imported = 0
        if isinstance(payload, dict):
            for key, value in payload.items():
                self.set_value(namespace, str(key), value)
                imported += 1
        else:
            self.set_value(namespace, "payload", payload)
            imported = 1

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO setting_imports (source_path, namespace, imported_at_utc)
                VALUES (?, ?, ?)
                """,
                (str(source_path), namespace, now),
            )
        return imported

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection
