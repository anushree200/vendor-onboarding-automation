import json
import sys
from types import SimpleNamespace

import llm
import pytest
from llm import draft_vendor_email, fuzzy_name_match
from rules import name_match_check


@pytest.fixture(autouse=True)
def reset_llm_circuit_breaker(monkeypatch):
    monkeypatch.setattr(llm, "_circuit_open_until", 0.0)


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
        lambda prompt, **kwargs: json.dumps(
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


def test_fuzzy_name_match_parses_fenced_json_and_percentage_confidence(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        llm,
        "call_llm",
        lambda prompt, **kwargs: (
            '```json\n{"match": false, "confidence": "41", '
            '"explanation": "The names appear unrelated."}\n```'
        ),
    )
    result = fuzzy_name_match("Sunrise Traders", "Rajesh Kumar Enterprises")
    assert result == {
        "match": False,
        "confidence": 0.41,
        "explanation": "The names appear unrelated.",
        "source": "llm",
    }


def test_fuzzy_name_match_parses_fractional_string_confidence(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "call_llm",
        lambda prompt, **kwargs: json.dumps(
            {
                "match": True,
                "confidence": "0.91",
                "explanation": "The names describe the same entity.",
            }
        ),
    )
    result = fuzzy_name_match("International Business Machines", "IBM")
    assert result["confidence"] == 0.91
    assert result["source"] == "llm"


def test_fuzzy_name_match_invalid_response_uses_fallback(monkeypatch) -> None:
    monkeypatch.setattr(llm, "call_llm", lambda prompt, **kwargs: "not json")
    result = fuzzy_name_match("Sunrise Traders", "Rajesh Kumar Enterprises")
    assert result["source"] == "fallback"
    assert result["match"] is False


@pytest.mark.parametrize(
    ("response", "reason"),
    [
        ('{"match": true, "confidence": "unclear", "explanation": "x"}', "not numeric"),
        ('{"match": true, "confidence": 0.8}', "explanation"),
        ('{"match": true, "confidence": 140, "explanation": "x"}', "outside"),
    ],
)
def test_fuzzy_name_match_logs_invalid_response_reason(
    monkeypatch, caplog, response, reason
) -> None:
    monkeypatch.setattr(
        llm, "call_llm", lambda prompt, **kwargs: response
    )
    result = fuzzy_name_match("Sunrise Traders", "Rajesh Kumar Enterprises")
    assert result["source"] == "fallback"
    assert reason in caplog.text


def test_call_llm_returns_none_without_an_api_key(monkeypatch) -> None:
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    assert llm.call_llm("test prompt") is None


def test_call_llm_configures_gemini_timeout_and_single_attempt(monkeypatch) -> None:
    import google.genai

    client_options = {}
    generation_options = {}

    class FakeModels:
        def generate_content(self, **kwargs):
            generation_options.update(kwargs)
            return SimpleNamespace(text="ok")

    class FakeClient:
        def __init__(self, **kwargs):
            client_options.update(kwargs)
            self.models = FakeModels()

    monkeypatch.setattr(
        llm,
        "_setting",
        lambda name, default=None: {
            "LLM_API_KEY": "private-test-key",
            "LLM_PROVIDER": "gemini",
            "LLM_MODEL": "test-model",
        }.get(name, default),
    )
    monkeypatch.setattr(google.genai, "Client", FakeClient)

    assert llm.call_llm("test prompt", json_response=True) == "ok"
    http_options = client_options["http_options"]
    assert http_options.timeout == 10_000
    assert http_options.retry_options.attempts == 1
    config = generation_options["config"]
    assert config.response_mime_type == "application/json"
    assert config.response_schema.required == ["match", "confidence", "explanation"]


def test_call_llm_disabled_skips_provider(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "_setting",
        lambda name, default=None: {
            "LLM_ENABLED": "false",
            "LLM_API_KEY": "private-test-key",
            "LLM_PROVIDER": "gemini",
        }.get(name, default),
    )

    def unexpected_client(**kwargs):
        raise AssertionError("disabled LLM must not create a provider client")

    import google.genai

    monkeypatch.setattr(google.genai, "Client", unexpected_client)
    assert llm.call_llm("test prompt") is None


def test_call_llm_circuit_breaker_skips_calls_for_120_seconds(monkeypatch) -> None:
    import google.genai

    now = [100.0]
    request_count = [0]
    monkeypatch.setattr(llm.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(
        llm,
        "_setting",
        lambda name, default=None: {
            "LLM_ENABLED": "true",
            "LLM_API_KEY": "private-test-key",
            "LLM_PROVIDER": "gemini",
            "LLM_MODEL": "test-model",
        }.get(name, default),
    )

    class BrokenModels:
        def generate_content(self, **kwargs):
            request_count[0] += 1
            raise RuntimeError("temporary provider failure")

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = BrokenModels()

    monkeypatch.setattr(google.genai, "Client", FakeClient)

    assert llm.call_llm("first request") is None
    assert request_count[0] == 1
    assert llm.call_llm("during outage") is None
    assert request_count[0] == 1

    now[0] += 120
    assert llm.call_llm("after cooldown") is None
    assert request_count[0] == 2


def test_call_llm_logs_provider_exception_without_exposing_key(
    monkeypatch, caplog
) -> None:
    import google.genai

    api_key = "private-test-key"

    class BrokenModels:
        def generate_content(self, **kwargs):
            raise RuntimeError(f"request failed with {api_key}")

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = BrokenModels()

    monkeypatch.setattr(llm, "_setting", lambda name, default=None: {
        "LLM_API_KEY": api_key,
        "LLM_PROVIDER": "gemini",
        "LLM_MODEL": "test-model",
    }.get(name, default))
    monkeypatch.setattr(google.genai, "Client", FakeClient)

    assert llm.call_llm("test prompt") is None
    assert "RuntimeError" in caplog.text
    assert "request failed" in caplog.text
    assert api_key not in caplog.text


def test_gemini_404_logs_first_ten_models_without_exposing_key(
    monkeypatch, caplog
) -> None:
    import google.genai

    api_key = "private-test-key"
    model_names = [f"models/test-model-{index}" for index in range(12)]
    requested_models = []

    class NotFoundError(Exception):
        code = 404

    class BrokenModels:
        def generate_content(self, **kwargs):
            requested_models.append(kwargs["model"])
            raise NotFoundError("404 NOT_FOUND: stale model")

        def list(self):
            return [SimpleNamespace(name=name) for name in model_names]

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = BrokenModels()

    monkeypatch.setattr(
        llm,
        "_setting",
        lambda name, default=None: {
            "LLM_API_KEY": api_key,
            "LLM_PROVIDER": "gemini",
            "LLM_MODEL": "stale-model",
        }.get(name, default),
    )
    monkeypatch.setattr(google.genai, "Client", FakeClient)

    assert llm.call_llm("test prompt") is None
    assert requested_models == ["stale-model"]
    assert "NotFoundError" in caplog.text
    assert "First available Gemini model names" in caplog.text
    assert model_names[:10][0] in caplog.text
    assert model_names[:10][-1] in caplog.text
    assert model_names[10] not in caplog.text
    assert api_key not in caplog.text


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
    assert "To continue, please provide:" in email
    assert email.endswith("Regards,\nProcurement Team")
    assert "[Your Name]" not in email


def test_rejected_email_explains_application_cannot_proceed(monkeypatch) -> None:
    monkeypatch.setattr(llm, "call_llm", lambda prompt: None)
    email = draft_vendor_email(
        "Example Vendor",
        "Asha Patel",
        "REJECTED",
        ["Please correct the tax ID."],
    )
    assert "We can't proceed with the application as submitted." in email
    assert "You may resubmit it with corrected details." in email
    assert "To continue, please provide" not in email
    assert "- Please correct the tax ID." in email
    assert email.endswith("Regards,\nProcurement Team")
    assert "[Your Name]" not in email


def test_llm_rejected_email_prompt_requires_rejection_wording(monkeypatch) -> None:
    prompts = []

    def fake_call_llm(prompt: str) -> str:
        prompts.append(prompt)
        return "Rejected email"

    monkeypatch.setattr(llm, "call_llm", fake_call_llm)
    email = draft_vendor_email(
        "Example Vendor", "Asha Patel", "REJECTED", ["Fix the tax ID."]
    )
    assert email.startswith("Rejected email")
    assert email.endswith("Regards,\nProcurement Team")
    assert "[Your Name]" not in email
    assert "cannot proceed as submitted" in prompts[0]
    assert "do not say 'To continue, please provide'" in prompts[0]
    assert "signed exactly 'Procurement Team'" in prompts[0]


def test_llm_email_signature_replaces_name_placeholder(monkeypatch) -> None:
    monkeypatch.setattr(
        llm,
        "call_llm",
        lambda prompt: "Hello,\n\nDetails here.\n\nRegards,\n[Your Name]",
    )
    email = draft_vendor_email("Example Vendor", "Asha Patel", "PENDING", ["Item"])
    assert email.endswith("Regards,\nProcurement Team")
    assert "[Your Name]" not in email


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
