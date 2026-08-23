"""Read-side aggregations for the dashboard.

Kept separate from the decision path: nothing here influences a money action, so it can
query freely without worrying about affecting policy.
"""

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import sources
from app.config import settings
from app.models import ActionLog, BanditArm, Classification, Decision, FailedPayment, PaymentStatus
from app.sources import Source

_RESOLVED = (PaymentStatus.RECOVERED.value, PaymentStatus.LOST.value)


def _recovery_rate(payments) -> dict:
    resolved = [p for p in payments if p.status in _RESOLVED]
    recovered = [p for p in resolved if p.status == PaymentStatus.RECOVERED.value]
    return {
        "total": len(payments),
        "resolved": len(resolved),
        "recovered": len(recovered),
        "rate": (len(recovered) / len(resolved)) if resolved else None,
    }


def overview(db: Session) -> dict:
    payments = list(db.scalars(select(FailedPayment)))

    intervention = _recovery_rate([p for p in payments if not p.control_group])
    control = _recovery_rate([p for p in payments if p.control_group])
    lift = (
        intervention["rate"] - control["rate"]
        if intervention["rate"] is not None and control["rate"] is not None
        else None
    )

    contact_counts = dict(
        db.execute(
            select(ActionLog.failed_payment_id, func.count(ActionLog.id)).group_by(
                ActionLog.failed_payment_id
            )
        ).all()
    )
    contacts = sum(contact_counts.values())
    gross = sum(p.amount_paise for p in payments if p.status == PaymentStatus.RECOVERED.value)
    churned = [p for p in payments if p.churned_from_dunning]
    churn_loss = sum(p.amount_paise * settings.ltv_multiple for p in churned)

    return {
        "intervention": intervention,
        "control": control,
        "causal_lift": lift,
        "by_status": dict(
            db.execute(
                select(FailedPayment.status, func.count(FailedPayment.id)).group_by(
                    FailedPayment.status
                )
            ).all()
        ),
        "by_source": dict(
            db.execute(
                select(FailedPayment.source, func.count(FailedPayment.id)).group_by(
                    FailedPayment.source
                )
            ).all()
        ),
        "by_rail": dict(
            db.execute(
                select(FailedPayment.rail, func.count(FailedPayment.id)).group_by(FailedPayment.rail)
            ).all()
        ),
        "economics": {
            "gross_recovered_paise": gross,
            "contacts": contacts,
            "contact_spend_paise": contacts * settings.contact_cost_paise,
            "churned_customers": len(churned),
            "churn_loss_paise": churn_loss,
            "net_paise": gross - contacts * settings.contact_cost_paise - churn_loss,
        },
    }


def compliance_invariants(db: Session) -> list[dict]:
    """Each invariant is measured against actions actually executed, never inferred
    from a payment's status — status can be reached by paths that never contacted
    anyone, so it cannot answer whether a rule was breached."""

    def contacted_where(*conditions) -> int:
        stmt = (
            select(func.count(ActionLog.id))
            .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
        )
        for condition in conditions:
            stmt = stmt.where(condition)
        return db.scalar(stmt) or 0

    # Contacts executed strictly AFTER consent was withdrawn. Comparing against the
    # revocation timestamp rather than the boolean is the whole point: a customer can
    # revoke after a contact that was entirely legal when it was made, and counting
    # those as breaches makes the invariant unpassable for reasons that are not
    # violations.
    contacted_after_revocation = (
        db.scalar(
            select(func.count(ActionLog.id))
            .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
            .where(
                FailedPayment.mandate_revoked_at.is_not(None),
                ActionLog.executed_at > FailedPayment.mandate_revoked_at,
            )
        )
        or 0
    )

    # DISTINCT because a payment accumulates one Classification per decide() cycle;
    # a plain join would multiply each contact by the number of times it was classified
    # and report inflated breach counts.
    risk_contacted = (
        db.scalar(
            select(func.count(func.distinct(ActionLog.id)))
            .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
            .join(Classification, Classification.failed_payment_id == FailedPayment.id)
            .where(Classification.category == "risk_block")
        )
        or 0
    )
    over_cap = (
        db.scalar(
            select(func.count(FailedPayment.id)).where(
                FailedPayment.retry_count > settings.max_retry_attempts
            )
        )
        or 0
    )

    # Source-specific ceilings. An abandoned checkout is not a debt, so contacting one
    # twice is not a smaller version of the same rule — it is a different rule being
    # broken, and it needs its own assertion rather than sharing the attempt cap.
    checkout_over_budget = (
        db.scalar(
            select(func.count(FailedPayment.id)).where(
                FailedPayment.source == Source.ABANDONED_CHECKOUT.value,
                FailedPayment.retry_count > sources.profile_for(Source.ABANDONED_CHECKOUT).max_contacts,
            )
        )
        or 0
    )

    # No mandate exists on these sources, so a retry could not have been legitimate
    # even once. Asserted on executed actions, not on what compliance intended.
    retried_without_mandate = (
        db.scalar(
            select(func.count(ActionLog.id))
            .join(FailedPayment, FailedPayment.id == ActionLog.failed_payment_id)
            .where(
                FailedPayment.source.in_(
                    [Source.ABANDONED_CHECKOUT.value, Source.OVERDUE_INVOICE.value]
                ),
                ActionLog.action_taken.in_(["retry_now", "retry_at"]),
            )
        )
        or 0
    )

    checks = [
        ("contacted after mandate revocation", contacted_after_revocation),
        ("control-group payments contacted", contacted_where(FailedPayment.control_group.is_(True))),
        ("payments exceeding attempt cap", over_cap),
        ("risk-blocked payments auto-actioned", risk_contacted),
        ("abandoned checkouts contacted more than once", checkout_over_budget),
        ("retry attempted without a mandate", retried_without_mandate),
    ]
    return [{"check": name, "violations": count, "pass": count == 0} for name, count in checks]


