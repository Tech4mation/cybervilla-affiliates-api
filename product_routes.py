"""The product catalogue, as the affiliate dashboard sees it.

Everything here reads from our own copy of the catalogue. The only call that
touches the store is the refresh, which is not something a person waits on.

What is deliberately absent, because it has not been decided yet:

  * What an affiliate earns on a product. The dashboard used to show a
    commission percentage from invented data; showing an invented figure next
    to a real price is worse than showing none, so it is gone until the
    commission model is settled.
  * Whether a particular affiliate may promote a particular product. Every
    product the store offers is listed for now.
"""

import hmac
import logging

from flask import Blueprint, Response, jsonify, request

from catalog import (
    StoreNotConfigured,
    catalogue_state,
    get_product,
    list_categories,
    product_picture,
    query_products,
    refresh_catalogue,
)
from config import Config

log = logging.getLogger(__name__)

product_bp = Blueprint("products", __name__)

# What the dashboard should say when it has nothing to show. Phrased for the
# affiliate reading it, not for whoever has to fix it.
NO_CATALOGUE_MESSAGE = (
    "The product list is not available at the moment. It is being loaded from "
    "the CyberVilla store — please try again shortly."
)


def _int_arg(name: str, default: int) -> int:
    raw = (request.args.get(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


@product_bp.route("/products", methods=["GET"])
def products():
    state = catalogue_state()

    # Nothing has ever been copied across, so there is genuinely nothing to
    # show. This is the one case that is an error rather than stale data.
    if not state["everSynced"]:
        return jsonify({
            "error": "catalogue_unavailable",
            "message": NO_CATALOGUE_MESSAGE,
            "catalogue": state,
        }), 503

    category_id = _int_arg("category", 0) or None
    page = _int_arg("page", 1)
    per_page = _int_arg("perPage", 24)

    rows, total = query_products(
        search=request.args.get("search", ""),
        category_id=category_id,
        page=page,
        per_page=per_page,
    )

    return jsonify({
        "products": [row.as_dict() for row in rows],
        "page": page,
        "perPage": per_page,
        "total": total,
        "catalogue": state,
    })


@product_bp.route("/products/<int:odoo_id>", methods=["GET"])
def product(odoo_id: int):
    found = get_product(odoo_id)
    if not found:
        return jsonify({
            "error": "product_not_found",
            "message": "That product is not in the CyberVilla catalogue.",
        }), 404
    return jsonify({"product": found.as_dict(), "catalogue": catalogue_state()})


@product_bp.route("/products/<int:odoo_id>/image", methods=["GET"])
def product_image(odoo_id: int):
    """One product photograph.

    Open, like the picture on any shop's product page — it is the store's own
    marketing image, and the dashboard's product cards are rendered by the
    browser before anyone has signed in. There is nothing here that is not on
    the public storefront.
    """
    picture = product_picture(odoo_id)
    if not picture:
        return jsonify({
            "error": "no_image",
            "message": "That product has no picture.",
        }), 404

    raw, content_type = picture
    return Response(raw, mimetype=content_type, headers={
        "Cache-Control": "public, max-age=3600",
        "Content-Length": str(len(raw)),
    })


@product_bp.route("/categories", methods=["GET"])
def categories():
    return jsonify({"categories": list_categories()})


@product_bp.route("/internal/catalogue/refresh", methods=["POST"])
def refresh():
    """Pull the catalogue across from the store. For a scheduler, not a person.

    Guarded by a shared secret rather than by a signed-in user, because the
    caller is a cron job. With no secret set it refuses everything, so an
    unconfigured deployment cannot be made to hammer the store by anyone who
    guesses the address.
    """
    supplied = request.headers.get("X-Internal-Key", "")
    if not Config.INTERNAL_API_KEY or not hmac.compare_digest(supplied, Config.INTERNAL_API_KEY):
        return jsonify({
            "error": "forbidden",
            "message": "This endpoint needs a valid X-Internal-Key header.",
        }), 403

    try:
        result = refresh_catalogue()
    except StoreNotConfigured as exc:
        return jsonify({"error": "store_not_configured", "message": str(exc)}), 503

    return jsonify(result), (200 if result.get("ok") else 502)
