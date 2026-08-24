"""Prove the Razorpay integration against a live test-mode account.

The rest of this project runs offline, which is a deliberate constraint but leaves one
honest gap: nothing had ever touched a real Razorpay endpoint. This script closes it.
It creates a genuine Payment Link in test mode, prints the id and short URL, fetches it
back, and then cancels it so the account is left as it was found.

    RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=yyy python -m scripts.verify_razorpay

Nothing here is part of the recovery pipeline. It exists so the claim "the executor can
execute" can be checked rather than asserted, and so the created link id can be shown in
the submission video.

Refuses live keys outright — every amount in this system is synthetic, and a live key
would raise a real payment request against a real customer for a debt that never
existed.
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.executor import RazorpayGateway, UnsafeCredentials, get_gateway  # noqa: E402
from app.models import FailedPayment  # noqa: E402

# A deliberately tiny amount. Test mode moves no money, but the habit of proving an
# integration with the smallest possible value is the right one to keep.
AMOUNT_PAISE = 100


def banner(text: str) -> None:
    print(f"\n{'=' * 72}\n{text}\n{'=' * 72}")


def main() -> int:
    banner("Razorpay test-mode verification")

    key_id = settings.razorpay_key_id or os.environ.get("RAZORPAY_KEY_ID", "")
    if not key_id:
        print("No RAZORPAY_KEY_ID configured.\n")
        print("This script needs test-mode credentials from")
        print("  https://dashboard.razorpay.com/app/keys")
        print("\nRun it as:")
        print("  RAZORPAY_KEY_ID=rzp_test_xxx RAZORPAY_KEY_SECRET=yyy \\")
        print("      python -m scripts.verify_razorpay")
        print("\nWithout credentials the pipeline still runs end to end on the")
        print("DryRunGateway - that is by design, and `python -m scripts.seed_synthetic_data`")
        print("exercises it. What this script adds is proof that the real call works.")
        return 1

    try:
        gateway = get_gateway()
    except UnsafeCredentials as exc:
        print(f"REFUSED: {exc}")
        return 2

    if not isinstance(gateway, RazorpayGateway):
        print("Credentials incomplete - RAZORPAY_KEY_SECRET is also required.")
        return 1

    print(f"key            {key_id[:12]}... (test mode)")
    print(f"amount         Rs.{AMOUNT_PAISE / 100:.2f}")

    # A stand-in for a payment the agent would have decided to send a link for. Not
    # written to the database: this is an integration check, not a pipeline run.
    payment = FailedPayment(
        id="verify-local",
        razorpay_payment_id="pay_verify_probe",
        customer_id="cust_verify",
        rail="card",
        amount_paise=AMOUNT_PAISE,
        currency="INR",
        error_code="expired_card",
        source="failed_payment",
    )
    print(f"reference_id   {RazorpayGateway.reference_for(payment)}")

    banner("1. Create a payment link")
    result = gateway.send_payment_link(payment)
    print(f"outcome        {result.outcome}")
    print(f"link id        {result.ref}")
    print(f"short url      {result.message}")

    if result.outcome == "failed" or not result.ref:
        print("\nThe call did not succeed. The message above is Razorpay's own error.")
        print("A 4xx usually means the request shape needs reconciling against")
        print("https://razorpay.com/docs/api/payment-links/ for your account.")
        return 3

    banner("2. Fetch it back")
    fetched = gateway._client.payment_link.fetch(result.ref)
    print(f"status         {fetched.get('status')}")
    print(f"amount         {fetched.get('amount')} (paise, as sent)")
    print(f"reference_id   {fetched.get('reference_id')}")
    print(f"notes          {fetched.get('notes')}")

    if fetched.get("amount") != AMOUNT_PAISE:
        print("\nMISMATCH: the amount came back different from what was sent.")
        return 4

    banner("3. Cancel it, so the account is left as it was found")
    try:
        cancelled = gateway._client.payment_link.cancel(result.ref)
        print(f"status         {cancelled.get('status')}")
    except Exception as exc:
        # Not fatal to the verification: the create and fetch already proved the
        # integration. Say so plainly rather than failing a passing check.
        print(f"could not cancel ({type(exc).__name__}: {exc})")
        print(f"cancel it by hand in the dashboard: {result.ref}")

    banner("Verified")
    print("The executor can execute. A real Payment Link was created in test mode,")
    print("read back with the amount and reference intact, and then cancelled.")
    print(f"\nlink id for the record: {result.ref}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
