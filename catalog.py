"""Keeping our copy of the CyberVilla catalogue, and answering questions from it.

The rule this module exists to enforce: a person waiting on a screen never
waits on the store. Every read the dashboard makes is answered from our own
database. The store is only ever contacted by a refresh, which runs on a
schedule and is allowed to be slow, and by the first request for a particular
product photograph.

When a refresh fails, nothing disappears. The previous catalogue stays exactly
where it was and is served with the time it was taken, so the dashboard can say
"this is from an hour ago" instead of showing an empty page.
"""

import base64
import logging

from sqlalchemy import func, or_

from config import Config
from models import CatalogueSync, Product, ProductImage, db, utcnow
from odoo_client import OdooClient, currency_code, many2one

log = logging.getLogger(__name__)


class StoreNotConfigured(RuntimeError):
    """Raised when the Odoo credentials are absent, so nothing can be fetched."""


def build_client() -> OdooClient:
    if not Config.store_is_configured():
        raise StoreNotConfigured(
            "The store connection is not configured. Set ODOO_URL, ODOO_DATABASE, "
            "ODOO_USERNAME and ODOO_API_KEY."
        )
    return OdooClient(
        url=Config.ODOO_URL,
        database=Config.ODOO_DATABASE,
        username=Config.ODOO_USERNAME,
        api_key=Config.ODOO_API_KEY,
    )


# --------------------------------------------------------------------------- #
# Refreshing                                                                    #
# --------------------------------------------------------------------------- #

def refresh_catalogue(batch_size: int = 200, with_images: bool = True) -> dict:
    """Copy the catalogue across from the store.

    A failure to reach the store is returned as a summary rather than raised,
    because both callers — a scheduled command and an HTTP endpoint — want to
    report it rather than blow up on it, and the failure is on the
    CatalogueSync row either way.

    Credentials being absent is different, and does raise: nothing was
    attempted, so there is nothing to log, and it is our own configuration at
    fault rather than the store.
    """
    client = build_client()

    attempt = CatalogueSync(started_at=utcnow())
    db.session.add(attempt)
    db.session.commit()

    seen: set[int] = set()
    try:
        fallback_currency = client.get_company_currency()

        for batch in client.iter_products(batch_size=batch_size):
            seen.update(_store_batch(batch, fallback_currency))
            db.session.commit()

        # Anything the store no longer offers. The row is kept rather than
        # deleted: an affiliate link or a past order will point at it long
        # after the store has forgotten it, and a dangling id is worse than a
        # product marked unavailable.
        withdrawn = 0
        if seen:
            withdrawn = (
                Product.query
                .filter(Product.available.is_(True), Product.odoo_id.notin_(seen))
                .update({"available": False, "synced_at": utcnow()},
                        synchronize_session=False)
            )

        # Pictures, while we already hold an authenticated connection. Doing it
        # here rather than when a card is first shown is what keeps the store
        # off the path of anyone waiting: authenticating costs about two
        # seconds, and a page of two dozen cards would pay it two dozen times.
        pictures = warm_images(client) if with_images else {}

        attempt.ok = True
        attempt.product_count = len(seen)
        attempt.finished_at = utcnow()
        db.session.commit()

        log.info("Catalogue refreshed: %s products, %s withdrawn", len(seen), withdrawn)
        return {
            "ok": True,
            "productCount": len(seen),
            "withdrawn": withdrawn,
            **({"pictures": pictures} if with_images else {}),
        }

    except Exception as exc:
        db.session.rollback()
        # Re-read the row: the rollback above detached whatever was pending on it.
        attempt = db.session.get(CatalogueSync, attempt.id)
        if attempt:
            attempt.ok = False
            attempt.product_count = len(seen)
            attempt.error = str(exc)[:1000]
            attempt.finished_at = utcnow()
            db.session.commit()
        log.warning("Catalogue refresh failed after %s products: %s", len(seen), exc)
        return {"ok": False, "productCount": len(seen), "error": str(exc)}


