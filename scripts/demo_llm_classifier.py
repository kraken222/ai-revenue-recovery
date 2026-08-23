"""Demonstrate the self-consistency classifier on realistic decline narrations.

Runs against the real API when ANTHROPIC_API_KEY is set. Without one it runs a
scripted stub, so the mechanism — three framings, majority vote, thresholds, and the
conservative fall-through to human review — is inspectable with no credentials and no
network. That matters for a reviewer cloning the repo.

    python -m scripts.demo_llm_classifier

The cases are chosen to exercise the decision boundary, not to flatter it: three that
should classify cleanly, one genuinely ambiguous ("DO NOT HONOUR" is a real decline
string that legitimately spans funds, risk and issuer policy), and one truncated
beyond recovery. A classifier that confidently answers all five is broken — the last
two should end at a human.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import llm, llm_classifier  # noqa: E402
from app.config import settings  # noqa: E402
from app.llm import LLMCall  # noqa: E402
from app.models import Rail  # noqa: E402

CASES = [
    (Rail.UPI_AUTOPAY, "INSUFFICIENT BAL IN AC XX4471 AS ON DATE", "clean soft decline"),
    (Rail.ENACH, "REMITTER BANK SWITCH UNAVAILABLE - NPCI TIMEOUT RC91", "clean technical"),
    (Rail.CARD, "DO NOT HONOUR", "genuinely ambiguous - could be funds, risk, or issuer policy"),
    (Rail.UPI_AUTOPAY, "MANDATE NOT REGISTERED AT REMITTER", "clean hard decline"),
    (Rail.CARD, "ERR", "truncated beyond recovery"),
]

# Scripted votes keyed by narration, used only when no credentials are present. The
# ambiguous and truncated cases deliberately split, so the offline demo shows the
# rejection path rather than a parade of confident answers.
_STUB = {
    "INSUFFICIENT BAL IN AC XX4471 AS ON DATE": [("soft_decline", 0.95)] * 3,
    "REMITTER BANK SWITCH UNAVAILABLE - NPCI TIMEOUT RC91": [("technical", 0.93)] * 3,
    "DO NOT HONOUR": [("soft_decline", 0.55), ("risk_block", 0.5), ("technical", 0.4)],
    "MANDATE NOT REGISTERED AT REMITTER": [("hard_decline", 0.97)] * 3,
    "ERR": [("soft_decline", 0.25), ("technical", 0.2), ("hard_decline", 0.2)],
}


def install_stub() -> None:
    state: dict[str, int] = {}

    def fake_complete_json(system, prompt, schema, max_tokens=512):
        key = next((k for k in _STUB if k in prompt), None)
        if key is None:
            return LLMCall(ok=False, error="no_stub_for_prompt")
        i = state.get(key, 0)
        state[key] = i + 1
        category, confidence = _STUB[key][min(i, len(_STUB[key]) - 1)]
        return LLMCall(
            ok=True,
            data={"category": category, "confidence": confidence, "reasoning": "scripted"},
        )

    llm.complete_json = fake_complete_json
    llm.available = lambda: True


def main() -> None:
    live = llm.available()
    if not live:
        install_stub()

    print(f"Self-consistency classifier - {'LIVE API' if live else 'OFFLINE STUB (no credentials)'}")
    print(
        f"thresholds: agreement >= {settings.llm_min_agreement}, "
        f"mean confidence >= {settings.llm_min_confidence}, "
        f"{settings.llm_self_consistency_samples} framings\n"
    )

    accepted = 0
    for rail, narration, note in CASES:
        result = llm_classifier.classify(rail, narration)
        verdict = "ACCEPTED" if result.accepted else "-> HUMAN"
        accepted += result.accepted

        print(f"[{verdict:8}] {rail.value:12} {narration}")
        print(f"{'':11} {note}")
        for vote in result.votes:
            if "error" in vote:
                print(f"{'':13} {vote['framing']:12} error: {vote['error']}")
            else:
                print(f"{'':13} {vote['framing']:12} {vote['category']:14} conf {vote['confidence']}")
        print(
            f"{'':13} => {result.category.value}  "
            f"agreement {result.agreement:.0%}  confidence {result.confidence:.2f}  ({result.reason})\n"
        )

    print(f"{accepted}/{len(CASES)} auto-classified; {len(CASES) - accepted} routed to human review.")
    print("Every rejection path - split vote, low confidence, API error, no credentials -")
    print("resolves to UNKNOWN, which compliance maps to escalate_human under COMP-002.")


if __name__ == "__main__":
    main()
