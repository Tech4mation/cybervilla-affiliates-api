"""Paystack telling us what became of a transfer.

A transfer is not finished when Paystack accepts it. It is accepted, and then
some time later it succeeds, fails, or is reversed by the receiving bank.
That later word arrives here, and it is the only thing that settles a payout
— nothing in this service concludes that money arrived because a call
returned without an error.

Three rules, all of which exist because this endpoint is public:

  * It is signed. Paystack signs the body with HMAC-**SHA512** using the
    secret key — note the difference from the Odoo webhook next door, which
    is SHA256 with its own shared secret. An unsigned or mis-signed body is
    refused, and with no key configured everything is refused, so an
    unconfigured deployment cannot be driven by whoever finds the address.
  * Repeats change nothing. Paystack retries anything it does not get a 200
    for, so the same event arriving twice must not pay twice or notify twice.
    That is enforced in `apply_provider_event`.
  * Events we do not care about still get a 200. Every event on the Paystack
    account comes to this one URL, and answering anything else would make
    Paystack retry charge notifications at us forever.
"""

import hashlib
import hmac
import logging

from flask import Blueprint, jsonify, request

from config import Config
from payout_service import apply_provider_event

log = logging.getLogger(__name__)

paystack_webhook_bp = Blueprint("paystack_webhook", __name__)

SIGNATURE_HEADER = "X-Paystack-Signature"

# The event name is more trustworthy than data.status for deciding what
# happened, because it is what Paystack chose to tell us about.
EVENT_STATUS = {
    "transfer.success": "success",
    "transfer.failed": "failed",
    "transfer.reversed": "reversed",
}


def _signature_ok(raw_body: bytes, supplied: str) -> bool:
    secret = Config.PAYSTACK_SECRET_KEY
    if not secret or not supplied:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha512).hexdigest()
    return hmac.compare_digest(expected, supplied)


def _failure_reason(data: dict) -> str:
    """Whatever Paystack offered by way of explanation.

    The shape varies by event, so each known spelling is tried rather than
    assuming one and showing the affiliate an empty reason.
    """
    for key in ("reason", "message", "failure_reason", "gateway_response"):
        value = data.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    failures = data.get("failures")
    if isinstance(failures, str) and failures.strip():
        return failures.strip()
    return ""


@paystack_webhook_bp.route("/paystack/webhook", methods=["POST"])
def paystack_webhook():
    # The raw bytes, before anything parses them: the signature covers
    # exactly what was sent, not a re-encoding of the parsed JSON.
    raw_body = request.get_data()
    supplied = request.headers.get(SIGNATURE_HEADER, "")

    if not _signature_ok(raw_body, supplied):
        log.warning("Rejected a Paystack webhook with a bad or missing signature")
        return jsonify({"error": "bad_signature",
                        "message": "This request is not signed by Paystack."}), 401

    payload = request.get_json(force=True, silent=True) or {}
    event = (payload.get("event") or "").strip()
    data = payload.get("data") or {}

    if event not in EVENT_STATUS:
        # Charges, subscriptions, everything else on the account lands here
        # too. Acknowledged and ignored, or Paystack will keep retrying.
        return jsonify({"ok": True, "ignored": event or "unknown"}), 200

    reference = (data.get("reference") or "").strip()
    transfer_code = (data.get("transfer_code") or "").strip()
    if not (reference or transfer_code):
        log.warning("Paystack %s arrived with nothing to identify it by; ignoring", event)
        return jsonify({"ok": True, "ignored": "no reference"}), 200

    try:
        payout = apply_provider_event(
            reference, EVENT_STATUS[event], _failure_reason(data), transfer_code,
        )
    except Exception:
        # A 500 asks Paystack to try again, which is what we want when the
        # fault is ours. Settling is idempotent, so a retry is safe.
        log.exception("Failed to apply Paystack %s for %s", event, reference)
        return jsonify({"error": "server_error",
                        "message": "Could not record that event."}), 500

    if payout is None:
        return jsonify({"ok": True, "ignored": "unknown reference"}), 200
    return jsonify({"ok": True, "payout": payout.reference, "status": payout.status}), 200