def _store_batch(rows: list, fallback_currency: str | None) -> list[int]:
    """Write one batch of store rows into our own products table."""
    ids = [int(row["id"]) for row in rows if row.get("id") is not None]
    if not ids:
        return []

    existing = {
        product.odoo_id: product
        for product in Product.query.filter(Product.odoo_id.in_(ids)).all()
    }

    now = utcnow()
    for row in rows:
        odoo_id = int(row["id"])
        category_id, category_name = many2one(row.get("categ_id"))
        product = existing.get(odoo_id)
        if product is None:
            product = Product(odoo_id=odoo_id, first_seen_at=now)
            db.session.add(product)

        product.name = (row.get("name") or "").strip() or f"Product {odoo_id}"
        # description_sale is the customer-facing blurb; False when unset.
        product.description = (row.get("description_sale") or "") or None
        product.list_price = row.get("list_price") or 0
        product.currency = currency_code(row.get("currency_id")) or fallback_currency
        product.category_id = category_id
        product.category_name = category_name
        product.available = True
        product.synced_at = now

    return ids


# --------------------------------------------------------------------------- #
# Reading                                                                       #
# --------------------------------------------------------------------------- #

def catalogue_state() -> dict:
    """How current our copy is, and whether there is one at all."""
    success = CatalogueSync.last_success()
    attempt = CatalogueSync.last_attempt()

    state: dict = {
        "syncedAt": None,
        "productCount": 0,
        "stale": True,
        "everSynced": success is not None,
        "storeConfigured": Config.store_is_configured(),
    }
    if success:
        state.update(success.as_dict(Config.CATALOGUE_TTL_MINUTES))

    # Surface the newest failure only while it is the most recent thing that
    # happened. A failure older than the last success is history, not news.
    if attempt and not attempt.ok and (success is None or attempt.id > success.id):
        state["lastError"] = attempt.error

    return state


def query_products(search: str = "", category_id: int | None = None,
                   page: int = 1, per_page: int = 24,
                   include_unavailable: bool = False):
    """A page of products, newest-first by nothing in particular — name order.

    Name order because this list is browsed by a person looking for a product
    they have in mind, not scanned for what changed.
    """
    query = Product.query
    if not include_unavailable:
        query = query.filter(Product.available.is_(True))

    search = (search or "").strip()
    if search:
        pattern = f"%{search}%"
        query = query.filter(or_(
            Product.name.ilike(pattern),
            Product.description.ilike(pattern),
        ))
    if category_id:
        query = query.filter(Product.category_id == category_id)

    total = query.order_by(None).count()
    page = max(1, int(page))
    per_page = max(1, min(int(per_page), 100))
    rows = (
        query.order_by(Product.name.asc(), Product.odoo_id.asc())
        .limit(per_page)
        .offset((page - 1) * per_page)
        .all()
    )
    return rows, total


def get_product(odoo_id: int) -> Product | None:
    return Product.query.filter_by(odoo_id=odoo_id).first()


def list_categories() -> list[dict]:
    """The categories that actually have products in them.

    Read from our own table rather than from the store, so the filter on the
    products page keeps working when the store is unreachable, and so it never
    offers a category that would return nothing.
    """
    rows = (
        db.session.query(
            Product.category_id,
            Product.category_name,
            func.count(Product.id),
        )
        .filter(Product.available.is_(True), Product.category_id.isnot(None))
        .group_by(Product.category_id, Product.category_name)
        .order_by(Product.category_name.asc())
        .all()
    )
    return [
        {"id": category_id, "name": name, "productCount": count}
        for category_id, name, count in rows
    ]


# --------------------------------------------------------------------------- #
# Pictures                                                                      #
# --------------------------------------------------------------------------- #

# How many pictures to ask for at once. Odoo reads them in one go either way;
# the cap is about the size of the answer coming back over XML-RPC, which is
# one XML document holding every picture in the batch.
IMAGE_BATCH = 50


