"""Tests for the live agent console: activity feed, human queue, operator actions.

Written before the implementation. The contract these pin down:

- The feed is **cursor-based**, not time-based. Several audit rows share a timestamp
  (a whole decide() cycle runs in one simulated instant), so a `since_timestamp` feed
  would either drop rows or repeat them. `AuditLog.id` is autoincrement precisely so
  there is a total order to page through.
- The queue is an **operator surface**, so resolving is a write, and a write from a
  human is still bound by compliance. An operator must not be able to hand-resolve a
  case into an action the rules forbid — that would make the whole deterministic core
  bypassable by clicking a button.
"""

from datetime import datetime, timedelta, timezone

import pytest

from app import console, decision_engine, pipeline
from app.db import make_session_factory
from app.models import AuditLog, FailedPayment, PaymentStatus

NOW = datetime(2026, 7, 1, 11, tzinfo=timezone.utc)


@pytest.fixture
def db():
    factory, engine = make_session_factory("sqlite:///:memory:")
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _ingest(db, pid: str, now=NOW, **overrides):
    payload = {
        "id": pid, "customer_id": "cust_1", "rail": "upi_autopay",
        "amount_paise": 99900, "error_code": "insufficient_balance",
        "error_description": "insufficient balance", "source": "failed_payment",
    }
    payload.update(overrides)
    pipeline.ingest_event(db, f"evt_{pid}", "payment.failed", {"payment": payload}, now=now)
    from sqlalchemy import select
    return db.scalar(select(FailedPayment).where(FailedPayment.razorpay_payment_id == pid))


# --- activity feed ------------------------------------------------------------


def test_feed_returns_newest_first(db):
    _ingest(db, "pay_a")
    _ingest(db, "pay_b", now=NOW + timedelta(minutes=1))

    feed = console.activity(db, limit=50)
    ids = [e["id"] for e in feed["events"]]
    assert ids == sorted(ids, reverse=True), "feed must read newest-first"


def test_feed_cursor_pages_without_gaps_or_repeats(db):
    """The property a timestamp cursor cannot give. A whole decide() cycle shares one
    timestamp, so paging on time either loses rows or serves them twice."""
    for i in range(4):
        _ingest(db, f"pay_{i}", now=NOW)

    everything = console.activity(db, limit=500)["events"]
    total = len(everything)
    assert total > 8, "expected several stages per payment"

    seen, cursor, pages = [], None, 0
    while pages < 50:
        page = console.activity(db, limit=3, before_id=cursor)
        if not page["events"]:
            break
        seen.extend(e["id"] for e in page["events"])
        cursor = page["next_before_id"]
        pages += 1

    assert len(seen) == total, "paging lost or duplicated rows"
    assert len(set(seen)) == total, "paging returned duplicates"
    assert sorted(seen, reverse=True) == seen


def test_feed_tails_only_what_is_new(db):
    """The live poll: give the console the highest id it has seen, get only rows after
    it. This is what makes the stream a stream rather than a re-render."""
    _ingest(db, "pay_a")
    watermark = console.activity(db, limit=1)["events"][0]["id"]

    assert console.activity(db, after_id=watermark)["events"] == []

    _ingest(db, "pay_b", now=NOW + timedelta(minutes=1))
    fresh = console.activity(db, after_id=watermark)["events"]
    assert fresh, "new activity must appear after the watermark"
    assert all(e["id"] > watermark for e in fresh)


def test_feed_carries_the_reasoning_not_just_the_verdict(db):
    """A console that shows only the final action is a log. The point is watching it
    think, so the stage and its detail have to travel with the row."""
    _ingest(db, "pay_a")
    events = console.activity(db, limit=100)["events"]

    stages = {e["stage"] for e in events}
    assert {"classification", "compliance", "guardrail", "decision"} <= stages

    compliance = next(e for e in events if e["stage"] == "compliance")
    assert compliance["detail"].get("policy_rule_id")
    assert compliance["payment"]["razorpay_payment_id"] == "pay_a"


