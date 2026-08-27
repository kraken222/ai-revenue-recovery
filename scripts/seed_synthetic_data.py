"""Generates a synthetic batch of failed payments, drives them through the full
pipeline (ingestion -> classify -> compliance -> guardrails -> execute), fast-forwards
a simulation clock past cooldown/notice windows so retries actually play out, and
prints a summary — including the intervention-vs-control recovery lift, which is the
number this whole design exists to produce honestly.

Run from the repo root:
    python -m scripts.seed_synthetic_data [N]
"""

import hashlib
import random
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import bandit, pipeline, promises, worker  # noqa: E402
from app.decision_engine import decide  # noqa: E402
from app.executor import execute  # noqa: E402
from app.bandit import parse_arm_key  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import SessionLocal, init_db  # noqa: E402
from app.escalation import PROMISE_GRACE_HOURS  # noqa: E402
from app.metrics import compliance_invariants  # noqa: E402
from app.models import (  # noqa: E402
    ActionLog,
    Category,
    Classification,
    Decision,
    FailedPayment,
    PaymentStatus,
    PromiseToPay,
    Rail,
)
from app.taxonomy import error_codes_for_rail  # noqa: E402

RAIL_WEIGHTS = {Rail.UPI_AUTOPAY: 0.5, Rail.CARD: 0.3, Rail.ENACH: 0.2}
CUSTOMERS = [f"cust_{i:03d}" for i in range(40)]
ISSUERS = [f"issuer_{i}" for i in range(6)]

# Ground-truth probability a retry succeeds, by category — used only to simulate the
# world for this demo. Intervention arm gets the system's chosen action; control arm
# gets nothing and only "organically" self-cures at a much lower rate, which is the
# whole premise of measuring a causal lift instead of a raw recovery count.
INTERVENTION_SUCCESS_PROB = {
    Category.SOFT_DECLINE.value: 0.55,
    Category.TECHNICAL.value: 0.65,
}
# Per-round self-cure probability for the untouched control arm. Calibrated so the
# CUMULATIVE rate over ROUNDS lands near ~15% (soft) / ~10% (technical), which is the
# realistic share of failed payments that resolve with no dunning at all. Setting the
# per-round figure to the intended cumulative one is an easy mistake and it inflates
# the control arm to ~70%, which would make any intervention look worthless.
ORGANIC_SELF_CURE_PROB = {
    Category.SOFT_DECLINE.value: 0.028,
    Category.TECHNICAL.value: 0.017,
}

# Hidden ground truth the bandit is supposed to discover: retries land better in the
# early-morning window, when salary credits and wallet top-ups have just posted. This
# mirrors the real time-of-day effect Stripe reports for Smart Retries. The bandit is
# NOT told these multipliers — it has to learn them from binary outcomes alone, which
# is exactly what the arm table at the end of the run demonstrates.
TOD_MULTIPLIER = {0: 1.45, 6: 1.05, 12: 0.70, 18: 0.85}

# Dunning pressure actually costs customers in this world, rising with each contact.
# Without this, the simulation has no downside to over-contacting, the EV gate can only
# ever give up revenue, and any "churn saved" figure would be an assumption scored as
# if it were a result. Making churn a real simulated event is what lets the ablation
# measure the gate's value instead of asserting it.
WORLD_CHURN_RISK_PER_CONTACT = 0.008

# Razorpay's OWN dunning on the card rail: it auto-retries a failed subscription
# charge daily, and the subscription halts once those are exhausted. Modelling this is
# not optional — the system deliberately does nothing while the gateway owns the retry,
# so a world that never fires those retries leaves every card payment waiting forever
# and makes correct behaviour look like a hang.
GATEWAY_MAX_RETRIES = 3
GATEWAY_RETRY_SUCCESS_PROB = 0.28

ROUNDS = 6
ROUND_STEP = timedelta(hours=25)


def world_draw(*parts) -> float:
    """A deterministic uniform draw keyed on what it describes, rather than on the
    position of a call in a global RNG stream.

    This is the common-random-numbers technique: the world's "luck" for a given payment
    at a given attempt is fixed, so two policies compared on the same batch face
    identical conditions and differ only in the decisions they make. Drawing from the
    shared RNG instead would let the stream diverge as soon as one policy consumed a
    different number of draws (the bandit samples per candidate slot), silently
    changing the world between arms and swamping the real effect with sampling noise.
    """
    key = "|".join(str(p) for p in parts).encode()
    digest = hashlib.blake2b(key, digest_size=8).digest()
    return int.from_bytes(digest, "big") / float(1 << 64)


