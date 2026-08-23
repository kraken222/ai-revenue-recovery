"""Polls for FailedPayments sitting in WAITING whose scheduled slot has arrived and
moves them forward. Run this on an interval (cron/scheduler) in a real deployment; for
the hackathon demo it's invoked directly by scripts/seed_synthetic_data.py after
fast-forwarding synthetic timestamps, since nothing here should require sleeping in
real time to observe a full retry cycle.

Two distinct cases reach WAITING, and they need different handling:
- action == retry_now/retry_at: compliance already picked the action, the slot just
  hadn't arrived yet — re-execute the *same* decision once it's due.
- action == wait: a guardrail (daily contact cap, issuer circuit breaker), not
  compliance, deferred this — the thing that was true when it deferred may no longer
  hold, so re-run the full decide() cycle rather than replaying a no-op "wait".
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.decision_engine import DecisionOutcome, decide
from app.executor import execute
from app.models import Decision, FailedPayment, PaymentStatus
from app.timeutil import as_aware, utcnow


def process_due_retries(db: Session, now: datetime | None = None) -> int:
    now = as_aware(now) or utcnow()
    waiting = db.scalars(
        select(FailedPayment).where(FailedPayment.status == PaymentStatus.WAITING.value)
    ).all()

    processed = 0
    for payment in waiting:
        last_decision = db.scalar(
            select(Decision)
            .where(Decision.failed_payment_id == payment.id)
            .order_by(Decision.id.desc())
        )
        if not last_decision or not last_decision.scheduled_at:
            continue
        if as_aware(last_decision.scheduled_at) > now:
            continue

        if last_decision.action in ("retry_now", "retry_at"):
            execute(
                db,
                payment,
                DecisionOutcome(
                    action=last_decision.action,
                    scheduled_at=last_decision.scheduled_at,
                    decision_id=last_decision.id,
                ),
                now=now,
            )
            processed += 1
        elif last_decision.action == "wait":
            new_decision = decide(db, payment, now=now)
            execute(db, payment, new_decision, now=now)
            processed += 1

    db.commit()
    return processed
