"""Tests for the three revenue-at-risk sources and their compliance profiles."""

from datetime import date, datetime, timedelta

import pytest

from app import receivables, sources, voice
from app.contact_policy import IST
from app.models import Rail
from app.sources import Source

ACCEPTED = date(2026, 1, 1)


# --- source profiles ----------------------------------------------------------


def test_abandoned_checkout_is_not_a_debt():
    """The distinction the whole module exists for. Nobody owes anything for a cart
    they looked at and left, so debt-collection rules do not reach them."""
    profile = sources.profile_for(Source.ABANDONED_CHECKOUT)
    assert profile.is_debt is False
    assert profile.escalation_allowed is False
    assert profile.max_contacts == 1


def test_only_failed_payments_can_be_retried():
    """A retry requires an instrument to debit. Neither an abandoned cart nor a B2B
    invoice has one, so retry is not merely discouraged there — it does not exist."""
    for source in (Source.ABANDONED_CHECKOUT, Source.OVERDUE_INVOICE):
        assert not sources.permits(source, "retry_now")
        assert not sources.permits(source, "retry_at")
        assert not sources.profile_for(source).has_mandate

    assert sources.permits(Source.FAILED_PAYMENT, "retry_at")


def test_dunning_escalation_is_never_available_against_a_non_debtor():
    assert not sources.permits(Source.ABANDONED_CHECKOUT, "escalate_human")
    assert sources.permits(Source.OVERDUE_INVOICE, "escalate_human")


def test_every_source_can_stop():
    for source in Source:
        assert sources.permits(source, "stop_lost")


def test_contact_window_applies_to_all_three():
    """Debt or not, a human is being contacted."""
    assert all(p.contact_window_applies for p in sources.PROFILES.values())


# --- MSMED s.15 appointed day -------------------------------------------------


def test_agreed_terms_cannot_exceed_the_statutory_ceiling():
    """s.15 says the agreed date or 45 days, WHICHEVER IS EARLIER — so a 90-day
    contract does not buy the buyer 90 days."""
    assert receivables.appointed_day(ACCEPTED, 90) == ACCEPTED + timedelta(days=45)
    assert receivables.appointed_day(ACCEPTED, 120) == ACCEPTED + timedelta(days=45)


def test_shorter_agreed_terms_bind():
    assert receivables.appointed_day(ACCEPTED, 30) == ACCEPTED + timedelta(days=30)


def test_no_written_agreement_gives_fifteen_days():
    assert receivables.appointed_day(ACCEPTED, None) == ACCEPTED + timedelta(days=15)


# --- MSMED s.16 interest ------------------------------------------------------


def test_nothing_accrues_before_the_appointed_day():
    a = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=10), 45)
    assert not a.is_overdue
    assert a.interest_paise == 0
    assert a.total_due_paise == 100_000_00
    assert a.tax_deduction_at_risk is False


def test_interest_is_three_times_the_bank_rate():
    a = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=200), 45)
    assert a.annual_rate == pytest.approx(receivables.RBI_BANK_RATE * 3)


def test_interest_compounds_monthly_not_simply():
    """The Act specifies compound interest with monthly rests. Simple interest would
    understate what is owed, and over the months these disputes run the gap is not
    academic."""
    principal = 100_000_00
    a = receivables.accrue(principal, ACCEPTED, ACCEPTED + timedelta(days=45 + 360), 45)

    monthly = a.annual_rate / 12
    simple = principal * monthly * a.months_elapsed
    assert a.interest_paise > simple


def test_partial_months_do_not_rest():
    """A month that has not completed has not rested. Counting it would overstate a
    statutory figure in a letter to a buyer, which is how a factual reminder becomes an
    indefensible one."""
    twenty_nine = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=45 + 29), 45)
    thirty_one = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=45 + 31), 45)
    assert twenty_nine.months_elapsed == 0
    assert twenty_nine.interest_paise == 0
    assert thirty_one.months_elapsed == 1
    assert thirty_one.interest_paise > 0


def test_tax_deduction_flag_follows_overdue_status():
    current = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=10), 45)
    late = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=90), 45)
    assert current.tax_deduction_at_risk is False
    assert late.tax_deduction_at_risk is True


def test_severity_bands():
    def sev(days):
        return receivables.severity(
            receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=45 + days), 45)
        )

    assert sev(0) == "current"
    assert sev(5) == "recently_overdue"
    assert sev(30) == "materially_overdue"
    assert sev(200) == "aged"


# --- statutory facts ----------------------------------------------------------


def test_no_facts_are_stated_before_anything_is_due():
    a = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=10), 45)
    assert receivables.statutory_facts(a) == []


def test_facts_are_statements_of_law_not_demands():
    """The line between a factual reminder and coercion. Every line must describe what
    the statute provides, never what we will do about it."""
    a = receivables.accrue(100_000_00, ACCEPTED, ACCEPTED + timedelta(days=150), 45)
    facts = receivables.statutory_facts(a)

    assert any("s.15" in f for f in facts)
    assert any("s.16" in f for f in facts)
    assert any("43B(h)" in f for f in facts)

    joined = " ".join(facts).lower()
    for threat in ("we will", "legal action", "court", "recovery agent", "must pay immediately"):
        assert threat not in joined


