import os

import razorpay


class PaymentServiceError(Exception):
    pass


def _client():
    """Builds a real Razorpay client from env vars only. Never returns or
    logs the credentials themselves -- see NEGOTIATION_SPEC.md Section 3A."""
    key_id = os.environ.get("RAZORPAY_KEY_ID")
    key_secret = os.environ.get("RAZORPAY_KEY_SECRET")
    if not key_id or not key_secret:
        raise PaymentServiceError(
            "RAZORPAY_KEY_ID and RAZORPAY_KEY_SECRET must be set in the environment."
        )
    return razorpay.Client(auth=(key_id, key_secret))


def create_order(amount, currency, client=None):
    """Real Razorpay test-mode order creation (orders.create). `amount` is
    an integer in the currency's smallest subunit (paise for INR). Returns
    the curated payment field subset from NEGOTIATION_SPEC.md Section 4A
    -- never the raw API response."""
    client = client or _client()
    order = client.order.create({"amount": amount, "currency": currency, "payment_capture": 1})
    return {
        "order_id": order["id"],
        "payment_id": None,
        "amount": order["amount"],
        "currency": order["currency"],
        "status": "created",
        "error_code": None,
        "error_description": None,
    }


def simulate_payment(payment, force_failure=False):
    """In-process simulation of the payment outcome. Not a real
    payments.capture call -- see NEGOTIATION_SPEC.md Section 3A for why.
    payment_id stays null in both outcomes (reserved for a future
    milestone with real checkout)."""
    result = dict(payment)
    if force_failure:
        result["status"] = "failed"
        result["error_code"] = "BAD_REQUEST_ERROR"
        result["error_description"] = "Payment failed (simulated test-mode failure)."
    else:
        result["status"] = "completed"
    return result