def _weighted_choice(weights: dict):
    items, probs = zip(*weights.items())
    return random.choices(items, weights=probs, k=1)[0]


# Real bank and PSP narrations that no rule table maps. A fraction of the batch gets
# one instead of a clean code, so the run actually exercises the tier-2 path rather
# than only the happy one: offline these route to human review (the conservative
# default), and with credentials the self-consistency ensemble classifies them.
UNMAPPED_NARRATIONS = [
    "INSUFFICIENT BAL IN AC XX4471 AS ON DATE",
    "DO NOT HONOUR",
    "REMITTER BANK SWITCH UNAVAILABLE - NPCI TIMEOUT RC91",
    "MANDATE NOT REGISTERED AT REMITTER BANK",
    "TXN DECLINED BY ISSUER - CONTACT CARD ISSUING BANK",
    "ERR",
]
UNMAPPED_SHARE = 0.06


# Mix of revenue-at-risk sources. Failed payments dominate real traffic, but the batch
# has to contain the other two or their compliance profiles are never exercised — and
# those profiles are where the three sources actually differ.
SOURCE_WEIGHTS = {"failed_payment": 0.72, "abandoned_checkout": 0.18, "overdue_invoice": 0.10}


def _random_payment_payload(i: int, t: datetime) -> dict:
    source = _weighted_choice(SOURCE_WEIGHTS)
    if source != "failed_payment":
        return _non_payment_payload(i, t, source)
    rail = _weighted_choice(RAIL_WEIGHTS)
    error_code = random.choice(error_codes_for_rail(rail))
    narration = None
    if random.random() < UNMAPPED_SHARE:
        narration = random.choice(UNMAPPED_NARRATIONS)
        error_code = "UNMAPPED"
    amount_paise = random.choice([49900, 99900, 199900, 499900, 999900, 1_800_000])
    mandate_revoked = rail in (Rail.UPI_AUTOPAY, Rail.ENACH) and random.random() < 0.05

    return {
        "id": f"pay_synth_{i:05d}",
        "customer_id": random.choice(CUSTOMERS),
        "subscription_id": f"sub_synth_{i % 50:04d}",
        "rail": rail.value,
        "amount_paise": amount_paise,
        "currency": "INR",
        "error_code": error_code,
        "error_description": narration or f"synthetic {error_code} on {rail.value}",
        "issuer_id": random.choice(ISSUERS),
        "mandate_revoked": mandate_revoked,
        "source": "failed_payment",
    }


def _non_payment_payload(i: int, t: datetime, source: str) -> dict:
    """An abandoned checkout or an overdue B2B invoice.

    Neither carries a rail in the payments sense — nothing was ever debited — but the
    column is not nullable, so they take `card` as the checkout surface they were
    abandoned on. The `source` field is what actually decides how they are treated.
    """
    base = {
        "id": f"pay_synth_{i:05d}",
        "customer_id": random.choice(CUSTOMERS),
        "rail": Rail.CARD.value,
        "currency": "INR",
        "issuer_id": random.choice(ISSUERS),
        "mandate_revoked": False,
        "source": source,
    }

    if source == "abandoned_checkout":
        base.update(
            {
                "amount_paise": random.choice([49900, 99900, 199900, 349900]),
                # No decline exists — nobody attempted a payment. Saying otherwise in
                # the record would make every downstream message a false statement.
                "error_code": "checkout_abandoned",
                "error_description": "customer left checkout without attempting payment",
            }
        )
        return base

    accepted = t - timedelta(days=random.choice([20, 50, 75, 120, 200]))
    base.update(
        {
            "amount_paise": random.choice([2_500_00, 12_000_00, 48_000_00, 150_000_00]),
            "error_code": "invoice_overdue",
            "error_description": "B2B invoice past agreed terms",
            "invoice_accepted_on": accepted.isoformat(),
            "agreed_credit_days": random.choice([30, 45, 60]),
            "supplier_is_msme": random.random() < 0.7,
        }
    )
    return base


def _latest_category(db, payment_id: str) -> str | None:
    row = db.scalar(
        select(Classification.category)
        .where(Classification.failed_payment_id == payment_id)
        .order_by(Classification.id.desc())
    )
    return row


