"""Expected-value stopping rule.

"Stop after 3 attempts in 7 days" is an arbitrary number. The economically correct
question at each decision point is whether one more attempt is worth what it costs:

    EV = P(recovery) * amount           <- upside
         - contact_cost                  <- the SMS/gateway spend
         - P(annoyance churn) * LTV      <- the part everyone forgets

Stop when that goes negative.

The third term is the one that matters and the one a naive implementation omits. With
only a contact cost, the gate never binds: a Rs.2 SMS against even a 1% shot at Rs.499
clears the bar trivially, so an EV rule reduces to "always retry" and the attempt cap
silently does all the real work. That is a fake stopping rule.

The genuine cost of dunning is churn. Every retry and every "your payment failed"
message is a prompt for the customer to reconsider the subscription, and a customer
lost to dunning fatigue costs their whole remaining lifetime value, not one invoice.
This is precisely the tradeoff recovery vendors sell against — recovering more of this
month's payments while quietly shedding subscribers is a bad trade. Modelling it makes
the stopping rule actually bind, and it binds in the right place: attempts stop when
the falling odds of recovery no longer justify the rising risk of losing the customer.

P(recovery) has two parts:
- the bandit's posterior mean for the slot it chose (empirical, learned per arm), and
- a hazard decay in the number of attempts already made.

The decay is a deliberate stand-in for a proper survival model. Recovery is a
time-to-event problem — each failed attempt is evidence this payment is less likely to
ever recover — and the principled version is a Cox proportional-hazards fit on
(attempts, time-since-first-failure, category). A single configured decay factor
captures the direction of that effect honestly without pretending to a rigour the
fitted model would need real traffic to earn. Swap it for the fitted hazard once
there's production data; the call site does not change.
"""

from dataclasses import dataclass

from app.config import settings


@dataclass
class EconomicVerdict:
    p_recovery: float
    p_churn: float
    gross_upside_paise: float
    churn_cost_paise: float
    expected_value_paise: float
    should_attempt: bool
    reason: str


def assess(
    posterior_mean: float,
    retry_count: int,
    amount_paise: int,
    contact_cost_paise: int | None = None,
    hazard_decay: float | None = None,
    churn_risk_per_contact: float | None = None,
    ltv_multiple: float | None = None,
) -> EconomicVerdict:
    contact_cost_paise = (
        contact_cost_paise if contact_cost_paise is not None else settings.contact_cost_paise
    )
    hazard_decay = hazard_decay if hazard_decay is not None else settings.retry_hazard_decay
    churn_risk_per_contact = (
        churn_risk_per_contact
        if churn_risk_per_contact is not None
        else settings.churn_risk_per_contact
    )
    ltv_multiple = ltv_multiple if ltv_multiple is not None else settings.ltv_multiple

    # Recovery odds fall with each failed attempt (hazard decay).
    p_recovery = posterior_mean * (hazard_decay**retry_count)

    # Churn risk rises with each additional time we bother the same customer.
    p_churn = min(1.0, churn_risk_per_contact * (retry_count + 1))

    gross_upside = p_recovery * amount_paise
    churn_cost = p_churn * amount_paise * ltv_multiple
    ev = gross_upside - contact_cost_paise - churn_cost

    return EconomicVerdict(
        p_recovery=p_recovery,
        p_churn=p_churn,
        gross_upside_paise=gross_upside,
        churn_cost_paise=churn_cost,
        expected_value_paise=ev,
        should_attempt=ev >= 0,
        reason="positive_expected_value" if ev >= 0 else "negative_expected_value",
    )
