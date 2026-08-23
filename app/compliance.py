"""Deterministic compliance core. Nothing in this module is ML- or LLM-driven, on
purpose: it defines the set of actions that are legally/contractually allowed, and
downstream stages (bandit in Sprint 2, guardrails below) may only narrow that set,
never widen it.

Regulatory basis (reconcile against current RBI circulars before production use):
- E-mandate recurring debits <= AFA_EXEMPT_CEILING can skip per-transaction 2FA given
  a one-time authenticated mandate registration; above it, a blind retry is not legal,
  the customer must re-authenticate, so the only compliant action is a fresh payment
  link / re-auth flow, not retry_now/retry_at.
- Mandate-based rails (UPI Autopay, eNACH) require advance notice before a debit
  attempt; PRE_DEBIT_NOTICE_HOURS enforces that floor on any scheduled retry.
- A revoked/cancelled mandate is a hard stop: the customer withdrew consent, so no
  further contact of any kind is compliant, regardless of category.
- Customer contact is confined to 08:00-19:00 IST by RBI's Fair Practices Code. Silent
  machine-to-machine debits are NOT contact and are unrestricted — see contact_policy.
- On cards, Razorpay's own dunning owns the retry and manual domestic-card charge is
  unsupported, so the merchant-side action there is to monitor, never to re-attempt.
"""

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app import contact_policy, receivables, sources
from app.config import settings
from app.models import Category, FailedPayment, Rail
from app.sources import Source
from app.timeutil import as_aware, utcnow

_MANDATE_RAILS = {Rail.UPI_AUTOPAY, Rail.ENACH}


@dataclass
class ComplianceResult:
    allowed_actions: list[str]
    earliest_slot: datetime | None
    blocked_reason: str | None
    policy_rule_id: str
    audit: dict = field(default_factory=dict)


def _evaluate_non_payment_source(
    payment: FailedPayment, profile, now: datetime
) -> ComplianceResult:
    """Abandoned checkouts and overdue invoices.

    Neither has a mandate, so no retry action exists on either — the only lever is
    asking. What separates them is whether anything is owed at all, and that decides
    how many times asking is acceptable.
    """
    contacts = payment.retry_count

    if contacts >= profile.max_contacts:
        return ComplianceResult(
            allowed_actions=["stop_lost"],
            earliest_slot=None,
            blocked_reason="source_contact_budget_exhausted",
            policy_rule_id="COMP-010-source-contact-budget",
            audit={"source": profile.source.value, "max_contacts": profile.max_contacts},
        )

    if profile.source is Source.ABANDONED_CHECKOUT:
        # One nudge, and it is a nudge rather than a dunning message: no payment was
        # ever attempted, so "your payment failed" would simply be untrue, and a
        # non-debtor gets no ladder above it.
        slot, deferral = contact_policy.constrain("send_payment_link", now)
        return ComplianceResult(
            allowed_actions=["send_payment_link"],
            earliest_slot=slot,
            blocked_reason=None,
            policy_rule_id="COMP-011-checkout-nudge-not-a-debt"
            if deferral is None
            else "COMP-008-rbi-contact-window",
            audit={
                "source": profile.source.value,
                "is_debt": False,
                "note": "no debt owed; marketing-consent rules apply, not debt collection",
                **({} if deferral is None else {"deferred": deferral}),
            },
        )

    # Overdue B2B invoice. The escalation here is precision about what the MSMED Act
    # already provides, not pressure.
    accrual = None
    if payment.invoice_accepted_on and payment.supplier_is_msme:
        accrual = receivables.accrue(
            principal_paise=payment.amount_paise,
            accepted_on=receivables.as_of_date(as_aware(payment.invoice_accepted_on)),
            as_of=receivables.as_of_date(now),
            agreed_credit_days=payment.agreed_credit_days,
        )

    slot, deferral = contact_policy.constrain("send_payment_link", now)
    audit = {"source": profile.source.value, "contacts_made": contacts}
    if accrual:
        audit.update(
            {
                "appointed_day": accrual.appointed_day.isoformat(),
                "days_overdue": accrual.days_overdue,
                "statutory_interest_paise": accrual.interest_paise,
                "severity": receivables.severity(accrual),
                "tax_deduction_at_risk": accrual.tax_deduction_at_risk,
            }
        )
    if deferral:
        audit["deferred"] = deferral

    return ComplianceResult(
        allowed_actions=["send_payment_link"],
        earliest_slot=slot,
        blocked_reason=None,
        policy_rule_id="COMP-012-msme-receivable"
        if deferral is None
        else "COMP-008-rbi-contact-window",
        audit=audit,
    )


