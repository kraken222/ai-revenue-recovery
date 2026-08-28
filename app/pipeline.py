"""Ties ingestion -> decision -> execution together, and handles the outcome webhook
that closes the loop. Both entry points are idempotent on `event_id` so redelivered
Razorpay webhooks (which do happen) can never double-process the same event.

Note on payload shape: this expects an envelope shaped like a Razorpay webhook,
extended with fields (`rail`, `issuer_id`, `mandate_revoked`) that a real integration
would derive from Razorpay's actual payment/subscription entity (`method`, `card`,
`upi` sub-objects, error fields) plus your own customer/mandate records — Razorpay
doesn't hand you "rail" as a literal field. Kept flat here for the demo/synthetic data
generator.
"""

import hashlib
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import audit, bandit, promises
from app.config import settings
from app.decision_engine import decide
from app.executor import execute
from app.models import ActionLog, Decision, Event, FailedPayment, PaymentStatus
from app.timeutil import as_aware, utcnow

_FAILURE_EVENT_TYPES = {"payment.failed", "subscription.charged.failed"}


def _already_processed(db: Session, event_id: str) -> bool:
    return db.scalar(select(Event.id).where(Event.razorpay_event_id == event_id)) is not None


def _assign_control_group(payment_id: str) -> bool:
    """Deterministic holdout assignment by hashing the payment id.

    Deliberately not a live `random.random()` draw: that makes assignment depend on how
    many random numbers happened to be consumed before it, so the control arm's
    composition shifts whenever unrelated logic changes. Hashing the unit id keeps
    assignment reproducible, independent of processing order, and stable across policy
    variants — which is what makes an A/B comparison between them valid at all. This is
    how production experiment frameworks bucket units, for the same reason.

    Uses blake2b rather than hash() because Python salts str hashing per process.
    """
    digest = hashlib.blake2b(payment_id.encode(), digest_size=8).digest()
    bucket = int.from_bytes(digest, "big") / float(1 << 64)
    return bucket < settings.control_group_rate


def ingest_event(
    db: Session, event_id: str, event_type: str, payload: dict, now: datetime | None = None
) -> FailedPayment | None:
    now = as_aware(now) or utcnow()
    if _already_processed(db, event_id):
        return None

    db.add(Event(razorpay_event_id=event_id, event_type=event_type, payload=payload, received_at=now))
    audit.record(
        db, failed_payment_id=None, stage="ingestion", actor="system",
        detail={"event_id": event_id, "event_type": event_type},
        now=now,
    )

    if event_type not in _FAILURE_EVENT_TYPES:
        db.commit()
        return None

    p = payload["payment"]
    control_group = _assign_control_group(p["id"])

    payment = FailedPayment(
        razorpay_payment_id=p["id"],
        subscription_id=p.get("subscription_id"),
        customer_id=p["customer_id"],
        rail=p["rail"],
        amount_paise=p["amount_paise"],
        currency=p.get("currency", "INR"),
        error_code=p["error_code"],
        error_description=p.get("error_description", ""),
        issuer_id=p.get("issuer_id"),
        source=p.get("source", "failed_payment"),
        invoice_accepted_on=(
            datetime.fromisoformat(p["invoice_accepted_on"])
            if p.get("invoice_accepted_on")
            else None
        ),
        agreed_credit_days=p.get("agreed_credit_days"),
        supplier_is_msme=p.get("supplier_is_msme", False),
        # A real integration derives these from the subscription/token entity and the
        # merchant's own customer record; Razorpay does not hand them back on the
        # failure webhook. Absent, `retry_charge` refuses rather than improvising.
        mandate_token=p.get("mandate_token"),
        customer_email=p.get("customer_email"),
        customer_contact=p.get("customer_contact"),
        mandate_revoked=p.get("mandate_revoked", False),
        # Already revoked when we first saw it, so every subsequent contact is a breach.
        mandate_revoked_at=now if p.get("mandate_revoked", False) else None,
        control_group=control_group,
        first_failed_at=now,
    )
    db.add(payment)
    db.flush()

    audit.record(
        db, failed_payment_id=payment.id, stage="ingestion", actor="system",
        detail={"control_group": control_group, "rail": payment.rail, "error_code": payment.error_code},
        now=now,
    )

    decision = decide(db, payment, now)
    execute(db, payment, decision, now)
    db.commit()
    return payment


def ingest_outcome(
    db: Session, event_id: str, razorpay_payment_id: str, success: bool, now: datetime | None = None
) -> FailedPayment | None:
    now = as_aware(now) or utcnow()
    if _already_processed(db, event_id):
        return None

    db.add(Event(
        razorpay_event_id=event_id, event_type="recovery.outcome",
        payload={"razorpay_payment_id": razorpay_payment_id, "success": success},
        received_at=now,
    ))

    payment = db.scalar(
        select(FailedPayment)
        .where(FailedPayment.razorpay_payment_id == razorpay_payment_id)
        .order_by(FailedPayment.first_failed_at.desc())
    )
    if payment is None:
        db.commit()
        return None

    pending_action = db.scalar(
        select(ActionLog)
        .where(ActionLog.failed_payment_id == payment.id, ActionLog.outcome == "pending")
        .order_by(ActionLog.id.desc())
    )
    if pending_action:
        pending_action.outcome = "success" if success else "failed"

    audit.record(
        db, failed_payment_id=payment.id, stage="execution", actor="system",
        detail={"outcome_event": event_id, "success": success},
        now=now,
    )

    # Resolve any open promise against this outcome. Without this a promise stays open
    # forever once the payment lands, permanently suppressing outreach on a case that
    # is already closed — and a broken one is never counted, so the escalation ladder
    # never learns the customer has a pattern.
    promises.settle_promises(db, payment.id, paid=success, now=now)

    # Close the learning loop: attribute this outcome back to whichever bandit arm
    # chose the slot. Only decisions the bandit actually made carry an arm key, so
    # control-group and non-retry actions contribute nothing to training.
    acting_decision = db.scalar(
        select(Decision)
        .where(Decision.failed_payment_id == payment.id, Decision.bandit_arm_key.is_not(None))
        .order_by(Decision.id.desc())
    )
    if acting_decision and acting_decision.bandit_arm_key:
        bandit.update(db, acting_decision.bandit_arm_key, success)
        audit.record(
            db, failed_payment_id=payment.id, stage="bandit", actor="system",
            detail={"reward": 1 if success else 0, "arm_key": acting_decision.bandit_arm_key},
            now=now,
        )

    if success:
        payment.status = PaymentStatus.RECOVERED.value
        payment.recovered_at = now
        db.commit()
        return payment

    # Failed again — re-run the loop. Compliance will hit the retry cap / cooldown /
    # circuit breaker on its own and route to stop_lost or wait as appropriate.
    decision = decide(db, payment, now)
    execute(db, payment, decision, now)
    db.commit()
    return payment
