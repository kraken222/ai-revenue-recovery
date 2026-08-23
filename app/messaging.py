"""Customer-facing recovery copy, and plain-English rendering of a decision trace.

Two rules govern everything here.

**Templates are the default; the model is the enhancement.** Every message has a
deterministic template that ships correct copy with no API call. `llm_copy_enabled`
is off by default, because this is the one stage whose output leaves the system and
reaches a real customer — that should be opted into, not out of. When generation
fails, is refused, or produces something that fails validation, the template ships.
A payment reminder is never blocked on an inference call.

**The model writes words, never numbers or decisions.** Amount, due date, channel,
and the action itself are all computed before the model is invoked and validated
after. `_validate` rejects any draft that invents a rupee figure the caller did not
supply, or that exceeds the channel's length budget. Hallucinating "₹4,999" into a
₹499 reminder is the specific failure this guards.

India-specific constraints that shape the copy:
- Commercial SMS runs over DLT-registered templates, so a free-form SMS body is not
  actually sendable in production. The SMS path is template-only by design and the
  registered template id is what would ship; treat the string here as its preview.
- WhatsApp business-initiated messages likewise require approved templates.
- Email is the only channel with genuine free-form latitude, which is why it is the
  only one the model is allowed to rewrite.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app import llm
from app.config import settings
from app.models import Category, Rail

# Channel length budgets. SMS is a hard technical limit; the others are readability
# ceilings past which recovery messages measurably stop being read.
_LIMITS = {"sms": 160, "whatsapp": 400, "email": 900}

_RAIL_INSTRUMENT = {
    Rail.CARD: "card",
    Rail.UPI_AUTOPAY: "UPI AutoPay mandate",
    Rail.ENACH: "bank mandate",
}


@dataclass
class Message:
    channel: str
    body: str
    source: str            # "template" | "llm"
    template_id: str
    fallback_reason: str | None = None


def _inr_indian_grouping(paise: int) -> str:
    """Indian digit grouping: lakhs and crores, not thousands. Rs.10,00,000 rather
    than Rs.1,000,000 — the wrong grouping reads as a different number to the
    customers these messages go to."""
    n = paise // 100
    s = str(n)
    if len(s) <= 3:
        return f"Rs.{s}"
    head, tail = s[:-3], s[-3:]
    head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
    return f"Rs.{head},{tail}"


def template_for(action: str, category: Category, rail: Rail, amount_paise: int, channel: str) -> Message:
    """The deterministic path. Correct, sendable, and free."""
    amount = _inr_indian_grouping(amount_paise)
    instrument = _RAIL_INSTRUMENT.get(rail, "payment method")

    if action in ("retry_now", "retry_at"):
        tid = "RCV-RETRY-01"
        body = (
            f"Your payment of {amount} did not go through. We will try again shortly — "
            f"please keep sufficient balance available. No action needed if already paid."
        )
    elif action == "send_payment_link":
        tid = "RCV-LINK-01"
        body = (
            f"Your payment of {amount} could not be completed because your {instrument} "
            f"is no longer valid. Please complete it using the secure link we have sent."
        )
    elif action == "request_new_mandate":
        tid = "RCV-MANDATE-01"
        body = (
            f"Your {instrument} for {amount} is no longer active, so we could not collect "
            f"this payment. Please re-authorise it to continue your subscription."
        )
    else:
        tid = "RCV-GENERIC-01"
        body = f"There was an issue collecting your payment of {amount}. Our team is looking into it."

    return Message(channel=channel, body=body[: _LIMITS.get(channel, 400)], source="template", template_id=tid)


_COPY_SYSTEM = """You write short payment-recovery messages for Indian customers of a \
merchant using Razorpay. The customer's payment failed for a mechanical reason — they \
have not chosen to cancel, and most of them want to keep paying.

