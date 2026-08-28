"""`retry_charge` against the documented Razorpay recurring-payment contract.

The contract these tests pin, verbatim from the docs and matched by the pinned SDK's
`client.payment.createRecurring`:

    POST /v1/orders                     -> order_id
    POST /v1/payments/create/recurring  -> {"razorpay_payment_id": "pay_..."}
      body: email, contact, amount, currency, order_id, customer_id, token,
            recurring: true, description, notes

  https://razorpay.com/docs/api/payments/recurring-payments/upi/create-subsequent-payments
  https://razorpay.com/docs/api/payments/recurring-payments/emandate/create-subsequent-payments

These tests establish that the request we build matches the documented shape. They do
NOT establish that the call succeeds against Razorpay -- that needs a token minted by a
real customer completing a UPI Autopay or eNACH authorisation, which no test here can
fabricate. The distinction is the whole point of keeping it explicit.
"""

import pytest

from app import executor
from app.config import settings
from app.executor import UnsupportedRailAction
from app.models import FailedPayment


class FakeOrder:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response or {"id": "order_TEST123"}
        self._error = error

    def create(self, params):
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._response


class FakePayment:
    def __init__(self, response=None, error=None):
        self.calls = []
        self._response = response or {"razorpay_payment_id": "pay_CHARGED1"}
        self._error = error

    def createRecurring(self, params):  # noqa: N802 - mirrors the SDK's method name
        self.calls.append(params)
        if self._error:
            raise self._error
        return self._response


def _gateway(monkeypatch, order=None, payment=None):
    monkeypatch.setattr(settings, "razorpay_key_id", "rzp_test_fake")
    monkeypatch.setattr(settings, "razorpay_key_secret", "fake")
    gw = executor.RazorpayGateway("rzp_test_fake", "fake")
    gw._client.order = order or FakeOrder()
    gw._client.payment = payment or FakePayment()
    return gw


def _mandate_payment(**kw) -> FailedPayment:
    defaults = dict(
        id="internal-1",
        razorpay_payment_id="pay_1",
        customer_id="cust_1",
        rail="upi_autopay",
        amount_paise=99900,
        currency="INR",
        error_code="insufficient_balance",
        source="failed_payment",
        mandate_token="token_1Aa00000000001",
        customer_email="customer@example.com",
        customer_contact="+919800000000",
        retry_count=0,
    )
    defaults.update(kw)
    return FailedPayment(**defaults)


# --- the documented request shape ----------------------------------------------


def test_charge_sends_every_field_the_endpoint_requires(monkeypatch):
    order, payment = FakeOrder(), FakePayment()
    _gateway(monkeypatch, order, payment).retry_charge(_mandate_payment())

    body = payment.calls[0]
    for field in (
        "email", "contact", "amount", "currency",
        "order_id", "customer_id", "token", "recurring",
    ):
        assert field in body, f"required field {field!r} missing from the request"
    assert body["recurring"] is True
    assert body["token"] == "token_1Aa00000000001"
    assert body["customer_id"] == "cust_1"


def test_amount_is_sent_in_paise(monkeypatch):
    """Razorpay takes the smallest currency unit. Rupees would debit a hundredth."""
    order, payment = FakeOrder(), FakePayment()
    _gateway(monkeypatch, order, payment).retry_charge(_mandate_payment(amount_paise=99900))

    assert order.calls[0]["amount"] == 99900
    assert payment.calls[0]["amount"] == 99900


def test_the_order_is_created_before_the_charge_and_its_id_is_used(monkeypatch):
    """The recurring endpoint takes an order_id and will not mint one. Passing a stale
    or invented id would charge against the wrong order."""
    order = FakeOrder(response={"id": "order_REAL"})
    payment = FakePayment()
    _gateway(monkeypatch, order, payment).retry_charge(_mandate_payment())

    assert order.calls, "no order was created"
    assert payment.calls[0]["order_id"] == "order_REAL"


def test_enach_uses_the_same_endpoint_as_upi(monkeypatch):
    """One endpoint serves both mandate rails; the rail is encoded in the token."""
    payment = FakePayment()
    _gateway(monkeypatch, FakeOrder(), payment).retry_charge(_mandate_payment(rail="enach"))
    assert payment.calls[0]["token"] == "token_1Aa00000000001"


