"""Is this thing running, and can it reach its database.

Deliberately answers 503 rather than 200 when the database is unreachable. A
health check that reports healthy while the service cannot do anything useful
is worse than no health check, because whatever is watching it will stop
looking.
"""

import logging

from flask import Blueprint, jsonify
from sqlalchemy import text

from catalog import catalogue_state
from models import db

log = logging.getLogger(__name__)

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    body: dict = {"service": "cybervilla-affiliates-api", "status": "ok", "database": "ok"}

    try:
        db.session.execute(text("SELECT 1"))
    except Exception as exc:
        db.session.rollback()
        log.warning("Health check could not reach the database: %s", exc)
        body["status"] = "unhealthy"
        body["database"] = "unreachable"
        return jsonify(body), 503

    try:
        body["catalogue"] = catalogue_state()
    except Exception as exc:
        # The tables may not have been migrated yet. That is worth reporting,
        # but it is not the service being down.
        log.warning("Health check could not read the catalogue state: %s", exc)
        body["catalogue"] = {"error": "unavailable"}

    return jsonify(body), 200
