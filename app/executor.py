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
  token and genuinely initiates the debit. Now a real call, and the docs settle the
  question the earlier comment left open: the same endpoint serves both mandate rails.
  Two steps, because the recurring endpoint takes an order_id and will not mint one:
  `POST /v1/orders`, then `POST /v1/payments/create/recurring` with
  {email, contact, amount, currency, order_id, customer_id, token, recurring: true}.
  Exposed by the pinned SDK as `client.payment.createRecurring`.
  https://razorpay.com/docs/api/payments/recurring-payments/upi/create-subsequent-payments
  https://razorpay.com/docs/api/payments/recurring-payments/emandate/create-subsequent-payments

  What is still NOT verified, and the distinction matters: the request shape is
  confirmed against the docs and the SDK, but no call has been executed against a live
  mandate, because that needs a customer to complete a real UPI Autopay or eNACH
  authorisation to mint a token. So this is "built to a documented contract, untested
  end to end", not "known to work". `scripts/verify_razorpay.py` says which of the two
  it can check for you.
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
from app.models import ActionLog, FailedPayment, PaymentStatus, Rail
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
        # Mirrors the real gateway's preconditions rather than succeeding
        # unconditionally. A dry run that debits a mandate the live path would refuse
        # teaches the offline demo the wrong shape of the system, and would hide a
        # missing token until the first run with credentials.
        if payment.rail == Rail.CARD.value:
            raise UnsupportedRailAction(
                "retry_charge is not a card action; Razorpay owns card retries"
            )
        if not payment.mandate_token:
            return GatewayResult(
                outcome="failed", ref=None, message="cannot charge mandate: missing mandate_token"
            )
        logger.info("[dry-run] would retry charge for %s", payment.razorpay_payment_id)
        return GatewayResult(outcome="pending", ref=f"dryrun_retry_{payment.id[:8]}")



class UnsafeCredentials(Exception):
    """Live credentials in a system whose data is synthetic."""


class UnsupportedRailAction(Exception):
    """An action that does not exist on this rail, as opposed to one that failed.

    Distinct from a GatewayResult(outcome="failed") on purpose: a failure is something
    the pipeline may sensibly re-attempt, while this means the caller built a decision
    that should have been unreachable. It should surface loudly rather than be absorbed
    into the retry accounting.
    """


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
        """Merchant-initiated debit against an existing mandate.

        Two calls, in this order, because the recurring endpoint takes an `order_id`
        and will not mint one for you:

            POST /v1/orders                     -> order_id
            POST /v1/payments/create/recurring  -> razorpay_payment_id

        Same endpoint for UPI Autopay and eNACH; the rail is already encoded in the
        token. Cards never reach here -- compliance routes them to
        `monitor_gateway_retry` -- and the guard below is defence in depth rather than
        a second opinion.
        """
        if payment.rail == Rail.CARD.value:
            # Not an error condition to be retried: on cards this action should not
            # exist. Razorpay runs its own dunning and manual charge of a domestic card
            # is unsupported, so issuing one would either duplicate an attempt the
            # gateway is already making or call an API that isn't there.
            raise UnsupportedRailAction(
                "retry_charge is not a card action; Razorpay owns card retries"
            )

        missing = [
            field
            for field in ("mandate_token", "customer_email", "customer_contact")
            if not getattr(payment, field, None)
        ]
        if missing:
            # A hard refusal, not a retryable failure. Without a token there is no
            # mandate to debit, and the correct recovery path is re-registration --
            # which is a different action, chosen by compliance, not something to
            # improvise here.
            return GatewayResult(
                outcome="failed",
                ref=None,
                message=f"cannot charge mandate: missing {', '.join(missing)}",
            )

        try:
            order = self._client.order.create(
                {
                    "amount": payment.amount_paise,
                    "currency": payment.currency,
                    # Stable per debt AND per attempt: a redelivered webhook re-uses the
                    # receipt, while a genuine second attempt gets its own. Note that
                    # Razorpay only rejects duplicate receipts when the account has
                    # receipt uniqueness enabled, so this narrows the window rather than
                    # closing it -- the pipeline's attempt accounting is what actually
                    # prevents a double charge.
                    "receipt": f"{self.reference_for(payment)}-{payment.retry_count}",
                    "notes": {
                        "razorpay_payment_id": payment.razorpay_payment_id,
                        "internal_id": payment.id,
                    },
                }
            )
        except Exception as exc:
            return self._classify_failure(exc)

        try:
            charge = self._client.payment.createRecurring(
                {
                    "email": payment.customer_email,
                    "contact": payment.customer_contact,
                    "amount": payment.amount_paise,
                    "currency": payment.currency,
                    "order_id": order.get("id"),
                    "customer_id": payment.customer_id,
                    "token": payment.mandate_token,
                    "recurring": True,
                    "description": f"Recovery charge for {payment.razorpay_payment_id}",
                    "notes": {
                        "razorpay_payment_id": payment.razorpay_payment_id,
                        "internal_id": payment.id,
                        "attempt": str(payment.retry_count),
                    },
                }
            )
        except Exception as exc:
            return self._classify_failure(exc)

        # `pending`, never `success`: the endpoint returns a payment id, not a settled
        # outcome. Whether the bank actually honoured the debit arrives later on the
        # outcome webhook, and calling it success here would close the loop on a result
        # nobody has yet reported -- and would feed the bandit a reward it never earned.
        return GatewayResult(
            outcome="pending",
            ref=charge.get("razorpay_payment_id"),
            message=f"order {order.get('id')}",
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
