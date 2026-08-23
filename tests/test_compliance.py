from datetime import datetime, timedelta, timezone

from app.compliance import evaluate
from app.models import Category, FailedPayment, Rail

NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _payment(**overrides) -> FailedPayment:
    defaults = dict(
        razorpay_payment_id="pay_1",
        customer_id="cust_1",
        rail=Rail.CARD.value,
        amount_paise=99900,
        error_code="insufficient_funds",
        retry_count=0,
        mandate_revoked=False,
        last_attempt_at=None,
    )
    defaults.update(overrides)
    return FailedPayment(**defaults)


def test_mandate_revoked_is_an_absolute_stop_regardless_of_category():
    payment = _payment(mandate_revoked=True)
    result = evaluate(payment, Category.SOFT_DECLINE, now=NOW)
    assert result.allowed_actions == ["stop_lost"]
    assert result.policy_rule_id == "COMP-001-consent-withdrawn"


def test_risk_block_never_auto_retried():
    payment = _payment()
    result = evaluate(payment, Category.RISK_BLOCK, now=NOW)
    assert result.allowed_actions == ["escalate_human"]


def test_low_confidence_unknown_also_goes_to_human_not_a_default_retry():
    payment = _payment()
    result = evaluate(payment, Category.UNKNOWN, now=NOW)
    assert result.allowed_actions == ["escalate_human"]


def test_hard_decline_on_card_gets_payment_link_not_a_retry():
    payment = _payment(rail=Rail.CARD.value, error_code="expired_card")
    result = evaluate(payment, Category.HARD_DECLINE, now=NOW)
    assert result.allowed_actions == ["send_payment_link"]


def test_hard_decline_attempts_are_capped_too_not_unbounded():
    payment = _payment(rail=Rail.CARD.value, error_code="expired_card", retry_count=3)
    result = evaluate(payment, Category.HARD_DECLINE, now=NOW)
    assert result.allowed_actions == ["stop_lost"]
    assert result.policy_rule_id == "COMP-004-attempt-cap"


def test_hard_decline_on_upi_mandate_gets_new_mandate_request():
    payment = _payment(rail=Rail.UPI_AUTOPAY.value, error_code="mandate_revoked")
    result = evaluate(payment, Category.HARD_DECLINE, now=NOW)
    assert result.allowed_actions == ["request_new_mandate"]


def test_max_retry_attempts_exhausted_stops_instead_of_looping_forever():
    payment = _payment(retry_count=3)
    result = evaluate(payment, Category.SOFT_DECLINE, now=NOW)
    assert result.allowed_actions == ["stop_lost"]
    assert result.policy_rule_id == "COMP-004-attempt-cap"


def test_amount_above_afa_ceiling_requires_fresh_auth_not_blind_retry():
    payment = _payment(amount_paise=20_00_000)  # above default 15,000 INR ceiling
    result = evaluate(payment, Category.SOFT_DECLINE, now=NOW)
    assert result.allowed_actions == ["send_payment_link"]
    assert result.policy_rule_id == "COMP-005-afa-required-above-ceiling"


def test_card_soft_decline_first_attempt_can_retry_immediately():
    payment = _payment(rail=Rail.CARD.value, last_attempt_at=None)
    result = evaluate(payment, Category.SOFT_DECLINE, now=NOW)
    # Corrected after checking Razorpay's docs: the gateway auto-retries card
    # subscription charges itself, and manual charge of a domestic card is not
    # supported. A merchant-issued retry here would double-attempt or hit an API that
    # does not exist, so monitoring is the honest action.
    assert result.allowed_actions == ["monitor_gateway_retry"]
    assert result.policy_rule_id == "COMP-007-gateway-owns-card-retry"


def test_upi_soft_decline_first_attempt_must_wait_for_pre_debit_notice_window():
    payment = _payment(rail=Rail.UPI_AUTOPAY.value, error_code="insufficient_balance", last_attempt_at=None)
    result = evaluate(payment, Category.SOFT_DECLINE, now=NOW)
    assert result.allowed_actions == ["retry_at"]
    assert result.earliest_slot == NOW + timedelta(hours=24)


def test_cooldown_still_running_blocks_immediate_retry():
    """Cooldown applies on the mandate rails, where the merchant actually initiates
    the debit. Cards are excluded earlier by COMP-007.

    Both floors are live here and the LATER one wins: the cooldown has already run an
    hour (so it clears at NOW+23h) while the pre-debit notice restarts from now (NOW+24h).
    Taking the max is what stops a partially-elapsed cooldown from shortening a
    regulatory notice window."""
    payment = _payment(rail=Rail.ENACH.value, error_code="bank_technical_failure",
                       last_attempt_at=NOW - timedelta(hours=1))
    result = evaluate(payment, Category.TECHNICAL, now=NOW)
    assert result.allowed_actions == ["retry_at"]
    assert result.earliest_slot == NOW + timedelta(hours=24)      # notice floor wins
    assert result.earliest_slot > NOW + timedelta(hours=23)       # not the cooldown floor


def test_card_retry_is_never_merchant_initiated():
    """The whole card rail routes to monitoring, regardless of category or attempt."""
    for attempts in (0, 1, 2):
        payment = _payment(rail=Rail.CARD.value, retry_count=attempts)
        for category in (Category.SOFT_DECLINE, Category.TECHNICAL):
            result = evaluate(payment, category, now=NOW)
            assert "retry_now" not in result.allowed_actions
            assert "retry_at" not in result.allowed_actions


def test_customer_contact_is_deferred_out_of_the_rbi_night():
    """A hard decline at 02:00 IST needs a payment link, which is customer contact —
    so it must wait for 08:00 IST rather than going out at once. The bandit's
    overnight window is legal for silent debits and illegal for this."""
    from app.contact_policy import IST, within_contact_window

    night = datetime(2026, 1, 15, 2, 0, tzinfo=IST)
    payment = _payment(rail=Rail.CARD.value, error_code="expired_card")
    result = evaluate(payment, Category.HARD_DECLINE, now=night)

    assert result.allowed_actions == ["send_payment_link"]
    assert result.earliest_slot > night
    assert within_contact_window(result.earliest_slot)
    assert result.policy_rule_id == "COMP-008-rbi-contact-window"


def test_daytime_contact_is_not_deferred():
    from app.contact_policy import IST

    midday = datetime(2026, 1, 15, 11, 0, tzinfo=IST)
    payment = _payment(rail=Rail.CARD.value, error_code="expired_card")
    result = evaluate(payment, Category.HARD_DECLINE, now=midday)

    assert result.earliest_slot == midday
    assert result.policy_rule_id == "COMP-003-instrument-dead-needs-fresh-auth"
