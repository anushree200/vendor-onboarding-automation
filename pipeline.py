import json
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from time import perf_counter
from typing import Any

import db
from llm import draft_vendor_email
from rules import (
    RuleResult,
    bank_account_format,
    bank_country_match,
    completeness,
    duplicate_check,
    email_domain_sanity,
    name_match_check,
    required_documents,
    submission_status,
    tax_id_format,
)


StageEvent = dict[str, Any]

_STAGES = (
    ("Intake & parsing", ()),
    ("Completeness & documents", (completeness, required_documents)),
    ("Format validation", (tax_id_format, bank_account_format)),
    ("Cross-reference", (bank_country_match, name_match_check)),
    ("Risk screening", (duplicate_check, email_domain_sanity)),
)


def _event(
    stage: str,
    status: str,
    results: list[RuleResult],
    started_at: float,
) -> StageEvent:
    return {
        "stage": stage,
        "status": status,
        "results": results,
        "elapsed_ms": int((perf_counter() - started_at) * 1000),
    }


def _decision_summary(status: str, reasons: list[RuleResult]) -> str:
    if status == "APPROVED":
        return "Approved: all onboarding checks passed."

    severity = "reject" if status == "REJECTED" else "pending"
    messages = [
        result["message"].rstrip(".").replace(". ", "; ")
        for result in reasons
        if result["severity"] == severity
    ]
    details = "; ".join(messages)
    if status == "REJECTED":
        return f"Rejected: {details}."
    return f"Pending: {details}."


def run_pipeline(
    submission: dict[str, Any], parent_run_id: int | None = None
) -> Iterator[StageEvent]:
    """Run onboarding checks and yield live progress and the final outcome."""
    if not isinstance(submission, dict):
        raise TypeError("submission must be a JSON object")

    db.init_db()
    master_vendors = _load_master_vendors()
    all_results: list[RuleResult] = []
    intake_started = perf_counter()
    yield _event("Intake & parsing", "running", [], intake_started)
    time.sleep(0.4)
    intake_result: RuleResult = {
        "rule": "intake",
        "passed": True,
        "severity": "ok",
        "message": "Submission JSON object received.",
    }
    all_results.append(intake_result)
    yield _event("Intake & parsing", "done", [intake_result], intake_started)

    for stage_name, stage_rules in _STAGES[1:]:
        stage_started = perf_counter()
        yield _event(stage_name, "running", [], stage_started)
        time.sleep(0.4)
        stage_results = [rule(submission, master_vendors) for rule in stage_rules]
        all_results.extend(stage_results)
        yield _event(stage_name, "done", stage_results, stage_started)

    decision_started = perf_counter()
    yield _event("Decision", "running", [], decision_started)
    time.sleep(0.4)
    failed_results = [
        result for result in all_results if result["severity"] != "ok"
    ]
    persisted_results = [
        result for result in all_results if result["rule"] != "intake"
    ]
    status = submission_status(all_results)
    summary = _decision_summary(status, failed_results)
    final = {
        "status": status,
        "reasons": failed_results,
        "email_draft": None,
        "decision_summary": summary,
    }

    if status == "APPROVED":
        run_id = db.save_run(
            str(submission.get("company_name") or ""),
            status,
            persisted_results,
            None,
            submission,
            parent_run_id,
        )
        final["run_id"] = run_id
        decision_event = _event(
            "Decision", "done", [*failed_results], decision_started
        )
        decision_event["final"] = final
        yield decision_event
        return

    yield _event("Decision", "done", failed_results, decision_started)

    communication_started = perf_counter()
    yield _event("Vendor communication", "running", [], communication_started)
    time.sleep(0.4)
    issues = [result["message"] for result in failed_results]
    final["email_draft"] = draft_vendor_email(
        str(submission.get("company_name") or "your company"),
        str(submission.get("contact_name") or ""),
        status,
        issues,
    )
    run_id = db.save_run(
        str(submission.get("company_name") or ""),
        status,
        persisted_results,
        final["email_draft"],
        submission,
        parent_run_id,
    )
    final["run_id"] = run_id
    communication_result: RuleResult = {
        "rule": "vendor_email_draft",
        "passed": True,
        "severity": "ok",
        "message": "Vendor follow-up email draft prepared.",
    }
    communication_event = _event(
        "Vendor communication", "done", [communication_result], communication_started
    )
    communication_event["final"] = final
    yield communication_event


def _load_master_vendors() -> list[dict[str, Any]]:
    master_path = Path(__file__).parent / "data" / "vendors_master.json"
    if not master_path.exists():
        return []
    with master_path.open(encoding="utf-8") as master_file:
        vendors: Any = json.load(master_file)
    if not isinstance(vendors, list):
        raise ValueError("vendors_master.json must contain a JSON array")
    return vendors


def _run_cli(sample_path: Path) -> None:
    try:
        with sample_path.open(encoding="utf-8") as sample_file:
            submission: Any = json.load(sample_file)
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Could not load submission {sample_path}: {error}") from error
    for event in run_pipeline(submission):
        print(json.dumps(event, ensure_ascii=False))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("Usage: python pipeline.py <submission.json>")
    _run_cli(Path(sys.argv[1]))
