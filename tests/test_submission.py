import pytest

from submission import SubmissionJSONError, parse_submission_json


@pytest.mark.parametrize(
    "contents",
    [
        b"{not json",
        b"\xff\xfe",
        b"[]",
        b"null",
        b'"submission"',
        b'{"country": []}',
        b'{"documents": "bank_proof"}',
        b'{"documents": [{}]}',
    ],
)
def test_invalid_submission_json_has_friendly_error(contents: bytes) -> None:
    with pytest.raises(SubmissionJSONError):
        parse_submission_json(contents)


def test_parse_submission_json_accepts_object() -> None:
    assert parse_submission_json(b'{"company_name":"Example Vendor"}') == {
        "company_name": "Example Vendor"
    }
