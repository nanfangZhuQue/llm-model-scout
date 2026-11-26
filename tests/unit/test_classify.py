import pytest

from llm_model_scout.core.classify import classify_error
from llm_model_scout.domain import ErrorCategory


def test_401_is_unauthorized():
    assert classify_error(401, {}) == ErrorCategory.UNAUTHORIZED


@pytest.mark.parametrize(
    "status,expected",
    [
        (403, "FORBIDDEN"),
        (402, "PAYMENT_REQUIRED"),
        (429, "RATE_LIMITED"),
        (500, "SERVER_ERROR"),
        (529, "SERVER_ERROR"),
        (200, "BAD_RESPONSE"),
        (404, "UNKNOWN_ERROR"),
        (400, "UNKNOWN_ERROR"),
    ],
)
def test_status_mapping(status, expected):
    assert classify_error(status, {}) == expected


def test_model_not_found_needs_evidence():
    data = {"error": {"code": "model_not_found", "message": "No model"}}
    assert classify_error(404, data) == "MODEL_NOT_FOUND"
    assert classify_error(404, data, stage="discovery") == "UNKNOWN_ERROR"


def test_balance_message():
    assert (
        classify_error(400, {"error": {"message": "credit balance is too low"}})
        == "PAYMENT_REQUIRED"
    )