Rules, all of them hard:
- Never invent an amount, date, reference number, link or account detail. Use only \
what the brief gives you.
- Never threaten, imply legal action, imply a credit-score impact, or shame the \
customer. A failed auto-debit is routine.
- Do not promise anything about refunds, timelines, or account status.
- Plain, warm, direct. No marketing voice, no exclamation marks.
- Assume this may be the third message they have received. Be brief.
- Write the message body only. No subject line, no greeting placeholder, no signature, \
no surrounding quotes or commentary."""


def _validate(draft: str, amount_paise: int, channel: str) -> tuple[bool, str | None]:
    """Post-generation guard. The model writes words; the caller owns the numbers, so
    any monetary figure that is not the one supplied is grounds for rejection."""
    if not draft:
        return False, "empty_draft"
    if len(draft) > _LIMITS.get(channel, 400):
        return False, "over_length"

    expected = str(amount_paise // 100)
    for found in re.findall(r"(?:Rs\.?|INR|₹)\s?([\d,]+)", draft, flags=re.IGNORECASE):
        if found.replace(",", "") != expected:
            return False, "hallucinated_amount"

    lowered = draft.lower()
    for banned in ("legal action", "credit score", "cibil", "penalty", "blacklist", "police"):
        if banned in lowered:
            return False, f"prohibited_language:{banned}"
    return True, None


def compose(
    action: str,
    category: Category,
    rail: Rail,
    amount_paise: int,
    channel: str = "sms",
    attempt: int = 1,
    hinglish: bool = False,
) -> Message:
    """Template first, model only as an enhancement, template again if it misbehaves."""
    base = template_for(action, category, rail, amount_paise, channel)

    if not settings.llm_copy_enabled or not llm.available():
        return base
    # SMS and WhatsApp require pre-registered DLT/business templates, so free-form
    # generation there would produce copy that cannot legally be sent.
    if channel != "email":
        return Message(**{**base.__dict__, "fallback_reason": "channel_requires_registered_template"})

    prompt = (
        f"Rail: {rail.value}\n"
        f"Failure category: {category.value}\n"
        f"Action being taken: {action}\n"
        f"Amount: {_inr_instructions(amount_paise)}\n"
        f"This is attempt {attempt}.\n"
        f"Channel: {channel} (max {_LIMITS[channel]} characters)\n"
        f"{'Write in natural Hinglish — Roman script, the way urban Indian customers actually text.' if hinglish else 'Write in English.'}\n\n"
        f"For reference, the approved template says:\n{base.body}\n\n"
        f"Write a clearer, warmer version carrying exactly the same facts."
    )

    call = llm.complete_text(system=_COPY_SYSTEM, prompt=prompt, max_tokens=400)
    if not call.ok:
        return Message(**{**base.__dict__, "fallback_reason": call.error})

    draft = call.text.strip().strip('"')
    ok, reason = _validate(draft, amount_paise, channel)
    if not ok:
        return Message(**{**base.__dict__, "fallback_reason": reason})

    return Message(channel=channel, body=draft, source="llm", template_id=base.template_id)


def _inr_instructions(amount_paise: int) -> str:
    return f"{_inr_indian_grouping(amount_paise)} (write it exactly like this, do not reformat or round)"


_EXPLAIN_SYSTEM = """You turn a payment-recovery system's decision trace into ONE plain \
sentence an operations person can read without knowing the system.

Explain only what the trace shows. Never invent a reason, never speculate about the \
customer, never soften a stop into something that sounds like an error. Name the rule \
that fired when the trace gives you one. Under 30 words, no jargon, no trailing period \
on a fragment."""


def explain(trace: dict) -> str:
    """One-line rendering of a decision, for the audit trail. Falls back to a
    deterministic sentence, so the audit log is never blank and never blocked."""
    deterministic = _deterministic_explanation(trace)
    if not settings.llm_copy_enabled or not llm.available():
        return deterministic

    call = llm.complete_text(
        system=_EXPLAIN_SYSTEM,
        prompt=f"Decision trace:\n{trace}\n\nExplain it in one sentence.",
        max_tokens=120,
    )
    if not call.ok or len(call.text) > 200:
        return deterministic
    return call.text.strip()


def _deterministic_explanation(trace: dict) -> str:
    action = trace.get("action") or trace.get("final_action") or "no action"
    rule = trace.get("policy_rule_id", "")
    reason = trace.get("blocked_reason")
    parts = [f"Action: {action}"]
    if rule:
        parts.append(f"under {rule}")
    if reason:
        parts.append(f"({reason.replace('_', ' ')})")
    return " ".join(parts)
