import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet

from config import get_setting

DB_PATH = Path(__file__).with_name("vendor_runs.sqlite3")
_ENCRYPTED_PREFIX = "fernet:v1:"


def _fernet() -> Fernet:
    configured_key = get_setting("DATA_ENCRYPTION_KEY")
    key_path = DB_PATH.parent / ".app-data.key"
    if configured_key:
        key = configured_key.encode("ascii")
    else:
        key_path.parent.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            key_path.write_bytes(Fernet.generate_key())
        key = key_path.read_bytes()
    return Fernet(key)


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                email_draft TEXT,
                submission_json TEXT NOT NULL,
                parent_run_id INTEGER REFERENCES runs(id)
            )
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(runs)")
        }
        if "parent_run_id" not in columns:
            connection.execute(
                "ALTER TABLE runs ADD COLUMN parent_run_id "
                "INTEGER REFERENCES runs(id)"
            )


def save_run(
    vendor_name: str,
    status: str,
    reasons: list[Any],
    email_draft: str | None,
    submission: dict[str, Any],
    parent_run_id: int | None = None,
) -> int:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        cursor = connection.execute(
            """
            INSERT INTO runs (
                timestamp, vendor_name, status, reasons_json,
                email_draft, submission_json, parent_run_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                vendor_name,
                status,
                json.dumps(reasons),
                email_draft,
                _encrypt_submission(submission),
                parent_run_id,
            ),
        )
        return int(cursor.lastrowid)


def list_runs() -> list[dict[str, Any]]:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.row_factory = sqlite3.Row
        rows = connection.execute(
            """
            SELECT id, timestamp, vendor_name, status, reasons_json,
                   email_draft, submission_json, parent_run_id
            FROM runs
            ORDER BY id DESC
            """
        ).fetchall()
        runs = [dict(row) for row in rows]
        for run in runs:
            stored_submission = run["submission_json"]
            if stored_submission.startswith(_ENCRYPTED_PREFIX):
                run["submission_json"] = _fernet().decrypt(
                    stored_submission[len(_ENCRYPTED_PREFIX):].encode("ascii")
                ).decode("utf-8")
            else:
                run["submission_json"] = _encrypt_json(stored_submission)
                connection.execute(
                    "UPDATE runs SET submission_json = ? WHERE id = ?",
                    (run["submission_json"], run["id"]),
                )
                run["submission_json"] = stored_submission
    return runs


def _encrypt_json(serialized_submission: str) -> str:
    token = _fernet().encrypt(serialized_submission.encode("utf-8")).decode("ascii")
    return _ENCRYPTED_PREFIX + token


def _encrypt_submission(submission: dict[str, Any]) -> str:
    return _encrypt_json(json.dumps(submission))


def clear_runs() -> None:
    with closing(sqlite3.connect(DB_PATH)) as connection, connection:
        connection.execute("DELETE FROM runs")
