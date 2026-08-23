"""Ablation harness: does the learned machinery actually earn its complexity?

Runs the same synthetic batch, same seed, same world, under four configurations:

    fixed-schedule     no bandit, no EV gate   <- what most implementations do
    +bandit            learned retry slots
    +ev-gate           economic stopping rule
    full               both

The honest question this answers is whether the bandit and EV gate beat a plain
compliant retry loop, or whether they are complexity for its own sake. Running it is
the difference between claiming a design is better and showing it.

Note that recovery rate and net value pull in opposite directions by design: the EV
gate deliberately abandons low-value chases, so it should LOWER recovery rate while
RAISING net value. A configuration that wins on both is not expected.

    python -m scripts.ablation [N]
"""

import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sqlalchemy import func, select  # noqa: E402

from app import sources  # noqa: E402
from app.config import settings  # noqa: E402
from app.db import make_session_factory  # noqa: E402
from app.models import ActionLog, FailedPayment, PaymentStatus  # noqa: E402
from scripts.seed_synthetic_data import run_simulation  # noqa: E402


# Reference costs used to SCORE every configuration, held fixed and deliberately read
# from neither `settings` nor the policy under test. Configurations mutate settings to
# change their own behaviour; if scoring also used those mutated values, each policy
# would be graded by its own rulebook (an EV-disabled run would score its churn cost at
# zero and appear free). The world charges the same prices regardless of what the
# policy believes.
EVAL_CONTACT_COST_PAISE = 200
EVAL_CHURN_RISK_PER_CONTACT = 0.008

# LTV is per-source in the world too, not just in the policy. Scoring every source at a
# subscription's 12x would charge the evaluator's own model a cost the world does not
# impose — a settled B2B invoice is not a cancelled subscription — and would grade the
# policy against a penalty that is not real.
def eval_ltv_multiple(source: str) -> float:
    return sources.profile_for(source or "failed_payment").ltv_multiple


@dataclass
class Result:
    name: str
    recovery_rate: float
    control_rate: float
    lift: float
    contacts: int
    churned: int
    gross_paise: float
    net_paise: float


def _measure(db, name: str) -> Result:
    payments = db.scalars(select(FailedPayment)).all()
    resolved_states = (PaymentStatus.RECOVERED.value, PaymentStatus.LOST.value)

    def rate(group):
        eligible = [p for p in group if p.status in resolved_states]
        if not eligible:
            return 0.0
        return sum(1 for p in eligible if p.status == PaymentStatus.RECOVERED.value) / len(eligible)

    intervention_rate = rate([p for p in payments if not p.control_group])
    control_rate = rate([p for p in payments if p.control_group])

    contact_counts = dict(
        db.execute(
            select(ActionLog.failed_payment_id, func.count(ActionLog.id)).group_by(
                ActionLog.failed_payment_id
            )
        ).all()
    )
    contacts = sum(contact_counts.values())
    gross = sum(p.amount_paise for p in payments if p.status == PaymentStatus.RECOVERED.value)
    # Churn loss is MEASURED off customers the simulated world actually lost, not
    # inferred from the policy's own assumptions.
    churn = sum(
        p.amount_paise * eval_ltv_multiple(p.source)
        for p in payments
        if p.churned_from_dunning
    )
    net = gross - contacts * EVAL_CONTACT_COST_PAISE - churn

    return Result(
        name=name,
        recovery_rate=intervention_rate,
        control_rate=control_rate,
        lift=intervention_rate - control_rate,
        contacts=contacts,
        churned=sum(1 for p in payments if p.churned_from_dunning),
        gross_paise=gross,
        net_paise=net,
    )


def run_config(name: str, n: int, seed: int, *, bandit_on: bool, ev_on: bool) -> Result:
    """Each configuration gets a fresh database so runs cannot contaminate each other
    (a shared bandit table would leak learning between arms of the ablation)."""
    original = (settings.bandit_enabled, settings.churn_risk_per_contact, settings.contact_cost_paise)
    settings.bandit_enabled = bandit_on
    if not ev_on:
        # Disable the gate by zeroing its cost terms — the EV arithmetic still runs and
        # is still audited, it just can never come out negative.
        settings.churn_risk_per_contact = 0.0
        settings.contact_cost_paise = 0

    # In-memory DB: this harness runs the sim dozens of times, and every commit against
    # a file-backed SQLite database is an fsync. Nothing here needs to outlive the run.
    try:
        factory, engine = make_session_factory("sqlite:///:memory:")
        db = factory()
        try:
            run_simulation(db, n, seed=seed)
            result = _measure(db, name)
        finally:
            db.close()
            engine.dispose()
    finally:
        (
            settings.bandit_enabled,
            settings.churn_risk_per_contact,
            settings.contact_cost_paise,
        ) = original
    return result


