import json
from pathlib import Path

import pytest

from rules import (
    bank_account_format,
    duplicate_check,
    email_domain_sanity,
    evaluate_submission,
    name_match_check,
    normalize_company_name,
    submission_status,
    tax_id_format,
)


ROOT = Path(__file__).resolve().parents[1]
SAMPLES = ROOT / "data" / "samples"
MASTER_VENDORS = json.loads(
    (ROOT / "data" / "vendors_master.json").read_text(encoding="utf-8")
)


@pytest.mark.parametrize(
    ("sample_name", "expected_status"),
    [
        ("happy_path.json", "APPROVED"),
        ("name_suffix_ok.json", "APPROVED"),
        ("wrong_tax_format.json", "REJECTED"),
        ("duplicate_bank.json", "REJECTED"),
        ("missing_doc.json", "PENDING"),
        ("name_mismatch.json", "REJECTED"),
    ],
)
def test_sample_submission_statuses(sample_name: str, expected_status: str) -> None:
    submission = json.loads((SAMPLES / sample_name).read_text(encoding="utf-8"))
    findings = evaluate_submission(submission, MASTER_VENDORS)
    assert submission_status(findings) == expected_status


def test_name_suffixes_normalize_to_the_same_company() -> None:
    assert normalize_company_name("Acme Pvt Ltd") == normalize_company_name(
        "ACME PRIVATE LIMITED"
    )
    assert normalize_company_name("Northstar, Inc.") == normalize_company_name(
        "Northstar Incorporated"
    )


def test_wrong_tax_id_reports_matching_foreign_format() -> None:
    submission = json.loads(
        (SAMPLES / "wrong_tax_format.json").read_text(encoding="utf-8")
    )
    result = tax_id_format(submission, MASTER_VENDORS)
    assert result["severity"] == "reject"
    assert "US EIN" in result["message"]


def test_duplicate_is_only_flagged_for_a_different_company_name() -> None:
    submission = {
        "company_name": "Northstar Office Supplies Pvt Ltd",
        "tax_id": "29AABCN1234E1Z6",
        "bank_account_number": "918273645001",
    }
    assert duplicate_check(submission, MASTER_VENDORS)["passed"]


def test_empty_vendor_master_does_not_claim_duplicate_screening_passed() -> None:
    result = duplicate_check(
        {"company_name": "A Company", "tax_id": "29ABCDE1234F1Z5"}, []
    )
    assert result["severity"] == "pending"
    assert "master is empty or unavailable" in result["message"]


@pytest.mark.parametrize(
    ("country", "account_number", "expected_pass"),
    [
        ("GB", "GB29NWBK60161331926819", True),
        ("GB", "DE89370400440532013000", False),
        ("DE", "DE89370400440532013000", True),
        ("IN", "1234567890", True),
        ("US", "12-3456789", False),
    ],
)
def test_bank_account_formats(
    country: str, account_number: str, expected_pass: bool
) -> None:
    submission = {"country": country, "bank_account_number": account_number}
    assert bank_account_format(submission, MASTER_VENDORS)["passed"] is expected_pass


def test_free_email_domain_requires_review() -> None:
    result = email_domain_sanity(
        {"contact_email": "vendor@gmail.com"}, MASTER_VENDORS
    )
    assert result["severity"] == "pending"


