import json
import re
import unicodedata
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Literal, TypedDict


Severity = Literal["ok", "pending", "reject"]


class RuleResult(TypedDict):
    rule: str
    passed: bool
    severity: Severity
    message: str


REQUIRED_FIELDS = (
    "company_name",
    "country",
    "tax_id",
    "registered_address",
    "contact_name",
    "contact_email",
    "bank_account_holder",
    "bank_name",
    "bank_account_number",
    "bank_country",
    "ifsc_or_routing",
    "documents",
)

REQUIRED_DOCUMENTS = {
    "IN": {"incorporation_cert", "tax_registration", "bank_proof"},
    "US": {"w9_or_w8", "bank_proof"},
    "GB": {"incorporation_cert", "tax_registration", "bank_proof"},
    "DE": {"incorporation_cert", "tax_registration", "bank_proof"},
}

TAX_ID_PATTERNS = {
    "IN": re.compile(r"[0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][1-9A-Z]Z[0-9A-Z]"),
    "US": re.compile(r"[0-9]{2}-[0-9]{7}"),
    "GB": re.compile(r"GB[0-9]{9}"),
    "DE": re.compile(r"DE[0-9]{9}"),
}
COUNTRY_NAMES = {
    "IN": "India GSTIN",
    "US": "US EIN",
    "GB": "UK VAT",
    "DE": "German VAT",
}

FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "yahoo.com",
    "yahoo.co.uk",
    "outlook.com",
    "outlook.co.uk",
}
MAX_TEXT_LENGTH = 10_000
MAX_NAME_MATCH_LENGTH = 1_000


def _result(
    rule: str, passed: bool, severity: Severity, message: str
) -> RuleResult:
    return {
        "rule": rule,
        "passed": passed,
        "severity": severity,
        "message": message,
    }


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _country(submission: dict[str, Any], field: str = "country") -> str | None:
    country = _text(submission.get(field)).upper()
    return country or None


def _nonempty(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    return value is not None


def _normalized_identifier(value: Any) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(value or "").upper())


def normalize_company_name(name: str) -> str:
    """Normalize punctuation, case, and common legal suffix variants."""
    normalized_name = unicodedata.normalize("NFKC", name).casefold()
    normalized = "".join(
        character if character.isalnum() else " "
        for character in normalized_name
    )
    normalized = re.sub(r"\s+", " ", normalized).strip()
    suffixes = (
        (r"(?:pvt|private)\s+(?:ltd|limited)$", "private limited"),
        (r"(?:inc|incorporated)$", "incorporated"),
        (r"(?:ltd|limited)$", "limited"),
        (r"llc$", "llc"),
        (r"gmbh$", "gmbh"),
    )
    for pattern, replacement in suffixes:
        normalized, count = re.subn(pattern, replacement, normalized)
        if count:
            break
    return normalized


