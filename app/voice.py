"""Hinglish voice recovery: the TRAI compliance gate, and the script.

This module deliberately **does not place calls.** It decides whether a call would be
lawful, and writes what would be said. Dialling is left unimplemented for the same
reason `retry_charge` is on the card rail — the honest position when the surrounding
regulation is this specific and the integration is not verified.

TCCCPR / TRAI, which govern any commercial voice call in India:

- **Number series is mandatory.** Promotional calls originate from 140-series numbers,
  service and transactional calls from 1600-series. Dialling commercial traffic from an
  ordinary mobile or landline is a violation regardless of what is said.
- **A collections call that carries any upsell is reclassified as Promotional**, which
  drags it under the stricter regime. So the script must be purely transactional: no
  offer, no cross-sell, no "while I have you".
- **DND / NCPR must be checked before dialling.** Calling a registered number without
  recorded prior consent is a violation, with penalties from Rs 2 lakh to Rs 10 lakh.
- **An automated call must disclose that it is automated within 15 seconds.** For a
  synthetic Hinglish voice that is not a nicety; it is the first thing said.
- **Every call must be logged** with timestamp, number, category, consent reference and
  DND status at time of call — auditable on inquiry.

RBI's 08:00-19:00 contact window applies on top of all of this, because a call is
unambiguously customer contact. A voice call is therefore the most heavily gated action
in the entire system, which is the right outcome: it is also the most intrusive.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from app import contact_policy
from app.models import Rail

# TCCCPR number series. Recovery calls are transactional and belong on 1600.
PROMOTIONAL_SERIES = "140"
TRANSACTIONAL_SERIES = "1600"

AUTOMATED_DISCLOSURE_SECONDS = 15


@dataclass
class VoiceEligibility:
    permitted: bool
    blockers: list[str] = field(default_factory=list)
    required_series: str = TRANSACTIONAL_SERIES
    audit: dict = field(default_factory=dict)


def check_eligibility(
    *,
    when: datetime,
    dnd_registered: bool,
    consent_reference: str | None,
    caller_number: str,
    is_debt: bool,
    contains_offer: bool = False,
) -> VoiceEligibility:
    """Every gate, evaluated together, so the audit record names all failures rather
    than only the first. An operator deciding whether to fix one blocker needs to know
    whether three others are waiting behind it."""
    blockers: list[str] = []

    # An upsell reclassifies the call as promotional, which needs the 140 series and a
    # different consent basis. Keeping recovery scripts offer-free is what keeps them
    # transactional.
    series = PROMOTIONAL_SERIES if contains_offer else TRANSACTIONAL_SERIES
    if contains_offer:
        blockers.append("script_contains_offer_reclassifies_call_as_promotional")

    if not caller_number.startswith(series):
        blockers.append(f"caller_number_not_on_{series}_series")

    if dnd_registered and not consent_reference:
        blockers.append("dnd_registered_without_consent_reference")

    if not contact_policy.within_contact_window(when):
        blockers.append("outside_rbi_contact_window")

    if not is_debt:
        # Calling someone who owes nothing about a cart they abandoned is a marketing
        # call wearing a service call's clothes.
        blockers.append("no_debt_owed_voice_contact_not_justifiable")

    return VoiceEligibility(
        permitted=not blockers,
        blockers=blockers,
        required_series=series,
        audit={
            "checked_at": when.isoformat(),
            "local_hour_ist": contact_policy.next_contact_window_open(when).astimezone(
                contact_policy.IST
            ).hour,
            "dnd_registered": dnd_registered,
            "consent_reference": consent_reference,
            "series_required": series,
        },
    )


_RAIL_HINGLISH = {
    Rail.CARD: "card",
    Rail.UPI_AUTOPAY: "UPI AutoPay mandate",
    Rail.ENACH: "bank mandate",
}


def build_script(
    *,
    merchant_name: str,
    amount_paise: int,
    rail: Rail | None = None,
    language: str = "hinglish",
) -> list[dict]:
    """The call script, as timed segments.

    Returned as a structured list rather than a paragraph because the compliance
    requirements are positional: the automated-nature disclosure has to land inside the
    first 15 seconds, and that is only checkable if the script knows where its own
    segments sit. A prose blob cannot be verified.

    Hinglish here is Roman-script Hindi-English code-mixing as urban Indian customers
    actually speak — not translated Hindi, and not English with a few Hindi words
    dropped in for flavour.
    """
    amount = f"{amount_paise // 100:,}".replace(",", ",")
    instrument = _RAIL_HINGLISH.get(rail, "payment method")

    if language == "hinglish":
        segments = [
            (0, "disclosure",
             f"Namaste. Yeh {merchant_name} ki taraf se ek automated call hai."),
            (6, "identify",
             f"Aapka Rs.{amount} ka payment complete nahi ho paya."),
            (12, "reason",
             f"Aapka {instrument} process nahi hua."),
            (18, "action",
             "Aap SMS mein bheje gaye secure link se payment complete kar sakte hain."),
            (26, "close",
             "Agar aapne already payment kar diya hai, toh is call ko ignore kijiye. Dhanyavaad."),
        ]
    else:
        segments = [
            (0, "disclosure",
             f"Hello. This is an automated call from {merchant_name}."),
            (6, "identify", f"Your payment of Rs.{amount} could not be completed."),
            (12, "reason", f"Your {instrument} did not go through."),
            (18, "action", "You can complete it using the secure link we sent by SMS."),
            (26, "close",
             "If you have already paid, please ignore this call. Thank you."),
        ]

    return [{"at_second": at, "role": role, "text": text} for at, role, text in segments]


def verify_script(script: list[dict]) -> tuple[bool, list[str]]:
    """Check the script against the rules that constrain what may be said, and where.

    Written as a verifier rather than trusted from the generator because the script is
    the artefact a regulator would actually examine, and because a generated variant
    (a translated one, a shortened one) has to clear the same bar as the handwritten
    original.
    """
    problems: list[str] = []

    disclosure = next((s for s in script if s["role"] == "disclosure"), None)
    if disclosure is None:
        problems.append("no_automated_nature_disclosure")
    elif disclosure["at_second"] >= AUTOMATED_DISCLOSURE_SECONDS:
        problems.append("disclosure_after_15_second_deadline")

    joined = " ".join(s["text"] for s in script).lower()

    # Any offer reclassifies the whole call as promotional.
    for token in ("offer", "discount", "upgrade", "sale", "cashback", "free"):
        if token in joined:
            problems.append(f"promotional_language:{token}")

    # RBI prohibits threats and coercion in recovery communication.
    for token in ("legal action", "police", "court", "recovery agent", "blacklist", "cibil"):
        if token in joined:
            problems.append(f"coercive_language:{token}")

    return (not problems), problems


def call_record(
    *,
    when: datetime,
    customer_number_masked: str,
    caller_number: str,
    eligibility: VoiceEligibility,
    placed: bool,
    script: list[dict],
) -> dict:
    """The auditable record TRAI expects to be able to inspect: when, who, what
    category, on what consent basis, and the DND status at the time of the call.

    Recorded whether or not the call was placed. A blocked call is exactly the event a
    compliance review most wants to see, and a log that only contains calls that went
    out cannot demonstrate that the gate was ever doing anything.
    """
    return {
        "timestamp": when.isoformat(),
        "customer_number": customer_number_masked,
        "caller_number": caller_number,
        "category": "transactional" if eligibility.required_series == TRANSACTIONAL_SERIES else "promotional",
        "series_required": eligibility.required_series,
        "consent_reference": eligibility.audit.get("consent_reference"),
        "dnd_registered": eligibility.audit.get("dnd_registered"),
        "permitted": eligibility.permitted,
        "blockers": eligibility.blockers,
        "placed": placed,
        "script_segments": len(script),
    }


def place_call(*args, **kwargs):
    """Not implemented, deliberately.

    Placing commercial voice traffic in India needs a registered 1600-series originating
    number, a DLT-registered entity, a live NCPR/DND lookup and a telephony provider
    integration. None of that is verified here, and a fake `place_call` that returned
    success would make every compliance check above decorative.
    """
    raise NotImplementedError(
        "Voice dialling requires a DLT-registered 1600-series originator and a live "
        "NCPR lookup; the compliance gate and script are built, the carrier is not."
    )