@pytest.mark.parametrize(
    "submission",
    [
        {},
        {"country": "IN"},
        {"country": None, "documents": None},
        {"country": [], "documents": [{"unexpected": "type"}]},
        {"country": 17, "tax_id": None},
        {"company_name": "A" * 20_000, "bank_account_holder": "B" * 20_000},
        {
            "company_name": "Ünïcode Traders GmbH",
            "bank_account_holder": "ÜNÏCODE TRADERS GMBH",
            "country": "DE",
            "bank_country": "de",
            "tax_id": " de987654321 ",
            "bank_account_number": "de00000000000000000000",
            "ifsc_or_routing": "DEMOXXXXXXX",
            "registered_address": "Unicode Way",
            "contact_name": "Ünïcode",
            "contact_email": "contact@unicode.example",
            "bank_name": "Unicode Bank",
            "documents": ["incorporation_cert", "tax_registration", "bank_proof"],
        },
        {
            "company_name": "Acme Ltd",
            "country": "in",
            "tax_id": "29abcde1234f1z5 ",
            "registered_address": "Road",
            "contact_name": "Contact",
            "contact_email": "a@acme.example",
            "bank_account_holder": "ACME PRIVATE LIMITED",
            "bank_name": "Bank",
            "bank_account_number": "123 456 7890",
            "bank_country": " in ",
            "ifsc_or_routing": "DEMO0001234",
            "documents": ["incorporation_cert", "tax_registration", "bank_proof"],
        },
        {
            "company_name": "Acme Ltd",
            "country": "IN",
            "tax_id": "29ABCDE1234F1Z5",
            "registered_address": "Road",
            "contact_name": "Contact",
            "contact_email": "a@acme.example",
            "bank_account_holder": "Acme Ltd",
            "bank_name": "Bank",
            "bank_account_number": "1234567890",
            "bank_country": "IN",
            "ifsc_or_routing": "DEMO0001234",
            "documents": ["incorporation_cert", "tax_registration", "bank_proof"],
            "unknown_field": "ignored is not safe",
        },
    ],
)
def test_untrusted_submission_shapes_never_crash(
    submission: dict[str, object],
) -> None:
    findings = evaluate_submission(submission, MASTER_VENDORS)
    assert len(findings) == 8
    assert submission_status(findings) in {"APPROVED", "PENDING", "REJECTED"}


def test_names_normalize_unicode_letters_and_spacing() -> None:
    assert normalize_company_name("Ünïcode   Traders GmbH") == normalize_company_name(
        "ÜNÏCODE Traders GmbH"
    )


def test_rules_work_when_data_directory_is_missing(tmp_path, monkeypatch) -> None:
    import rules

    monkeypatch.setattr(rules, "__file__", str(tmp_path / "project" / "rules.py"))
    findings = rules.evaluate_submission({})
    assert findings
    duplicate = next(item for item in findings if item["rule"] == "duplicate_check")
    assert duplicate["severity"] == "pending"


def test_case_and_whitespace_are_normalized_for_tax_and_bank_rules() -> None:
    submission = {
        "country": " in ",
        "tax_id": "29abcde1234f1z5 ",
        "bank_account_number": "123 456 7890",
    }
    assert tax_id_format(submission, MASTER_VENDORS)["passed"]
    assert bank_account_format(submission, MASTER_VENDORS)["passed"]


def test_unknown_fields_are_reported_as_pending() -> None:
    result = next(
        finding
        for finding in evaluate_submission(
            {"unexpected": True}, MASTER_VENDORS
        )
        if finding["rule"] == "completeness"
    )
    assert result["severity"] == "pending"
    assert "unexpected fields" in result["message"]


def test_missing_names_are_not_a_successful_name_match() -> None:
    result = name_match_check(
        {"company_name": None, "bank_account_holder": None}, []
    )
    assert not result["passed"]
    assert result["severity"] == "pending"


def test_rules_collect_multiple_failures_and_rejection_takes_precedence() -> None:
    findings = evaluate_submission(
        {
            "company_name": "Different Company",
            "country": "IN",
            "tax_id": "12-3456789",
            "registered_address": "Address",
            "contact_name": "Contact",
            "contact_email": "person@gmail.com",
            "bank_account_holder": "Unrelated Person",
            "bank_name": "Bank",
            "bank_account_number": "invalid",
            "bank_country": "US",
            "ifsc_or_routing": "routing",
            "documents": [],
        },
        MASTER_VENDORS,
    )
    failed_rules = {finding["rule"] for finding in findings if not finding["passed"]}
    assert {
        "required_documents",
        "tax_id_format",
        "bank_country_match",
        "bank_account_format",
        "email_domain_sanity",
        "name_match_check",
    } <= failed_rules
    assert submission_status(findings) == "REJECTED"
