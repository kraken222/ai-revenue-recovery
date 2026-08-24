"""Orchestrates classification -> compliance -> guardrails -> bandit -> EV gate -> action.

The ordering encodes the central design rule: **deterministic core, learned edge.**

    classify        what kind of failure is this            rules, then LLM ensemble
    compliance      what is LEGALLY allowed                 deterministic, never learned
    guardrails      what is OPERATIONALLY wise               deterministic, never learned
    bandit          which allowed slot is best               learned (Thompson Sampling)
    EV gate         is one more attempt worth its cost       learned posterior + economics

Each stage may only narrow what the previous one permitted. The bandit cannot invent an
action compliance forbade, and the EV gate can only ever downgrade to `stop_lost`. So
every money action remains explainable and bounded even though part of the policy is
learned — which is the property the whole design exists to hold.

Holdout: payments flagged `control_group` get the compliant action computed and logged
(so we know what the system *would* have done) but the executed action is forced to
`control_no_action`, and they contribute no bandit reward. That keeps the control arm a
clean counterfactual, which is what makes the recovered-vs-control number a real causal
estimate instead of a raw recovery count.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from app import audit, bandit, contact_policy, economics, escalation, promises, sources
from app.classifier import classify
from app.compliance import evaluate as evaluate_compliance
from app.config import settings
from app.guardrails import evaluate as evaluate_guardrails
from app.models import Classification, Decision, FailedPayment, Rail
from app.timeutil import as_aware, utcnow

# Actions that cost money/goodwill to attempt, and so must clear the EV gate.
_CONTACT_ACTIONS = {"retry_now", "retry_at", "send_payment_link", "request_new_mandate"}


@dataclass
class DecisionOutcome:
    action: str
    scheduled_at: datetime | None
    decision_id: int


def decide(db: Session, payment: FailedPayment, now: datetime | None = None) -> DecisionOutcome:
    now = as_aware(now) or utcnow()
    result = classify(
        Rail(payment.rail),
        payment.error_code,
        payment.error_description,
        source=getattr(payment, "source", None) or "failed_payment",
    )
    db.add(
        Classification(
            failed_payment_id=payment.id,
            category=result.category.value,
            confidence=result.confidence,
            source=result.source,
            raw_model_output=result.raw,
            created_at=now,
        )
    )
    audit.record(
        db,
        failed_payment_id=payment.id,
        stage="classification",
        # "llm" only when a model actually ran. A `rule_miss` means the table missed
        # AND no model was available, so attributing it to the LLM would credit an
        # actor that never acted — in an audit trail that is a correctness bug, not a
        # label nit.
        actor="llm" if result.source in ("llm", "llm_rejected") else "system",
        detail=_classification_audit(result),
        now=now,
    )

    compliance = evaluate_compliance(payment, result.category, now)
    audit.record(
        db,
        failed_payment_id=payment.id,
        stage="compliance",
        actor="system",
        detail={
            "allowed_actions": compliance.allowed_actions,
            "blocked_reason": compliance.blocked_reason,
            "policy_rule_id": compliance.policy_rule_id,
            "earliest_slot": compliance.earliest_slot.isoformat() if compliance.earliest_slot else None,
            **compliance.audit,
        },
        now=now,
    )

    guardrail = evaluate_guardrails(db, payment, compliance, now)
    audit.record(
        db,
        failed_payment_id=payment.id,
        stage="guardrail",
        actor="system",
        detail={
            "allowed_actions": guardrail.allowed_actions,
            "blocked_reason": guardrail.blocked_reason,
            "policy_rule_id": guardrail.policy_rule_id,
        },
        now=now,
    )

    compliant_action = guardrail.allowed_actions[0]
    scheduled_at = _resolve_scheduled_at(compliant_action, guardrail.policy_rule_id, compliance.earliest_slot, now)
    chosen_arm_key: str | None = None
    verdict = None

    # An open promise to pay suppresses outreach until it matures. Chasing someone who
    # has already committed to a date is how a customer who intended to pay is lost.
    # This can only ever hold a contact back — it never creates one, never shortens a
    # compliance window, and cannot reopen a case compliance already stopped.
    promise_state = promises.state_for(db, payment.id, now)
    rung, rung_reason = escalation.rung_for(
        attempt=payment.retry_count,
        broken_promises=promise_state.broken_count,
        gateway_owns_retry=(compliant_action == "monitor_gateway_retry"),
    )
    if contact_policy.is_contact_action(compliant_action):
        suppression = escalation.evaluate_promise(promise_state, now)
        if suppression.suppressed:
            audit.record(
                db, failed_payment_id=payment.id, stage="promise", actor="system",
                detail={
                    "suppressed_action": compliant_action,
                    "reason": suppression.reason,
                    "release_at": suppression.release_at.isoformat(),
                },
                now=now,
            )
            compliant_action = "wait"
            scheduled_at = suppression.release_at

    audit.record(
        db, failed_payment_id=payment.id, stage="escalation", actor="system",
        detail={
            "rung": rung.level,
            "name": rung.name,
            "summary": escalation.describe(rung, promise_state),
            "reached_because": rung_reason,
            "broken_promises": promise_state.broken_count,
            "contacts_made": payment.retry_count,
        },
        now=now,
    )

    # Bandit picks the retry slot, but only among slots compliance already allows, and
    # only when a retry is the standing compliant action.
    if settings.bandit_enabled and compliant_action in ("retry_now", "retry_at") and compliance.earliest_slot:
        slots = bandit.candidate_slots(compliance.earliest_slot, settings.max_retry_window_hours)
        choice = bandit.select_slot(db, payment.rail, result.category.value, slots)
        if choice:
            scheduled_at = choice.scheduled_at
            chosen_arm_key = choice.arm_key
            compliant_action = "retry_now" if choice.scheduled_at <= now else "retry_at"
            audit.record(
                db,
                failed_payment_id=payment.id,
                stage="bandit",
                actor="system",
                detail={
                    "arm_key": choice.arm_key,
                    "tod_bucket": choice.tod_bucket,
                    "posterior_mean": round(choice.posterior_mean, 4),
                    "thompson_samples": choice.samples,
                    "scheduled_at": choice.scheduled_at.isoformat(),
                },
                now=now,
            )

    # Expected-value gate: is one more attempt worth its cost, given what we've learned
    # about this slot and how many attempts have already failed?
    if compliant_action in _CONTACT_ACTIONS:
        posterior = (
            bandit.get_or_create_arm(db, chosen_arm_key).posterior_mean
            if chosen_arm_key
            else settings.hard_decline_recovery_prior
        )
        # LTV is a property of the SOURCE, not a global constant: a lost subscription
        # forfeits every future period, a settled invoice forfeits nothing of the kind.
        verdict = economics.assess(
            posterior,
            payment.retry_count,
            payment.amount_paise,
            ltv_multiple=sources.profile_for(
                getattr(payment, "source", None) or "failed_payment"
            ).ltv_multiple,
        )
        audit.record(
            db,
            failed_payment_id=payment.id,
            stage="economics",
            actor="system",
            detail={
                "p_recovery": round(verdict.p_recovery, 4),
                "expected_value_paise": round(verdict.expected_value_paise, 2),
                "amount_paise": payment.amount_paise,
                "retry_count": payment.retry_count,
                "verdict": verdict.reason,
            },
            now=now,
        )
        if not verdict.should_attempt:
            compliant_action = "stop_lost"
            scheduled_at = None
            chosen_arm_key = None

    # Second broken promise is a pattern, not an accident, and the ladder's top rung is
    # a human rather than more pressure — RBI's code escalates involvement, never force.
    if rung.level == 3 and compliant_action in _CONTACT_ACTIONS:
        audit.record(
            db, failed_payment_id=payment.id, stage="escalation", actor="system",
            detail={"escalated_from": compliant_action, "reason": rung_reason},
            now=now,
        )
        compliant_action = "escalate_human"
        scheduled_at = None
        chosen_arm_key = None

    would_be_action = compliant_action

    # The holdout withholds the *intervention*, nothing else. A compliance hard-stop
    # (revoked mandate, risk block, attempt cap) is the absence of an intervention, not
    # one, so it must land identically in both arms — otherwise the two arms end in
    # different terminal states and the recovered-vs-control comparison is biased.
    # Only genuine contact actions get suppressed for control payments.
    suppress_for_control = payment.control_group and compliant_action in _CONTACT_ACTIONS
    final_action = "control_no_action" if suppress_for_control else compliant_action
    if suppress_for_control:
        # Control payments also contribute no reward signal, keeping the bandit's
        # training set strictly to actions it actually chose and executed.
        scheduled_at = None
        chosen_arm_key = None

    decision = Decision(
        failed_payment_id=payment.id,
        action=final_action,
        scheduled_at=scheduled_at,
        compliant_action_set=guardrail.allowed_actions,
        policy_rule_id=guardrail.policy_rule_id,
        compliance_rule_id=compliance.policy_rule_id,
        blocked_reason=guardrail.blocked_reason,
        bandit_arm_key=chosen_arm_key,
        expected_value_paise=verdict.expected_value_paise if verdict else None,
        created_at=now,
    )
    db.add(decision)
    db.flush()  # assign decision.id without committing

    audit.record(
        db,
        failed_payment_id=payment.id,
        stage="decision",
        actor="system",
        detail={
            "would_be_action": would_be_action,
            "final_action": final_action,
            "control_group": payment.control_group,
            "bandit_arm_key": chosen_arm_key,
            "decision_id": decision.id,
        },
        now=now,
    )

    return DecisionOutcome(action=final_action, scheduled_at=decision.scheduled_at, decision_id=decision.id)


def _classification_audit(result) -> dict:
    """Carry the ensemble's votes into the audit trail, not just its verdict.

    A rejected classification is the interesting case: "three probes looked at this
    and split two ways" is what justifies the escalation to a human, and an audit
    entry that recorded only `category: unknown` would lose exactly the evidence an
    operator needs to resolve it.
    """
    detail = {
        "category": result.category.value,
        "confidence": round(result.confidence, 3),
        "source": result.source,
    }
    if result.source in ("llm", "llm_rejected") and result.raw:
        detail["agreement"] = result.raw.get("agreement")
        detail["ensemble_verdict"] = result.raw.get("reason")
        detail["votes"] = [
            {k: v for k, v in vote.items() if k != "reasoning"}
            for vote in result.raw.get("votes", [])
        ]
    return detail


def _resolve_scheduled_at(
    final_action: str,
    guardrail_policy_rule_id: str,
    compliance_earliest_slot: datetime | None,
    now: datetime,
) -> datetime | None:
    """When does this decision next become actionable, if ever? Distinct from
    compliance's `earliest_slot` because a guardrail (not a compliance rule) can push
    the actionable time out further — e.g. a daily contact cap or an open circuit
    breaker isn't "retry at this exact compliant instant", it's "check back later"."""
    if final_action in ("control_no_action", "escalate_human", "stop_lost"):
        return None
    if guardrail_policy_rule_id == "GUARD-001-contact-cap":
        return now + timedelta(hours=24)
    if guardrail_policy_rule_id == "GUARD-002-issuer-circuit-breaker":
        return now + timedelta(minutes=settings.issuer_circuit_breaker_window_minutes)
    return compliance_earliest_slot
