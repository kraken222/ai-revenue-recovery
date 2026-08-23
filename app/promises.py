"""Persistence and lifecycle for promises to pay.

Kept apart from `escalation.py`, which holds the pure rules and no database. The split
matters because the rules are the part worth testing exhaustively, and they should not
need a session to exercise.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit
from app.escalation import (
    PROMISE_GRACE_HOURS,
    PromiseState,
    promise_horizon_ok,
)
from app.models import PromiseToPay
from app.timeutil import as_aware, utcnow


def record_promise(
    db: Session,
    payment_id: str,
    promised_for: datetime,
    source: str = "customer",
    now: datetime | None = None,
) -> PromiseToPay | None:
    """Log a dated commitment. Returns None when the date is unusable — in the past, or
    so far out that it is a deferral rather than a plan — because accepting one would
    suppress outreach indefinitely on the strength of a date nobody intends to meet."""
    now = as_aware(now) or utcnow()
    promised_for = as_aware(promised_for)

    if not promise_horizon_ok(promised_for, now):
        audit.record(
            db, failed_payment_id=payment_id, stage="promise", actor="system",
            detail={"rejected": "outside_allowed_horizon", "promised_for": promised_for.isoformat()},
            now=now,
        )
        return None

    # Supersede any still-open promise rather than stacking them: two open promises
    # would each independently suppress outreach, and the later one would silently
    # extend the earlier one's hold.
    #
    # Superseded is NOT broken, and the distinction decides whether a customer meets a
    # human. A break is a date the customer missed; a supersede is a date they revised
    # before it arrived. Filing revisions as breaks means someone who rescheduled twice
    # trips the two-broken-promises rule and gets escalated for the crime of keeping us
    # informed.
    for existing in _open_promises(db, payment_id, now):
        existing.status = "superseded"
        existing.resolved_at = now

    promise = PromiseToPay(
        failed_payment_id=payment_id,
        promised_for=promised_for,
        source=source,
        status="open",
        created_at=now,
    )
    db.add(promise)
    db.flush()

    audit.record(
        db, failed_payment_id=payment_id, stage="promise", actor="system",
        detail={"recorded": promise.id, "promised_for": promised_for.isoformat(), "source": source},
        now=now,
    )
    return promise


def _open_promises(db: Session, payment_id: str, now: datetime) -> list[PromiseToPay]:
    # The session runs with autoflush=False, so a status set earlier in this
    # transaction is still only in memory and a query would read the stale row. That
    # bit for real: settle_promises() marked a promise `broken`, this query then read
    # it as still `open`, and record_promise() overwrote it to `superseded` — silently
    # erasing a missed promise so the escalation ladder never counted it and a customer
    # who missed two dates never reached a human.
    db.flush()
    return list(
        db.scalars(
            select(PromiseToPay).where(
                PromiseToPay.failed_payment_id == payment_id,
                PromiseToPay.status == "open",
            )
        )
    )


def settle_promises(db: Session, payment_id: str, paid: bool, now: datetime | None = None) -> None:
    """Resolve open promises. A payment closes them as kept; a matured date without
    payment closes them as broken. Promises whose date has not yet arrived stay open —
    they are not broken until the grace period lapses."""
    now = as_aware(now) or utcnow()
    for promise in _open_promises(db, payment_id, now):
        promised_for = as_aware(promise.promised_for)
        if paid:
            promise.status = "kept"
            promise.resolved_at = now
        elif now >= promised_for + timedelta(hours=PROMISE_GRACE_HOURS):
            promise.status = "broken"
            promise.resolved_at = now
        else:
            continue
        audit.record(
            db, failed_payment_id=payment_id, stage="promise", actor="system",
            detail={"promise_id": promise.id, "resolved": promise.status},
            now=now,
        )


def state_for(db: Session, payment_id: str, now: datetime | None = None) -> PromiseState:
    now = as_aware(now) or utcnow()
    promises = list(
        db.scalars(select(PromiseToPay).where(PromiseToPay.failed_payment_id == payment_id))
    )
    open_at = next(
        (as_aware(p.promised_for) for p in promises if p.status == "open"),
        None,
    )
    return PromiseState(
        open_promise_at=open_at,
        broken_count=sum(1 for p in promises if p.status == "broken"),
        kept_count=sum(1 for p in promises if p.status == "kept"),
    )