def test_feed_survives_an_empty_ledger(db):
    feed = console.activity(db)
    assert feed["events"] == []
    assert feed["next_before_id"] is None


# --- human review queue -------------------------------------------------------


def test_queue_holds_only_cases_awaiting_a_person(db):
    _ingest(db, "pay_ok")
    _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")

    queue = console.review_queue(db)
    pids = [item["razorpay_payment_id"] for item in queue]
    assert "pay_risk" in pids
    assert "pay_ok" not in pids


def test_queue_puts_the_longest_waiting_first(db):
    """An operator works a queue top-down, so the case that has been waiting longest
    has to surface first or it starves."""
    _ingest(db, "pay_new", now=NOW, error_code="debit_declined_by_bank_risk")
    _ingest(db, "pay_old", now=NOW - timedelta(days=3), error_code="debit_declined_by_bank_risk")

    queue = console.review_queue(db)
    assert queue[0]["razorpay_payment_id"] == "pay_old"


def test_queue_item_explains_why_it_is_there(db):
    """An operator opening a case needs the reason, not just the id."""
    _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    item = console.review_queue(db)[0]

    assert item["reason"], "queue item must carry why it escalated"
    assert item["amount_paise"] == 99900
    assert item["waiting_since"]


# --- operator actions ---------------------------------------------------------


def test_operator_resolution_is_recorded_as_a_human_action(db):
    """The audit trail must attribute this to a person. Recording an operator decision
    as `system` would make the trail claim the agent did something a human did."""
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")

    console.resolve(db, payment.id, outcome="written_off",
                    note="customer disputed, refunded", operator="anoop", now=NOW)

    from sqlalchemy import select
    entry = db.scalar(
        select(AuditLog)
        .where(AuditLog.failed_payment_id == payment.id, AuditLog.stage == "operator")
        .order_by(AuditLog.id.desc())
    )
    assert entry is not None
    assert entry.actor == "human"
    assert entry.detail["operator"] == "anoop"
    assert entry.detail["outcome"] == "written_off"
    assert entry.detail["note"]


def test_resolution_moves_the_case_out_of_the_queue(db):
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    assert len(console.review_queue(db)) == 1

    console.resolve(db, payment.id, outcome="written_off", operator="anoop", now=NOW)
    assert console.review_queue(db) == []


def test_operator_cannot_contact_a_customer_who_withdrew_consent(db):
    """The load-bearing one. A revoked mandate is an absolute stop, and it must stay
    absolute when the instruction comes from a human. If an operator can hand-resolve a
    case into an outreach the rules forbid, the deterministic core is decorative."""
    payment = _ingest(db, "pay_revoked", mandate_revoked=True)
    payment.status = PaymentStatus.HUMAN_REVIEW.value
    db.flush()

    # `returned_to_agent` is the resolution that resumes automated outreach, so it is
    # the one consent withdrawal has to block. (An earlier version of this test used a
    # made-up outcome and passed for the wrong reason: it was refused as unrecognised
    # before the consent check was ever reached.)
    with pytest.raises(console.OperatorActionRefused) as exc:
        console.resolve(db, payment.id, outcome="returned_to_agent", operator="anoop", now=NOW)

    assert "consent" in str(exc.value).lower() or "revoked" in str(exc.value).lower()


def test_refused_operator_actions_are_still_recorded(db):
    """A refusal is exactly the event a compliance review wants to see. A trail that
    only contains permitted actions cannot show the gate ever stopped anyone."""
    payment = _ingest(db, "pay_revoked", mandate_revoked=True)
    payment.status = PaymentStatus.HUMAN_REVIEW.value
    db.flush()

    with pytest.raises(console.OperatorActionRefused):
        console.resolve(db, payment.id, outcome="returned_to_agent", operator="anoop", now=NOW)

    from sqlalchemy import select
    entry = db.scalar(
        select(AuditLog)
        .where(AuditLog.failed_payment_id == payment.id, AuditLog.stage == "operator")
        .order_by(AuditLog.id.desc())
    )
    assert entry is not None
    assert entry.detail["refused"] is True


