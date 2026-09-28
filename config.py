"""Settings, read once from the environment.

Everything the service needs to know about the outside world arrives as an
environment variable. There is no settings table and no per-tenant connector
config: this service serves one store, CyberVilla, and knowing that is what
lets it stay small.
"""

import os

from dotenv import load_dotenv

load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    raw = (os.getenv(name) or "").strip()
    try:
        return int(raw) if raw else default
    except ValueError:
        return default


class Config:
    # --- Database ---------------------------------------------------------
    # Any SQLAlchemy URL is accepted so a throwaway SQLite file can be used for
    # a smoke test, but PostgreSQL is what this runs on.
    SQLALCHEMY_DATABASE_URI = (os.getenv("DATABASE_URL") or "").strip()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    # Connections held open across a quiet night are dead by morning; checking
    # one before it is handed out costs a round trip and saves a 500.
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True}

    # --- The CyberVilla store --------------------------------------------
    ODOO_URL = (os.getenv("ODOO_URL") or "").strip()
    ODOO_DATABASE = (os.getenv("ODOO_DATABASE") or "").strip()
    ODOO_USERNAME = (os.getenv("ODOO_USERNAME") or "").strip()
    ODOO_API_KEY = (os.getenv("ODOO_API_KEY") or "").strip()

    CATALOGUE_TTL_MINUTES = _int("CATALOGUE_TTL_MINUTES", 60)

    # --- Web --------------------------------------------------------------
    CORS_ORIGINS = [
        origin.strip()
        for origin in (os.getenv("CORS_ORIGINS") or "*").split(",")
        if origin.strip()
    ]
    INTERNAL_API_KEY = (os.getenv("INTERNAL_API_KEY") or "").strip()

    # --- The affiliate store integration ---------------------------------
    # Shared with the cybervilla_affiliate Odoo module. Odoo signs every
    # paid/cancelled notification with this (HMAC-SHA256); we refuse any that
    # does not match, so the endpoint cannot be driven by whoever finds it.
    # It must equal the module's `cybervilla_affiliate.webhook_secret`.
    ODOO_WEBHOOK_SECRET = (os.getenv("ODOO_WEBHOOK_SECRET") or "").strip()

    # The most an affiliate may add to a price. Enforced here as well as in
    # Odoo, so a bad markup is stopped before it is ever pushed to the store.
    MAX_MARKUP_PERCENT = _int("MAX_MARKUP_PERCENT", 10)

    # --- Authentication & Admin ------------------------------------------
    # No defaults here, deliberately. A fallback secret in source is a secret
    # everyone has: it signs every session token, so anyone who can read this
    # file could mint an admin token for a deployment that never set one.
    # Same for the admin password. Both must come from the environment, and
    # the service refuses to start without them — see `missing_auth_config`.
    JWT_SECRET = (os.getenv("JWT_SECRET") or "").strip()
    JWT_EXPIRY_HOURS = _int("JWT_EXPIRY_HOURS", 72)
    ADMIN_EMAIL = (os.getenv("ADMIN_EMAIL") or "admin@cybervilla.io").strip().lower()
    ADMIN_PASSWORD = (os.getenv("ADMIN_PASSWORD") or "").strip()
    ADMIN_NAME = (os.getenv("ADMIN_NAME") or "CyberVilla Admin").strip()

    LOG_LEVEL = (os.getenv("LOG_LEVEL") or "INFO").strip().upper()

    @classmethod
    def missing_database(cls) -> bool:
        return not cls.SQLALCHEMY_DATABASE_URI

    @classmethod
    def missing_auth_config(cls) -> list[str]:
        """Which authentication settings have not been supplied.

        Returned rather than raised so the caller decides how loudly to fail;
        running without these is not a degraded mode, it is an open door.
        """
        missing = []
        if not cls.JWT_SECRET:
            missing.append("JWT_SECRET")
        if not cls.ADMIN_PASSWORD:
            missing.append("ADMIN_PASSWORD")
        return missing

    @classmethod
    def store_is_configured(cls) -> bool:
        """True when all four Odoo credentials are present.

        The service is allowed to run without them — it will serve whatever
        catalogue was last stored and say plainly that it cannot refresh —
        because a missing API key should not take the dashboard down.
        """
        return all([cls.ODOO_URL, cls.ODOO_DATABASE, cls.ODOO_USERNAME, cls.ODOO_API_KEY])
