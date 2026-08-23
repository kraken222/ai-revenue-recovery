from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app import escalation, promises
from app.db import make_session_factory
from app.escalation import PROMISE_GRACE_HOURS, PromiseState
from app.models import FailedPayment, PromiseToPay

NOW = datetime(2026, 1, 15, 10, tzinfo=timezone.utc)


@pytest.fixture
def db():
    factory, engine = make_session_factory("sqlite:///:memory:")
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _payment(db, **kw) -> FailedPayment:
    defaults = dict(
        razorpay_payment_id="pay_1", customer_id="cust_1", rail="upi_autopay",
        amount_paise=99900, error_code="insufficient_balance", first_failed_at=NOW,
    )
    defaults.update(kw)
    p = FailedPayment(**defaults)
    db.add(p)
    db.flush()
    return p


# --- the ladder ---------------------------------------------------------------


def test_ladder_climbs_with_contacts_made():
    assert escalation.rung_for(attempt=0)[0].name == "reminder"
    assert escalation.rung_for(attempt=1)[0].name == "assisted"
    assert escalation.rung_for(attempt=2)[0].name == "human"


def test_ladder_is_capped_and_never_runs_off_the_end():
    for attempt in range(2, 50):
        assert escalation.rung_for(attempt=attempt)[0].level == 3


def test_gateway_owned_retry_starts_passive():
    """A card can fail three times inside Razorpay's own retry cycle without us having
    said anything. Counting those as our escalation would open at the top rung against
    a customer we have never contacted."""
    rung, reason = escalation.rung_for(attempt=0, gateway_owns_retry=True)
    assert rung.name == "passive"
    assert reason == "gateway_owns_retry"


def test_second_broken_promise_jumps_to_a_human():
    """One missed date is life; two is a pattern. The top rung is a person rather than
    more pressure — the ladder escalates involvement, never force."""
    assert escalation.rung_for(attempt=0, broken_promises=1)[0].name != "human"
    rung, reason = escalation.rung_for(attempt=0, broken_promises=2)
    assert rung.name == "human"
    assert reason == "repeated_broken_promises"


# --- promise lifecycle --------------------------------------------------------


def test_open_promise_suppresses_outreach():
    state = PromiseState(open_promise_at=NOW + timedelta(days=2), broken_count=0, kept_count=0)
    decision = escalation.evaluate_promise(state, NOW)
    assert decision.suppressed
    assert decision.reason == "open_promise_to_pay"
    assert decision.release_at == NOW + timedelta(days=2, hours=PROMISE_GRACE_HOURS)


def test_matured_promise_releases_outreach():
    promised = NOW - timedelta(days=1)
    state = PromiseState(open_promise_at=promised, broken_count=0, kept_count=0)
    assert not escalation.evaluate_promise(state, NOW).suppressed


def test_grace_period_prevents_a_false_break():
    """Payments settle overnight, so treating the promised evening as an instant
    deadline manufactures broken promises out of normal settlement lag."""
    promised = NOW
    assert escalation.is_promise_open(promised, NOW + timedelta(hours=PROMISE_GRACE_HOURS - 1))
    assert not escalation.is_promise_open(promised, NOW + timedelta(hours=PROMISE_GRACE_HOURS + 1))


def test_paid_promise_is_never_broken():
    promised = NOW - timedelta(days=5)
    assert not escalation.is_promise_broken(promised, NOW, paid=True)
    assert escalation.is_promise_broken(promised, NOW, paid=False)


def test_horizon_rejects_past_and_far_future_dates():
    assert escalation.promise_horizon_ok(NOW + timedelta(days=7), NOW)
    assert not escalation.promise_horizon_ok(NOW - timedelta(days=1), NOW)   # already passed
    assert not escalation.promise_horizon_ok(NOW + timedelta(days=90), NOW)  # a deferral


# --- persistence --------------------------------------------------------------


def test_recording_and_reading_a_promise(db):
    payment = _payment(db)
    promise = promises.record_promise(db, payment.id, NOW + timedelta(days=3), now=NOW)
    assert promise is not None

    state = promises.state_for(db, payment.id, NOW)
    assert state.has_open_promise
    assert state.broken_count == 0


def test_unusable_dates_are_refused(db):
    """An out-of-horizon date must not be stored: an accepted promise suppresses
    outreach, so a date nobody intends to meet would silently mute the case."""
    payment = _payment(db)
    assert promises.record_promise(db, payment.id, NOW - timedelta(days=1), now=NOW) is None
    assert promises.record_promise(db, payment.id, NOW + timedelta(days=365), now=NOW) is None
    assert not promises.state_for(db, payment.id, NOW).has_open_promise


def test_a_new_promise_supersedes_the_open_one(db):
    """Two open promises would each independently suppress outreach, and the later one
    would silently extend the earlier one's hold."""
    payment = _payment(db)
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)
    promises.record_promise(db, payment.id, NOW + timedelta(days=5), now=NOW)

    rows = db.scalars(select(PromiseToPay).where(PromiseToPay.failed_payment_id == payment.id)).all()
    assert len(rows) == 2
    assert sum(1 for r in rows if r.status == "open") == 1
    assert sum(1 for r in rows if r.status == "superseded") == 1
    assert sum(1 for r in rows if r.status == "broken") == 0