def test_unknown_outcome_is_refused(db):
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    with pytest.raises(console.OperatorActionRefused):
        console.resolve(db, payment.id, outcome="make_it_go_away", operator="anoop", now=NOW)


def test_resolving_a_case_twice_is_refused(db):
    """Double-resolution would let one case be written off and then recovered, or two
    operators to act on the same row without either seeing the other."""
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    console.resolve(db, payment.id, outcome="written_off", operator="anoop", now=NOW)

    with pytest.raises(console.OperatorActionRefused):
        console.resolve(db, payment.id, outcome="recovered_manually",
                        operator="someone_else", now=NOW + timedelta(minutes=5))


def test_operator_can_record_a_manual_recovery(db):
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    console.resolve(db, payment.id, outcome="recovered_manually", operator="anoop", now=NOW)

    db.refresh(payment)
    assert payment.status == PaymentStatus.RECOVERED.value
    assert payment.recovered_at is not None


def test_manual_recovery_does_not_pollute_the_causal_measurement(db):
    """A hand-recovered case was not recovered BY the agent. Counting it in the
    intervention arm would credit the system with an operator's work and inflate the
    lift the whole design exists to report honestly."""
    payment = _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    console.resolve(db, payment.id, outcome="recovered_manually", operator="anoop", now=NOW)

    db.refresh(payment)
    assert payment.recovered_manually is True


# --- live counters ------------------------------------------------------------


def test_pulse_reports_what_the_agent_is_currently_holding(db):
    _ingest(db, "pay_a")
    _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")

    pulse = console.pulse(db)
    assert pulse["awaiting_review"] >= 1
    assert pulse["total"] >= 2
    assert "by_status" in pulse
    assert pulse["latest_event_id"] is not None


# --- schema drift -------------------------------------------------------------


def test_schema_drift_is_reported_clearly_not_as_a_500(tmp_path):
    """A database created before a column was added does not get it: SQLAlchemy's
    create_all adds missing TABLES, never missing COLUMNS. Without a check, the only
    symptom is an OperationalError on every request, which reads as a server fault
    rather than as a stale file the developer needs to reseed.
    """
    from sqlalchemy import create_engine, text

    from app.db import schema_drift

    path = tmp_path / "stale.db"
    engine = create_engine(f"sqlite:///{path}")
    # A table shaped like an older revision: right name, missing the newer columns.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE TABLE failed_payments ("
            "id VARCHAR PRIMARY KEY, razorpay_payment_id VARCHAR, customer_id VARCHAR)"
        ))

    drift = schema_drift(engine)
    assert drift, "stale schema must be detected"
    assert any("recovered_manually" in d for d in drift)
    assert any("failed_payments" in d for d in drift)
    engine.dispose()


def test_a_current_schema_reports_no_drift():
    from app.db import make_session_factory, schema_drift

    factory, engine = make_session_factory("sqlite:///:memory:")
    try:
        assert schema_drift(engine) == []
    finally:
        engine.dispose()


def test_queue_reason_names_why_a_person_is_needed_not_the_ladder_rung(db):
    """A risk-blocked case is in front of a person because compliance refused to act on
    it, not because of how many contacts have happened. The escalation entry is written
    after the compliance one, so a naive "latest relevant stage" lookup surfaces
    `attempts_made` — true about the ladder, useless as a reason to open the case.
    """
    _ingest(db, "pay_risk", error_code="debit_declined_by_bank_risk")
    reason = console.review_queue(db)[0]["reason"]

    assert reason != "attempts_made"
    assert "risk" in reason.lower() or "COMP-002" in reason
