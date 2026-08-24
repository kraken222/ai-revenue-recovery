"""Read side of the live agent console, plus the one place a human writes back.

The agent already runs — perceive in `pipeline.ingest_event`, reason in
`decision_engine.decide`, act in `executor.execute`, learn in `bandit.update`. What it
has never had is a surface where you can watch it do that, or act on the cases it hands
to a person. This module is that surface.

Two design decisions worth stating, because both were arrived at by writing the tests
first and finding the naive version broken:

**The feed is cursor-based, not time-based.** An entire `decide()` cycle records eight
or more audit rows at one instant, so a `since_timestamp` feed either drops rows that
share the boundary timestamp or serves them twice. `AuditLog.id` is autoincrement
specifically so a total order exists to page through, and the console pages on that.

**An operator write is still bound by compliance.** `resolve` is the only path in the
system where a human instructs the agent, and it deliberately cannot authorise an action
the deterministic core forbids. If clicking a button could contact a customer who
withdrew consent, every rule in `compliance.py` would be advisory.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import audit
from app.models import AuditLog, FailedPayment, PaymentStatus
from app.timeutil import as_aware, utcnow


class OperatorActionRefused(Exception):
    """A human asked for something the rules do not permit, or that does not apply."""


# What an operator may record. Deliberately small: an operator closes a case, they do
# not get to invent an action for the agent to take.
RESOLUTIONS = {
    "recovered_manually": "customer paid through another channel",
    "written_off": "not worth further pursuit",
    "disputed": "customer disputes the charge; out of recovery scope",
    "returned_to_agent": "human review complete, resume automated handling",
}

# Resolutions that would put us back in contact with the customer. These are the ones
# compliance has to clear, because they are the ones that reach a person.
_RESOLUTIONS_IMPLYING_CONTACT = {"returned_to_agent"}

_ACTIVE_STATUSES = (
    PaymentStatus.NEW.value,
    PaymentStatus.WAITING.value,
    PaymentStatus.EXECUTED.value,
)


def _payment_summary(p: FailedPayment) -> dict:
    return {
        "id": p.id,
        "razorpay_payment_id": p.razorpay_payment_id,
        "source": p.source,
        "rail": p.rail,
        "amount_paise": p.amount_paise,
        "status": p.status,
        "control_group": p.control_group,
    }


def activity(
    db: Session,
    limit: int = 60,
    before_id: int | None = None,
    after_id: int | None = None,
) -> dict:
    """The stream the console renders.

    `after_id` tails: give the highest id already on screen, get only what is newer —
    this is what makes the poll a stream rather than a re-render. `before_id` pages
    backwards through history. Both are ids rather than timestamps for the reason in the
    module docstring.
    """
    stmt = (
        select(AuditLog, FailedPayment)
        .join(FailedPayment, FailedPayment.id == AuditLog.failed_payment_id)
        .order_by(AuditLog.id.desc())
        .limit(limit)
    )
    if before_id is not None:
        stmt = stmt.where(AuditLog.id < before_id)
    if after_id is not None:
        # Newest-first still, but bounded below — the caller already has everything
        # at or under the watermark.
        stmt = stmt.where(AuditLog.id > after_id)

    rows = db.execute(stmt).all()
    events = [
        {
            "id": entry.id,
            "stage": entry.stage,
            "actor": entry.actor,
            "detail": entry.detail,
            "at": entry.created_at.isoformat() if entry.created_at else None,
            "payment": _payment_summary(payment),
        }
        for entry, payment in rows
    ]

    # Only offer a backward cursor when a full page came back. A short page is the end
    # of the ledger, and handing out a cursor there makes the client poll forever.
    next_before = events[-1]["id"] if len(events) == limit else None
    return {"events": events, "next_before_id": next_before}


def _escalation_reason(db: Session, payment_id: str) -> str:
    """Why this case is sitting in front of a person.

    Compliance is asked first, and deliberately so. It is the stage that actually
    refused to act, and it runs BEFORE escalation — so taking whichever relevant stage
    happens to be latest surfaces the escalation rung's `reached_because`
    ("attempts_made"), which is true about the ladder and useless as a reason to open
    the case. Ordered by which stage owns the answer, not by which wrote last.
    """
    for stage, keys in (
        ("compliance", ("blocked_reason", "policy_rule_id")),
        ("classification", ("ensemble_verdict",)),
        ("escalation", ("reason", "reached_because")),
    ):
        entry = db.scalar(
            select(AuditLog)
            .where(AuditLog.failed_payment_id == payment_id, AuditLog.stage == stage)
            .order_by(AuditLog.id.desc())
        )
        detail = (entry.detail if entry else None) or {}
        for key in keys:
            if detail.get(key):
                return str(detail[key])
    return "escalated"


def review_queue(db: Session) -> list[dict]:
    """Cases awaiting a human, longest-waiting first so nothing starves at the bottom."""
    payments = db.scalars(
        select(FailedPayment)
        .where(FailedPayment.status == PaymentStatus.HUMAN_REVIEW.value)
        .order_by(FailedPayment.first_failed_at.asc())
    ).all()

    return [
        {
            **_payment_summary(p),
            "reason": _escalation_reason(db, p.id),
            "waiting_since": p.first_failed_at.isoformat() if p.first_failed_at else None,
            "retry_count": p.retry_count,
            "error_code": p.error_code,
        }
        for p in payments
    ]


def resolve(
    db: Session,
    payment_id: str,
    outcome: str,
    operator: str,
    note: str | None = None,
    now: datetime | None = None,
) -> FailedPayment:
    """Record a human's decision on an escalated case.

    Refusals raise rather than return, and are written to the trail before the raise —
    a compliance review most wants to see the actions that were STOPPED, and a log
    containing only permitted ones cannot show the gate ever did anything.
    """
    now = as_aware(now) or utcnow()
    payment = db.get(FailedPayment, payment_id)
    if payment is None:
        raise OperatorActionRefused(f"no such payment: {payment_id}")

    def refuse(reason: str) -> None:
        audit.record(
            db, failed_payment_id=payment_id, stage="operator", actor="human",
            detail={
                "operator": operator, "outcome": outcome, "note": note,
                "refused": True, "reason": reason,
            },
            now=now,
        )
        db.commit()
        raise OperatorActionRefused(reason)

    if outcome not in RESOLUTIONS:
        refuse(f"unrecognised resolution: {outcome}")

    if payment.status != PaymentStatus.HUMAN_REVIEW.value:
        # Covers double-resolution: the first resolve moves the case out of review, so
        # a second one finds it already closed rather than racing the first.
        refuse(f"case is not awaiting review (status: {payment.status})")

    # A human instruction does not outrank consent withdrawal. This is the check that
    # keeps the deterministic core from being bypassable by clicking a button.
    if payment.mandate_revoked and outcome in _RESOLUTIONS_IMPLYING_CONTACT:
        refuse("customer withdrew consent; no further contact is permitted")

    audit.record(
        db, failed_payment_id=payment_id, stage="operator", actor="human",
        detail={
            "operator": operator, "outcome": outcome, "note": note,
            "refused": False, "description": RESOLUTIONS[outcome],
        },
        now=now,
    )

    if outcome == "recovered_manually":
        payment.status = PaymentStatus.RECOVERED.value
        payment.recovered_at = now
        # Flagged so the causal measurement can exclude it. A case an operator rescued
        # by hand was not recovered BY the agent, and counting it in the intervention
        # arm would credit the system with a person's work.
        payment.recovered_manually = True
    elif outcome == "returned_to_agent":
        payment.status = PaymentStatus.WAITING.value
    else:
        payment.status = PaymentStatus.LOST.value

    db.commit()
    return payment


def pulse(db: Session) -> dict:
    """Counters the console header keeps live."""
    by_status = dict(
        db.execute(
            select(FailedPayment.status, func.count(FailedPayment.id)).group_by(
                FailedPayment.status
            )
        ).all()
    )
    return {
        "total": sum(by_status.values()),
        "by_status": by_status,
        "awaiting_review": by_status.get(PaymentStatus.HUMAN_REVIEW.value, 0),
        "in_flight": sum(by_status.get(s, 0) for s in _ACTIVE_STATUSES),
        "latest_event_id": db.scalar(select(func.max(AuditLog.id))),
    }
