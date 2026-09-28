"""The service itself: what it is made of and how it is started.

A factory rather than a module-level app, so that the Flask command line, the
production server and a test can each build one with their own settings without
fighting over a global.
"""

import logging
import sys

import click
from flask import Flask, jsonify
from flask_cors import CORS
from flask_migrate import Migrate

from config import Config
from models import db

migrate = Migrate()


def create_app(config_object: type = Config) -> Flask:
    app = Flask(__name__)
    app.config.from_object(config_object)

    logging.basicConfig(
        level=getattr(logging, config_object.LOG_LEVEL, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.StreamHandler(sys.stdout)],
    )
    log = logging.getLogger(__name__)

    if config_object.missing_database():
        raise RuntimeError(
            "DATABASE_URL is not set, so there is nothing to connect to. "
            "Copy .env.example to .env and fill it in."
        )

    missing_auth = config_object.missing_auth_config()
    if missing_auth:
        raise RuntimeError(
            f"{' and '.join(missing_auth)} must be set. These used to fall back to "
            "values written in the source, which meant any deployment that did not "
            "set them could have its admin session tokens forged by anyone who read "
            "the code. Generate them with: python -c \"import secrets; "
            "print(secrets.token_urlsafe(48))\""
        )

    db.init_app(app)
    migrate.init_app(app, db)

    # The dashboard is served from a different origin, so the browser will not
    # call this at all without being told the call is allowed.
    CORS(app, resources={r"/*": {"origins": config_object.CORS_ORIGINS}},
         supports_credentials=True)

    from health_routes import health_bp
    from product_routes import product_bp
    from affiliate_routes import affiliate_bp
    from webhook_routes import webhook_bp
    from auth_routes import auth_bp

    app.register_blueprint(health_bp)
    app.register_blueprint(product_bp)
    app.register_blueprint(affiliate_bp)
    app.register_blueprint(webhook_bp)
    app.register_blueprint(auth_bp)

    @app.route("/", methods=["GET"])
    def index():
        return jsonify({
            "service": "cybervilla-affiliates-api",
            "health": "/health",
        })

    @app.errorhandler(404)
    def not_found(_error):
        return jsonify({
            "error": "not_found",
            "message": "There is nothing at that address.",
        }), 404

    @app.errorhandler(500)
    def server_error(error):
        log.exception("Unhandled error: %s", error)
        return jsonify({
            "error": "server_error",
            "message": "Something went wrong at our end.",
        }), 500

    _register_commands(app)

    # Ensure single admin account exists if DB tables are present
    with app.app_context():
        try:
            from auth_service import ensure_admin_account
            ensure_admin_account()
        except Exception:
            # Tolerant if tables have not been migrated yet during deploy
            log.debug("Admin auto-seed skipped or deferred (tables may be pending migration).")

    if not config_object.store_is_configured():
        log.warning(
            "The CyberVilla store connection is not configured — the catalogue "
            "cannot be refreshed. Set ODOO_URL, ODOO_DATABASE, ODOO_USERNAME "
            "and ODOO_API_KEY."
        )

    return app


def _register_commands(app: Flask) -> None:
    @app.cli.command("create-admin")
    @click.option("--email", default=None, help="Admin email address.")
    @click.option("--password", default=None, help="Admin password.")
    @click.option("--name", default=None, help="Admin full name.")
    def create_admin_command(email, password, name):
        """Create or ensure the single admin account. Safe to re-run."""
        from auth_service import ensure_admin_account

        admin = ensure_admin_account(email=email, password=password, name=name)
        click.echo(f"Admin account ready: {admin.email} (ID: {admin.id}, Name: {admin.name})")

    @app.cli.command("sync-catalogue")
    @click.option("--batch-size", default=200, show_default=True,
                  help="How many products to read from the store per call.")
    @click.option("--no-images", is_flag=True,
                  help="Skip product pictures. The first run with pictures is "
                       "the slow one; this makes prices current without it.")
    def sync_catalogue(batch_size: int, no_images: bool):
        """Copy the CyberVilla catalogue into this service. Safe to re-run."""
        from catalog import StoreNotConfigured, refresh_catalogue

        try:
            result = refresh_catalogue(batch_size=batch_size, with_images=not no_images)
        except StoreNotConfigured as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        if result.get("ok"):
            click.echo(
                f"Catalogue refreshed: {result['productCount']} products, "
                f"{result.get('withdrawn', 0)} no longer offered."
            )
            pictures = result.get("pictures")
            if pictures:
                click.echo(
                    f"Pictures: {pictures['stored']} stored, "
                    f"{pictures['withoutPicture']} products have none, "
                    f"{pictures['failed']} could not be read."
                )
            return
        click.echo(f"Catalogue refresh failed: {result.get('error')}", err=True)
        raise SystemExit(1)

    @app.cli.command("warm-images")
    @click.option("--limit", type=int, default=None,
                  help="Stop after this many products. Useful for a first look.")
    def warm_images_command(limit: int | None):
        """Fetch the product pictures we do not have yet. Safe to re-run."""
        from catalog import StoreNotConfigured, warm_images

        try:
            result = warm_images(limit=limit)
        except StoreNotConfigured as exc:
            click.echo(str(exc), err=True)
            raise SystemExit(1)
        click.echo(
            f"Pictures: {result['stored']} stored, "
            f"{result['withoutPicture']} products have none, "
            f"{result['failed']} could not be read."
        )