def _wilson_interval(alpha: float, beta: float, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for the arm's success rate.

    The mean alone is the claim without the quantity: an arm at 3 pulls and one at 30
    can share a mean while warranting completely different confidence, and a bandit
    surface that hides that is showing a point estimate under the word "posterior".

    Wilson rather than a normal approximation, specifically because these arms live at
    low n where the normal approximation is not merely coarse but wrong in a direction
    that flatters the result — on Beta(5,3) it puts the upper bound at 94% against an
    exact ~89%, overstating an arm's ceiling on the strength of six pulls. On a page
    whose whole argument is honest measurement, an interval that overstates confidence
    is the first number a statistically literate reader will check. Wilson is closed
    form, stays inside [0,1] by construction, and needs no SciPy.

    Arms carry Beta(1,1) priors, so successes/failures are alpha-1 and beta-1.
    """
    successes = alpha - 1.0
    failures = beta - 1.0
    n = successes + failures
    if n <= 0:
        return 0.0, 1.0

    p = successes / n
    denom = 1.0 + z**2 / n
    centre = (p + z**2 / (2 * n)) / denom
    half = (z / denom) * (((p * (1 - p) + z**2 / (4 * n)) / n) ** 0.5)
    return max(0.0, centre - half), min(1.0, centre + half)


def bandit_arms(db: Session) -> list[dict]:
    arms = db.scalars(select(BanditArm).order_by(BanditArm.key)).all()
    out = []
    for a in arms:
        if a.pulls <= 0:
            continue
        lo, hi = _wilson_interval(a.alpha, a.beta)
        out.append(
            {
                "rail": a.rail,
                "category": a.category,
                "tod_bucket": a.tod_bucket,
                "slot": f"{a.tod_bucket:02d}:00-{a.tod_bucket + 6:02d}:00",
                "pulls": a.pulls,
                "alpha": a.alpha,
                "beta": a.beta,
                "posterior_mean": a.posterior_mean,
                "ci_low": lo,
                "ci_high": hi,
            }
        )
    return out


def stop_reasons(db: Session) -> list[dict]:
    """Reports the COMPLIANCE rule, falling back to the guardrail one.

    Guardrails run last and pass most cases straight through, so grouping on
    `policy_rule_id` alone labels nearly everything GUARD-000-passthrough and hides the
    rule that actually shaped the decision.
    """
    rule = func.coalesce(Decision.compliance_rule_id, Decision.policy_rule_id)
    rows = db.execute(
        select(rule, Decision.blocked_reason, func.count(Decision.id))
        .where(Decision.action == "stop_lost")
        .group_by(rule, Decision.blocked_reason)
        .order_by(func.count(Decision.id).desc())
    ).all()
    return [
        {"policy_rule_id": r, "reason": reason or "negative_expected_value", "count": count}
        for r, reason, count in rows
    ]


def rules_fired(db: Session) -> list[dict]:
    """Every compliance rule that shaped a decision, not only the stopping ones. This
    is where the rail-aware and contact-window rules become visible — they change what
    the system does without ever ending a case."""
    rule = func.coalesce(Decision.compliance_rule_id, Decision.policy_rule_id)
    rows = db.execute(
        select(rule, Decision.action, func.count(Decision.id))
        .group_by(rule, Decision.action)
        .order_by(func.count(Decision.id).desc())
    ).all()
    return [{"rule": r, "action": action, "count": count} for r, action, count in rows]
