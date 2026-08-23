"""Revenue-at-risk sources, and the compliance profile each one carries.

Track 03 names three kinds of slipping revenue in one sentence — "from payment failures
and checkout abandonment to overdue receivables" — and they genuinely are one problem:
detect, decide, act, bounded. But they are emphatically **not one compliance regime**,
and collapsing them is the mistake that makes a recovery agent creepy or illegal.

    FAILED_PAYMENT      an existing customer, an active mandate, money genuinely owed
    ABANDONED_CHECKOUT  a prospect who did not finish. NOTHING is owed.
    OVERDUE_INVOICE     a B2B buyer past terms, under the MSMED Act

The distinction that matters most is the middle one. **An abandoned checkout is not a
debt.** Nobody owes anything; the customer looked and left. Running collections
escalation against them would be both wrong and, under TCCCPR, a marketing contact
dressed up as a service one. So that source gets a single gentle nudge and no ladder,
and it is governed by marketing-consent rules rather than by RBI's debt-collection code.

The third inverts the usual leverage. In India a late B2B payment to an MSME supplier
accrues compound interest at three times the RBI bank rate by operation of law, and
from April 2024 the buyer also loses the tax deduction on it. The most effective thing
a receivables chaser can do is therefore not to apply pressure but to **state what is
already accruing** — which is a fact, not a threat, and stays inside the prohibition on
coercion precisely because it is one.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field


class Source(str, enum.Enum):
    FAILED_PAYMENT = "failed_payment"
    ABANDONED_CHECKOUT = "abandoned_checkout"
    OVERDUE_INVOICE = "overdue_invoice"


@dataclass(frozen=True)
class SourceProfile:
    """What is legally and ethically available for this kind of revenue at risk."""

    source: Source
    is_debt: bool
    """Whether money is actually owed. Drives which regime applies at all: RBI's Fair
    Practices Code governs debt collection, and it simply does not reach a prospect who
    abandoned a cart."""

    has_mandate: bool
    """Whether we hold an instrument we may debit. Without one, no retry exists as an
    action — only asking."""

    max_contacts: int
    escalation_allowed: bool
    allowed_actions: tuple[str, ...]
    contact_window_applies: bool

    ltv_multiple: float = 12.0
    """What over-contacting actually costs, expressed as a multiple of the amount at
    stake. This is NOT one number across sources, and treating it as one produced a
    real and backwards failure: a subscription lost to dunning fatigue costs its whole
    remaining lifetime, but a B2B buyer settling a late invoice is not cancelling a
    subscription, and a prospect who abandoned a cart was never a customer to lose.
    Charging a 12x subscription LTV against a large invoice made the EV gate refuse to
    chase the single most valuable recoverable item in the batch."""

    notes: str = ""
    extra: dict = field(default_factory=dict)


PROFILES: dict[Source, SourceProfile] = {
    Source.FAILED_PAYMENT: SourceProfile(
        source=Source.FAILED_PAYMENT,
        is_debt=True,
        has_mandate=True,
        max_contacts=3,
        escalation_allowed=True,
        allowed_actions=(
            "monitor_gateway_retry",
            "retry_now",
            "retry_at",
            "send_payment_link",
            "request_new_mandate",
            "escalate_human",
            "stop_lost",
        ),
        contact_window_applies=True,
        # A recurring subscription: losing the customer forfeits every future period.
        ltv_multiple=12.0,
        notes="Active mandate, money owed. Full ladder available under the Fair Practices Code.",
    ),
    Source.ABANDONED_CHECKOUT: SourceProfile(
        source=Source.ABANDONED_CHECKOUT,
        is_debt=False,
        has_mandate=False,
        # One nudge. A second is marketing pressure against someone who owes nothing,
        # and the recovery framing ("your payment failed") would be a false statement:
        # no payment was ever attempted.
        max_contacts=1,
        escalation_allowed=False,
        allowed_actions=("send_payment_link", "stop_lost"),
        contact_window_applies=True,
        # Never a customer, so there is no lifetime to forfeit. The single-contact
        # budget is what bounds this source, not an economic penalty.
        ltv_multiple=0.0,
        notes=(
            "Not a debt. One nudge, no ladder, no dunning language. Governed by "
            "marketing-consent rules (TCCCPR), not by debt-collection rules."
        ),
    ),
    Source.OVERDUE_INVOICE: SourceProfile(
        source=Source.OVERDUE_INVOICE,
        is_debt=True,
        has_mandate=False,
        max_contacts=4,
        escalation_allowed=True,
        allowed_actions=("send_payment_link", "escalate_human", "stop_lost"),
        contact_window_applies=True,
        # A one-off invoice, not a subscription. Over-chasing can still cost repeat
        # business, so this is not zero — but it is nothing like 12x, and using the
        # subscription figure here made the gate abandon the largest recoverable items.
        ltv_multiple=2.0,
        notes=(
            "B2B, no mandate, so no retry exists. Leverage is the statutory interest "
            "already accruing under MSMED s.16 and the buyer's own s.43B(h) tax "
            "exposure - stated as fact, never as threat."
        ),
    ),
}


def profile_for(source: str | Source) -> SourceProfile:
    return PROFILES[Source(source)]


def permits(source: str | Source, action: str) -> bool:
    return action in profile_for(source).allowed_actions