def _acting_arm_bucket(db, payment_id: str) -> int | None:
    """Which time-of-day bucket did the bandit actually pick for the attempt we're now
    resolving? Read from the decision itself rather than inferred from execution time,
    since the worker fires at its poll tick, not exactly at the scheduled instant."""
    key = db.scalar(
        select(Decision.bandit_arm_key)
        .where(Decision.failed_payment_id == payment_id, Decision.bandit_arm_key.is_not(None))
        .order_by(Decision.id.desc())
    )
    return parse_arm_key(key)[2] if key else None


def simulate_outcomes_for_executed(db, t: datetime) -> int:
    executed = db.scalars(
        select(FailedPayment).where(FailedPayment.status == PaymentStatus.EXECUTED.value)
    ).all()
    for payment in executed:
        category = _latest_category(db, payment.id)
        prob = INTERVENTION_SUCCESS_PROB.get(category, 0.3)

        bucket = _acting_arm_bucket(db, payment.id)
        if bucket is not None:
            prob = min(1.0, prob * TOD_MULTIPLIER[bucket])

        attempt_no = payment.retry_count
        success = world_draw(payment.razorpay_payment_id, "attempt", attempt_no) < prob

        # Every contact carries a chance the customer cancels out of annoyance, rising
        # with how many times we've already chased them. Rolled regardless of outcome:
        # a customer can pay this invoice and still cancel the subscription.
        churn_prob = min(1.0, WORLD_CHURN_RISK_PER_CONTACT * (attempt_no + 1))
        churned = world_draw(payment.razorpay_payment_id, "churn", attempt_no) < churn_prob

        pipeline.ingest_outcome(
            db, f"evt_outcome_{payment.id}_{attempt_no}", payment.razorpay_payment_id, success, now=t
        )

        if churned:
            payment.churned_from_dunning = True
            # A cancelled subscription is a revoked mandate — reusing that flag means
            # the existing compliance hard-stop halts further contact automatically.
            # The timestamp matters: contacts made before this instant were legal.
            payment.mandate_revoked = True
            payment.mandate_revoked_at = t
            if payment.status != PaymentStatus.RECOVERED.value:
                payment.status = PaymentStatus.LOST.value

    db.commit()  # once per round, not once per payment (each commit is an fsync)
    return len(executed)


def simulate_gateway_retries(db, t: datetime, round_idx: int) -> int:
    """Fire Razorpay's own retry cycle for cards we are monitoring.

    Applied to BOTH arms, deliberately. The gateway retries regardless of anything the
    merchant does, so crediting those recoveries to the intervention arm alone would
    manufacture lift out of something we did not cause. Keeping it symmetrical is what
    keeps the causal estimate honest.
    """
    watching = db.scalars(
        select(FailedPayment).where(
            FailedPayment.rail == Rail.CARD.value,
            FailedPayment.status == PaymentStatus.WAITING.value,
            FailedPayment.gateway_exhausted.is_(False),
        )
    ).all()

    fired = 0
    for payment in watching:
        payment.gateway_retry_count += 1
        fired += 1
        won = world_draw(payment.razorpay_payment_id, "gateway", payment.gateway_retry_count) < (
            GATEWAY_RETRY_SUCCESS_PROB
        )
        if won:
            payment.status = PaymentStatus.RECOVERED.value
            payment.recovered_at = t
        elif payment.gateway_retry_count >= GATEWAY_MAX_RETRIES:
            # Subscription halts. Control of the case returns to the merchant, which is
            # where this system's card-rail work actually begins.
            payment.gateway_exhausted = True
    db.commit()
    return fired


def simulate_control_group_self_cure(db, t: datetime, is_last_round: bool, round_idx: int = 0) -> int:
    """Synthetic-only: the control arm gets zero intervention from us, but a fraction
    of customers pay on their own anyway (top up a wallet, retry in the app
    themselves). This is what makes "recovered vs control" a real causal estimate
    instead of a raw recovery count — see the sprint-3 design notes."""
    control_waiting = db.scalars(
        select(FailedPayment).where(
            FailedPayment.status == PaymentStatus.WAITING.value,
            FailedPayment.control_group.is_(True),
        )
    ).all()
    cured = 0
    for payment in control_waiting:
        category = _latest_category(db, payment.id)
        prob = ORGANIC_SELF_CURE_PROB.get(category, 0.05)
        if world_draw(payment.razorpay_payment_id, "selfcure", round_idx) < prob:
            payment.status = PaymentStatus.RECOVERED.value
            payment.recovered_at = t
            cured += 1
        elif is_last_round:
            payment.status = PaymentStatus.LOST.value
    db.commit()
    return cured


