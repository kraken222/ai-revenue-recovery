from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.orm import sessionmaker

from app.db import Base, make_session_factory
from app.metrics import _wilson_interval, compliance_invariants
from app.models import ActionLog, Decision, FailedPayment

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def db():
    factory, engine = make_session_factory("sqlite:///:memory:")
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _payment(db, **overrides) -> FailedPayment:
    defaults = dict(
        razorpay_payment_id="pay_1",
        customer_id="cust_1",
        rail="card",
        amount_paise=99900,
        error_code="insufficient_funds",
        status="executed",
        retry_count=1,
        first_failed_at=NOW,
    )
    defaults.update(overrides)
    payment = FailedPayment(**defaults)
    db.add(payment)
    db.flush()
    return payment


def _contact(db, payment: FailedPayment, at: datetime) -> None:
    decision = Decision(
        failed_payment_id=payment.id,
        action="retry_now",
        compliant_action_set=["retry_now"],
        policy_rule_id="COMP-006-compliant-retry-window",
        created_at=at,
    )
    db.add(decision)
    db.flush()
    db.add(
        ActionLog(
            failed_payment_id=payment.id,
            decision_id=decision.id,
            action_taken="retry_now",
            outcome="failed",
            executed_at=at,
        )
    )
    db.flush()


def _invariant(db, name: str) -> dict:
    return next(c for c in compliance_invariants(db) if c["check"] == name)


REVOCATION = "contacted after mandate revocation"


def test_contact_before_revocation_is_not_a_breach(db):
    """The bug this pins: a customer can revoke consent AFTER a contact that was
    entirely legal when made. Counting that retroactively makes the invariant
    unpassable for something that is not a violation."""
    payment = _payment(db, mandate_revoked=True, mandate_revoked_at=NOW + timedelta(days=2))
    _contact(db, payment, at=NOW + timedelta(hours=1))

    assert _invariant(db, REVOCATION)["violations"] == 0
    assert _invariant(db, REVOCATION)["pass"] is True


def test_contact_after_revocation_is_a_breach(db):
    payment = _payment(db, mandate_revoked=True, mandate_revoked_at=NOW)
    _contact(db, payment, at=NOW + timedelta(hours=1))

    assert _invariant(db, REVOCATION)["violations"] == 1
    assert _invariant(db, REVOCATION)["pass"] is False


def test_payment_revoked_before_we_ever_saw_it_may_never_be_contacted(db):
    payment = _payment(db, mandate_revoked=True, mandate_revoked_at=NOW)
    _contact(db, payment, at=NOW + timedelta(minutes=1))
    _contact(db, payment, at=NOW + timedelta(days=1))

    assert _invariant(db, REVOCATION)["violations"] == 2


def test_never_revoked_payment_is_never_a_breach(db):
    payment = _payment(db, mandate_revoked=False, mandate_revoked_at=None)
    _contact(db, payment, at=NOW + timedelta(hours=5))

    assert _invariant(db, REVOCATION)["violations"] == 0


def test_control_group_contact_is_a_breach(db):
    payment = _payment(db, control_group=True)
    _contact(db, payment, at=NOW)

    assert _invariant(db, "control-group payments contacted")["violations"] == 1


def test_clean_ledger_passes_every_invariant(db):
    payment = _payment(db)
    _contact(db, payment, at=NOW)

    assert all(c["pass"] for c in compliance_invariants(db))


# --- Wilson interval -----------------------------------------------------------


def test_interval_always_stays_inside_zero_one():
    """Wilson is bounded by construction; a normal approximation is not, which is
    part of why it was wrong at these sample sizes."""
    for alpha, beta in [(1, 1), (1, 30), (30, 1), (2, 2), (5, 3), (50, 50)]:
        lo, hi = _wilson_interval(alpha, beta)
        assert 0.0 <= lo <= hi <= 1.0


def test_no_evidence_yields_no_information():
    assert _wilson_interval(1, 1) == (0.0, 1.0)


def test_interval_narrows_as_evidence_accumulates():
    """The property the bar exists to show: same success rate, more pulls, tighter
    interval. Without this a reader cannot tell a lucky arm from a proven one."""
    thin = _wilson_interval(3, 2)     # 2/3 over 3 pulls
    thick = _wilson_interval(21, 11)  # 2/3 over 30 pulls
    assert (thick[1] - thick[0]) < (thin[1] - thin[0])


def test_does_not_overstate_the_ceiling_at_low_n():
    """The specific defect this replaced: the normal approximation put Beta(5,3)'s
    upper bound at 94% against an exact ~89%, flattering an arm on six pulls."""
    _, hi = _wilson_interval(5, 3)
    assert hi < 0.92


def test_interval_brackets_the_observed_rate():
    for alpha, beta in [(5, 3), (9, 2), (2, 8)]:
        lo, hi = _wilson_interval(alpha, beta)
        observed = (alpha - 1) / ((alpha - 1) + (beta - 1))
        assert lo <= observed <= hi
