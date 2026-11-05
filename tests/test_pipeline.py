import json
from pathlib import Path

import llm
import pipeline
from resubmission import simulate_vendor_resubmission
from rules import RuleResult


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"


def test_pipeline_emits_ordered_live_events_and_persists_run(
    tmp_path, monkeypatch
) -> None:
    submission = json.loads((SAMPLES / "missing_doc.json").read_text())
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    monkeypatch.setattr(pipeline.db, "DB_PATH", tmp_path / "runs.sqlite3")

    events = list(pipeline.run_pipeline(submission))
    stage_order = [
        "Intake & parsing",
        "Completeness & documents",
        "Format validation",
        "Cross-reference",
        "Risk screening",
        "Decision",
        "Vendor communication",
    ]
    assert [event["stage"] for event in events[::2]] == stage_order
    assert all(
        event["status"] == expected
        for pair in zip(events[::2], events[1::2])
        for event, expected in zip(pair, ("running", "done"))
    )
    assert all(
        isinstance(event["elapsed_ms"], int) and event["elapsed_ms"] >= 0
        for event in events
    )
    final = events[-1]["final"]
    assert final["status"] == "PENDING"
    assert "Missing required documents: bank_proof." in final["decision_summary"]
    assert final["email_draft"]
    assert pipeline.db.list_runs()[0]["status"] == "PENDING"
    saved_results = json.loads(pipeline.db.list_runs()[0]["reasons_json"])
    assert any(result["rule"] == "tax_id_format" for result in saved_results)
    assert any(result["severity"] == "ok" for result in saved_results)


def test_approved_pipeline_skips_vendor_communication(tmp_path, monkeypatch) -> None:
    submission = json.loads((SAMPLES / "happy_path.json").read_text())
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(pipeline.db, "DB_PATH", tmp_path / "runs.sqlite3")

    events = list(pipeline.run_pipeline(submission))
    assert events[-1]["final"]["status"] == "APPROVED"
    assert events[-1]["stage"] == "Decision"
    assert "Vendor communication" not in {event["stage"] for event in events}
    assert pipeline.db.list_runs()[0]["email_draft"] is None


def test_decision_summary_is_one_sentence() -> None:
    reasons: list[RuleResult] = [
        {
            "rule": "name_match_check",
            "passed": False,
            "severity": "reject",
            "message": "Names differ. Please provide a bank letter. Fallback similarity is low.",
        }
    ]
    summary = pipeline._decision_summary("REJECTED", reasons)
    assert summary.count(".") == 1
    assert summary.startswith("Rejected: ")


def test_missing_document_resubmission_is_approved_and_parent_linked(
    tmp_path, monkeypatch
) -> None:
    submission = json.loads((SAMPLES / "missing_doc.json").read_text())
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    monkeypatch.setattr(pipeline.db, "DB_PATH", tmp_path / "runs.sqlite3")

    initial_events = list(pipeline.run_pipeline(submission))
    initial_final = initial_events[-1]["final"]
    corrected, changes = simulate_vendor_resubmission(
        submission, initial_final["reasons"]
    )
    followup_events = list(
        pipeline.run_pipeline(
            corrected, parent_run_id=initial_final["run_id"]
        )
    )

    assert "Added documents: bank_proof." in changes
    assert followup_events[-1]["final"]["status"] == "APPROVED"
    assert followup_events[-1]["final"]["run_id"] != initial_final["run_id"]
    saved_runs = {run["id"]: run for run in pipeline.db.list_runs()}
    assert saved_runs[followup_events[-1]["final"]["run_id"]][
        "parent_run_id"
    ] == initial_final["run_id"]


def test_empty_submission_resubmission_adds_missing_fields(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    monkeypatch.setattr(pipeline.db, "DB_PATH", tmp_path / "runs.sqlite3")

    initial_events = list(pipeline.run_pipeline({}))
    initial_final = initial_events[-1]["final"]
    corrected, changes = simulate_vendor_resubmission(
        {}, initial_final["reasons"]
    )
    followup_events = list(
        pipeline.run_pipeline(
            corrected, parent_run_id=initial_final["run_id"]
        )
    )

    assert any("company_name" in change for change in changes)
    assert followup_events[-1]["final"]["status"] == "APPROVED"


def test_pipeline_handles_untrusted_json_shapes(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(pipeline.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    monkeypatch.setattr(pipeline.db, "DB_PATH", tmp_path / "runs.sqlite3")
    submissions = [
        {},
        {"country": [], "documents": [{"unexpected": "type"}]},
        {"country": None, "company_name": None, "tax_id": None},
        {"company_name": "A" * 20_000, "bank_account_holder": "B" * 20_000},
        {"company_name": "Example", "unknown": "extra"},
    ]

    for submission in submissions:
        events = list(pipeline.run_pipeline(submission))
        assert events[-1]["final"]["status"] in {
            "APPROVED",
            "PENDING",
            "REJECTED",
        }


def test_missing_data_directory_does_not_break_pipeline(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(pipeline, "__file__", str(tmp_path / "project" / "pipeline.py"))
    assert pipeline._load_master_vendors() == []
