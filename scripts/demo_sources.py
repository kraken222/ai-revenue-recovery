"""Show the same engine treating three kinds of revenue-at-risk differently.

Track 03 names all three in one sentence — "payment failures and checkout abandonment
to overdue receivables" — and they are one problem: detect, decide, act, bounded. They
are emphatically not one compliance regime, and this script exists to make that visible
rather than asserted.

    python -m scripts.demo_sources

Runs entirely offline against an in-memory database.
"""

import sys
from datetime import datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import select  # noqa: E402

from app import decision_engine, receivables, sources, voice  # noqa: E402
from app.contact_policy import IST  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.models import AuditLog, FailedPayment, Rail  # noqa: E402
from app.sources import Source  # noqa: E402

NOON = datetime(2026, 7, 1, 12, tzinfo=IST)
NIGHT = datetime(2026, 7, 1, 2, tzinfo=IST)


def rule(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def make(db, **kw) -> FailedPayment:
    defaults = dict(
        razorpay_payment_id=kw.pop("pid", "pay_demo"),
        customer_id="cust_demo",
        rail=Rail.CARD.value,
        amount_paise=99900,
        error_code="insufficient_funds",
        first_failed_at=NOON,
    )
    defaults.update(kw)
    payment = FailedPayment(**defaults)
    db.add(payment)
    db.flush()
    return payment


def latest_compliance(db, payment_id: str) -> dict:
    entry = db.scalar(
        select(AuditLog)
        .where(AuditLog.failed_payment_id == payment_id, AuditLog.stage == "compliance")
        .order_by(AuditLog.id.desc())
    )
    return entry.detail if entry else {}


def show_profiles() -> None:
    rule("1. Three sources, three compliance regimes")
    for source in Source:
        p = sources.profile_for(source)
        print(f"\n  {source.value}")
        print(f"    debt owed        {p.is_debt}")
        print(f"    mandate held     {p.has_mandate}")
        print(f"    contact budget   {p.max_contacts}")
        print(f"    ladder allowed   {p.escalation_allowed}")
        print(f"    actions          {', '.join(p.allowed_actions)}")
        print(f"    {p.notes}")


def show_decisions(db) -> None:
    rule("2. The same engine, asked about each source at noon")

    cases = [
        ("failed subscription charge", dict(pid="pay_a", source=Source.FAILED_PAYMENT.value)),
        ("abandoned checkout", dict(pid="pay_b", source=Source.ABANDONED_CHECKOUT.value,
                                    error_code="checkout_abandoned")),
        ("overdue MSME invoice", dict(
            pid="pay_c", source=Source.OVERDUE_INVOICE.value, error_code="invoice_overdue",
            amount_paise=50_000_00, invoice_accepted_on=datetime(2026, 1, 1, tzinfo=IST),
            agreed_credit_days=45, supplier_is_msme=True)),
    ]

    for label, kw in cases:
        payment = make(db, **kw)
        decision = decision_engine.decide(db, payment, now=NOON)
        detail = latest_compliance(db, payment.id)
        print(f"\n  {label}")
        print(f"    action  {decision.action}")
        print(f"    rule    {detail.get('policy_rule_id')}")
        if "days_overdue" in detail:
            print(f"    overdue {detail['days_overdue']} days, "
                  f"statutory interest Rs.{detail['statutory_interest_paise'] / 100:,.2f}, "
                  f"tax deduction at risk: {detail['tax_deduction_at_risk']}")
        if detail.get("note"):
            print(f"    note    {detail['note']}")


def show_checkout_budget(db) -> None:
    rule("3. An abandoned checkout is not a debt: one nudge, then stop")
    payment = make(db, pid="pay_d", source=Source.ABANDONED_CHECKOUT.value,
                   error_code="checkout_abandoned")

    for contacts in (0, 1, 2):
        payment.retry_count = contacts
        decision = decision_engine.decide(db, payment, now=NOON)
        detail = latest_compliance(db, payment.id)
        print(f"  after {contacts} contact(s): {decision.action:20} ({detail.get('policy_rule_id')})")

    print("\n  No ladder, no escalation to a human, no dunning language - nobody owes")
    print("  anything. Contacting twice would be marketing pressure, not recovery.")


def show_msmed() -> None:
    rule("4. MSMED Act: the law is already doing the chasing")

    accepted = datetime(2026, 1, 1).date()
    for label, days, credit in [
        ("inside terms", 20, 45),
        ("just past the appointed day", 50, 45),
        ("aged", 200, 45),
        # Same appointed day and same interest as the 45-day row above, which is
        # exactly the point: s.15 says "agreed date OR 45 days, whichever is
        # EARLIER", so a 90-day contract buys the buyer nothing.
        ("90-day contract (identical: s.15 caps it at 45)", 200, 90),
    ]:
        a = receivables.accrue(50_000_00, accepted, accepted + timedelta(days=days), credit)
        print(f"\n  {label}  (accepted +{days}d, agreed {credit}d)")
        print(f"    appointed day {a.appointed_day}  overdue {a.days_overdue}d  "
              f"severity {receivables.severity(a)}")
        print(f"    interest Rs.{a.interest_paise / 100:>10,.2f}   "
              f"total Rs.{a.total_due_paise / 100:,.2f}")

    print("\n  What a reminder may state - facts about the statute, never a threat:")
    aged = receivables.accrue(50_000_00, accepted, accepted + timedelta(days=200), 45)
    for fact in receivables.statutory_facts(aged):
        print(f"    - {fact}")


def show_voice() -> None:
    rule("5. Hinglish voice: the most gated action in the system")

    script = voice.build_script(merchant_name="Acme Foods", amount_paise=99900,
                                rail=Rail.UPI_AUTOPAY)
    ok, problems = voice.verify_script(script)
    print(f"\n  Script verified: {ok}  {problems or ''}")
    for seg in script:
        print(f"    +{seg['at_second']:>2}s  [{seg['role']:10}] {seg['text']}")

    print("\n  Eligibility gate:")
    scenarios = [
        ("compliant daytime call", dict(when=NOON, dnd_registered=False,
                                        consent_reference=None, caller_number="1600123456",
                                        is_debt=True)),
        ("same call at 02:00 IST", dict(when=NIGHT, dnd_registered=False,
                                        consent_reference=None, caller_number="1600123456",
                                        is_debt=True)),
        ("from an ordinary mobile", dict(when=NOON, dnd_registered=False,
                                         consent_reference=None, caller_number="9876543210",
                                         is_debt=True)),
        ("DND, no consent on file", dict(when=NOON, dnd_registered=True,
                                         consent_reference=None, caller_number="1600123456",
                                         is_debt=True)),
        ("abandoned checkout (no debt)", dict(when=NOON, dnd_registered=False,
                                              consent_reference=None,
                                              caller_number="1600123456", is_debt=False)),
    ]
    for label, kw in scenarios:
        e = voice.check_eligibility(**kw)
        verdict = "PERMITTED" if e.permitted else "BLOCKED"
        print(f"    [{verdict:9}] {label}")
        for blocker in e.blockers:
            print(f"                  - {blocker}")

    print("\n  Dialling itself is NOT implemented: it needs a DLT-registered 1600-series")
    print("  originator and a live NCPR lookup. A place_call() that returned success")
    print("  would make every check above decorative.")


def main() -> None:
    factory, engine = make_session_factory("sqlite:///:memory:")
    db = factory()
    try:
        show_profiles()
        show_decisions(db)
        show_checkout_budget(db)
        show_msmed()
        show_voice()
        print(f"\n{'=' * 78}")
        print("One bounded recovery engine. Three regimes. The compliance profile, not")
        print("the algorithm, is what differs between them.")
    finally:
        db.close()
        engine.dispose()


if __name__ == "__main__":
    main()
