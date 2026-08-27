"""The synthetic world must be a pure function of its seed.

The ablation compares policies using common random numbers: every configuration runs
against the same set of worlds, so a difference between them is the policy and not seed
luck. That argument only holds if the world really is identical across runs. When it is
not, the harness still prints a confident paired confidence interval -- it just silently
folds world variance into what it attributes to the policy.

This was real. `_settle_matured_promises` keyed its world draw on
`promise.failed_payment_id` -- the internal primary key, a uuid4, which `random.seed`
does not touch -- so the draw was genuinely random per run. The draw's third key part
was `promise.id`, an insertion sequence number, which made it additionally sensitive to
the order rows happened to be written in. Both are now keyed on stable facts: the
synthetic payment id and the promise's own date.

It surfaced only as the fixed-schedule baseline drifting a few hundred rupees between
runs of an otherwise fully pinned harness. Every individual assertion in the suite still
held; only the aggregate moved.

### Why this is tested at the function, not through a whole run

The obvious test -- run the world twice, compare -- turns out to be nearly blind here,
and it is worth recording why, because it looked convincing. Most promises are settled
by the outcome webhook (`pipeline.ingest_outcome` -> `settle_promises`), whose result
follows the payment, not the draw. Only a promise that matures while its payment is
still unresolved reaches `_settle_matured_promises`, and across a 300-payment world that
happens once or twice. A single Bernoulli draw agrees across two buggy runs about half
the time, so a whole-world comparison passes with the bug in place more often than not.
Both reverts were confirmed to pass such a test.

So the draw is pinned where it lives, with enough promises in one batch that a leak
cannot survive: 24 settlements, agreeing by chance with probability (0.6^2 + 0.4^2)^24,
about one in ten million. The whole-world test is kept as well, but as a coarse smoke
check, and is not relied on to catch this class of bug.
"""

import uuid as uuid_module
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db import make_session_factory
from app.models import FailedPayment, PaymentStatus, PromiseToPay
from scripts.seed_synthetic_data import _settle_matured_promises, run_simulation

SEED = 20260907
N = 150
T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)
# Enough settlements that a uuid leak cannot hide behind coin flips. See module docstring.
PROMISE_BATCH = 24


class _UuidStream:
    """Deterministic stand-in for uuid4, so two runs can be given *different* but
    individually reproducible id streams. Different ids, same seed: exactly the
    contrast that exposes a world draw keyed on a primary key."""

    def __init__(self, tag: int):
        self.tag = tag
        self.n = 0

    def __call__(self):
        self.n += 1
        return uuid_module.UUID(int=(self.tag << 96) | self.n)


class _Streamed:
    def __init__(self, tag: int):
        self.tag = tag

    def __enter__(self):
        self._original = uuid_module.uuid4
        uuid_module.uuid4 = _UuidStream(self.tag)
        return self

    def __exit__(self, *exc):
        uuid_module.uuid4 = self._original


def _settle_batch(tag: int) -> list[tuple[str, str]]:
    """Build a batch of promises that mature with their payments still unresolved --
    the only path that reaches the world draw -- and return how each one settled."""
    with _Streamed(tag):
        factory, engine = make_session_factory("sqlite:///:memory:")
        db = factory()
        try:
            for i in range(PROMISE_BATCH):
                payment = FailedPayment(
                    razorpay_payment_id=f"pay_det_{i:04d}",
                    customer_id=f"cust_{i:04d}",
                    rail="upi_autopay",
                    amount_paise=99900,
                    error_code="insufficient_balance",
                    status=PaymentStatus.WAITING.value,
                    first_failed_at=T0,
                )
                db.add(payment)
                db.flush()
                db.add(
                    PromiseToPay(
                        failed_payment_id=payment.id,
                        promised_for=T0 + timedelta(days=1),
                        status="open",
                        created_at=T0,
                    )
                )
            db.commit()

            # Well past promised_for + grace, so every promise matures at once.
            settled = _settle_matured_promises(db, T0 + timedelta(days=30))
            assert settled == PROMISE_BATCH, f"only {settled} matured; test cannot detect"

            return sorted(
                (p.failed_payment.razorpay_payment_id, p.status)
                for p in db.scalars(select(PromiseToPay)).all()
            )
        finally:
            db.close()
            engine.dispose()


def test_promise_settlement_does_not_depend_on_internal_uuids():
    """The core property, pinned where the bug lived.

    Same inputs, different primary keys. If the world draw touches an id, the batch
    settles differently and this fails.
    """
    first, second = _settle_batch(0xAAAA), _settle_batch(0xBBBB)
    assert first == second, (
        "promise settlement moved when only the uuid stream changed - a world draw is "
        "keyed on an internal id, so common random numbers are broken and the "
        "ablation's paired CI attributes world variance to the policy"
    )


def test_promise_settlement_is_not_trivially_constant():
    """Guard against 'fixing' determinism by making the draw always return the same
    answer, which would pass the test above and destroy the simulation."""
    outcomes = {status for _, status in _settle_batch(0xAAAA)}
    assert outcomes == {"kept", "broken"}, (
        f"expected both outcomes across {PROMISE_BATCH} promises, got {outcomes}"
    )


def test_promise_settlement_is_repeatable_within_a_stream():
    assert _settle_batch(0xAAAA) == _settle_batch(0xAAAA)


# --- coarse whole-world smoke check -------------------------------------------
# Deliberately not the primary guard; see the module docstring for why it is weak
# against this specific bug. It still catches a gross loss of determinism.


def _world(seed: int, tag: int) -> dict:
    with _Streamed(tag):
        factory, engine = make_session_factory("sqlite:///:memory:")
        db = factory()
        try:
            run_simulation(db, N, seed=seed)
            payments = db.scalars(select(FailedPayment)).all()
            return {
                "payments": {
                    p.razorpay_payment_id: (p.status, p.retry_count, p.churned_from_dunning)
                    for p in payments
                },
                "control": {p.razorpay_payment_id for p in payments if p.control_group},
            }
        finally:
            db.close()
            engine.dispose()


@pytest.fixture(scope="module")
def worlds():
    return {
        "a": _world(SEED, 0xAAAA),
        "b": _world(SEED, 0xBBBB),
        "other_seed": _world(SEED + 1, 0xAAAA),
    }


def test_world_is_stable_across_uuid_streams(worlds):
    assert worlds["a"]["payments"] == worlds["b"]["payments"]


def test_holdout_assignment_is_stable_across_uuid_streams(worlds):
    """Control assignment hashes the synthetic payment id rather than drawing from the
    RNG, so it must survive a change of primary keys - that is what makes a comparison
    between policy variants valid at all."""
    assert worlds["a"]["control"], "no payments landed in the holdout; no control arm"
    assert worlds["a"]["control"] == worlds["b"]["control"]


def test_different_seeds_produce_different_worlds(worlds):
    """The guard against freezing the world: a simulation that ignored its seed would
    pass every test above."""
    assert worlds["a"]["payments"] != worlds["other_seed"]["payments"]