def print_summary(db) -> None:
    payments = db.scalars(select(FailedPayment)).all()
    print(f"\n=== {len(payments)} synthetic failed payments ===")

    by_status = Counter(p.status for p in payments)
    print("By status:", dict(by_status))

    by_source = Counter(p.source for p in payments)
    print("By source:", dict(by_source))
    by_rail = Counter(p.rail for p in payments)
    print("By rail:  ", dict(by_rail))

    intervention = [p for p in payments if not p.control_group]
    control = [p for p in payments if p.control_group]

    def recovery_rate(group):
        eligible = [p for p in group if p.status in (PaymentStatus.RECOVERED.value, PaymentStatus.LOST.value)]
        if not eligible:
            return None
        recovered = sum(1 for p in eligible if p.status == PaymentStatus.RECOVERED.value)
        return recovered / len(eligible), len(eligible), recovered

    int_rate = recovery_rate(intervention)
    ctrl_rate = recovery_rate(control)

    print(f"\nIntervention arm: n={len(intervention)}", end="")
    if int_rate:
        rate, n, recovered = int_rate
        print(f", resolved={n}, recovered={recovered}, recovery_rate={rate:.1%}")
    else:
        print(", nothing resolved yet")

    print(f"Control arm (holdout, no action): n={len(control)}", end="")
    if ctrl_rate:
        rate, n, recovered = ctrl_rate
        print(f", resolved={n}, recovered={recovered}, recovery_rate={rate:.1%}")
    else:
        print(", nothing resolved yet")

    if int_rate and ctrl_rate:
        lift = int_rate[0] - ctrl_rate[0]
        print(f"\n>>> Causal recovery lift: {lift:+.1%} (intervention vs. no-action control) <<<")

    recovered_paise = sum(p.amount_paise for p in payments if p.status == PaymentStatus.RECOVERED.value)
    print(f"\nGross recovered: Rs.{recovered_paise / 100:,.2f}")
    print_stop_reasons(db)
    print_net_value(db, payments)
    print_compliance_audit(db)


def print_stop_reasons(db) -> None:
    rows = db.execute(
        select(Decision.policy_rule_id, Decision.blocked_reason, func.count(Decision.id))
        .where(Decision.action == "stop_lost")
        .group_by(Decision.policy_rule_id, Decision.blocked_reason)
        .order_by(func.count(Decision.id).desc())
    ).all()
    if not rows:
        return
    print("\nWhy payments were stopped:")
    for rule, reason, count in rows:
        print(f"  {count:>4}  {rule}  ({reason or 'ev_gate'})")


def print_net_value(db, payments) -> None:
    """Raw recovery rate alone makes the EV gate look like a regression — it refuses
    chases on purpose. Net value is the number the gate is actually optimising, so it
    has to be reported alongside, or the tradeoff is invisible.

    The churn figure is a MODELLED estimate using the same assumptions the gate decides
    on, not a measurement. It is reported to show the tradeoff being made, and it is
    only as good as `churn_risk_per_contact`, which needs real cohort data to calibrate.
    """
    contact_counts = dict(
        db.execute(
            select(ActionLog.failed_payment_id, func.count(ActionLog.id)).group_by(
                ActionLog.failed_payment_id
            )
        ).all()
    )

    recovered = sum(p.amount_paise for p in payments if p.status == PaymentStatus.RECOVERED.value)
    total_contacts = sum(contact_counts.values())
    contact_spend = total_contacts * settings.contact_cost_paise
    modelled_churn = sum(
        min(1.0, settings.churn_risk_per_contact * contact_counts.get(p.id, 0))
        * p.amount_paise
        * settings.ltv_multiple
        for p in payments
    )

    print("\nNet economic value (what the EV gate optimises):")
    print(f"  gross recovered        Rs.{recovered / 100:>12,.2f}")
    print(f"  contact spend          Rs.{-contact_spend / 100:>12,.2f}  ({total_contacts} contacts)")
    print(f"  modelled churn cost    Rs.{-modelled_churn / 100:>12,.2f}")
    print(f"  {'-' * 42}")
    print(f"  net                    Rs.{(recovered - contact_spend - modelled_churn) / 100:>12,.2f}")


