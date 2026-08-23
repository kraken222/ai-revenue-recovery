"""B2B overdue receivables under the MSMED Act.

Chasing a late B2B invoice in India is a different problem from chasing a consumer
subscription, and the difference is that **the law is already doing the chasing.**

Micro, Small and Medium Enterprises Development Act, 2006:

- **s.15** — the buyer must pay by the agreed date or within **45 days** of acceptance,
  whichever is earlier. With no written agreement the period is **15 days**. The
  agreed date cannot lawfully exceed 45 days, so a contract saying "90 days net" does
  not move the appointed day.
- **s.16** — from the appointed day the buyer owes **compound interest, compounded
  monthly, at three times the RBI bank rate**. This accrues by operation of law; it is
  not something the supplier elects or waives.
- **s.23** — that interest is **not deductible** for the buyer's income tax.
- **s.43B(h) of the Income Tax Act** (from 1 April 2024) — payments to MSME-registered
  suppliers delayed beyond the s.15 period are **disallowed as a deduction** in the
  year incurred, landing in the buyer's taxable income.
- **s.22** — buyers must disclose outstanding MSME dues beyond 45 days in their annual
  financial statements.

So the most effective and least aggressive thing a receivables agent can do is state
what is already true: the interest that has accrued, and the tax deduction at risk.
That is *information*, and the distinction from a threat is not cosmetic — RBI's Fair
Practices Code prohibits coercion and intimidation, and reciting a statutory
consequence that operates whether or not anyone mentions it is neither.

Everything here is a computation the supplier is entitled to make. Nothing in this
module asserts a legal claim, initiates proceedings, or names a remedy — MSME Samadhaan
and the facilitation council exist for that, and referring a case there is a decision
for a person, which is why it lands at the human rung rather than in an automated
message.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta

# MSMED s.15: statutory ceiling on the appointed day, and the default with no written
# agreement. The 45-day figure is a ceiling, not a target — an agreed 30 days binds at 30.
MSMED_MAX_CREDIT_DAYS = 45
MSMED_DEFAULT_CREDIT_DAYS = 15

# MSMED s.16: three times the RBI bank rate, compounded monthly. The bank rate moves;
# this default must be reconciled against the RBI notification in force for the period
# being computed, and a real implementation would hold a dated rate table rather than a
# single constant, because interest spanning a rate change uses each rate for its own
# window.
RBI_BANK_RATE = 0.0625
MSMED_INTEREST_MULTIPLE = 3


@dataclass(frozen=True)
class InterestAccrual:
    appointed_day: date
    days_overdue: int
    principal_paise: int
    annual_rate: float
    months_elapsed: int
    interest_paise: int
    total_due_paise: int
    tax_deduction_at_risk: bool

    @property
    def is_overdue(self) -> bool:
        return self.days_overdue > 0


def appointed_day(
    accepted_on: date, agreed_credit_days: int | None = None
) -> date:
    """MSMED s.15. The appointed day is the agreed date or 45 days from acceptance,
    **whichever is earlier** — so a contract granting 90 days does not push it out.
    Absent a written agreement the period is 15 days."""
    if agreed_credit_days is None:
        days = MSMED_DEFAULT_CREDIT_DAYS
    else:
        days = min(agreed_credit_days, MSMED_MAX_CREDIT_DAYS)
    return accepted_on + timedelta(days=days)


def accrue(
    principal_paise: int,
    accepted_on: date,
    as_of: date,
    agreed_credit_days: int | None = None,
    bank_rate: float = RBI_BANK_RATE,
) -> InterestAccrual:
    """Compound interest with monthly rests from the appointed day, per s.16.

    Monthly rests, not daily compounding and not simple interest: the Act says
    compound interest with monthly rests, and the three differ materially over the
    months these disputes actually run. Whole elapsed months only — a partial month
    has not rested yet, so counting it would overstate what is owed, and overstating a
    statutory figure in a letter to a buyer is exactly the error that turns a factual
    reminder into an indefensible one.
    """
    due = appointed_day(accepted_on, agreed_credit_days)
    days_overdue = (as_of - due).days
    annual_rate = bank_rate * MSMED_INTEREST_MULTIPLE

    if days_overdue <= 0:
        return InterestAccrual(
            appointed_day=due,
            days_overdue=max(0, days_overdue),
            principal_paise=principal_paise,
            annual_rate=annual_rate,
            months_elapsed=0,
            interest_paise=0,
            total_due_paise=principal_paise,
            tax_deduction_at_risk=False,
        )

    months = days_overdue // 30
    monthly_rate = annual_rate / 12
    compounded = principal_paise * ((1 + monthly_rate) ** months)
    interest = int(round(compounded - principal_paise))

    return InterestAccrual(
        appointed_day=due,
        days_overdue=days_overdue,
        principal_paise=principal_paise,
        annual_rate=annual_rate,
        months_elapsed=months,
        interest_paise=interest,
        total_due_paise=principal_paise + interest,
        # s.43B(h): the deduction is disallowed once payment runs past the s.15 period.
        tax_deduction_at_risk=True,
    )


def statutory_facts(accrual: InterestAccrual) -> list[str]:
    """The factual lines a reminder may carry. Deliberately returns *statements about
    what the law provides*, never a demand, a deadline of our own invention, or a
    threatened remedy — the escalation here is precision, not volume."""
    if not accrual.is_overdue:
        return []

    facts = [
        f"Payment was due on {accrual.appointed_day.isoformat()} "
        f"({accrual.days_overdue} days ago) under MSMED Act s.15.",
        f"Interest accrues under s.16 at {accrual.annual_rate:.2%} per annum "
        f"(three times the RBI bank rate), compounded monthly.",
    ]
    if accrual.months_elapsed >= 1:
        facts.append(
            f"Accrued to date: Rs.{accrual.interest_paise / 100:,.2f} over "
            f"{accrual.months_elapsed} completed month(s)."
        )
    if accrual.tax_deduction_at_risk:
        facts.append(
            "Under s.43B(h) of the Income Tax Act, this expense is not deductible "
            "in the year incurred while it remains unpaid."
        )
    return facts


def severity(accrual: InterestAccrual) -> str:
    """How far past terms this is, in words an operator can triage on. Drives which
    rung the receivables ladder starts at, since a 3-day slip and a 6-month one are not
    the same conversation."""
    if not accrual.is_overdue:
        return "current"
    if accrual.days_overdue <= 15:
        return "recently_overdue"
    if accrual.days_overdue <= 60:
        return "materially_overdue"
    return "aged"


def as_of_date(when: datetime | date) -> date:
    return when.date() if isinstance(when, datetime) else when
