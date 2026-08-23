"""Escalation ladder and promise-to-pay tracking.

Track 03's bar names "compliant escalation" specifically, and a flat action set does
not satisfy it. Real recovery escalates: a first miss is a mechanical hiccup and gets
a light touch, a third is a pattern and gets a different one. What must NOT escalate
is pressure — RBI's Fair Practices Code prohibits threats, coercion, and contacting
anyone but the customer, at every rung. So this ladder escalates **channel, specificity
and human involvement**, never intimidation.

    rung 0  passive     the gateway is still retrying; say nothing
    rung 1  reminder    one message, softest framing, "no action needed if already paid"
    rung 2  assisted    payment link / re-auth flow with an explicit call to action
    rung 3  human       hand to an operator with the full trace

The ladder is capped by the same attempt cap the rest of the system uses, so it cannot
climb forever, and rung 3 is a person rather than a harder machine.

### Promise to pay

A promise is a dated commitment from the customer, and treating it as a first-class
record rather than a note is what lets dunning pause: chasing someone who has already
said "Friday" is how you lose a customer who intended to pay. Industry practice, which
this implements: honour an open promise by suppressing outreach until it matures,
re-engage promptly once it is broken rather than waiting out the next cycle, and
escalate on the *second* broken promise rather than the first — one missed date is
life, two is a pattern.

A promise never overrides compliance. It can only ever *suppress* contact, never
create one, and a revoked mandate still stops everything.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app.contact_policy import IST

# How long after a promised date we wait before treating it as broken. Payments settle
# overnight, so same-evening impatience produces false breaks.
PROMISE_GRACE_HOURS = 12

# Broken promises tolerated before the case goes to a human.
BROKEN_PROMISES_BEFORE_ESCALATION = 2

# The furthest ahead a customer may push a promise. Beyond this it is not a payment
# plan, it is an indefinite deferral, and the case needs a person.
MAX_PROMISE_HORIZON_DAYS = 30


@dataclass(frozen=True)
class Rung:
    level: int
    name: str
    intent: str


LADDER = (
    Rung(0, "passive", "gateway is still retrying; no merchant contact"),
    Rung(1, "reminder", "one soft message, no call to action"),
    Rung(2, "assisted", "payment link or re-auth with an explicit next step"),
    Rung(3, "human", "operator review with the full decision trace"),
)


def rung_for(
    attempt: int, broken_promises: int = 0, gateway_owns_retry: bool = False
) -> tuple[Rung, str]:
    """Which rung this case has climbed to, and WHY it got there.

    The reason is returned rather than inferred by the caller because two different
    paths reach the top rung — a customer who missed two promised dates, and one we
    have simply contacted twice — and they are not the same situation. Labelling both
    "repeated broken promises" puts a reason in the audit trail that never happened,
    which is precisely the kind of claim this system exists not to make.

    `attempt` is contacts already made, not failures observed: a payment can fail three
    times inside the gateway's own retry cycle without us having said anything, and
    treating those as our escalation would open at the top rung against a customer we
    have never contacted.
    """
    if gateway_owns_retry and attempt == 0:
        return LADDER[0], "gateway_owns_retry"
    if broken_promises >= BROKEN_PROMISES_BEFORE_ESCALATION:
        return LADDER[3], "repeated_broken_promises"
    rung = LADDER[min(attempt + 1, 3)]
    return rung, ("contact_attempts_exhausted" if rung.level == 3 else "attempts_made")


@dataclass
class PromiseState:
    """Derived view of a customer's promise history on one payment."""

    open_promise_at: datetime | None
    broken_count: int
    kept_count: int

    @property
    def has_open_promise(self) -> bool:
        return self.open_promise_at is not None


def is_promise_open(promised_for: datetime | None, now: datetime) -> bool:
    """A promise is open until its date plus a grace period. Open promises suppress
    outreach; matured ones release it."""
    if promised_for is None:
        return False
    return now < promised_for + timedelta(hours=PROMISE_GRACE_HOURS)


def is_promise_broken(promised_for: datetime | None, now: datetime, paid: bool) -> bool:
    if promised_for is None or paid:
        return False
    return now >= promised_for + timedelta(hours=PROMISE_GRACE_HOURS)


def promise_horizon_ok(promised_for: datetime, now: datetime) -> bool:
    """A promise must be in the future and inside the horizon. A date in the past is a
    data error, not a commitment; one six months out is a deferral wearing a promise's
    clothes."""
    return now < promised_for <= now + timedelta(days=MAX_PROMISE_HORIZON_DAYS)


@dataclass
class SuppressionDecision:
    suppressed: bool
    reason: str | None
    release_at: datetime | None


def evaluate_promise(state: PromiseState, now: datetime) -> SuppressionDecision:
    """Should outreach be held because the customer has already committed to a date?

    This can only ever suppress contact. It never schedules one, never shortens a
    compliance window, and never overrides a revoked mandate — those are decided
    upstream and a promise is not permitted to reopen them.
    """
    if not is_promise_open(state.open_promise_at, now):
        return SuppressionDecision(False, None, None)
    release = state.open_promise_at + timedelta(hours=PROMISE_GRACE_HOURS)
    return SuppressionDecision(True, "open_promise_to_pay", release)


def describe(rung: Rung, state: PromiseState) -> str:
    """One line for the audit trail, so an operator can see why a case is where it is."""
    parts = [f"rung {rung.level} ({rung.name}): {rung.intent}"]
    if state.broken_count:
        parts.append(f"{state.broken_count} broken promise(s)")
    if state.kept_count:
        parts.append(f"{state.kept_count} kept")
    return "; ".join(parts)


def local_hour(when: datetime) -> int:
    """IST hour, for reporting. The contact window itself lives in contact_policy."""
    return when.astimezone(IST).hour