def print_compliance_audit(db) -> None:
    """Print the invariants the dashboard asserts, from the same code.

    This used to be a second implementation of the same queries, and it had already
    drifted: the dashboard grew two source-specific checks that this copy did not have,
    so a run could report four green invariants while the API reported six. One
    definition, two renderings.
    """
    print("\n=== Compliance invariants (measured on executed actions) ===")
    for check in compliance_invariants(db):
        status = "PASS" if check["pass"] else "FAIL"
        print(f"  [{status}] {check['check']}: {check['violations']}")


def print_bandit_table(db) -> None:
    arms = [a for a in bandit.snapshot(db) if a.pulls > 0]
    if not arms:
        print("\nNo bandit arms pulled.")
        return

    print("\n=== Learned retry-slot posteriors (Thompson Sampling) ===")
    print("Ground truth the bandit was never told:", TOD_MULTIPLIER)
    print(f"{'rail':<13} {'category':<14} {'slot(UTC)':<11} {'pulls':>6} {'win rate':>9}")
    for arm in sorted(arms, key=lambda a: (a.rail, a.category, a.tod_bucket)):
        slot = f"{arm.tod_bucket:02d}:00-{arm.tod_bucket + 6:02d}:00"
        print(f"{arm.rail:<13} {arm.category:<14} {slot:<11} {arm.pulls:>6} {arm.posterior_mean:>8.1%}")

    by_bucket: dict[int, list[float]] = {}
    for arm in arms:
        by_bucket.setdefault(arm.tod_bucket, []).append(arm.posterior_mean)
    print("\nMean posterior by time-of-day bucket (should track the ground truth above):")
    for bucket in sorted(by_bucket):
        mean = sum(by_bucket[bucket]) / len(by_bucket[bucket])
        bar = "#" * int(mean * 40)
        print(f"  {bucket:02d}:00-{bucket + 6:02d}:00  {mean:>6.1%}  {bar}")


# Share of contacted customers who respond with a dated commitment, and the share of
# those who then keep it. Both are invented, but the shape is what matters for the
# demo: most promises are kept, a minority break, and a small number break twice —
# which is what drives a case to the top of the escalation ladder.
PROMISE_RESPONSE_RATE = 0.35
PROMISE_KEPT_RATE = 0.6


def simulate_promises(db, t: datetime) -> int:
    """Some customers who were contacted reply with "I will pay on Friday".

    Without this the promise machinery is dead code in every run: no promise is ever
    made, so nothing is ever suppressed and the ladder never sees a broken commitment.
    """
    # Anyone we have actually contacted and who is still unresolved. Selecting only
    # EXECUTED misses almost everyone: that status lasts a single round before the
    # outcome resolves it, so the window to reply is effectively zero and the promise
    # machinery never runs.
    contacted = db.scalars(
        select(FailedPayment)
        .join(ActionLog, ActionLog.failed_payment_id == FailedPayment.id)
        .where(
            FailedPayment.status.in_(
                (PaymentStatus.EXECUTED.value, PaymentStatus.WAITING.value)
            ),
            FailedPayment.control_group.is_(False),
        )
        .distinct()
        # Defensive, and honestly labelled as such: with the settlement draw now keyed
        # on the promise's own date rather than on its insertion sequence, row order no
        # longer changes any outcome, and removing this line does not make the world
        # non-deterministic (verified by reverting it against
        # tests/test_simulation_determinism.py). It is kept because SELECT DISTINCT
        # dedupes through a temporary structure keyed on the selected columns --
        # `failed_payments.id` among them, a uuid4 and so unseeded -- which means row
        # order here is genuinely arbitrary between runs. Any future draw that keys on
        # creation order would silently inherit that arbitrariness. Ordering on the
        # synthetic payment id costs nothing and closes the door.
        .order_by(FailedPayment.razorpay_payment_id)
    ).all()

    made = 0
    for payment in contacted:
        if world_draw(payment.razorpay_payment_id, "promise", payment.retry_count) >= PROMISE_RESPONSE_RATE:
            continue
        # Two to four days out: inside the horizon, past the grace period.
        days = 2 + int(world_draw(payment.razorpay_payment_id, "promiseday", payment.retry_count) * 3)
        if promises.record_promise(db, payment.id, t + timedelta(days=days), now=t):
            made += 1
    db.commit()
    return made


