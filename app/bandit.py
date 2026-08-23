"""Thompson Sampling over compliant retry slots.

Why a bandit and not a rule table: fixed retry schedules (24h/48h/72h) assume success
probability is uniform across time, and it isn't — it clusters around balance top-ups
and salary credits. Adyen published this exact problem shape (AutoRescue: contextual
bandit, action space = future retry times inside the allowed window, reward = 1 on a
successful charge), and Stripe's Smart Retries independently found strong time-of-day
effects. A static table cannot learn any of that; a bandit can, while every arm stays
individually inspectable.

Scope of what is learned, deliberately narrow: the bandit only ever picks *among slots
that app.compliance already declared legal*. It cannot invent an action, extend a retry
window, or override a stop. Compliance is the hard boundary; the bandit optimizes
inside it.

Arms are keyed on (rail, category, time-of-day bucket) — 3 rails x 2 retryable
categories x 4 buckets = 24 arms, which is small enough to actually converge on a
few-hundred-payment batch. Adding retry_count or issuer as further context dimensions
is the obvious next step once there's real traffic to support it.
"""

import random
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import BanditArm

# Start hour of each 6-hour UTC bucket.
TOD_BUCKETS = [0, 6, 12, 18]


def bucket_for_hour(hour: int) -> int:
    return (hour // 6) * 6


def arm_key(rail: str, category: str, tod_bucket: int) -> str:
    return f"{rail}|{category}|{tod_bucket:02d}"


def parse_arm_key(key: str) -> tuple[str, str, int]:
    rail, category, bucket = key.split("|")
    return rail, category, int(bucket)


@dataclass
class SlotChoice:
    tod_bucket: int
    scheduled_at: datetime
    arm_key: str
    posterior_mean: float
    samples: dict[int, float]


def _first_occurrence_at_or_after(t: datetime, hour: int) -> datetime:
    candidate = t.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate < t:
        candidate += timedelta(days=1)
    return candidate


def candidate_slots(earliest_slot: datetime, max_window_hours: int) -> list[tuple[int, datetime]]:
    """Every time-of-day bucket whose next occurrence falls inside the allowed retry
    window, measured from the earliest legally-compliant moment."""
    slots = []
    for bucket in TOD_BUCKETS:
        dt = _first_occurrence_at_or_after(earliest_slot, bucket)
        if (dt - earliest_slot).total_seconds() <= max_window_hours * 3600:
            slots.append((bucket, dt))
    return slots


def get_or_create_arm(db: Session, key: str) -> BanditArm:
    arm = db.scalar(select(BanditArm).where(BanditArm.key == key))
    if arm is None:
        rail, category, bucket = parse_arm_key(key)
        # Beta(1,1) = uniform prior: no assumption about which slot is best before
        # any evidence arrives.
        arm = BanditArm(key=key, rail=rail, category=category, tod_bucket=bucket, alpha=1.0, beta=1.0)
        db.add(arm)
        db.flush()
    return arm


def select_slot(
    db: Session, rail: str, category: str, slots: list[tuple[int, datetime]]
) -> SlotChoice | None:
    """Thompson Sampling: draw once from each arm's Beta posterior, take the argmax.
    Exploration falls out of the posterior width automatically — an arm with few pulls
    has a wide posterior and will occasionally sample high, so it keeps getting tried
    without needing an explicit epsilon schedule."""
    if not slots:
        return None

    best: tuple[float, int, datetime, BanditArm] | None = None
    samples: dict[int, float] = {}

    for bucket, dt in slots:
        arm = get_or_create_arm(db, arm_key(rail, category, bucket))
        draw = random.betavariate(arm.alpha, arm.beta)
        samples[bucket] = round(draw, 4)
        if best is None or draw > best[0]:
            best = (draw, bucket, dt, arm)

    _, bucket, dt, arm = best
    return SlotChoice(
        tod_bucket=bucket,
        scheduled_at=dt,
        arm_key=arm.key,
        posterior_mean=arm.posterior_mean,
        samples=samples,
    )


def update(db: Session, key: str, success: bool) -> None:
    arm = get_or_create_arm(db, key)
    if success:
        arm.alpha += 1.0
    else:
        arm.beta += 1.0
    arm.pulls += 1


def snapshot(db: Session) -> list[BanditArm]:
    return list(db.scalars(select(BanditArm).order_by(BanditArm.key)).all())