def _significance(lo: float, hi: float) -> str:
    """Report significance in BOTH directions.

    This previously read `lo > 0`, which can only ever recognise a positive effect: an
    interval lying entirely below zero — a statistically significant HARM — was
    reported as "effect not established". A significance test that can only detect
    improvement is not a test, it is a filter that flatters whatever it measures, and
    it hid exactly the result most worth seeing.
    """
    if lo > 0:
        return "CI excludes zero - improvement established"
    if hi < 0:
        return "CI excludes zero - this configuration is significantly WORSE"
    return "CI includes zero - effect not established at this n"


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _stdev(xs: list[float]) -> float:
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def main(n: int, replicates: int = 8) -> None:
    configs = [
        ("fixed-schedule", False, False),
        ("+bandit", True, False),
        ("+ev-gate", False, True),
        ("full", True, True),
    ]
    seeds = [20260905 + i for i in range(replicates)]

    print(
        f"Ablation over {n} synthetic failed payments x {replicates} seeds.\n"
        f"Churn is a rare, high-cost event, so net value from any single run is mostly "
        f"noise;\nreplicating and reporting spread is what makes the comparison "
        f"trustworthy.\n"
    )

    # Common random numbers again, one level up: every configuration sees the same set
    # of worlds, so differences between them are not seed luck.
    runs: dict[str, list[Result]] = {}
    for name, bandit_on, ev_on in configs:
        runs[name] = [
            run_config(name, n, seed, bandit_on=bandit_on, ev_on=ev_on) for seed in seeds
        ]

    header = f"{'config':<17}{'recovery':>10}{'lift':>9}{'contacts':>10}{'churned':>9}{'net Rs. (mean +/- sd)':>28}"
    print(header)
    print("-" * len(header))
    for name, _, _ in configs:
        rs = runs[name]
        nets = [r.net_paise / 100 for r in rs]
        print(
            f"{name:<17}{_mean([r.recovery_rate for r in rs]):>9.1%}"
            f"{_mean([r.lift for r in rs]):>+9.1%}"
            f"{_mean([float(r.contacts) for r in rs]):>10.0f}"
            f"{_mean([float(r.churned) for r in rs]):>9.1f}"
            f"{_mean(nets):>16,.0f} +/-{_stdev(nets):>9,.0f}"
        )

    base_nets = [r.net_paise / 100 for r in runs["fixed-schedule"]]
    full_nets = [r.net_paise / 100 for r in runs["full"]]
    # Paired differences: each seed's full run minus its own baseline run. Pairing
    # removes between-world variance, which is the dominant term here.
    deltas = [f - b for f, b in zip(full_nets, base_nets)]
    wins = sum(1 for d in deltas if d > 0)

    # The per-seed spread is comparable to the effect, so "7/8 seeds improved" on its
    # own would overstate the evidence. Report the paired interval instead.
    mean_delta, sd_delta = _mean(deltas), _stdev(deltas)
    stderr = sd_delta / (len(deltas) ** 0.5) if len(deltas) > 1 else 0.0
    # Two-sided 95% t critical values, df = n-1.
    t_crit = {1: 12.71, 2: 4.30, 3: 3.18, 4: 2.78, 5: 2.57, 6: 2.45, 7: 2.36, 8: 2.31, 9: 2.26}
    t = t_crit.get(len(deltas) - 1, 2.0)
    lo, hi = mean_delta - t * stderr, mean_delta + t * stderr

    print(
        f"\nfull vs fixed-schedule, paired by seed:"
        f"\n  mean net delta  Rs.{mean_delta:+,.0f}  (sd {sd_delta:,.0f}, se {stderr:,.0f})"
        f"\n  95% CI          Rs.{lo:+,.0f} .. Rs.{hi:+,.0f}"
        f"\n  seeds improved  {wins}/{len(deltas)}"
        f"\n  {_significance(lo, hi)}"
    )
    print(
        "\nRecovery rate and net value trade off by design: the EV gate abandons "
        "low-value chases,\nso it should lower recovery while raising net. Judge the "
        "configurations on net."
    )


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 400
    reps = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    main(count, reps)
