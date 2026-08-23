from app.classifier import classify
from app.models import Category, Rail


def test_known_soft_decline_maps_correctly():
    result = classify(Rail.UPI_AUTOPAY, "insufficient_balance")
    assert result.category == Category.SOFT_DECLINE
    assert result.confidence == 1.0
    assert result.source == "rule"


def test_known_hard_decline_maps_correctly():
    result = classify(Rail.CARD, "expired_card")
    assert result.category == Category.HARD_DECLINE


def test_unknown_error_code_falls_back_to_unknown_not_a_guess():
    result = classify(Rail.CARD, "some_totally_new_code_not_in_taxonomy")
    assert result.category == Category.UNKNOWN
    assert result.confidence == 0.0
    assert result.source == "rule_miss"


def test_category_is_rail_specific():
    # same error_code string, different rail -> must not accidentally cross-match
    assert classify(Rail.CARD, "insufficient_funds").category == Category.SOFT_DECLINE
    assert classify(Rail.UPI_AUTOPAY, "insufficient_funds").category is Category.UNKNOWN