# --- voice: TRAI gate ---------------------------------------------------------


def _when(hour_ist=11):
    return datetime(2026, 1, 15, hour_ist, tzinfo=IST)


def test_compliant_call_is_permitted():
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=False, consent_reference=None,
        caller_number="1600123456", is_debt=True,
    )
    assert e.permitted, e.blockers


def test_wrong_number_series_blocks_the_call():
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=False, consent_reference=None,
        caller_number="9876543210", is_debt=True,
    )
    assert not e.permitted
    assert any("series" in b for b in e.blockers)


def test_dnd_without_consent_blocks_the_call():
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=True, consent_reference=None,
        caller_number="1600123456", is_debt=True,
    )
    assert not e.permitted
    assert "dnd_registered_without_consent_reference" in e.blockers


def test_dnd_with_recorded_consent_is_permitted():
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=True, consent_reference="consent_2026_0114_abc",
        caller_number="1600123456", is_debt=True,
    )
    assert e.permitted, e.blockers


def test_night_call_blocked_by_the_rbi_window():
    e = voice.check_eligibility(
        when=_when(2), dnd_registered=False, consent_reference=None,
        caller_number="1600123456", is_debt=True,
    )
    assert not e.permitted
    assert "outside_rbi_contact_window" in e.blockers


def test_calling_a_non_debtor_is_never_justifiable():
    """Phoning someone about a cart they abandoned is a marketing call wearing a
    service call's clothes."""
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=False, consent_reference=None,
        caller_number="1600123456", is_debt=False,
    )
    assert not e.permitted
    assert "no_debt_owed_voice_contact_not_justifiable" in e.blockers


def test_an_offer_reclassifies_the_call_as_promotional():
    e = voice.check_eligibility(
        when=_when(11), dnd_registered=False, consent_reference=None,
        caller_number="1600123456", is_debt=True, contains_offer=True,
    )
    assert not e.permitted
    assert e.required_series == voice.PROMOTIONAL_SERIES


def test_all_blockers_are_reported_not_just_the_first():
    """An operator fixing one blocker needs to know three others are queued behind it."""
    e = voice.check_eligibility(
        when=_when(3), dnd_registered=True, consent_reference=None,
        caller_number="9876543210", is_debt=False,
    )
    assert len(e.blockers) >= 4


# --- voice: script ------------------------------------------------------------


def test_hinglish_script_discloses_automation_within_fifteen_seconds():
    script = voice.build_script(merchant_name="Acme", amount_paise=99900, rail=Rail.UPI_AUTOPAY)
    ok, problems = voice.verify_script(script)
    assert ok, problems

    disclosure = next(s for s in script if s["role"] == "disclosure")
    assert disclosure["at_second"] < voice.AUTOMATED_DISCLOSURE_SECONDS
    assert "automated" in disclosure["text"].lower()


def test_script_carries_the_amount_and_instrument():
    script = voice.build_script(merchant_name="Acme", amount_paise=99900, rail=Rail.CARD)
    joined = " ".join(s["text"] for s in script)
    assert "999" in joined
    assert "card" in joined


def test_verifier_rejects_a_late_disclosure():
    script = voice.build_script(merchant_name="Acme", amount_paise=99900)
    script[0]["at_second"] = 20
    ok, problems = voice.verify_script(script)
    assert not ok
    assert "disclosure_after_15_second_deadline" in problems


def test_verifier_rejects_promotional_language():
    script = voice.build_script(merchant_name="Acme", amount_paise=99900)
    script.append({"at_second": 30, "role": "extra", "text": "Also, enjoy 20% cashback!"})
    ok, problems = voice.verify_script(script)
    assert not ok
    assert any(p.startswith("promotional_language") for p in problems)


def test_verifier_rejects_coercive_language():
    script = voice.build_script(merchant_name="Acme", amount_paise=99900)
    script.append({"at_second": 30, "role": "extra", "text": "We will take legal action."})
    ok, problems = voice.verify_script(script)
    assert not ok
    assert any(p.startswith("coercive_language") for p in problems)


def test_blocked_calls_are_still_logged():
    """A log containing only calls that went out cannot demonstrate the gate ever
    stopped anything — which is exactly what a compliance review is looking for."""
    when = _when(2)
    e = voice.check_eligibility(
        when=when, dnd_registered=False, consent_reference=None,
        caller_number="1600123456", is_debt=True,
    )
    record = voice.call_record(
        when=when, customer_number_masked="+91XXXXXX4471", caller_number="1600123456",
        eligibility=e, placed=False, script=voice.build_script(merchant_name="A", amount_paise=1000),
    )
    assert record["permitted"] is False
    assert record["placed"] is False
    assert record["blockers"]
    assert record["category"] == "transactional"


def test_dialling_is_not_faked():
    """A place_call that returned success would make every check above decorative."""
    with pytest.raises(NotImplementedError):
        voice.place_call()


