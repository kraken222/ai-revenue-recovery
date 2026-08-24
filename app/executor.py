"""Executes the decided action against Razorpay (or a dry-run stand-in), then records
the outcome. Kept behind a small gateway interface so a real credential swap-in doesn't
touch the pipeline logic above it.

What is and is not a real Razorpay call, checked against the docs rather than assumed:

- `send_payment_link` -> Payment Links API. Verified and documented.
  https://razorpay.com/docs/api/payment-links/
- `monitor_gateway_retry` -> deliberately NO call. Razorpay auto-retries a failed
  subscription charge itself (the following day, until the subscription halts), and
  manual charge of a domestic card is not supported at all. Issuing our own card retry
  would either duplicate an attempt the gateway is already making or invoke an API that
  does not exist. Compliance routes cards here; the honest action is to wait and read
  the outcome webhook. https://razorpay.com/docs/subscriptions/payment-retries/
- `retry_charge` -> only reachable on UPI Autopay / eNACH, where the merchant holds a
  token and genuinely initiates the debit. Still marked unverified: the exact
  recurring-charge call needs confirming against the live account before go-live.
- `request_new_mandate` -> the documented flow is customer -> order with token details
  -> authorisation payment, which returns a new token_id. Not a single call, so it
  stays unimplemented rather than faked.
  https://razorpay.com/docs/payments/payment-gateway/s2s-integration/recurring-payments/upi/
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app import audit
from app.config import settings
from app.decision_engine import DecisionOutcome
from app.models import ActionLog, FailedPayment, PaymentStatus
from app.timeutil import as_aware, utcnow

logger = logging.getLogger(__name__)


@dataclass
class GatewayResult:
    outcome: str  # "success" | "failed" | "pending"
    ref: str | None
    message: str | None = None


class DryRunGateway:
    """No network calls. Used whenever Razorpay credentials aren't configured, so the
    whole pipeline is runnable and demoable with zero external dependencies."""

    def send_payment_link(self, payment: FailedPayment) -> GatewayResult:
        logger.info("[dry-run] would create payment link for %s", payment.razorpay_payment_id)
        return GatewayResult(outcome="pending", ref=f"dryrun_link_{payment.id[:8]}")

    def request_new_mandate(self, payment: FailedPayment) -> GatewayResult:
        logger.info("[dry-run] would request new mandate for %s", payment.razorpay_payment_id)
        return GatewayResult(outcome="pending", ref=f"dryrun_mandate_{payment.id[:8]}")

    def retry_charge(self, payment: FailedPayment) -> GatewayResult:
        logger.info("[dry-run] would retry charge for %s", payment.razorpay_payment_id)
        return GatewayResult(outcome="pending", ref=f"dryrun_retry_{payment.id[:8]}")



class UnsafeCredentials(Exception):
    """Live credentials in a system whose data is synthetic."""


class RazorpayGateway:
    """Real Razorpay test-mode calls. Only send_payment_link is a confirmed API;
    retry_charge is a placeholder — see module docstring."""

    def __init__(self, key_id: str, key_secret: str):
        import razorpay

        self._client = razorpay.Client(auth=(key_id, key_secret))

    def send_payment_link(self, payment: FailedPayment) -> GatewayResult:
        try:
            link = self._client.payment_link.create(
                {
                    "amount": payment.amount_paise,   # smallest unit; rupees would be 1/100th
                    "currency": payment.currency,
                    "description": f"Payment for {payment.razorpay_payment_id}",
                    # Razorpay mints a NEW link on every create call, so a redelivered
                    # webhook or a worker re-run would send one customer several
                    # different links for a single debt. A stable reference_id keyed on
                    # the payment makes the second attempt collide server-side instead.
                    "reference_id": self.reference_for(payment),
                    "notify": {"sms": True, "email": True},
                    "reminder_enable": False,   # our escalation ladder owns the cadence
                    "notes": {
                        "razorpay_payment_id": payment.razorpay_payment_id,
                        "internal_id": payment.id,
                        "source": payment.source,
                    },
                }
            )
        except Exception as exc:
            return self._classify_failure(exc)

        return GatewayResult(outcome="pending", ref=link.get("id"), message=link.get("short_url"))

    @staticmethod
    def reference_for(payment: FailedPayment) -> str:
        """One stable id per debt, so a repeated create is a collision rather than a
        second link."""
        return f"rcv-{payment.razorpay_payment_id}"

    @staticmethod
    def _classify_failure(exc: Exception) -> GatewayResult:
        """A 4xx tells us the request was wrong, so nothing was created and `failed` is
        accurate. A 5xx tells us nothing at all — the link may well exist — so it is
        recorded as `pending`. Calling that `failed` would let a retry raise a second
        link against the same debt.
        """
        import razorpay.errors as rzp

        if isinstance(exc, (rzp.ServerError, rzp.GatewayError)):
            outcome = "pending"
        elif isinstance(exc, rzp.BadRequestError):
            outcome = "failed"
        else:
            # An unrecognised failure is treated as unknown rather than as a definite
            # failure, for the same reason as a 5xx.
            outcome = "pending"

        logger.warning("razorpay call failed (%s): %s", type(exc).__name__, exc)
        return GatewayResult(outcome=outcome, ref=None, message=f"{type(exc).__name__}: {exc}")

    def request_new_mandate(self, payment: FailedPayment) -> GatewayResult:
        # TODO: verify current Razorpay Subscriptions/eNACH registration-link API
        # (https://razorpay.com/docs/api/payments/subscriptions/) before go-live.
        raise NotImplementedError("request_new_mandate needs a verified Razorpay API call")

    def retry_charge(self, payment: FailedPayment) -> GatewayResult:
        # Only reachable on UPI Autopay / eNACH — compliance routes cards to
        # monitor_gateway_retry, because Razorpay owns card retries and manual charge
        # of a domestic card is unsupported. The mandate-rail recurring-charge call
        # still needs verifying against a live account, so this refuses rather than
        # guessing a method name.
        raise NotImplementedError(
            "retry_charge: verify the mandate-rail recurring-charge API before enabling"
        )


def get_gateway():
    key_id = settings.razorpay_key_id
    if key_id and settings.razorpay_key_secret:
        # Every figure in this system is synthetic. A live key would raise real payment
        # links against real customers for debts that do not exist, so it is refused
        # here rather than left to a deployment checklist.
        if key_id.startswith("rzp_live_"):
            raise UnsafeCredentials(
                "RAZORPAY_KEY_ID is a live key. This system runs on synthetic data and "
                "must only be pointed at a test-mode account (rzp_test_...)."
            )
        return RazorpayGateway(key_id, settings.razorpay_key_secret)
    return DryRunGateway()


_ACTION_TO_GATEWAY_METHOD = {
    "retry_now": "retry_charge",
    "retry_at": "retry_charge",
    "send_payment_link": "send_payment_link",
    "request_new_mandate": "request_new_mandate",
}

# No gateway call, no ActionLog, no attempt consumed. `monitor_gateway_retry` belongs
# here rather than on the gateway path for a concrete reason: an ActionLog row is what
# the compliance invariants count as a customer contact, and retry_count is what the
# attempt cap spends. Monitoring does neither — it observes that Razorpay is already
# retrying. Filing it as an action would make a control-group card payment look
# "contacted" and would burn an attempt on something that never attempted anything.
_NO_OP_ACTIONS = {
    "escalate_human",
    "stop_lost",
    "wait",
    "control_no_action",
    "monitor_gateway_retry",
}

_TERMINAL_STATUS = {
    "escalate_human": PaymentStatus.HUMAN_REVIEW,
    "monitor_gateway_retry": PaymentStatus.WAITING,
    "stop_lost": PaymentStatus.LOST,
    "wait": PaymentStatus.WAITING,
    "control_no_action": PaymentStatus.WAITING,
}


def execute(db: Session, payment: FailedPayment, decision: DecisionOutcome, now: datetime | None = None) -> None:
    now = as_aware(now) or utcnow()
    if decision.action in _NO_OP_ACTIONS:
        payment.status = _TERMINAL_STATUS[decision.action].value
        audit.record(
            db,
            failed_payment_id=payment.id,
            stage="execution",
            actor="system",
            detail={"action": decision.action, "note": "no gateway call — terminal/no-op action"},
            now=now,
        )
        return

    scheduled_at = as_aware(decision.scheduled_at)
    if scheduled_at and scheduled_at > now:
        # Compliant slot is in the future (cooldown / RBI pre-debit notice window) —
        # do NOT call the gateway yet. A worker picks this up once it's actually due;
        # see worker.process_due_retries.
        payment.status = PaymentStatus.WAITING.value
        audit.record(
            db,
            failed_payment_id=payment.id,
            stage="execution",
            actor="system",
            detail={"action": decision.action, "note": "scheduled, not yet due", "scheduled_at": decision.scheduled_at.isoformat()},
            now=now,
        )
        return

    gateway = get_gateway()
    method_name = _ACTION_TO_GATEWAY_METHOD[decision.action]
    method = getattr(gateway, method_name)

    try:
        result = method(payment)
        outcome, ref, message = result.outcome, result.ref, result.message
    except NotImplementedError as exc:
        outcome, ref, message = "failed", None, str(exc)

    db.add(
        ActionLog(
            failed_payment_id=payment.id,
            decision_id=decision.decision_id,
            action_taken=decision.action,
            gateway_ref=ref,
            message_text=message,
            outcome=outcome,
            executed_at=now,
        )
    )

    payment.retry_count += 1
    payment.last_attempt_at = now
    payment.status = PaymentStatus.EXECUTED.value

    audit.record(
        db,
        failed_payment_id=payment.id,
        stage="execution",
        actor="system",
        detail={"action": decision.action, "gateway_ref": ref, "outcome": outcome, "message": message},
        now=now,
    )
