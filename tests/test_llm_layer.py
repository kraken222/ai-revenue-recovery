"""Tests for the LLM tier.

Everything here runs offline. `llm.complete_json` / `complete_text` are the only
seams that touch the network, so they are the only things stubbed — the ensemble
voting, the thresholds, the validation guard and the fallback chain are all exercised
for real. That is deliberate: the logic worth testing is what happens around the
model, not the model.
"""

import pytest

from app import classifier, llm, llm_classifier, messaging
from app.config import settings
from app.llm import LLMCall
from app.models import Category, Rail


@pytest.fixture(autouse=True)
def _offline():
    """Guarantee no test can reach the network even if credentials are present in the
    developer's environment."""
    llm.reset_cache()
    yield
    llm.reset_cache()


def _votes(monkeypatch, *categories, confidence=0.9):
    """Stub one JSON completion per framing, in order."""
    queue = list(categories)

    def fake(system, prompt, schema, max_tokens=512):
        if not queue:
            return LLMCall(ok=False, error="exhausted")
        item = queue.pop(0)
        if isinstance(item, str) and item.startswith("!"):
            return LLMCall(ok=False, error=item[1:])
        cat, conf = item if isinstance(item, tuple) else (item, confidence)
        return LLMCall(ok=True, data={"category": cat, "confidence": conf, "reasoning": "test"})

    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr(llm, "available", lambda: True)


# --- ensemble voting ----------------------------------------------------------


def test_unanimous_agreement_is_accepted(monkeypatch):
    _votes(monkeypatch, "soft_decline", "soft_decline", "soft_decline")
    r = llm_classifier.classify(Rail.UPI_AUTOPAY, "INSUFFICIENT BAL AC XX4471")
    assert r.accepted and r.category == Category.SOFT_DECLINE
    assert r.agreement == 1.0


def test_two_of_three_clears_the_floor(monkeypatch):
    _votes(monkeypatch, "soft_decline", "soft_decline", "technical")
    r = llm_classifier.classify(Rail.CARD, "some ambiguous narration")
    assert r.accepted and r.category == Category.SOFT_DECLINE
    assert r.agreement == pytest.approx(2 / 3)


def test_three_way_split_is_rejected(monkeypatch):
    """No majority exists, so this must reach a human rather than a plurality pick."""
    _votes(monkeypatch, "soft_decline", "technical", "hard_decline")
    r = llm_classifier.classify(Rail.CARD, "garbled narration")
    assert not r.accepted
    assert r.category == Category.UNKNOWN
    assert r.reason == "tied_vote"


def test_even_split_is_a_tie_not_a_winner(monkeypatch):
    """The specific bug this pins: with one probe erroring, two usable votes can each
    hold 50%, and Counter.most_common would break the tie by insertion order —
    turning a coin flip into a confident decision."""
    _votes(monkeypatch, "soft_decline", "technical", "!timeout")
    r = llm_classifier.classify(Rail.CARD, "narration")
    assert not r.accepted
    assert r.reason == "tied_vote"


def test_low_confidence_is_rejected_despite_unanimity(monkeypatch):
    """Agreement and confidence are independent gates — three probes can agree
    confidently that they are unsure."""
    _votes(monkeypatch, "soft_decline", "soft_decline", "soft_decline", confidence=0.3)
    r = llm_classifier.classify(Rail.CARD, "vague")
    assert not r.accepted
    assert r.reason == "below_confidence_floor"
    assert r.category == Category.SOFT_DECLINE  # preserved for the audit trail


def test_all_probes_failing_is_rejected(monkeypatch):
    _votes(monkeypatch, "!timeout", "!refusal", "!unparseable_json")
    r = llm_classifier.classify(Rail.CARD, "narration")
    assert not r.accepted
    assert r.reason == "no_usable_votes"
    assert len(r.votes) == 3  # the failures are still recorded


def test_surviving_probes_still_decide_when_one_errors(monkeypatch):
    _votes(monkeypatch, "technical", "technical", "!timeout")
    r = llm_classifier.classify(Rail.ENACH, "NPCI TIMEOUT")
    assert r.accepted and r.category == Category.TECHNICAL
    assert r.agreement == 1.0  # measured over usable votes, not attempted ones


