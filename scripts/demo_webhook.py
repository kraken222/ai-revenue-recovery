"""Fire one webhook at a running server and print the reasoning chain it produced.

For the pitch video: one command on camera, one payment through the whole funnel, and
the audit trail read back so the chain is visible without clicking through the UI.

    uvicorn app.main:app --reload      # in another terminal
    python -m scripts.demo_webhook

The payment id is fixed at `pay_PITCH_01` on purpose. Holdout assignment hashes the
payment id, so a randomly-generated one lands in the control arm about fifteen percent
of the time -- and a control payment's chain ends in `control_no_action`, which is
correct behaviour and a terrible thing to demo. This id is checked to fall in the
treated arm, so the chain runs all the way to a scheduled retry every time.

Only the event id varies per run, so re-running is a genuinely new event rather than a
redelivery the pipeline would (correctly) dedupe and ignore.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

DEFAULT_BASE = "http://localhost:8000"

# Verified to hash into the treated arm -- see the module docstring.
PAYMENT_ID = "pay_PITCH_01"

PAYLOAD = {
    "payment": {
        "id": PAYMENT_ID,
        "customer_id": "cust_pitch_01",
        "subscription_id": "sub_pitch_01",
        "rail": "upi_autopay",
        "amount_paise": 99900,
        "currency": "INR",
        "error_code": "insufficient_balance",
        # A real remitter-bank narration. The rule table resolves the code, so the LLM
        # tier is not consulted -- which is the point being demonstrated.
        "error_description": "INSUFFICIENT BAL IN AC XX4471 AS ON DATE",
        "issuer_id": "HDFC",
        "source": "failed_payment",
        "mandate_token": "token_pitch_01",
        "customer_email": "demo@example.com",
        "customer_contact": "+919800000001",
    }
}

STAGE_LABEL = {
    "ingestion": "ingestion",
    "classification": "classification",
    "compliance": "compliance",
    "guardrail": "guardrail",
    "escalation": "escalation",
    "promise": "promise",
    "bandit": "bandit",
    "economics": "economics",
    "decision": "decision",
    "execution": "execution",
}


def _post(url: str, body: dict) -> dict:
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return json.loads(response.read())


def _get(url: str) -> list:
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.loads(response.read())


def _summarise(stage: str, detail: dict) -> str:
    """One line per stage, showing the field that stage actually decided."""
    if stage == "ingestion":
        return f"{detail.get('rail')} {detail.get('error_code')}"
    if stage == "classification":
        return f"{detail.get('category')} ({detail.get('source')})"
    if stage == "compliance":
        return f"{detail.get('policy_rule_id')} -> {', '.join(detail.get('allowed_actions') or [])}"
    if stage == "guardrail":
        return f"{', '.join(detail.get('allowed_actions') or [])}"
    if stage == "escalation":
        return f"rung {detail.get('rung')} {detail.get('name')}"
    if stage == "bandit":
        slot = detail.get("tod_bucket")
        posterior = detail.get("posterior_mean")
        if slot is None:
            return json.dumps(detail)[:70]
        return f"slot {slot:02d}:00 - posterior {posterior:.0%}"
    if stage == "economics":
        return (
            f"EV Rs.{detail.get('expected_value_paise', 0) / 100:,.0f} - "
            f"p={detail.get('p_recovery', 0):.1%} - {detail.get('verdict')}"
        )
    if stage == "decision":
        return f"{detail.get('final_action')}"
    if stage == "execution":
        return f"{detail.get('note') or detail.get('action')}"
    return json.dumps(detail)[:70]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default=DEFAULT_BASE, help=f"server base URL (default {DEFAULT_BASE})")
    args = parser.parse_args()

    event_id = f"evt_pitch_{int(time.time())}"
    body = {"event_id": event_id, "event": "payment.failed", "payload": PAYLOAD}

    print(f"POST {args.base}/webhooks/razorpay")
    print(f"  {PAYMENT_ID}  upi_autopay  Rs.999.00  insufficient_balance\n")

    try:
        result = _post(f"{args.base}/webhooks/razorpay", body)
    except urllib.error.URLError as exc:
        print(f"could not reach the server at {args.base}: {exc}")
        print("start it with:  uvicorn app.main:app --reload")
        return 1

    payment_id = result.get("failed_payment_id")
    if not payment_id:
        print(f"the webhook was accepted but produced no payment: {result}")
        return 1

    entries = _get(f"{args.base}/payments/{payment_id}/audit")
    for entry in entries:
        stage = STAGE_LABEL.get(entry["stage"], entry["stage"])
        print(f"{stage:<16}{_summarise(entry['stage'], entry['detail'])}")

    print(f"\nfull trace: {args.base}/payments/{payment_id}/audit")
    print(f"console:    {args.base}/console")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
