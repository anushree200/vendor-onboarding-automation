import json

import db


def test_clear_runs_removes_history(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.sqlite3")
    db.init_db()
    db.save_run(
        "Example Vendor",
        "PENDING",
        [{"rule": "required_documents", "severity": "pending"}],
        "Please send bank proof.",
        {"company_name": "Example Vendor"},
    )

    db.clear_runs()

    assert db.list_runs() == []


def test_parent_run_links_resubmission(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(db, "DB_PATH", tmp_path / "runs.sqlite3")
    db.init_db()
    parent_id = db.save_run("Example Vendor", "PENDING", [], None, {})
    child_id = db.save_run(
        "Example Vendor",
        "APPROVED",
        [],
        None,
        {},
        parent_run_id=parent_id,
    )

    runs_by_id = {run["id"]: run for run in db.list_runs()}
    assert runs_by_id[child_id]["parent_run_id"] == parent_id
    assert runs_by_id[parent_id]["parent_run_id"] is None


def test_database_creates_missing_parent_and_encrypts_submission(
    tmp_path, monkeypatch
) -> None:
    import sqlite3

    database_path = tmp_path / "not-created-yet" / "runs.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    submission = {
        "company_name": "Example Vendor",
        "tax_id": "29ABCDE1234F1Z5",
        "bank_account_number": "123456789012",
    }

    db.init_db()
    run_id = db.save_run("Example Vendor", "PENDING", [], None, submission)

    assert database_path.exists()
    assert db.list_runs()[0]["submission_json"]
    with sqlite3.connect(database_path) as connection:
        stored = connection.execute(
            "SELECT submission_json FROM runs WHERE id = ?", (run_id,)
        ).fetchone()[0]
    assert "29ABCDE1234F1Z5" not in stored
    assert "123456789012" not in stored
    assert json.loads(db.list_runs()[0]["submission_json"]) == submission


def test_init_db_migrates_existing_runs_table(tmp_path, monkeypatch) -> None:
    import sqlite3

    database_path = tmp_path / "legacy.sqlite3"
    monkeypatch.setattr(db, "DB_PATH", database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            CREATE TABLE runs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                vendor_name TEXT NOT NULL,
                status TEXT NOT NULL,
                reasons_json TEXT NOT NULL,
                email_draft TEXT,
                submission_json TEXT NOT NULL
            )
            """
        )

    db.init_db()

    with sqlite3.connect(database_path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
    assert "parent_run_id" in columns