def _settle_matured_promises(db, t: datetime) -> int:
    """Resolve promises whose date plus grace has elapsed. Whether one is kept is a
    world fact, not a system decision, so it is drawn here rather than inferred."""
    matured = db.scalars(
        select(PromiseToPay).where(
            PromiseToPay.status == "open",
            PromiseToPay.promised_for < t - timedelta(hours=PROMISE_GRACE_HOURS),
        )
    ).all()
    for promise in matured:
        payment = db.get(FailedPayment, promise.failed_payment_id)
        # Key the draw on the SYNTHETIC payment id, as every other world_draw does.
        # This read `promise.failed_payment_id`, which is the internal primary key --
        # a uuid4, and so not seeded by `random.seed`. That made whether a promise was
        # kept genuinely random per run rather than a fixed property of the world, and
        # it broke common random numbers precisely where the ablation depends on them:
        # two policies compared on "the same" batch were silently facing different
        # promise outcomes. It surfaced as the fixed-schedule baseline drifting by a
        # few hundred rupees between runs of an otherwise fully pinned harness.
        # Keyed on the promise's own date rather than on `promise.id`. The id is an
        # insertion sequence number, so keying on it makes the world's answer depend on
        # the order promises happened to be created in -- the same class of bug as
        # keying on the uuid, just one step removed. A promised date is a property of
        # the promise itself and holds whatever order the rows were written in.
        kept = (
            world_draw(
                payment.razorpay_payment_id,
                "promisekept",
                promise.promised_for.isoformat(),
            )
            < PROMISE_KEPT_RATE
            if payment
            else False
        )
        promises.settle_promises(db, promise.failed_payment_id, paid=kept, now=t)
        if kept:
            if payment and payment.status not in (
                PaymentStatus.RECOVERED.value,
                PaymentStatus.LOST.value,
            ):
                payment.status = PaymentStatus.RECOVERED.value
                payment.recovered_at = t
    db.commit()
    return len(matured)


def _rerun_halted_cards(db, t: datetime) -> int:
    """Re-decide cards the gateway has just given up on. Until the handover they were
    correctly parked in WAITING with no merchant action; once halted, compliance offers
    a payment link and the case has to be re-evaluated to pick it up."""
    halted = db.scalars(
        select(FailedPayment).where(
            FailedPayment.rail == Rail.CARD.value,
            FailedPayment.status == PaymentStatus.WAITING.value,
            FailedPayment.gateway_exhausted.is_(True),
        )
    ).all()
    for payment in halted:
        decision = decide(db, payment, now=t)
        execute(db, payment, decision, now=t)
    db.commit()
    return len(halted)


def run_simulation(db, n: int, seed: int | None = None) -> None:
    """Drive n synthetic failures through the real pipeline against a simulated clock.
    Separated from reporting so the ablation harness can reuse it under different
    configurations."""
    if seed is not None:
        random.seed(seed)

    t = datetime(2026, 1, 1, tzinfo=timezone.utc)

    # Generate the whole population BEFORE any of it is ingested. Interleaving would
    # let policy decisions (the bandit samples per candidate slot) consume RNG draws
    # between generations, so a later payment's rail/amount would depend on the policy
    # under test — and the ablation would no longer be comparing policies on the same
    # batch.
    payloads = [_random_payment_payload(i, t) for i in range(n)]
    for i, payload in enumerate(payloads):
        pipeline.ingest_event(db, f"evt_fail_{i:05d}", "payment.failed", {"payment": payload}, now=t)

    simulate_outcomes_for_executed(db, t)

    for round_idx in range(ROUNDS):
        t += ROUND_STEP
        simulate_gateway_retries(db, t, round_idx)
        _rerun_halted_cards(db, t)
        simulate_promises(db, t)
        # Mature any promise whose date has passed. Some are kept (the customer pays),
        # the rest break — and it is the broken ones that drive the escalation ladder,
        # so a run where every promise silently stays open exercises nothing.
        _settle_matured_promises(db, t)
        worker.process_due_retries(db, now=t)
        simulate_outcomes_for_executed(db, t)
        simulate_control_group_self_cure(
            db, t, is_last_round=(round_idx == ROUNDS - 1), round_idx=round_idx
        )


def main(n: int) -> None:
    init_db()
    db = SessionLocal()
    run_simulation(db, n)
    print_summary(db)
    print_bandit_table(db)
    db.close()


if __name__ == "__main__":
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 80
    main(n)
