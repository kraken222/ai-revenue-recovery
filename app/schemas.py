from datetime import datetime

from pydantic import BaseModel


class WebhookEnvelope(BaseModel):
    event_id: str
    event: str
    payload: dict


class OutcomeEnvelope(BaseModel):
    event_id: str
    razorpay_payment_id: str
    success: bool


class AuditEntryOut(BaseModel):
    stage: str
    actor: str
    detail: dict
    created_at: datetime

    model_config = {"from_attributes": True}


class FailedPaymentOut(BaseModel):
    id: str
    razorpay_payment_id: str
    customer_id: str
    # Which regime this row belongs to. A response schema that omits it makes three
    # legally distinct sources look like one kind of record to every consumer.
    source: str
    rail: str
    amount_paise: int
    error_code: str
    status: str
    retry_count: int
    gateway_retry_count: int
    gateway_exhausted: bool
    control_group: bool
    first_failed_at: datetime
    recovered_at: datetime | None

    model_config = {"from_attributes": True}


class ResolveRequest(BaseModel):
    """An operator closing an escalated case. `outcome` is validated against
    console.RESOLUTIONS server-side rather than by an enum here, so the refusal is
    recorded in the audit trail instead of being rejected before it reaches one."""

    outcome: str
    operator: str
    note: str | None = None