def test_payment_settles_an_open_promise_as_kept(db):
    payment = _payment(db)
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)
    promises.settle_promises(db, payment.id, paid=True, now=NOW + timedelta(days=1))

    state = promises.state_for(db, payment.id, NOW + timedelta(days=1))
    assert state.kept_count == 1
    assert not state.has_open_promise


def test_matured_unpaid_promise_settles_as_broken(db):
    payment = _payment(db)
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)
    later = NOW + timedelta(days=2, hours=PROMISE_GRACE_HOURS + 1)
    promises.settle_promises(db, payment.id, paid=False, now=later)

    assert promises.state_for(db, payment.id, later).broken_count == 1


def test_promise_not_yet_due_stays_open(db):
    """Not-yet-due is not broken. Settling early would count a promise the customer
    still has time to keep."""
    payment = _payment(db)
    promises.record_promise(db, payment.id, NOW + timedelta(days=5), now=NOW)
    promises.settle_promises(db, payment.id, paid=False, now=NOW + timedelta(days=1))

    state = promises.state_for(db, payment.id, NOW + timedelta(days=1))
    assert state.has_open_promise
    assert state.broken_count == 0


# --- integration with the decision path ---------------------------------------


def test_open_promise_holds_back_a_payment_link(db):
    from app import decision_engine

    payment = _payment(db, rail="card", error_code="expired_card")
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)

    decision = decision_engine.decide(db, payment, now=NOW)
    assert decision.action == "wait", "an open promise must suppress the outreach"
    assert decision.scheduled_at == NOW + timedelta(days=2, hours=PROMISE_GRACE_HOURS)


def test_promise_cannot_resurrect_a_compliance_stop(db):
    """A promise may only ever suppress contact. It must never reopen a case that
    compliance already stopped — consent withdrawal outranks any commitment."""
    from app import decision_engine

    payment = _payment(db, mandate_revoked=True, mandate_revoked_at=NOW)
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)

    decision = decision_engine.decide(db, payment, now=NOW)
    assert decision.action == "stop_lost"


def test_two_broken_promises_route_to_a_human(db):
    from app import decision_engine

    payment = _payment(db, rail="card", error_code="expired_card")
    # Each promise must be made and then MISSED before the next is made — a promise
    # made while another is still open would supersede it rather than break it.
    for offset in (2, 6):
        made_at = NOW + timedelta(days=offset - 2)
        promises.record_promise(db, payment.id, made_at + timedelta(days=2), now=made_at)
        promises.settle_promises(
            db, payment.id, paid=False,
            now=made_at + timedelta(days=2, hours=PROMISE_GRACE_HOURS + 1),
        )

    later = NOW + timedelta(days=8)
    assert promises.state_for(db, payment.id, later).broken_count == 2

    decision = decision_engine.decide(db, payment, now=later)
    assert decision.action == "escalate_human"


def test_superseding_a_promise_is_not_breaking_it(db):
    """A revised date is not a missed one. Counting revisions as breaks would escalate
    a customer to a human for rescheduling twice — i.e. for keeping us informed."""
    payment = _payment(db)
    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)
    promises.record_promise(db, payment.id, NOW + timedelta(days=4), now=NOW)
    promises.record_promise(db, payment.id, NOW + timedelta(days=6), now=NOW)

    state = promises.state_for(db, payment.id, NOW)
    assert state.broken_count == 0, "revisions must not count as broken promises"
    assert state.has_open_promise
    assert escalation.rung_for(attempt=0, broken_promises=state.broken_count)[0].name != "human"


def test_a_broken_promise_is_not_overwritten_by_the_next_one(db):
    """Regression: with autoflush off, a status set in memory is invisible to the next
    query, so a promise settled as `broken` was read back as `open` and downgraded to
    `superseded` by the following promise. That erased the break, and two missed dates
    never added up to the escalation they should have."""
    payment = _payment(db)

    promises.record_promise(db, payment.id, NOW + timedelta(days=2), now=NOW)
    promises.settle_promises(
        db, payment.id, paid=False, now=NOW + timedelta(days=2, hours=PROMISE_GRACE_HOURS + 1)
    )
    second_made = NOW + timedelta(days=4)
    promises.record_promise(db, payment.id, second_made + timedelta(days=2), now=second_made)
    promises.settle_promises(
        db, payment.id, paid=False,
        now=second_made + timedelta(days=2, hours=PROMISE_GRACE_HOURS + 1),
    )

    state = promises.state_for(db, payment.id, NOW + timedelta(days=10))
    assert state.broken_count == 2, "the first break must survive the second promise"
    assert escalation.rung_for(attempt=0, broken_promises=state.broken_count)[0].name == "human"


def test_top_rung_reports_which_path_reached_it():
    """Two different situations reach rung 3, and the audit trail has to tell them
    apart: a customer who missed two promised dates is not the same as one we have
    simply contacted twice. Reporting both as broken promises puts a reason in the
    record that never happened."""
    by_attempts = escalation.rung_for(attempt=3, broken_promises=0)
    by_promises = escalation.rung_for(attempt=0, broken_promises=2)

    assert by_attempts[0].level == by_promises[0].level == 3
    assert by_attempts[1] == "contact_attempts_exhausted"
    assert by_promises[1] == "repeated_broken_promises"
    assert by_attempts[1] != by_promises[1]
