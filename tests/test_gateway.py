"""Tests for the Razorpay boundary: webhook authenticity and gateway calls.

Written before the implementation.

The first block is the serious one. `/webhooks/razorpay` currently accepts any POST
from anyone: the secret is declared in config and never used. That endpoint creates
payment records, runs compliance, and can execute a gateway action that sends a real
payment link to a real customer — so an unauthenticated caller who knows the URL can
make this system contact people. Signature verification is not a hardening nicety
here, it is the difference between an agent and an open relay.

Everything runs offline. The signature tests compute real HMACs rather than stubbing
the check, so they exercise the actual comparison; the gateway tests use a fake client
so no network call is ever made.
"""

import hashlib
import hmac
import json

import pytest
from fastapi.testclient import TestClient

from app import executor
from app.config import settings
from app.db import make_session_factory
from app.models import FailedPayment

SECRET = "whsec_test_abc123"


def sign(body: bytes, secret: str = SECRET) -> str:
    """Razorpay signs the raw request body with HMAC-SHA256, hex-encoded."""
    return hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


@pytest.fixture
def client(monkeypatch, tmp_path):
    """An app wired to a throwaway database, with a webhook secret configured."""
    monkeypatch.setattr(settings, "razorpay_webhook_secret", SECRET)
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{tmp_path/'t.db'}")

    from app import db as db_module
    from app import main

    factory, engine = make_session_factory(f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setattr(db_module, "SessionLocal", factory)
    monkeypatch.setattr(db_module, "engine", engine)

    with TestClient(main.app) as c:
        yield c
    engine.dispose()


def _payload(pid: str = "pay_sig_1") -> dict:
    return {
        "event_id": f"evt_{pid}",
        "event": "payment.failed",
        "payload": {"payment": {
            "id": pid, "customer_id": "cust_1", "rail": "upi_autopay",
            "amount_paise": 99900, "error_code": "insufficient_balance",
            "error_description": "insufficient balance", "source": "failed_payment",
        }},
    }


# --- webhook authenticity -----------------------------------------------------


def test_unsigned_webhook_is_rejected(client):
    """The hole this closes. Without verification, anyone who knows the URL can inject
    a failed payment and make the agent contact a real customer."""
    body = json.dumps(_payload()).encode()
    res = client.post("/webhooks/razorpay", content=body,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 401


def test_wrong_signature_is_rejected(client):
    body = json.dumps(_payload()).encode()
    res = client.post("/webhooks/razorpay", content=body,
                      headers={"Content-Type": "application/json",
                               "X-Razorpay-Signature": sign(body, "the-wrong-secret")})
    assert res.status_code == 401


def test_correctly_signed_webhook_is_accepted(client):
    body = json.dumps(_payload()).encode()
    res = client.post("/webhooks/razorpay", content=body,
                      headers={"Content-Type": "application/json",
                               "X-Razorpay-Signature": sign(body)})
    assert res.status_code == 200, res.text
    assert res.json()["processed"] is True


def test_signature_covers_the_body_not_just_the_secret(client):
    """A signature valid for one payload must not authenticate a different one —
    otherwise an attacker could replay a captured header against altered content."""
    original = json.dumps(_payload("pay_original")).encode()
    tampered = json.dumps(_payload("pay_tampered")).encode()

    res = client.post("/webhooks/razorpay", content=tampered,
                      headers={"Content-Type": "application/json",
                               "X-Razorpay-Signature": sign(original)})
    assert res.status_code == 401


def test_outcome_webhook_is_protected_too(client):
    """The outcome endpoint moves a payment to RECOVERED. Leaving it unauthenticated
    while protecting the other one would let anyone mark debts settled."""
    body = json.dumps({
        "event_id": "evt_out_1", "razorpay_payment_id": "pay_x", "success": True
    }).encode()
    res = client.post("/webhooks/razorpay/outcome", content=body,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 401


def test_verification_is_skipped_only_when_no_secret_is_configured(client, monkeypatch):
    """Local development has no secret and must still run. But this is the one branch
    that could silently disable the check in production, so it is pinned: unset means
    open, and any set value means enforced."""
    monkeypatch.setattr(settings, "razorpay_webhook_secret", "")
    body = json.dumps(_payload("pay_nosecret")).encode()
    res = client.post("/webhooks/razorpay", content=body,
                      headers={"Content-Type": "application/json"})
    assert res.status_code == 200


# --- gateway selection --------------------------------------------------------


def test_dry_run_is_the_default_without_credentials(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "")
    monkeypatch.setattr(settings, "razorpay_key_secret", "")
    assert isinstance(executor.get_gateway(), executor.DryRunGateway)


def test_real_gateway_is_used_when_credentials_exist(monkeypatch):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake_secret")
    assert isinstance(executor.get_gateway(), executor.RazorpayGateway)


def test_live_keys_are_refused(monkeypatch):
    """A `rzp_live_` key in a project whose data is synthetic would move real money.
    Refusing it outright is cheaper than trusting a deployment checklist."""
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_live_realkey")
    monkeypatch.setattr(settings, "razorpay_key_secret", "real_secret")
    with pytest.raises(executor.UnsafeCredentials):
        executor.get_gateway()


# --- payment link creation ----------------------------------------------------


class FakePaymentLink:
    """Stands in for `client.payment_link`, recording what it was asked to create."""

    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response or {
            "id": "plink_TEST123", "short_url": "https://rzp.io/i/TEST123",
            "status": "created",
        }
        self._error = error

    def create(self, params):
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._response


def _gateway(monkeypatch, link):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake")
    gw = executor.RazorpayGateway("rzp_test_fake", "fake")
    gw._client.payment_link = link
    return gw


def _payment(**kw) -> FailedPayment:
    defaults = dict(
        id="internal-1", razorpay_payment_id="pay_1", customer_id="cust_1",
        rail="card", amount_paise=99900, currency="INR",
        error_code="expired_card", source="failed_payment",
    )
    defaults.update(kw)
    return FailedPayment(**defaults)


def test_payment_link_sends_the_amount_in_paise(monkeypatch):
    """Razorpay takes the smallest currency unit. Sending rupees would create a link
    for a hundredth of the debt."""
    link = FakePaymentLink()
    result = _gateway(monkeypatch, link).send_payment_link(_payment(amount_paise=99900))

    assert link.calls[0]["amount"] == 99900
    assert link.calls[0]["currency"] == "INR"
    assert result.ref == "plink_TEST123"
    assert "rzp.io" in result.message


def test_payment_link_is_idempotent_per_payment(monkeypatch):
    """Razorpay creates a new link on every create call, so a retried webhook or a
    worker re-run would send the same customer several different links for one debt."""
    link = FakePaymentLink()
    gw = _gateway(monkeypatch, link)
    payment = _payment()

    gw.send_payment_link(payment)
    gw.send_payment_link(payment)

    keys = [c.get("reference_id") for c in link.calls]
    assert keys[0] == keys[1] is not None, "reference_id must be stable for one payment"


def test_gateway_errors_do_not_crash_the_pipeline(monkeypatch):
    """A declined or malformed request is an outcome, not a reason for the recovery
    loop to stop processing the rest of the batch."""
    import razorpay.errors as rzp_errors

    link = FakePaymentLink(error=rzp_errors.BadRequestError("amount is invalid"))
    result = _gateway(monkeypatch, link).send_payment_link(_payment())

    assert result.outcome == "failed"
    assert "amount is invalid" in (result.message or "")


def test_server_errors_are_reported_as_pending_not_failed(monkeypatch):
    """A 5xx means we do not know whether the link was created. Recording it as failed
    would let a retry create a second link for the same debt."""
    import razorpay.errors as rzp_errors

    link = FakePaymentLink(error=rzp_errors.ServerError("gateway timeout"))
    result = _gateway(monkeypatch, link).send_payment_link(_payment())

    assert result.outcome == "pending"


def test_link_carries_a_reference_back_to_the_payment(monkeypatch):
    """Reconciliation needs the link to point at the debt it was raised for."""
    link = FakePaymentLink()
    _gateway(monkeypatch, link).send_payment_link(_payment(razorpay_payment_id="pay_xyz"))

    call = link.calls[0]
    assert "pay_xyz" in json.dumps(call)