def test_empty_narration_never_calls_the_model(monkeypatch):
    called = []
    monkeypatch.setattr(llm, "complete_json", lambda **k: called.append(1) or LLMCall(ok=False))
    r = llm_classifier.classify(Rail.CARD, "   ")
    assert not r.accepted and r.reason == "empty_narration"
    assert not called


def test_audit_payload_records_the_dissent(monkeypatch):
    _votes(monkeypatch, "soft_decline", "soft_decline", "technical")
    audit = llm_classifier.classify(Rail.CARD, "narration").as_audit()
    assert audit["accepted"] is True
    assert [v.get("category") for v in audit["votes"]].count("technical") == 1


# --- classifier integration ---------------------------------------------------


def test_rule_hit_never_invokes_the_model(monkeypatch):
    """The tiering claim: a clean error code must not cost an API call."""
    called = []
    monkeypatch.setattr(llm, "complete_json", lambda **k: called.append(1) or LLMCall(ok=False))
    monkeypatch.setattr(llm, "available", lambda: True)

    result = classifier.classify(Rail.CARD, "expired_card")
    assert result.source == "rule"
    assert result.category == Category.HARD_DECLINE
    assert not called


def test_offline_rule_miss_falls_through_to_human_review(monkeypatch):
    monkeypatch.setattr(llm, "available", lambda: False)
    result = classifier.classify(Rail.CARD, "SOME_UNMAPPED_CODE", "bank said no")
    assert result.source == "rule_miss"
    assert result.category == Category.UNKNOWN


def test_accepted_ensemble_reaches_the_classifier(monkeypatch):
    _votes(monkeypatch, "technical", "technical", "technical")
    result = classifier.classify(Rail.UPI_AUTOPAY, "PSP_DOWN_XYZ", "remitter switch unavailable")
    assert result.source == "llm"
    assert result.category == Category.TECHNICAL


def test_rejected_ensemble_is_distinguishable_from_no_model_running(monkeypatch):
    """`llm_rejected` and `rule_miss` both end at human review, but the audit trail
    must be able to tell 'the model could not agree' from 'no model ran'."""
    _votes(monkeypatch, "soft_decline", "technical", "hard_decline")
    result = classifier.classify(Rail.CARD, "UNMAPPED", "garbled")
    assert result.source == "llm_rejected"
    assert result.category == Category.UNKNOWN
    assert result.raw["votes"]


def test_unmatched_error_code_is_fed_to_the_model_as_evidence(monkeypatch):
    """Some PSPs put the whole narration in the code field, so the code must be part
    of what gets classified, not discarded."""
    seen = {}

    def fake(system, prompt, schema, max_tokens=512):
        seen["prompt"] = prompt
        return LLMCall(ok=True, data={"category": "soft_decline", "confidence": 0.9, "reasoning": "x"})

    monkeypatch.setattr(llm, "complete_json", fake)
    monkeypatch.setattr(llm, "available", lambda: True)
    classifier.classify(Rail.CARD, "INSUFFICIENT_BALANCE_XX99", "")
    assert "INSUFFICIENT_BALANCE_XX99" in seen["prompt"]


# --- message composition ------------------------------------------------------


def test_template_ships_when_copy_generation_is_disabled():
    msg = messaging.compose("send_payment_link", Category.HARD_DECLINE, Rail.CARD, 99900)
    assert msg.source == "template"
    assert "Rs.999" in msg.body


def test_hallucinated_amount_is_rejected_and_template_ships(monkeypatch):
    """The failure this guard exists for: the model writing a different rupee figure
    than the one it was given."""
    monkeypatch.setattr(settings, "llm_copy_enabled", True)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "complete_text",
        lambda **k: LLMCall(ok=True, text="Your payment of Rs.4,999 failed. Please pay."),
    )
    msg = messaging.compose("send_payment_link", Category.HARD_DECLINE, Rail.CARD, 99900, channel="email")
    assert msg.source == "template"
    assert msg.fallback_reason == "hallucinated_amount"
    assert "Rs.4,999" not in msg.body


