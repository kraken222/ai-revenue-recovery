from datetime import datetime, timedelta, timezone

from app.contact_policy import (
    IST,
    constrain,
    is_contact_action,
    next_contact_window_open,
    within_contact_window,
)


def utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def ist(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=IST)


# --- the window itself --------------------------------------------------------


def test_midday_ist_is_inside_the_window():
    assert within_contact_window(ist(2026, 1, 15, 12))


def test_boundaries_are_inclusive_at_open_exclusive_at_close():
    assert within_contact_window(ist(2026, 1, 15, 8, 0))
    assert within_contact_window(ist(2026, 1, 15, 18, 59))
    assert not within_contact_window(ist(2026, 1, 15, 19, 0))
    assert not within_contact_window(ist(2026, 1, 15, 7, 59))


def test_the_bandits_favourite_slot_is_outside_the_contact_window():
    """The conflict this module exists for: the bandit learns 00:00-06:00 recovers the
    most money (salary credits land overnight), and that window is legal for a silent
    debit but is harassment for a customer contact."""
    assert not within_contact_window(ist(2026, 1, 15, 2))
    assert not within_contact_window(ist(2026, 1, 15, 5, 30))


# --- timezone correctness -----------------------------------------------------


def test_window_is_evaluated_in_ist_not_utc():
    """The bug a naive `dt.hour` check produces. 20:00 UTC is 01:30 IST — the middle
    of the night in India — and a UTC-based check reading `hour < 19` would wave it
    through as if it were evening."""
    assert not within_contact_window(utc(2026, 1, 15, 20, 0))

    # And the mirror image: 03:00 UTC is 08:30 IST, legal, but looks like night in UTC.
    assert within_contact_window(utc(2026, 1, 15, 3, 0))


def test_utc_window_edges_map_to_ist_hours():
    # 08:00 IST == 02:30 UTC
    assert not within_contact_window(utc(2026, 1, 15, 2, 29))
    assert within_contact_window(utc(2026, 1, 15, 2, 30))
    # 19:00 IST == 13:30 UTC
    assert within_contact_window(utc(2026, 1, 15, 13, 29))
    assert not within_contact_window(utc(2026, 1, 15, 13, 30))


def test_naive_datetimes_are_treated_as_utc():
    assert within_contact_window(datetime(2026, 1, 15, 3, 0))
    assert not within_contact_window(datetime(2026, 1, 15, 20, 0))


# --- next opening -------------------------------------------------------------


def test_inside_the_window_is_returned_unchanged():
    t = ist(2026, 1, 15, 11)
    assert next_contact_window_open(t) == t


def test_before_dawn_defers_to_the_same_morning():
    opened = next_contact_window_open(ist(2026, 1, 15, 3))
    assert opened.astimezone(IST).hour == 8
    assert opened.astimezone(IST).day == 15


def test_after_close_defers_to_the_next_morning():
    opened = next_contact_window_open(ist(2026, 1, 15, 21))
    assert opened.astimezone(IST).hour == 8
    assert opened.astimezone(IST).day == 16


def test_deferral_never_moves_a_slot_backwards():
    for hour in range(24):
        t = ist(2026, 1, 15, hour)
        assert next_contact_window_open(t) >= t


def test_every_returned_slot_is_actually_legal():
    for hour in range(24):
        for minute in (0, 30):
            assert within_contact_window(next_contact_window_open(ist(2026, 1, 15, hour, minute)))


# --- constrain ----------------------------------------------------------------


def test_silent_debit_keeps_the_overnight_slot():
    """A machine-to-machine debit against an existing mandate contacts nobody, so the
    bandit's overnight window survives."""
    slot = ist(2026, 1, 15, 2)
    for action in ("retry_now", "retry_at"):
        adjusted, reason = constrain(action, slot)
        assert adjusted == slot
        assert reason is None


def test_customer_contact_is_deferred_out_of_the_night():
    slot = ist(2026, 1, 15, 2)
    adjusted, reason = constrain("send_payment_link", slot)
    assert adjusted > slot
    assert adjusted.astimezone(IST).hour == 8
    assert reason == "deferred_to_rbi_contact_window"


def test_contact_already_in_window_is_untouched():
    slot = ist(2026, 1, 15, 10)
    adjusted, reason = constrain("send_payment_link", slot)
    assert adjusted == slot
    assert reason is None


def test_action_classification():
    assert is_contact_action("send_payment_link")
    assert is_contact_action("request_new_mandate")
    assert not is_contact_action("retry_now")
    assert not is_contact_action("monitor_gateway_retry")


# --- monitoring is an observation, not an action ------------------------------


def test_monitoring_spends_no_attempt_and_logs_no_contact():
    """Razorpay owns the card retry, so our action there is to watch. Filing that as
    an action would spend an attempt from the cap and — because the compliance
    invariants count ActionLog rows as customer contacts — make a control-group card
    payment look like it had been contacted.
    """
    from sqlalchemy import select

    from app import decision_engine, executor
    from app.db import make_session_factory
    from app.metrics import compliance_invariants
    from app.models import ActionLog, FailedPayment, PaymentStatus

    factory, engine = make_session_factory("sqlite:///:memory:")
    db = factory()
    try:
        now = utc(2026, 1, 15, 10)
        payment = FailedPayment(
            razorpay_payment_id="pay_card_1",
            customer_id="cust_1",
            rail="card",
            amount_paise=99900,
            error_code="insufficient_funds",
            control_group=True,
            first_failed_at=now,
        )
        db.add(payment)
        db.flush()

        decision = decision_engine.decide(db, payment, now=now)
        assert decision.action == "monitor_gateway_retry"

        executor.execute(db, payment, decision, now=now)
        db.flush()

        assert payment.retry_count == 0, "monitoring must not spend an attempt"
        assert payment.status == PaymentStatus.WAITING.value
        assert db.scalars(select(ActionLog)).all() == [], "monitoring must not log a contact"
        assert all(c["pass"] for c in compliance_invariants(db))
    finally:
        db.close()
        engine.dispose()
