"""The store telling us an affiliate order was paid, or cancelled.

The cybervilla_affiliate Odoo module POSTs here when an attributed order is
confirmed or cancelled. Two things have to be true before we act on it:

  * It is signed with the secret we share with the module. The body is signed
    with HMAC-SHA256, exactly as the store's Paystack webhook is, and a body
    whose signature does not match is refused. Without a secret configured we
    refuse everything, so an unconfigured deployment cannot be driven by anyone
    who finds the address.
  * We answer 200 only once it is safely recorded. The module retries anything
    it did not get a 200 for, which is why recording is idempotent on the order
    reference — a retry after a timeout that actually worked changes nothing.
"""

import hashlib
import hmac
import logging

from flask import Blueprint, jsonify, request

from affiliate_service import record_order_event
from config import Config

log = logging.getLogger(__name__)

webhook_bp = Blueprint("odoo_webhook", __name__)

SIGNATURE_HEADER = "X-Cybervilla-Signature"
EVENT_HEADER = "X-Cybervilla-Event"


def _signature_ok(raw_body: bytes, supplied: str) -> bool:
    secret = Config.ODOO_WEBHOOK_SECRET
    if not secret or not supplied:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, supplied)


@webhook_bp.route("/odoo/webhook", methods=["POST"])
def odoo_webhook():
    # Read the raw body before anything parses it — the signature is over the
    # exact bytes the store sent, not over a re-encoding of the parsed JSON.
    raw_body = request.get_data()
    supplied = request.headers.get(SIGNATURE_HEADER, "")

    if not _signature_ok(raw_body, supplied):
        log.warning("Rejected an Odoo webhook with a bad or missing signature")
        return jsonify({"error": "bad_signature",
                        "message": "This request is not signed by the store."}), 401

    try:
        payload = request.get_json(force=True, silent=False) or {}
    except Exception:
        return jsonify({"error": "bad_payload",
                        "message": "The body was not valid JSON."}), 400

    try:
        earning = record_order_event(payload)
    except ValueError as exc:
        return jsonify({"error": "bad_payload", "message": str(exc)}), 400
    except Exception:
        log.exception("Failed to record an affiliate order event")
        # A 500 tells the store to retry, which is what we want if our own
        # database hiccuped rather than the payload being wrong.
        return jsonify({"error": "server_error",
                        "message": "Could not record the event."}), 500

    return jsonify({"ok": True, "status": earning.status}), 200