def test_receipt_is_stable_per_attempt(monkeypatch):
    """A redelivered webhook must re-use the receipt; a genuine second attempt must
    not. Same attempt twice -> same receipt."""
    order = FakeOrder()
    gw = _gateway(monkeypatch, order, FakePayment())
    gw.retry_charge(_mandate_payment(retry_count=1))
    gw.retry_charge(_mandate_payment(retry_count=1))
    assert order.calls[0]["receipt"] == order.calls[1]["receipt"]

    gw.retry_charge(_mandate_payment(retry_count=2))
    assert order.calls[2]["receipt"] != order.calls[0]["receipt"]


# --- refusals ------------------------------------------------------------------


def test_card_rail_is_refused_outright(monkeypatch):
    """Not a failure to be retried: on cards this action should not exist. Razorpay
    owns card retries and manual domestic-card charge is unsupported."""
    order, payment = FakeOrder(), FakePayment()
    with pytest.raises(UnsupportedRailAction):
        _gateway(monkeypatch, order, payment).retry_charge(_mandate_payment(rail="card"))
    assert not order.calls, "an order was created for a rail that cannot be charged"
    assert not payment.calls


@pytest.mark.parametrize(
    "missing", ["mandate_token", "customer_email", "customer_contact"]
)
def test_missing_mandate_fields_refuse_without_calling_razorpay(monkeypatch, missing):
    """Without a token there is nothing to debit, and the right recovery path is
    re-registration -- a different action, chosen by compliance. Improvising here, or
    creating the order anyway, would spend money and leave a dangling order."""
    order, payment = FakeOrder(), FakePayment()
    result = _gateway(monkeypatch, order, payment).retry_charge(
        _mandate_payment(**{missing: None})
    )

    assert result.outcome == "failed"
    assert missing in result.message
    assert not order.calls, "an order was created for a charge that cannot be built"
    assert not payment.calls


# --- outcome semantics ---------------------------------------------------------


def test_a_created_charge_is_pending_not_success(monkeypatch):
    """The endpoint returns a payment id, not a settled outcome. Whether the bank
    honoured the debit arrives on the outcome webhook. Calling this success would close
    the loop on a result nobody reported, and feed the bandit a reward it never earned.
    """
    result = _gateway(monkeypatch).retry_charge(_mandate_payment())
    assert result.outcome == "pending"
    assert result.ref == "pay_CHARGED1"


def test_server_error_on_the_charge_is_pending_not_failed(monkeypatch):
    """A 5xx means we do not know whether the debit was raised. Calling it failed would
    licence a second charge against the same debt."""
    import razorpay.errors as rzp

    payment = FakePayment(error=rzp.ServerError("upstream exploded"))
    result = _gateway(monkeypatch, FakeOrder(), payment).retry_charge(_mandate_payment())
    assert result.outcome == "pending"


def test_bad_request_on_the_charge_is_failed(monkeypatch):
    """A 4xx means the request was rejected and nothing was created."""
    import razorpay.errors as rzp

    payment = FakePayment(error=rzp.BadRequestError("token invalid"))
    result = _gateway(monkeypatch, FakeOrder(), payment).retry_charge(_mandate_payment())
    assert result.outcome == "failed"


def test_order_failure_stops_before_the_charge(monkeypatch):
    """If the order could not be created there is nothing to charge against, and
    calling createRecurring with a missing order_id would be a guaranteed 4xx."""
    import razorpay.errors as rzp

    order = FakeOrder(error=rzp.ServerError("orders down"))
    payment = FakePayment()
    result = _gateway(monkeypatch, order, payment).retry_charge(_mandate_payment())

    assert result.outcome == "pending"
    assert not payment.calls, "charged without an order"


# --- the dry-run path mirrors the real one -------------------------------------


def test_dry_run_refuses_cards_too():
    with pytest.raises(UnsupportedRailAction):
        executor.DryRunGateway().retry_charge(_mandate_payment(rail="card"))


def test_dry_run_refuses_a_missing_token():
    """A dry run that debits a mandate the live path would refuse teaches the offline
    demo the wrong shape of the system."""
    result = executor.DryRunGateway().retry_charge(_mandate_payment(mandate_token=None))
    assert result.outcome == "failed"


def test_dry_run_charges_a_well_formed_mandate():
    result = executor.DryRunGateway().retry_charge(_mandate_payment())
    assert result.outcome == "pending"
