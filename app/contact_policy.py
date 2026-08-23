"""RBI Fair Practices Code contact-hours window.

The distinction this module exists to make, which a naive dunning engine misses
entirely: **a debit attempt and a customer contact are not the same act.**

- A *silent* debit against an existing mandate is machine-to-machine. No human is
  contacted, nothing rings, and attempting one at 00:30 disturbs nobody. This is
  exactly the window the bandit learns is best, because salary credits and wallet
  top-ups land overnight.
- A *contact* — SMS, WhatsApp, email, a call — reaches a person. RBI's Fair Practices
  Code restricts recovery contact to **08:00–19:00 local time**, and communication
  outside that window is classified as harassment rather than as a permitted attempt.

Conflating the two produces a specific, real violation: the bandit correctly learns
that 00:00–06:00 recovers the most money, and the system then sends a "your payment
failed" SMS at half past midnight. The money-optimal slot and the legally-permitted
contact window genuinely disagree, and only the actions that touch a customer are
bound by the second.

Timezone is load-bearing. The window is Indian local time; everything in this system
is stored and reasoned about in UTC. 08:00 IST is 02:30 UTC and 19:00 IST is 13:30
UTC, so a naive `dt.hour` check against 8 and 19 would permit contact from 08:00 UTC
(13:30 IST) all the way to 19:00 UTC (00:30 IST) — clearing the legal window at one
end and running four and a half hours past it at the other, which is the harassment
case. Convert first, then compare.

References (reconcile against the current circular before production use):
- RBI Fair Practices Code / Recovery Agent Code of Conduct: contact permitted only
  between 08:00 and 19:00; abusive or threatening language prohibited; only the
  borrower and one nominated reference may be contacted.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

IST = timezone(timedelta(hours=5, minutes=30))

CONTACT_WINDOW_START_HOUR_IST = 8
CONTACT_WINDOW_END_HOUR_IST = 19

# Actions that reach a human. Everything here is bound by the contact window.
CONTACT_ACTIONS = frozenset({"send_payment_link", "request_new_mandate", "send_reminder"})

# Actions that are machine-to-machine debit attempts against an existing mandate.
# No customer is contacted, so the contact window does not apply.
SILENT_ACTIONS = frozenset({"retry_now", "retry_at", "monitor_gateway_retry"})


def is_contact_action(action: str) -> bool:
    return action in CONTACT_ACTIONS


def within_contact_window(when: datetime) -> bool:
    """True when `when` falls inside 08:00–19:00 IST. Naive datetimes are treated as
    UTC, matching the rest of the system's convention."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(IST)
    return CONTACT_WINDOW_START_HOUR_IST <= local.hour < CONTACT_WINDOW_END_HOUR_IST


def next_contact_window_open(when: datetime) -> datetime:
    """The earliest instant at or after `when` at which contacting a customer is
    permitted. Returns `when` unchanged if it is already inside the window."""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    local = when.astimezone(IST)

    if within_contact_window(when):
        return when

    opens = local.replace(hour=CONTACT_WINDOW_START_HOUR_IST, minute=0, second=0, microsecond=0)
    if local.hour >= CONTACT_WINDOW_END_HOUR_IST:
        # Past this evening's close — the next opening is tomorrow morning.
        opens += timedelta(days=1)
    return opens.astimezone(when.tzinfo or timezone.utc)


def constrain(action: str, slot: datetime) -> tuple[datetime, str | None]:
    """Push a scheduled slot forward to the next legal contact time when the action
    reaches a customer. Silent debit attempts pass through untouched, which is what
    lets the bandit keep the overnight window it correctly learned is best.

    Returns (slot, adjustment_reason). The reason is recorded in the audit trail so a
    deferred contact is visibly a compliance decision rather than an unexplained delay.
    """
    if not is_contact_action(action):
        return slot, None
    if within_contact_window(slot):
        return slot, None
    return next_contact_window_open(slot), "deferred_to_rbi_contact_window"