def completeness(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    string_fields = set(REQUIRED_FIELDS) - {"documents"}
    missing = [
        field for field in REQUIRED_FIELDS
        if field not in submission
        or (
            field in string_fields
            and isinstance(submission.get(field), str)
            and not submission[field].strip()
        )
        or (
            field == "documents"
            and isinstance(submission.get(field), list)
            and not submission[field]
        )
    ]
    invalid_types = [
        field
        for field in string_fields
        if field in submission and not isinstance(submission[field], str)
    ]
    if "documents" in submission and not isinstance(submission["documents"], list):
        invalid_types.append("documents")
    too_long = [
        field
        for field in string_fields
        if isinstance(submission.get(field), str)
        and len(submission[field]) > MAX_TEXT_LENGTH
    ]
    unknown = sorted(set(submission) - set(REQUIRED_FIELDS))
    problems = []
    if missing:
        problems.append(f"missing fields: {', '.join(missing)}")
    if invalid_types:
        problems.append(
            "fields with invalid types (expected text"
            + (", except documents which must be a list" if "documents" in invalid_types else "")
            + f"): {', '.join(sorted(invalid_types))}"
        )
    if too_long:
        problems.append(
            f"fields exceeding {MAX_TEXT_LENGTH} characters: {', '.join(too_long)}"
        )
    if unknown:
        problems.append(f"unexpected fields: {', '.join(unknown)}")
    if problems:
        return _result(
            "completeness",
            False,
            "pending",
            "Submission needs attention: " + "; ".join(problems) + ".",
        )
    return _result("completeness", True, "ok", "All required fields are present.")


def required_documents(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    country = _country(submission)
    required = REQUIRED_DOCUMENTS.get(country)
    if required is None:
        return _result(
            "required_documents",
            False,
            "pending",
            f"Cannot determine required documents for country {country!r}.",
        )

    documents = submission.get("documents")
    supplied = (
        {document.strip().casefold() for document in documents if isinstance(document, str)}
        if isinstance(documents, list)
        else set()
    )
    missing = sorted(required - supplied)
    if missing:
        return _result(
            "required_documents",
            False,
            "pending",
            f"Missing required documents: {', '.join(missing)}.",
        )
    return _result(
        "required_documents", True, "ok", "All country-required documents are present."
    )


def tax_id_format(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    country = _country(submission)
    tax_id = "".join(_text(submission.get("tax_id")).split()).upper()
    expected_pattern = TAX_ID_PATTERNS.get(country)
    if expected_pattern is None:
        return _result(
            "tax_id_format",
            False,
            "reject",
            f"Unsupported country {country!r}; cannot validate tax ID.",
        )
    if expected_pattern.fullmatch(tax_id):
        return _result(
            "tax_id_format",
            True,
            "ok",
            f"Tax ID matches the {COUNTRY_NAMES[country]} format.",
        )

    other_countries = [
        COUNTRY_NAMES[other_country]
        for other_country, pattern in TAX_ID_PATTERNS.items()
        if other_country != country and pattern.fullmatch(tax_id)
    ]
    message = (
        f"Tax ID does not match the {COUNTRY_NAMES[country]} format."
    )
    if other_countries:
        message += f" Its format matches {', '.join(other_countries)} instead."
    return _result("tax_id_format", False, "reject", message)


def bank_country_match(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    country = _country(submission)
    bank_country = _country(submission, "bank_country")
    if bank_country != country:
        return _result(
            "bank_country_match",
            False,
            "reject",
            f"Bank country {bank_country!r} does not match vendor country {country!r}.",
        )
    return _result(
        "bank_country_match", True, "ok", "Bank country matches vendor country."
    )


def bank_account_format(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    country = _country(submission)
    account_number = "".join(
        _text(submission.get("bank_account_number")).split()
    ).upper()
    patterns = {
        "IN": re.compile(r"[0-9]+"),
        "US": re.compile(r"[0-9]+"),
        "GB": re.compile(r"GB[0-9]{2}[A-Z]{4}[0-9]{14}"),
        "DE": re.compile(r"DE[0-9]{20}"),
    }
    pattern = patterns.get(country)
    if pattern is None or not pattern.fullmatch(account_number):
        expected = {
            "IN": "a digits-only account number",
            "US": "a digits-only account number",
            "GB": "a 22-character GB IBAN",
            "DE": "a 22-character DE IBAN",
        }.get(country, "a supported country bank account format")
        return _result(
            "bank_account_format",
            False,
            "reject",
            f"Bank account number must be {expected}.",
        )
    return _result(
        "bank_account_format", True, "ok", "Bank account number format is valid."
    )


def duplicate_check(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    if not master_vendors:
        return _result(
            "duplicate_check",
            False,
            "pending",
            "Approved-vendor master is empty or unavailable; duplicate screening "
            "needs manual review.",
        )
    company_name = normalize_company_name(_text(submission.get("company_name")))
    tax_id = _normalized_identifier(submission.get("tax_id"))
    bank_account_number = _normalized_identifier(
        submission.get("bank_account_number")
    )
    for vendor in master_vendors:
        master_name = normalize_company_name(str(vendor.get("name") or ""))
        if not master_name or master_name == company_name:
            continue
        tax_matches = bool(tax_id) and tax_id == _normalized_identifier(
            vendor.get("tax_id")
        )
        account_matches = bool(bank_account_number) and (
            bank_account_number
            == _normalized_identifier(vendor.get("bank_account_number"))
        )
        if tax_matches or account_matches:
            matched_field = "tax ID" if tax_matches else "bank account number"
            return _result(
                "duplicate_check",
                False,
                "reject",
                "Possible duplicate/fraud: "
                f"{matched_field} matches a different approved vendor "
                f"({vendor.get('name', 'unnamed vendor')}).",
            )
    return _result(
        "duplicate_check",
        True,
        "ok",
        "No tax ID or bank account match was found under a different company name.",
    )


def email_domain_sanity(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    email = _text(submission.get("contact_email")).casefold()
    domain = email.rpartition("@")[2]
    if domain in FREE_EMAIL_DOMAINS:
        return _result(
            "email_domain_sanity",
            False,
            "pending",
            f"Contact email uses the free provider {domain}; please provide a corporate email.",
        )
    return _result(
        "email_domain_sanity", True, "ok", "Contact email does not use a flagged free provider."
    )


def name_match_check(
    submission: dict[str, Any], master_vendors: list[dict[str, Any]]
) -> RuleResult:
    del master_vendors
    from llm import fuzzy_name_match

    company_name = _text(submission.get("company_name"))
    account_holder = _text(submission.get("bank_account_holder"))
    if (
        len(company_name) > MAX_NAME_MATCH_LENGTH
        or len(account_holder) > MAX_NAME_MATCH_LENGTH
    ):
        return _result(
            "name_match_check",
            False,
            "pending",
            "Company or bank-holder name is too long to compare automatically; "
            "please provide a bank letter showing the legal name.",
        )
    if not company_name or not account_holder:
        return _result(
            "name_match_check",
            False,
            "pending",
            "Company name and bank account holder are both needed for name "
            "comparison; please provide the missing name or a bank letter.",
        )
    comparison = fuzzy_name_match(company_name, account_holder)
    company_normalized = normalize_company_name(company_name)
    holder_normalized = normalize_company_name(account_holder)
    name_similarity = SequenceMatcher(
        None, company_normalized, holder_normalized
    ).ratio()
    names_look_different = name_similarity < 0.6
    if comparison["confidence"] < 0.6 and names_look_different:
        return _result(
            "name_match_check",
            False,
            "reject",
            "Company and bank-holder names are very different with low match "
            f"confidence ({comparison['confidence']:.0%}). Please provide a bank "
            "letter showing the legal name. "
            f"{comparison['explanation']}",
        )
    if comparison["match"]:
        if (
            comparison["source"] == "llm"
            and name_similarity < 0.85
        ):
            return _result(
                "name_match_check",
                False,
                "pending",
                "The LLM suggested a name match, but the names are substantially "
                "different. Please provide a bank letter for manual review.",
            )
        return _result(
            "name_match_check",
            True,
            "ok",
            "Company and bank-holder names are consistent "
            f"({comparison['source']}, confidence "
            f"{comparison['confidence']:.0%}): {comparison['explanation']}",
        )
    return _result(
        "name_match_check",
        False,
        "pending",
        "Company and bank-holder names could not be confirmed as the same legal "
        "entity. Please provide a bank letter showing the legal name. "
        f"{comparison['explanation']}",
    )


RULES = (
    completeness,
    required_documents,
    tax_id_format,
    bank_country_match,
    bank_account_format,
    duplicate_check,
    email_domain_sanity,
    name_match_check,
)


def evaluate_submission(
    submission: dict[str, Any],
    master_vendors: list[dict[str, Any]] | None = None,
) -> list[RuleResult]:
    """Run all deterministic checks, loading the bundled master by default."""
    if master_vendors is None:
        master_path = Path(__file__).parent / "data" / "vendors_master.json"
        if master_path.exists():
            with master_path.open(encoding="utf-8") as master_file:
                master_vendors = json.load(master_file)
        else:
            master_vendors = []
    return [rule(submission, master_vendors) for rule in RULES]


def submission_status(results: list[RuleResult]) -> str:
    """Return the most restrictive status represented by rule findings."""
    if any(result["severity"] == "reject" for result in results):
        return "REJECTED"
    if any(result["severity"] == "pending" for result in results):
        return "PENDING"
    return "APPROVED"
