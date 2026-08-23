import random
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app import bandit
from app.db import Base

NOW = datetime(2026, 1, 1, 9, 30, tzinfo=timezone.utc)


@pytest.fixture
def db():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(bind=engine)
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


def test_candidate_slots_are_all_in_the_future_and_within_window():
    slots = bandit.candidate_slots(NOW, max_window_hours=48)
    assert len(slots) == 4
    for _, dt in slots:
        assert dt >= NOW
        assert dt - NOW <= timedelta(hours=48)


def test_candidate_slots_respect_a_narrow_window():
    # Only buckets landing within 6h of 09:30 — that's 12:00 alone.
    slots = bandit.candidate_slots(NOW, max_window_hours=6)
    assert [bucket for bucket, _ in slots] == [12]


def test_slots_never_precede_the_compliant_floor():
    """The bandit must not be able to schedule earlier than compliance allows —
    that would let a learned policy undercut a regulatory window."""
    floor = datetime(2026, 1, 1, 23, 0, tzinfo=timezone.utc)
    for _, dt in bandit.candidate_slots(floor, max_window_hours=48):
        assert dt >= floor


def test_arm_is_created_with_uniform_prior(db):
    arm = bandit.get_or_create_arm(db, bandit.arm_key("card", "soft_decline", 0))
    assert (arm.alpha, arm.beta) == (1.0, 1.0)
    assert arm.posterior_mean == 0.5


def test_reward_updates_move_the_posterior_in_the_right_direction(db):
    key = bandit.arm_key("card", "soft_decline", 0)
    for _ in range(9):
        bandit.update(db, key, success=True)
    bandit.update(db, key, success=False)

    arm = bandit.get_or_create_arm(db, key)
    assert arm.pulls == 10
    assert arm.posterior_mean > 0.7


def test_thompson_sampling_concentrates_on_the_better_arm(db):
    """Give one slot a clearly better history, then confirm selection favours it.
    This is the property the whole bandit exists for — a rule table cannot do it."""
    random.seed(1234)
    good = bandit.arm_key("card", "soft_decline", 0)
    bad = bandit.arm_key("card", "soft_decline", 12)
    for _ in range(30):
        bandit.update(db, good, success=True)
        bandit.update(db, bad, success=False)

    slots = [(0, NOW + timedelta(hours=1)), (12, NOW + timedelta(hours=3))]
    picks = [bandit.select_slot(db, "card", "soft_decline", slots).tod_bucket for _ in range(50)]
    assert picks.count(0) > 45


def test_select_slot_returns_none_when_no_compliant_slots_exist(db):
    assert bandit.select_slot(db, "card", "soft_decline", []) is None


def test_arm_key_round_trips():
    key = bandit.arm_key("upi_autopay", "technical", 18)
    assert bandit.parse_arm_key(key) == ("upi_autopay", "technical", 18)