def evaluate(payment: FailedPayment, category: Category, now: datetime | None = None) -> ComplianceResult:
    now = as_aware(now) or utcnow()
    rail = Rail(payment.rail)
    profile = sources.profile_for(getattr(payment, "source", None) or Source.FAILED_PAYMENT)

    # Sources that are not a failed subscription charge have their own, narrower
    # regimes and are decided in full before the mandate logic below — which assumes an
    # instrument exists to debit, and on those sources none does.
    if profile.source is not Source.FAILED_PAYMENT:
        return _evaluate_non_payment_source(payment, profile, now)

    if payment.mandate_revoked:
        return ComplianceResult(
            allowed_actions=["stop_lost"],
            earliest_slot=None,
            blocked_reason="mandate_revoked_by_customer",
            policy_rule_id="COMP-001-consent-withdrawn",
        )

    if category in (Category.RISK_BLOCK, Category.UNKNOWN):
        return ComplianceResult(
            allowed_actions=["escalate_human"],
            earliest_slot=None,
            blocked_reason=None if category == Category.RISK_BLOCK else "classification_below_confidence_floor",
            policy_rule_id="COMP-002-risk-or-unclassified",
        )

    # Attempt cap applies uniformly from here on — a hard decline that keeps sending
    # payment links the customer never completes is exactly as unbounded a loop as an
    # unlimited retry, so it goes through the same cap, not a separate unbounded path.
    if payment.retry_count >= settings.max_retry_attempts:
        return ComplianceResult(
            allowed_actions=["stop_lost"],
            earliest_slot=None,
            blocked_reason="max_retry_attempts_exhausted",
            policy_rule_id="COMP-004-attempt-cap",
        )

    if category == Category.HARD_DECLINE:
        action = "send_payment_link" if rail == Rail.CARD else "request_new_mandate"
        slot, deferral = contact_policy.constrain(action, now)
        return ComplianceResult(
            allowed_actions=[action],
            earliest_slot=slot,
            blocked_reason=None,
            policy_rule_id="COMP-003-instrument-dead-needs-fresh-auth"
            if deferral is None
            else "COMP-008-rbi-contact-window",
            audit={} if deferral is None else {"deferred": deferral, "from": now.isoformat()},
        )

    if payment.amount_paise > settings.afa_exempt_ceiling_paise:
        # Above the AFA-exemption ceiling a blind merchant-initiated retry is not
        # compliant — the customer has to re-authenticate.
        slot, deferral = contact_policy.constrain("send_payment_link", now)
        return ComplianceResult(
            allowed_actions=["send_payment_link"],
            earliest_slot=slot,
            blocked_reason=None,
            policy_rule_id="COMP-005-afa-required-above-ceiling",
            audit={
                "amount_paise": payment.amount_paise,
                "ceiling": settings.afa_exempt_ceiling_paise,
                **({} if deferral is None else {"deferred": deferral}),
            },
        )

    # A merchant-initiated retry is only a real action on mandate rails, where we hold
    # a token and initiate the debit ourselves. On cards it is not: Razorpay runs its
    # own dunning (auto-retry the following day, until the subscription halts), and
    # manual charge of a domestic card is not supported at all. Issuing our own retry
    # there would either duplicate an attempt Razorpay is already making — burning
    # issuer goodwill on a decline the network already saw — or call an API that does
    # not exist. Monitoring is the honest action while the gateway owns the retry.
    if rail == Rail.CARD and not payment.gateway_exhausted:
        return ComplianceResult(
            allowed_actions=["monitor_gateway_retry"],
            earliest_slot=now,
            blocked_reason=None,
            policy_rule_id="COMP-007-gateway-owns-card-retry",
            audit={
                "note": "Razorpay auto-retries card subscription charges; merchant retry would double-attempt",
                "gateway_retries_so_far": payment.gateway_retry_count,
            },
        )

    if rail == Rail.CARD:
        # The gateway has exhausted its own retries and the subscription has halted.
        # This is the handover: merchant-side recovery starts exactly where Razorpay's
        # dunning stops, which is why a payment link (not a retry) is the action —
        # manual charge of a domestic card is not supported at all.
        slot, deferral = contact_policy.constrain("send_payment_link", now)
        return ComplianceResult(
            allowed_actions=["send_payment_link"],
            earliest_slot=slot,
            blocked_reason=None,
            policy_rule_id="COMP-009-gateway-exhausted-merchant-takes-over"
            if deferral is None
            else "COMP-008-rbi-contact-window",
            audit={"gateway_retries": payment.gateway_retry_count,
                   **({} if deferral is None else {"deferred": deferral})},
        )

    last_attempt_at = as_aware(payment.last_attempt_at)
    cooldown_floor = (
        last_attempt_at + timedelta(hours=settings.retry_cooldown_hours) if last_attempt_at else now
    )
    notice_floor = now + timedelta(hours=settings.pre_debit_notice_hours) if rail in _MANDATE_RAILS else now
    earliest_slot = max(cooldown_floor, notice_floor)

    action = "retry_now" if earliest_slot <= now else "retry_at"
    return ComplianceResult(
        allowed_actions=[action],
        earliest_slot=earliest_slot,
        blocked_reason=None,
        policy_rule_id="COMP-006-compliant-retry-window",
        audit={"cooldown_floor": cooldown_floor.isoformat(), "notice_floor": notice_floor.isoformat()},
    )
