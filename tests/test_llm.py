import json
import sys
from types import SimpleNamespace

import llm
from llm import draft_vendor_email, fuzzy_name_match
from rules import name_match_check


def test_fuzzy_name_match_exact_result_does_not_call_llm(monkeypatch) -> None:
    def unexpected_call(prompt: str) -> str:
        raise AssertionError("exact name match should not call the LLM")

    monkeypatch.setattr(llm, "call_llm", unexpected_call)
    result = fuzzy_name_match("Acme Pvt Ltd", "ACME PRIVATE LIMITED")
    assert result == {
        "match": True,
        "confidence": 1.0,
        "explanation": "Names match exactly after legal-suffix normalization.",
        "source": "exact",
    }


def test_fuzzy_name_match_parses_valid_llm_json(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "call_llm",
        lambda prompt: json.dumps(
            {
                "match": True,
                "confidence": 0.91,
                "explanation": "The names are a known abbreviation and full name.",
            }
        ),
    )
    result = fuzzy_name_match("International Business Machines", "IBM")
    assert result["match"] is True
    assert result["confidence"] == 0.91
    assert result["source"] == "llm"


def test_fuzzy_name_match_invalid_response_uses_fallback(monkeypatch) -> None:
    monkeypatch.setattr(llm, "call_llm", lambda prompt: "not json")
    result = fuzzy_name_match("Sunrise Traders", "Rajesh Kumar Enterprises")
    assert result["source"] == "fallback"
    assert result["match"] is False


def test_call_llm_returns_none_without_an_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert llm.call_llm("test prompt") is None


def test_settings_read_streamlit_secrets_and_prefer_environment(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "streamlit",
        SimpleNamespace(secrets={"LLM_API_KEY": "cloud-secret"}),
    )
    assert llm._setting("LLM_API_KEY") == "cloud-secret"

    monkeypatch.setenv("LLM_API_KEY", "environment-key")
    assert llm._setting("LLM_API_KEY") == "environment-key"


def test_draft_vendor_email_falls_back_and_lists_all_issues(monkeypatch) -> None:
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    email = draft_vendor_email(
        "Example Vendor",
        "Asha Patel",
        "PENDING",
        ["Please send bank proof.", "Please confirm the tax ID."],
    )
    assert email.startswith("Hello Asha Patel,")
    assert "- Please send bank proof." in email
    assert "- Please confirm the tax ID." in email


def test_name_match_rule_classifies_low_confidence_different_names(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm,
        "fuzzy_name_match",
        lambda company, holder: {
            "match": False,
            "confidence": 0.3,
            "explanation": "Names are very different.",
            "source": "fallback",
        },
    )
    result = name_match_check(
        {"company_name": "Sunrise Traders", "bank_account_holder": "Rajesh Kumar"},
        [],
    )
    assert result["severity"] == "reject"
    assert "bank letter" in result["message"]


def test_name_match_rule_rejects_low_confidence_even_if_llm_says_match(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm,
        "fuzzy_name_match",
        lambda company, holder: {
            "match": True,
            "confidence": 0.4,
            "explanation": "There is not enough evidence.",
            "source": "llm",
        },
    )
    result = name_match_check(
        {"company_name": "Sunrise Traders", "bank_account_holder": "Rajesh Kumar"},
        [],
    )
    assert result["severity"] == "reject"


def test_name_match_rule_does_not_auto_approve_unrelated_llm_names(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm,
        "fuzzy_name_match",
        lambda company, holder: {
            "match": True,
            "confidence": 0.96,
            "explanation": "The model claims these are the same.",
            "source": "llm",
        },
    )
    result = name_match_check(
        {"company_name": "Sunrise Traders", "bank_account_holder": "Rajesh Kumar"},
        [],
    )
    assert result["severity"] == "pending"


def test_llm_name_match_needs_deterministic_support_to_approve(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm,
        "fuzzy_name_match",
        lambda company, holder: {
            "match": True,
            "confidence": 0.96,
            "explanation": "The names refer to the same entity.",
            "source": "llm",
        },
    )
    result = name_match_check(
        {
            "company_name": "Redwood Components",
            "bank_account_holder": "Redwood Component Services",
        },
        [],
    )
    assert result["severity"] == "pending"
