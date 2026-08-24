import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Rail(str, enum.Enum):
    CARD = "card"
    UPI_AUTOPAY = "upi_autopay"
    ENACH = "enach"


class Category(str, enum.Enum):
    SOFT_DECLINE = "soft_decline"
    HARD_DECLINE = "hard_decline"
    TECHNICAL = "technical"
    RISK_BLOCK = "risk_block"
    UNKNOWN = "unknown"
    # Distinct from UNKNOWN. UNKNOWN means "we tried to classify a decline and could
    # not"; NOT_APPLICABLE means there was no decline to classify in the first place.
    # Collapsing them would hide a skipped question inside a failed one.
    NOT_APPLICABLE = "not_applicable"


class PaymentStatus(str, enum.Enum):
    NEW = "new"
    CLASSIFIED = "classified"
    DECIDED = "decided"
    WAITING = "waiting"          # compliant action exists but scheduled for later
    EXECUTED = "executed"
    RECOVERED = "recovered"
    LOST = "lost"
    HUMAN_REVIEW = "human_review"


class ActionType(str, enum.Enum):
    RETRY_NOW = "retry_now"
    RETRY_AT = "retry_at"
    SEND_PAYMENT_LINK = "send_payment_link"
    REQUEST_NEW_MANDATE = "request_new_mandate"
    ESCALATE_HUMAN = "escalate_human"
    STOP_LOST = "stop_lost"
    WAIT = "wait"                # compliant, but earliest allowed slot is in the future


class Event(Base):
    """Append-only, idempotent record of every inbound webhook. Source of truth —
    all derived state (FailedPayment, etc.) is a projection of this log, so it can
    be rebuilt by replay if a downstream table ever drifts."""

    __tablename__ = "events"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    razorpay_event_id: Mapped[str] = mapped_column(String, unique=True, index=True)
    event_type: Mapped[str] = mapped_column(String)
    payload: Mapped[dict] = mapped_column(JSON)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


class FailedPayment(Base):
    __tablename__ = "failed_payments"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=_uuid)
    razorpay_payment_id: Mapped[str] = mapped_column(String, index=True)
    subscription_id: Mapped[str | None] = mapped_column(String, nullable=True)
    customer_id: Mapped[str] = mapped_column(String, index=True)

    rail: Mapped[str] = mapped_column(String)
    # Which kind of revenue-at-risk this is. Not cosmetic: it selects the compliance
    # profile in sources.py, and an abandoned checkout is not a debt at all.
    source: Mapped[str] = mapped_column(String, default="failed_payment")
    amount_paise: Mapped[int] = mapped_column(Integer)
    currency: Mapped[str] = mapped_column(String, default="INR")

    # Overdue-invoice fields. Null on the other two sources.
    invoice_accepted_on: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    agreed_credit_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    supplier_is_msme: Mapped[bool] = mapped_column(Boolean, default=False)

    error_code: Mapped[str] = mapped_column(String)
    error_description: Mapped[str] = mapped_column(String, default="")
    issuer_id: Mapped[str | None] = mapped_column(String, nullable=True)  # BIN / bank identifier

    mandate_revoked: Mapped[bool] = mapped_column(Boolean, default=False)
    # WHEN consent was withdrawn, not just whether. Without this, "did we contact
    # after revocation?" can only be approximated as "does a revoked payment have any
    # contacts?" — which retroactively condemns every contact that was perfectly legal
    # at the time it was made, because revocation can happen later than the contact.
    mandate_revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the customer cancelled in response to dunning pressure. Distinct from
    # mandate_revoked (which is the mechanism); this records the *cause*, so the cost
    # of over-contacting can be measured rather than only assumed.
    churned_from_dunning: Mapped[bool] = mapped_column(Boolean, default=False)

    status: Mapped[str] = mapped_column(String, default=PaymentStatus.NEW.value)
    # OUR attempts. Distinct from gateway_retry_count below, and the distinction is
    # load-bearing: only our own contacts spend the attempt cap and carry churn cost.
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    # Razorpay's own dunning attempts, which we observe rather than initiate. Once the
    # gateway exhausts these the subscription halts and merchant-side recovery begins —
    # that handover is the moment this system's job actually starts on the card rail.
    gateway_retry_count: Mapped[int] = mapped_column(Integer, default=0)
    gateway_exhausted: Mapped[bool] = mapped_column(Boolean, default=False)
    control_group: Mapped[bool] = mapped_column(Boolean, default=False)  # holdout, no intervention

    first_failed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    recovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    classifications: Mapped[list["Classification"]] = relationship(back_populates="failed_payment")
    decisions: Mapped[list["Decision"]] = relationship(back_populates="failed_payment")
    actions: Mapped[list["ActionLog"]] = relationship(back_populates="failed_payment")
    audit_entries: Mapped[list["AuditLog"]] = relationship(back_populates="failed_payment")
    promises: Mapped[list["PromiseToPay"]] = relationship(back_populates="failed_payment")


