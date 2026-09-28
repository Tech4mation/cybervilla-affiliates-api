"""Managing affiliates and links from the dashboard's back end.

These are the machine-to-machine calls that create affiliates and their links
and push them to the store. They are guarded by the shared internal key, the
same guard the catalogue refresh uses, because the caller is the dashboard's
server, not a browser.

Deliberately NOT here yet: the affiliate-facing API a signed-in affiliate calls
to see their own links and earnings. That waits on the still-open decision about
how affiliates sign up and sign in; wiring it to the wrong auth now would be
harder to unpick than adding it once that is settled. The data and the store
sync it needs are all in place.
"""

import hmac
import logging

from flask import Blueprint, jsonify, request

from affiliate_service import create_affiliate, create_link, resync_pending
from config import Config
from models import Affiliate, Earning

log = logging.getLogger(__name__)

affiliate_bp = Blueprint("affiliates", __name__)


def _internal_ok() -> bool:
    supplied = request.headers.get("X-Internal-Key", "")
    return bool(Config.INTERNAL_API_KEY) and hmac.compare_digest(supplied, Config.INTERNAL_API_KEY)


def _forbidden():
    return jsonify({"error": "forbidden",
                    "message": "This endpoint needs a valid X-Internal-Key header."}), 403


@affiliate_bp.route("/internal/affiliates", methods=["POST"])
def create_affiliate_route():
    if not _internal_ok():
        return _forbidden()
    body = request.get_json(silent=True) or {}
    try:
        affiliate = create_affiliate(
            name=body.get("name", ""),
            email=body.get("email", ""),
            phone=body.get("phone", ""),
        )
    except ValueError as exc:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400
    return jsonify({"affiliate": affiliate.as_dict()}), 201


@affiliate_bp.route("/internal/affiliates/<backend_ref>/links", methods=["POST"])
def create_link_route(backend_ref: str):
    if not _internal_ok():
        return _forbidden()
    affiliate = Affiliate.query.filter_by(backend_ref=backend_ref).first()
    if not affiliate:
        return jsonify({"error": "not_found", "message": "No such affiliate."}), 404

    body = request.get_json(silent=True) or {}
    link = create_link(
        affiliate=affiliate,
        markup_percent=body.get("markupPercent", 0),
        label=body.get("label", ""),
    )
    # 201 whether or not the store push landed; `synced` says which. A link the
    # store has not accepted yet will not price there until resync succeeds.
    return jsonify({"link": link.as_dict()}), 201


@affiliate_bp.route("/internal/affiliates/resync", methods=["POST"])
def resync_route():
    if not _internal_ok():
        return _forbidden()
    return jsonify(resync_pending()), 200


@affiliate_bp.route("/internal/earnings", methods=["GET"])
def earnings_route():
    """A read of recorded earnings, for checking the webhook end to end."""
    if not _internal_ok():
        return _forbidden()
    code = request.args.get("affiliateCode")
    query = Earning.query
    if code:
        query = query.filter_by(affiliate_code=code)
    rows = query.order_by(Earning.id.desc()).limit(200).all()
    return jsonify({"earnings": [row.as_dict() for row in rows]}), 200
