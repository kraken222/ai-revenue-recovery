"""Operational guardrails layered on top of the compliance core. These are not legal
requirements (compliance.py already enforced those) — they're self-imposed limits that
protect customer goodwill and issuer relationships. They may only narrow the compliant
action set further, never widen it.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.compliance import ComplianceResult
from app.config import settings
from app.models import ActionLog, FailedPayment

_CUSTOMER_CONTACT_ACTIONS = {"retry_now", "retry_at", "send_payment_link", "request_new_mandate"}


@dataclass
class GuardrailResult:
    allowed_actions: list[str]
    blocked_reason: str | None
    policy_rule_id: str


def evaluate(
    db: Session, payment: FailedPayment, compliance: ComplianceResult, now: datetime | None = None
) -> GuardrailResult:
    now = now or datetime.now(timezone.utc)
    if not any(a in _CUSTOMER_CONTACT_ACTIONS for a in compliance.allowed_actions):
        # escalate_human / stop_lost — nothing for the guardrail layer to check.
        return GuardrailResult(
            allowed_actions=compliance.allowed_actions,
            blocked_reason=compliance.blocked_reason,
            policy_rule_id="GUARD-000-passthrough",
        )

    if _daily_contact_cap_hit(db, payment.customer_id, now):
        return GuardrailResult(
            allowed_actions=["wait"],
            blocked_reason="daily_contact_cap_reached",
            policy_rule_id="GUARD-001-contact-cap",
        )

    if payment.issuer_id and _issuer_circuit_open(db, payment.issuer_id, now):
        return GuardrailResult(
            allowed_actions=["wait"],
            blocked_reason="issuer_circuit_breaker_open",
            policy_rule_id="GUARD-002-issuer-circuit-breaker",
        )

    return GuardrailResult(
        allowed_actions=compliance.allowed_actions,
        blocked_reason=compliance.blocked_reason,
        policy_rule_id="GUARD-000-passthrough",
    )


def _daily_contact_cap_hit(db: Session, customer_id: str, now: datetime) -> bool:
    since = now - timedelta(days=1)
    count = db.scalar(
        select(func.count(ActionLog.id))
        .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
        .where(
            FailedPayment.customer_id == customer_id,
            ActionLog.action_taken.in_(_CUSTOMER_CONTACT_ACTIONS),
            ActionLog.executed_at >= since,
        )
    )
    return (count or 0) >= settings.daily_contact_cap


def _issuer_circuit_open(db: Session, issuer_id: str, now: datetime) -> bool:
    """Rolling-window decline rate for one issuer/BIN. If retries against this issuer
    are failing at an abnormal rate (likely an issuer-side outage rather than
    individually bad payments), stop hammering it and let it recover."""
    since = now - timedelta(minutes=settings.issuer_circuit_breaker_window_minutes)
    rows = db.execute(
        select(ActionLog.outcome)
        .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
        .where(
            FailedPayment.issuer_id == issuer_id,
            ActionLog.action_taken.in_({"retry_now", "retry_at"}),
            ActionLog.executed_at >= since,
            ActionLog.outcome != "pending",
        )
    ).all()

    total = len(rows)
    if total < settings.issuer_circuit_breaker_min_samples:
        return False

    declines = sum(1 for (outcome,) in rows if outcome == "failed")
    return (declines / total) >= settings.issuer_circuit_breaker_decline_rate_threshold
