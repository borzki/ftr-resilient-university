import pytest
from fastapi.exceptions import RequestValidationError

from ftcommon import config
from ftcommon.validation import validate_payload
from payment.main import LoosePaymentIn, PaymentIn
from records.main import GradeIn, LooseGradeIn

VALID_PAYMENT = {"student_id": 1001, "amount": "150.00", "term": "Fall-2026", "method": "card"}


def test_valid_payment_is_accepted():
    p = validate_payload(PaymentIn, LoosePaymentIn, VALID_PAYMENT)
    assert str(p.amount) == "150.00"


@pytest.mark.parametrize("override", [
    {"amount": "-5.00"}, {"amount": "0"}, {"amount": "999999999.00"}, {"amount": "1.234"},
    {"term": "<script>alert(1)</script>"}, {"term": "Fall-2026'; DROP TABLE payments;--"},
    {"method": "crypto"}, {"student_id": "abc"}, {"student_id": 5},
])
def test_invalid_payments_are_rejected(override):
    with pytest.raises(RequestValidationError):
        validate_payload(PaymentIn, LoosePaymentIn, {**VALID_PAYMENT, **override})


def test_non_object_body_is_rejected():
    with pytest.raises(RequestValidationError):
        validate_payload(PaymentIn, LoosePaymentIn, ["not", "an", "object"])


@pytest.mark.parametrize("override", [{"grade": "Z+"}, {"course_code": "X' OR '1'='1"}, {"term": "2026"}])
def test_invalid_grades_are_rejected(override):
    good = {"student_id": 1001, "course_code": "CSE301", "term": "Fall-2026", "grade": "A-"}
    validate_payload(GradeIn, LooseGradeIn, good)
    with pytest.raises(RequestValidationError):
        validate_payload(GradeIn, LooseGradeIn, {**good, **override})


def test_baseline_mode_only_checks_types(monkeypatch):
    """Documents the baseline weakness measured in experiment E8."""
    monkeypatch.setattr(config, "VALIDATION_STRICT", False)
    p = validate_payload(PaymentIn, LoosePaymentIn, {**VALID_PAYMENT, "amount": "-500"})
    assert str(p.amount) == "-500"