def test_threatening_copy_is_rejected(monkeypatch):
    monkeypatch.setattr(settings, "llm_copy_enabled", True)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "complete_text",
        lambda **k: LLMCall(ok=True, text="Pay Rs.999 now or we will take legal action."),
    )
    msg = messaging.compose("retry_now", Category.SOFT_DECLINE, Rail.CARD, 99900, channel="email")
    assert msg.source == "template"
    assert msg.fallback_reason.startswith("prohibited_language")


def test_sms_never_uses_generated_copy(monkeypatch):
    """Commercial SMS in India must run over a registered DLT template, so free-form
    generation there would produce copy that cannot legally be sent."""
    monkeypatch.setattr(settings, "llm_copy_enabled", True)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", lambda **k: LLMCall(ok=True, text="anything at all"))
    msg = messaging.compose("retry_now", Category.SOFT_DECLINE, Rail.CARD, 99900, channel="sms")
    assert msg.source == "template"
    assert msg.fallback_reason == "channel_requires_registered_template"


def test_valid_generated_copy_is_used(monkeypatch):
    monkeypatch.setattr(settings, "llm_copy_enabled", True)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(
        llm, "complete_text",
        lambda **k: LLMCall(ok=True, text="Your Rs.999 payment did not go through. We will retry shortly."),
    )
    msg = messaging.compose("retry_now", Category.SOFT_DECLINE, Rail.CARD, 99900, channel="email")
    assert msg.source == "llm"
    assert "Rs.999" in msg.body


def test_api_failure_falls_back_to_template(monkeypatch):
    monkeypatch.setattr(settings, "llm_copy_enabled", True)
    monkeypatch.setattr(llm, "available", lambda: True)
    monkeypatch.setattr(llm, "complete_text", lambda **k: LLMCall(ok=False, error="APITimeoutError"))
    msg = messaging.compose("retry_now", Category.SOFT_DECLINE, Rail.CARD, 99900, channel="email")
    assert msg.source == "template"
    assert msg.fallback_reason == "APITimeoutError"


def test_every_template_stays_within_its_channel_budget():
    for action in ("retry_now", "send_payment_link", "request_new_mandate", "escalate_human"):
        for rail in Rail:
            for channel, limit in messaging._LIMITS.items():
                msg = messaging.template_for(action, Category.SOFT_DECLINE, rail, 12345678, channel)
                assert len(msg.body) <= limit, f"{action}/{rail}/{channel} overflowed"


def test_explanation_falls_back_deterministically():
    text = messaging.explain(
        {"action": "stop_lost", "policy_rule_id": "COMP-004-attempt-cap", "blocked_reason": "max_retry_attempts_exhausted"}
    )
    assert "stop_lost" in text and "COMP-004" in text


# --- offline safety -----------------------------------------------------------


def test_llm_calls_are_inert_without_credentials(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    llm.reset_cache()
    assert llm.available() is False
    assert llm.complete_json("s", "p", {}).error == "llm_unavailable"
    assert llm.complete_text("s", "p").error == "llm_unavailable"


# --- audit attribution --------------------------------------------------------


def test_audit_actor_names_only_actors_that_actually_acted(monkeypatch):
    """A rule-table miss with no model available must not be attributed to the LLM.
    Crediting an actor that never ran is a correctness bug in an audit trail, not a
    cosmetic one — the whole point of the trail is that it says what happened.
    """
    from datetime import datetime, timezone

    from sqlalchemy import select

    from app import decision_engine
    from app.db import make_session_factory
    from app.models import AuditLog, FailedPayment

    monkeypatch.setattr(llm, "available", lambda: False)
    factory, engine = make_session_factory("sqlite:///:memory:")
    db = factory()
    try:
        now = datetime(2026, 1, 1, tzinfo=timezone.utc)
        payment = FailedPayment(
            razorpay_payment_id="pay_x",
            customer_id="cust_x",
            rail="card",
            amount_paise=99900,
            error_code="UNMAPPED",
            error_description="DO NOT HONOUR",
            first_failed_at=now,
        )
        db.add(payment)
        db.flush()

        decision_engine.decide(db, payment, now=now)
        entry = db.scalar(
            select(AuditLog).where(AuditLog.stage == "classification").order_by(AuditLog.id.desc())
        )
        assert entry.detail["source"] == "rule_miss"
        assert entry.actor == "system", "no model ran, so the LLM must not be credited"
    finally:
        db.close()
        engine.dispose()
