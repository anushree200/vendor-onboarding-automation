import json
from typing import Any


class SubmissionJSONError(ValueError):
    """Raised when an uploaded file is not a JSON submission object."""


def parse_submission_json(contents: bytes) -> dict[str, Any]:
    try:
        submission: Any = json.loads(contents.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise SubmissionJSONError(
            "Please upload a valid UTF-8 JSON file."
        ) from error
    if not isinstance(submission, dict):
        raise SubmissionJSONError("The submission JSON must contain an object.")
    if "country" in submission and not isinstance(submission["country"], str):
        raise SubmissionJSONError("The country value must be a country-code string.")
    if "documents" in submission and not isinstance(submission["documents"], list):
        raise SubmissionJSONError("The documents value must be a JSON array.")
    if "documents" in submission and not all(
        isinstance(document, str) for document in submission["documents"]
    ):
        raise SubmissionJSONError("Each document name must be a string.")
    return submission
