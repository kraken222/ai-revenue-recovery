"""Webhook authenticity.

Before this existed, `/webhooks/razorpay` accepted any POST from anyone. That endpoint
creates payment records, runs them through compliance, and can execute a gateway action
that sends a real payment link to a real customer — so an unauthenticated caller who
knew the URL could make this system contact people about debts it invented. Signature
verification is not hardening here; it is the difference between an agent and an open
relay.

Two details that matter more than they look:

**Verify the raw bytes, before parsing.** The signature covers the exact body Razorpay
sent. Re-serialising a parsed dict produces different bytes (key order, whitespace,
float formatting) and the HMAC will not match, so the check has to run on the request
body as received.

**Compare in constant time.** A byte-by-byte early-exit comparison leaks how much of a
forged signature was correct, which is enough to reconstruct one over many attempts.
`hmac.compare_digest` is the standard remedy and costs nothing.
"""

from __future__ import annotations

import hashlib
import hmac
import logging

logger = logging.getLogger(__name__)

SIGNATURE_HEADER = "X-Razorpay-Signature"


def expected_signature(body: bytes, secret: str) -> str:
    """Razorpay signs the raw body with HMAC-SHA256, hex-encoded."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def is_authentic(body: bytes, signature: str | None, secret: str) -> bool:
    """True when `signature` is a valid HMAC of `body` under `secret`.

    An unset secret returns True: local development and CI run without one, and the
    whole project is designed to work offline. That is the single branch that could
    silently disable this check in a real deployment, so it is stated plainly here and
    pinned by a test rather than left implicit.
    """
    if not secret:
        return True
    if not signature:
        return False
    return hmac.compare_digest(expected_signature(body, secret), signature)


def rejection_reason(signature: str | None, secret: str) -> str:
    """What to log when a webhook is refused. Deliberately does not echo the supplied
    signature — a rejected request is potentially hostile, and logging attacker-supplied
    values verbatim is how log injection happens."""
    if not secret:
        return "no secret configured"
    return "missing signature header" if not signature else "signature did not match"