class Classification(Base):
    __tablename__ = "classifications"

    # Autoincrement PK (not a UUID) is deliberate: this table is read via "most recent
    # for this payment" lookups, and a simulated/backfilled clock can produce multiple
    # rows with an identical created_at. ORDER BY created_at DESC has no defined
    # tiebreak in that case; ORDER BY id DESC does, on both SQLite and Postgres.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failed_payment_id: Mapped[str] = mapped_column(ForeignKey("failed_payments.id"), index=True)
    category: Mapped[str] = mapped_column(String)
    confidence: Mapped[float] = mapped_column(Float)
    source: Mapped[str] = mapped_column(String)  # "rule" | "llm" | "rule_miss"
    raw_model_output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="classifications")


class Decision(Base):
    __tablename__ = "decisions"

    # See Classification.id docstring — same autoincrement-for-ordering reasoning.
    # This is what worker.process_due_retries relies on to find the TRUE latest
    # decision for a payment; getting this wrong silently stranded payments in
    # WAITING forever when two decisions landed on the same simulated timestamp.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failed_payment_id: Mapped[str] = mapped_column(ForeignKey("failed_payments.id"), index=True)
    action: Mapped[str] = mapped_column(String)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    compliant_action_set: Mapped[list] = mapped_column(JSON)
    # The guardrail rule that decided this. Guardrails run last, so this alone reports
    # GUARD-000-passthrough for almost everything and hides the compliance rule that
    # actually shaped the action — which is where the interesting reasoning lives.
    policy_rule_id: Mapped[str] = mapped_column(String)
    compliance_rule_id: Mapped[str | None] = mapped_column(String, nullable=True)
    blocked_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    # Which bandit arm produced this decision's retry slot, if any. Set only when the
    # bandit actually chose (retry actions, non-control payments) — that's what makes
    # reward attribution unambiguous, and what keeps the control arm out of training.
    bandit_arm_key: Mapped[str | None] = mapped_column(String, nullable=True)
    expected_value_paise: Mapped[float | None] = mapped_column(Float, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="decisions")


class PromiseToPay(Base):
    """A dated commitment from the customer, stored as a record rather than a note.

    The reason this is a table and not a free-text field: a promise has to be able to
    *suppress* outreach until it matures and then release it, and to be counted when it
    breaks. Neither is possible if the commitment only exists inside a message body.
    """

    __tablename__ = "promises_to_pay"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failed_payment_id: Mapped[str] = mapped_column(ForeignKey("failed_payments.id"), index=True)
    promised_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    source: Mapped[str] = mapped_column(String, default="customer")  # customer|agent|import
    # Resolution is explicit rather than inferred, so a promise that was never resolved
    # is visibly unresolved instead of silently counting as kept.
    # open|kept|broken|superseded. "superseded" is deliberately distinct from "broken":
    # only a missed date counts toward escalation, never a revised one.
    status: Mapped[str] = mapped_column(String, default="open")
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="promises")


class BanditArm(Base):
    """Beta-Bernoulli posterior for one (rail, category, time-of-day bucket) arm.

    Context is (rail, category); the action is which time-of-day bucket to retry in.
    Reward is the binary outcome webhook. This mirrors Adyen's AutoRescue framing —
    action space = future retry times within the allowed window, reward = 1 on a
    successful charge — and Stripe's finding that retry success is strongly
    time-of-day dependent (balance top-ups and salary credits cluster).

    Deliberately a small, inspectable table rather than a learned black box: every
    arm's win rate is readable straight off the row, which is what keeps "every money
    action explainable" true even though the slot choice is learned.
    """

    __tablename__ = "bandit_arms"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String, unique=True, index=True)
    rail: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    tod_bucket: Mapped[int] = mapped_column(Integer)  # start hour of a 6h UTC bucket
    alpha: Mapped[float] = mapped_column(Float, default=1.0)  # successes + 1
    beta: Mapped[float] = mapped_column(Float, default=1.0)   # failures + 1
    pulls: Mapped[int] = mapped_column(Integer, default=0)

    @property
    def posterior_mean(self) -> float:
        return self.alpha / (self.alpha + self.beta)


class ActionLog(Base):
    __tablename__ = "actions_log"

    # See Classification.id docstring — same autoincrement-for-ordering reasoning.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failed_payment_id: Mapped[str] = mapped_column(ForeignKey("failed_payments.id"), index=True)
    decision_id: Mapped[int] = mapped_column(ForeignKey("decisions.id"))
    action_taken: Mapped[str] = mapped_column(String)
    gateway_ref: Mapped[str | None] = mapped_column(String, nullable=True)
    message_text: Mapped[str | None] = mapped_column(String, nullable=True)
    outcome: Mapped[str] = mapped_column(String, default="pending")  # pending|success|failed
    executed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="actions")


class AuditLog(Base):
    """Append-only. Every state transition writes here, in addition to its own table,
    so the full reasoning trace for one payment can be pulled with a single query."""

    __tablename__ = "audit_log"

    # Autoincrement, not a UUID: this is an append-only log whose entire value is
    # being readable in the order things happened. Several entries share a timestamp
    # (a whole decide() cycle runs within one simulated instant), so created_at cannot
    # order them and a random UUID would actively scramble them.
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    failed_payment_id: Mapped[str | None] = mapped_column(
        ForeignKey("failed_payments.id"), nullable=True, index=True
    )
    stage: Mapped[str] = mapped_column(String)   # ingestion|classification|compliance|guardrail|decision|execution
    actor: Mapped[str] = mapped_column(String)   # system|llm|human
    detail: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    failed_payment: Mapped["FailedPayment"] = relationship(back_populates="audit_entries")