# --- end-to-end through the decision engine -----------------------------------


@pytest.fixture
def db():
    from app.db import make_session_factory

    factory, engine = make_session_factory("sqlite:///:memory:")
    session = factory()
    yield session
    session.close()
    engine.dispose()


def _payment(db, **kw):
    from app.models import FailedPayment

    now = kw.pop("now", datetime(2026, 1, 15, 11, tzinfo=IST))
    defaults = dict(
        razorpay_payment_id="pay_x", customer_id="cust_x", rail="card",
        amount_paise=99900, error_code="insufficient_funds", first_failed_at=now,
    )
    defaults.update(kw)
    p = FailedPayment(**defaults)
    db.add(p)
    db.flush()
    return p


def test_abandoned_checkout_gets_one_nudge_then_stops(db):
    """Not a debt, so no ladder: exactly one ask, then the case closes."""
    from app import decision_engine

    now = datetime(2026, 1, 15, 11, tzinfo=IST)
    payment = _payment(db, source="abandoned_checkout", now=now)

    first = decision_engine.decide(db, payment, now=now)
    assert first.action == "send_payment_link"

    payment.retry_count = 1
    second = decision_engine.decide(db, payment, now=now)
    assert second.action == "stop_lost"


def test_abandoned_checkout_is_never_retried(db):
    """No mandate exists, so a retry is not merely discouraged — it is impossible."""
    from app import decision_engine

    now = datetime(2026, 1, 15, 11, tzinfo=IST)
    for rail in ("card", "upi_autopay", "enach"):
        payment = _payment(db, source="abandoned_checkout", rail=rail,
                           razorpay_payment_id=f"pay_{rail}", now=now)
        action = decision_engine.decide(db, payment, now=now).action
        assert action not in ("retry_now", "retry_at", "monitor_gateway_retry")


def test_overdue_msme_invoice_carries_its_statutory_position(db):
    """The audit trail must show the accrued interest and the tax exposure, because
    that is the entire leverage on this source and it has to be inspectable."""
    from sqlalchemy import select

    from app import decision_engine
    from app.models import AuditLog

    now = datetime(2026, 7, 1, 11, tzinfo=IST)
    payment = _payment(
        db, source="overdue_invoice", amount_paise=50_000_00,
        invoice_accepted_on=datetime(2026, 1, 1, tzinfo=IST),
        agreed_credit_days=45, supplier_is_msme=True, now=now,
    )

    decision = decision_engine.decide(db, payment, now=now)
    assert decision.action == "send_payment_link"

    entry = db.scalar(
        select(AuditLog).where(AuditLog.stage == "compliance").order_by(AuditLog.id.desc())
    )
    assert entry.detail["days_overdue"] > 100
    assert entry.detail["statutory_interest_paise"] > 0
    assert entry.detail["tax_deduction_at_risk"] is True
    assert entry.detail["severity"] == "aged"


def test_invoice_still_inside_terms_is_not_chased_as_overdue(db):
    from sqlalchemy import select

    from app import decision_engine
    from app.models import AuditLog

    now = datetime(2026, 1, 20, 11, tzinfo=IST)
    payment = _payment(
        db, source="overdue_invoice", amount_paise=50_000_00,
        invoice_accepted_on=datetime(2026, 1, 1, tzinfo=IST),
        agreed_credit_days=45, supplier_is_msme=True, now=now,
    )
    decision_engine.decide(db, payment, now=now)

    entry = db.scalar(
        select(AuditLog).where(AuditLog.stage == "compliance").order_by(AuditLog.id.desc())
    )
    assert entry.detail["days_overdue"] == 0
    assert entry.detail["statutory_interest_paise"] == 0
    assert entry.detail["tax_deduction_at_risk"] is False


def test_non_msme_invoice_claims_no_statutory_interest(db):
    """The MSMED interest applies to MSME-registered suppliers. Asserting it for a
    supplier outside the Act would be stating a legal position that does not exist."""
    from sqlalchemy import select

    from app import decision_engine
    from app.models import AuditLog

    now = datetime(2026, 7, 1, 11, tzinfo=IST)
    payment = _payment(
        db, source="overdue_invoice", amount_paise=50_000_00,
        invoice_accepted_on=datetime(2026, 1, 1, tzinfo=IST),
        agreed_credit_days=45, supplier_is_msme=False, now=now,
    )
    decision_engine.decide(db, payment, now=now)

    entry = db.scalar(
        select(AuditLog).where(AuditLog.stage == "compliance").order_by(AuditLog.id.desc())
    )
    assert "statutory_interest_paise" not in entry.detail


def test_night_contact_is_deferred_on_every_source(db):
    from app import decision_engine
    from app.contact_policy import within_contact_window

    night = datetime(2026, 1, 15, 2, tzinfo=IST)
    for i, source in enumerate(("abandoned_checkout", "overdue_invoice")):
        payment = _payment(db, source=source, razorpay_payment_id=f"pay_n{i}", now=night)
        decision = decision_engine.decide(db, payment, now=night)
        assert within_contact_window(decision.scheduled_at), source
