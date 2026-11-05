import json
from typing import Any

from rules import REQUIRED_DOCUMENTS, REQUIRED_FIELDS


def simulate_vendor_resubmission(
    submission: dict[str, Any], reasons: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[str]]:
    """Apply demo corrections for pending requirements without changing input."""
    corrected = json.loads(json.dumps(submission))
    changes: list[str] = []
    country = corrected.get("country")
    country = country.strip().upper() if isinstance(country, str) else "IN"
    company_name = corrected.get("company_name")
    if not isinstance(company_name, str) or not company_name.strip():
        company_name = "Demo Vendor Ltd"
    defaults: dict[str, Any] = {
        "company_name": company_name,
        "country": "IN",
        "tax_id": "29ZZZZZ9876Q1Z2",
        "registered_address": "10 Demo Street, Bengaluru, Karnataka 560001",
        "contact_name": "Demo Contact",
        "contact_email": "contact@demo-vendor.example",
        "bank_account_holder": company_name,
        "bank_name": "Demo National Bank",
        "bank_account_number": "981234567890",
        "bank_country": country,
        "ifsc_or_routing": "DEMO0001234",
        "documents": [],
    }
    defaults.update(
        {
            "IN": {
                "tax_id": "29ZZZZZ9876Q1Z2",
                "bank_account_number": "981234567890",
                "ifsc_or_routing": "DEMO0001234",
            },
            "US": {
                "tax_id": "12-3456789",
                "bank_account_number": "981234567890",
                "ifsc_or_routing": "123456789",
            },
            "GB": {
                "tax_id": "GB987654321",
                "bank_account_number": "GB00DEMO00000000000000",
                "ifsc_or_routing": "123456",
            },
            "DE": {
                "tax_id": "DE987654321",
                "bank_account_number": "DE00000000000000000000",
                "ifsc_or_routing": "DEMOXXXXXXX",
            },
        }.get(country, {})
    )

    for field in REQUIRED_FIELDS:
        if field == "documents":
            documents = corrected.get(field)
            if not isinstance(documents, list) or not all(
                isinstance(document, str) for document in documents
            ):
                corrected[field] = []
                changes.append("Replaced invalid document entries.")
        elif (
            not isinstance(corrected.get(field), str)
            or not corrected[field].strip()
        ):
            corrected[field] = defaults[field]
            changes.append(f"Added a demo value for {field}.")

    unknown_fields = set(corrected) - set(REQUIRED_FIELDS)
    if unknown_fields:
        for field in unknown_fields:
            del corrected[field]
        changes.append(f"Removed unsupported fields: {', '.join(sorted(unknown_fields))}.")

    reason_rules = {reason.get("rule") for reason in reasons}
    required_documents = REQUIRED_DOCUMENTS.get(country, set())
    current_documents = corrected.get("documents", [])
    if not isinstance(current_documents, list) or not all(
        isinstance(document, str) for document in current_documents
    ):
        current_documents = []
    if "required_documents" in reason_rules or not required_documents.issubset(
        current_documents
    ):
        missing_documents = sorted(required_documents - set(current_documents))
        corrected["documents"] = sorted(set(current_documents) | required_documents)
        if missing_documents:
            changes.append(f"Added documents: {', '.join(missing_documents)}.")

    if "email_domain_sanity" in reason_rules:
        corrected["contact_email"] = "contact@demo-vendor.example"
        changes.append("Changed the contact email to a corporate demo address.")
    if "name_match_check" in reason_rules:
        corrected["bank_account_holder"] = corrected["company_name"]
        changes.append("Simulated bank proof confirming the company legal name.")

    return corrected, changes