def warm_images(client: OdooClient | None = None, limit: int | None = None) -> dict:
    """Fetch the pictures we do not have yet, in batches.

    Twelve pictures in one call cost the same as one, so the batch is what makes
    this affordable at all: five thousand products is a hundred calls rather
    than five thousand.

    Only products we have never asked about are fetched. A product recorded as
    having no picture is not asked about again, and neither is one whose picture
    we already hold, so a second run costs almost nothing.
    """
    client = client or build_client()

    pending = (
        db.session.query(Product.odoo_id)
        .outerjoin(ProductImage, ProductImage.odoo_id == Product.odoo_id)
        .filter(ProductImage.id.is_(None), Product.available.is_(True))
        .order_by(Product.odoo_id)
    )
    if limit:
        pending = pending.limit(limit)
    ids = [row[0] for row in pending.all()]

    stored = 0
    without = 0
    failed = 0
    for start in range(0, len(ids), IMAGE_BATCH):
        batch = ids[start:start + IMAGE_BATCH]
        try:
            found = client.product_images(batch)
        except Exception as exc:
            # One bad batch should not cost us the rest of the catalogue's
            # pictures; the next run picks these up again.
            failed += len(batch)
            log.warning("Could not read pictures for %s products: %s", len(batch), exc)
            continue

        for odoo_id in batch:
            decoded = _decode_image(found.get(odoo_id), odoo_id)
            if decoded is None:
                _record_image(odoo_id, None, None)
                without += 1
            else:
                _record_image(odoo_id, decoded[0], decoded[1])
                stored += 1
        db.session.commit()

    if ids:
        log.info("Pictures: %s stored, %s have none, %s could not be read",
                 stored, without, failed)
    return {"stored": stored, "withoutPicture": without, "failed": failed}


def _decode_image(encoded: object, odoo_id: int) -> tuple[bytes, str] | None:
    """Turn what Odoo returned into bytes and a media type, or None."""
    if not encoded:
        return None
    try:
        raw = base64.b64decode(encoded)
    except Exception:
        log.warning("The picture for product %s could not be decoded", odoo_id)
        return None
    if not raw:
        return None
    # Odoo stores whatever was uploaded, so the bytes decide the type rather
    # than the field name. A JPEG served as a PNG is refused by some clients.
    return raw, ("image/png" if raw[:8] == b"\x89PNG\r\n\x1a\n" else "image/jpeg")


def _record_image(odoo_id: int, raw: bytes | None, content_type: str | None) -> None:
    """Write down a picture, or the fact that there is not one. No commit."""
    stored = ProductImage.query.filter_by(odoo_id=odoo_id).first()
    if stored is None:
        stored = ProductImage(odoo_id=odoo_id)
        db.session.add(stored)
    stored.data = raw
    stored.content_type = content_type
    stored.missing = raw is None
    stored.fetched_at = utcnow()


def product_picture(odoo_id: int) -> tuple[bytes, str] | None:
    """The photograph for one product.

    Almost always answered from our own table, because the sync fetches
    pictures in batches. The trip to the store here is the exception — a
    product added since the last sync — and it costs a couple of seconds,
    which is why it is not how the common case is served.

    Returns None both when the product has no picture and when the store could
    not be reached; the caller has no useful way to act differently on those,
    and the difference is in the log.
    """
    stored = ProductImage.query.filter_by(odoo_id=odoo_id).first()
    if stored and stored.missing:
        return None
    if stored and stored.data:
        return stored.data, stored.content_type or "image/jpeg"

    try:
        client = build_client()
        found = client.product_images([odoo_id])
    except StoreNotConfigured:
        return None
    except Exception as exc:
        log.warning("Could not read the picture for product %s: %s", odoo_id, exc)
        return None

    decoded = _decode_image(found.get(odoo_id), odoo_id)
    if decoded is None:
        # Nothing there. Written down so this is asked once, not once per view.
        _record_image(odoo_id, None, None)
        db.session.commit()
        return None

    _record_image(odoo_id, decoded[0], decoded[1])
    db.session.commit()
    return decoded
