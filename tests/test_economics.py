from app.economics import assess


def test_good_odds_on_a_first_attempt_is_worth_it():
    verdict = assess(posterior_mean=0.6, retry_count=0, amount_paise=99900)
    assert verdict.should_attempt
    assert verdict.expected_value_paise > 0


def test_recovery_odds_decay_with_each_failed_attempt():
    first = assess(posterior_mean=0.5, retry_count=0, amount_paise=99900)
    third = assess(posterior_mean=0.5, retry_count=3, amount_paise=99900)
    assert third.p_recovery < first.p_recovery


def test_churn_risk_rises_with_each_contact():
    first = assess(posterior_mean=0.5, retry_count=0, amount_paise=99900)
    third = assess(posterior_mean=0.5, retry_count=3, amount_paise=99900)
    assert third.p_churn > first.p_churn
    assert third.churn_cost_paise > first.churn_cost_paise


def test_gate_eventually_stops_a_payment_that_keeps_failing():
    """Falling recovery odds against rising churn risk must cross over — otherwise the
    'stopping rule' is decorative and the attempt cap is doing all the work."""
    verdicts = [assess(posterior_mean=0.5, retry_count=n, amount_paise=99900) for n in range(6)]
    assert verdicts[0].should_attempt
    assert not verdicts[-1].should_attempt


def test_tiny_amount_stops_earlier_than_a_large_one():
    """The churn term scales with the amount (LTV does too), so it cancels out of the
    comparison; the fixed contact cost is what makes small amounts uneconomic. A Rs.5
    invoice is not worth an SMS at odds a Rs.9,999 one clears easily, and a fixed
    attempt cap cannot express that."""
    tiny = assess(posterior_mean=0.25, retry_count=0, amount_paise=500)
    large = assess(posterior_mean=0.25, retry_count=0, amount_paise=999900)
    assert not tiny.should_attempt
    assert large.should_attempt


def test_a_better_slot_earns_more_attempts_than_a_weak_one():
    """Because the stopping point falls out of the odds rather than a fixed cap, a
    high-confidence retry slot should survive further into the attempt sequence."""

    def last_attempt_allowed(posterior: float) -> int:
        allowed = [n for n in range(8) if assess(posterior, n, 99900).should_attempt]
        return max(allowed) if allowed else -1

    assert last_attempt_allowed(0.85) > last_attempt_allowed(0.35)


def test_hopeless_odds_are_never_worth_the_churn_risk():
    verdict = assess(posterior_mean=0.02, retry_count=2, amount_paise=99900)
    assert not verdict.should_attempt
    assert verdict.reason == "negative_expected_value"


def test_ev_components_are_reported_for_the_audit_trail():
    verdict = assess(posterior_mean=0.5, retry_count=1, amount_paise=99900)
    expected = verdict.gross_upside_paise - 200 - verdict.churn_cost_paise
    assert verdict.expected_value_paise == expected
