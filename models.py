"""What this service stores.

Three tables, and all three exist for one reason: the CyberVilla store is a
separate system reached over XML-RPC, which is slow, single-threaded per
connection, and occasionally down. Nothing a person waits for is allowed to
depend on it being up. So the catalogue is copied here, and the dashboard
reads the copy.

For the catalogue, nothing here is the source of truth — Odoo is, and these
rows are a photograph of it with the time it was taken kept alongside.

The affiliate tables below are the opposite: this service IS their source of
truth. An affiliate, their links and their markups are decided here and pushed
down to the store; the store's copy exists only so its checkout can price and
attribute a sale. Earnings are the one exception — those are reported back up
from the store, because only the store knows when the money actually landed.
"""

from datetime import datetime, timedelta, timezone

from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Product(db.Model):
    """One saleable product, as the store last described it."""

    __tablename__ = "product"

    id = db.Column(db.Integer, primary_key=True)
    # The store's own id. Every other system we talk to names a product by
    # this, so it is what links and orders will point at — never our id.
    odoo_id = db.Column(db.Integer, unique=True, nullable=False, index=True)

    name = db.Column(db.String(512), nullable=False)
    description = db.Column(db.Text)
    list_price = db.Column(db.Numeric(14, 2), nullable=False, default=0)
    # Read from the store per product, never assumed. A figure without its
    # currency is how a customer gets quoted 500 of the wrong money.
    currency = db.Column(db.String(8))

    category_id = db.Column(db.Integer, index=True)
    category_name = db.Column(db.String(255))

    # False once a product stops coming back from the store — discontinued,
    # archived, or no longer saleable. The row stays, because affiliate links
    # and past orders will point at it long after the store forgets it.
    available = db.Column(db.Boolean, nullable=False, default=True, index=True)

    first_seen_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    synced_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def as_dict(self) -> dict:
        return {
            "id": self.odoo_id,
            "name": self.name,
            "description": self.description or "",
            "price": float(self.list_price or 0),
            "currency": self.currency,
            "category": self.category_name,
            "categoryId": self.category_id,
            "available": bool(self.available),
            "imageUrl": f"/products/{self.odoo_id}/image",
        }


class ProductImage(db.Model):
    """One product photograph, kept so we fetch it from the store once.

    Odoo does serve pictures at a public address, but that address answers 200
    with a grey placeholder for any id at all, including ids that do not
    exist — so linking straight to it would put blank boxes on the dashboard
    with nothing in the logs to say so. We read the picture over the
    authenticated connection and serve it ourselves.

    Fetched when first asked for rather than during a sync: a picture is around
    a thousand times the size of the row it belongs to, and most of a catalogue
    is never looked at.
    """

    __tablename__ = "product_image"

    id = db.Column(db.Integer, primary_key=True)
    odoo_id = db.Column(db.Integer, unique=True, nullable=False, index=True)
    # Empty with missing=True: the store confirmed this product has no picture.
    # Remembering that is what stops us asking again on every page view.
    data = db.Column(db.LargeBinary)
    content_type = db.Column(db.String(64))
    missing = db.Column(db.Boolean, nullable=False, default=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)


class CatalogueSync(db.Model):
    """One attempt at copying the catalogue across, kept as a log.

    The newest successful row is what tells a caller how current the catalogue
    is. The failed rows are what tell somebody why it stopped being current.
    """

    __tablename__ = "catalogue_sync"

    id = db.Column(db.Integer, primary_key=True)
    started_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    finished_at = db.Column(db.DateTime(timezone=True))
    ok = db.Column(db.Boolean, nullable=False, default=False, index=True)
    product_count = db.Column(db.Integer, nullable=False, default=0)
    # Kept short on purpose: an XML-RPC failure can carry a whole document.
    error = db.Column(db.String(1000))

    @staticmethod
    def last_success() -> "CatalogueSync | None":
        return (
            CatalogueSync.query.filter_by(ok=True)
            .order_by(CatalogueSync.id.desc())
            .first()
        )

    @staticmethod
    def last_attempt() -> "CatalogueSync | None":
        return CatalogueSync.query.order_by(CatalogueSync.id.desc()).first()

    def as_dict(self, ttl_minutes: int) -> dict:
        finished = self.finished_at
        stale = True
        if finished:
            # A row read back from SQLite comes without a timezone even though
            # it went in with one, and comparing those two raises. Treat a
            # naive timestamp as the UTC it was written as.
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            stale = utcnow() - finished > timedelta(minutes=ttl_minutes)
        return {
            "syncedAt": finished.isoformat() if finished else None,
            "productCount": self.product_count,
            "stale": stale,
        }


# --------------------------------------------------------------------------- #
# The affiliate programme                                                      #
# --------------------------------------------------------------------------- #
# Decided here, pushed to the store. See the note at the top of this file for
# why these three are the source of truth and the catalogue is not.


class Affiliate(db.Model):
    """A person who sells CyberVilla stock for a markup.

    Bank details sit here because paying an affiliate out is the whole point;
    they are collected once, at approval, and are what a Paystack transfer
    recipient is built from later. Nothing is paid to an affiliate who is not
    approved.
    """

    __tablename__ = "affiliate"

    id = db.Column(db.Integer, primary_key=True)
    # Our own stable id for this affiliate, as told to the store. "AFF-<id>".
    backend_ref = db.Column(db.String(64), unique=True, index=True)

    name = db.Column(db.String(255), nullable=False)
    email = db.Column(db.String(255), index=True)
    phone = db.Column(db.String(64))

    # pending -> approved -> suspended. An affiliate is created pending; only an
    # approval (a person's decision) lets them earn and be paid.
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)

    # Where a payout goes. Verified against Paystack's account-name lookup at
    # approval time, before any money is owed. recipient_code is filled in when
    # the Paystack transfer recipient is created (a later piece of work).
    bank_code = db.Column(db.String(16))
    bank_account_number = db.Column(db.String(32))
    bank_account_name = db.Column(db.String(255))
    paystack_recipient_code = db.Column(db.String(64))

    # The store's id for our copy of this affiliate, returned by the push.
    odoo_affiliate_id = db.Column(db.Integer, index=True)
    synced_at = db.Column(db.DateTime(timezone=True))

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    links = db.relationship("AffiliateLink", back_populates="affiliate",
                            cascade="all, delete-orphan")

    def as_dict(self) -> dict:
        return {
            "id": self.backend_ref,
            "name": self.name,
            "email": self.email or "",
            "phone": self.phone or "",
            "status": self.status,
            "bankAccountName": self.bank_account_name or "",
            "synced": self.odoo_affiliate_id is not None,
            "joinedAt": self.created_at.isoformat() if self.created_at else None,
        }


class User(db.Model):
    """An authenticated user: either an admin or an affiliate.

    Affiliates sign up through the registration/sign-up page with status='pending'.
    Sign-ups do not become members of the affiliate program until approved by the admin.
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    name = db.Column(db.String(255), nullable=False)
    phone = db.Column(db.String(64))

    # 'admin' or 'affiliate'
    role = db.Column(db.String(32), nullable=False, default="affiliate", index=True)

    # 'pending', 'approved', 'rejected', 'suspended' (admin accounts default to 'active')
    status = db.Column(db.String(32), nullable=False, default="pending", index=True)
    rejection_reason = db.Column(db.String(500))

    # Prospective affiliate application details
    promotional_channel = db.Column(db.String(128))
    channel_url = db.Column(db.String(512))
    audience_size = db.Column(db.String(64))
    why_join = db.Column(db.Text)

    # Optional 1-to-1 link to Affiliate profile
    affiliate_id = db.Column(db.Integer, db.ForeignKey("affiliate.id"), nullable=True, unique=True, index=True)
    affiliate = db.relationship("Affiliate", backref=db.backref("user", uselist=False))

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def set_password(self, password: str) -> None:
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    @property
    def is_admin(self) -> bool:
        return self.role == "admin"

    @property
    def is_member(self) -> bool:
        """True only if approved member of the affiliate program or admin."""
        if self.is_admin:
            return True
        return self.status == "approved"

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "email": self.email,
            "phone": self.phone or "",
            "role": self.role,
            "status": self.status,
            "isMember": self.is_member,
            "promotionalChannel": self.promotional_channel or "",
            "channelUrl": self.channel_url or "",
            "audienceSize": self.audience_size or "",
            "whyJoin": self.why_join or "",
            "rejectionReason": self.rejection_reason,
            "affiliateId": self.affiliate.backend_ref if self.affiliate else None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class AffiliateLink(db.Model):
    """A shareable code that prices the shop at one affiliate's markup.

    Every link is storewide — the markup applies to whatever the buyer ends up
    with, not one product. (``target_type``/``product_odoo_id`` remain on the
    table from an earlier per-product design; nothing sets them anymore.) The
    store builds the actual pricelist from the markup; here we only remember
    what was asked for and what the store said it applied.
    """

    __tablename__ = "affiliate_link"

    id = db.Column(db.Integer, primary_key=True)
    backend_ref = db.Column(db.String(64), unique=True, index=True)

    affiliate_id = db.Column(db.Integer, db.ForeignKey("affiliate.id"),
                             nullable=False, index=True)
    affiliate = db.relationship("Affiliate", back_populates="links")

    # What goes in the address: cybervilla.io/r/<code>. Unique across the store.
    code = db.Column(db.String(64), unique=True, nullable=False, index=True)
    label = db.Column(db.String(255))

    # "storewide" or "product". A product link points its landing page at one
    # product; the markup still applies to whatever the buyer ends up with.
    target_type = db.Column(db.String(16), nullable=False, default="storewide")
    product_odoo_id = db.Column(db.Integer, index=True)

    markup_percent = db.Column(db.Numeric(5, 2), nullable=False, default=0)

    # What the store gave back: the link's id and the pricelist it built.
    odoo_link_id = db.Column(db.Integer, index=True)
    odoo_pricelist_id = db.Column(db.Integer)
    synced_at = db.Column(db.DateTime(timezone=True))

    active = db.Column(db.Boolean, nullable=False, default=True, index=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def url(self) -> str:
        from config import Config
        store_url = Config.ODOO_URL.rstrip("/") if Config.ODOO_URL else "https://cybervilla.io"
        return f"{store_url}/r/{self.code}"

    def as_dict(self) -> dict:
        return {
            "id": self.backend_ref,
            "code": self.code,
            "label": self.label or "",
            "url": self.url(),
            "targetType": "Product" if self.target_type == "product" else "Storewide",
            "productId": self.product_odoo_id,
            "markupPercent": float(self.markup_percent or 0),
            "active": bool(self.active),
            "synced": self.odoo_link_id is not None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class Earning(db.Model):
    """What an affiliate has earned on one order, as the store reported it.

    Reported up from Odoo, not decided here, because only the store knows when
    a payment actually landed. Keyed on the store's order reference so a
    notification that is retried updates this row rather than duplicating it —
    the store may deliver the same paid event more than once, on purpose,
    rather than risk losing it.

    The markup and the earned amount are frozen here as they were reported, so
    later re-pricing a link never rewrites a past earning.
    """

    __tablename__ = "earning"

    id = db.Column(db.Integer, primary_key=True)

    odoo_order_ref = db.Column(db.String(64), unique=True, nullable=False, index=True)
    odoo_order_id = db.Column(db.Integer)

    # Resolved to our own records where we can; the raw refs are kept too, so an
    # earning is never lost just because a link was made in the store directly.
    affiliate_id = db.Column(db.Integer, db.ForeignKey("affiliate.id"), index=True)
    link_id = db.Column(db.Integer, db.ForeignKey("affiliate_link.id"), index=True)
    affiliate_backend_ref = db.Column(db.String(64), index=True)
    link_backend_ref = db.Column(db.String(64))
    affiliate_code = db.Column(db.String(64), index=True)

    currency = db.Column(db.String(8))
    markup_percent = db.Column(db.Numeric(5, 2))
    amount_total = db.Column(db.Numeric(14, 2))
    amount_untaxed = db.Column(db.Numeric(14, 2))
    earning = db.Column(db.Numeric(14, 2))
    customer_email = db.Column(db.String(255))

    # pending -> approved -> payable -> paid, or reversed if the order is
    # cancelled or refunded. New earnings start pending; moving them on is a
    # later piece (the approval rules and the payout run).
    status = db.Column(db.String(16), nullable=False, default="pending", index=True)
    last_event = db.Column(db.String(32))

    occurred_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    def as_dict(self) -> dict:
        return {
            "orderRef": self.odoo_order_ref,
            "affiliateCode": self.affiliate_code,
            "currency": self.currency,
            "markupPercent": float(self.markup_percent or 0),
            "amountTotal": float(self.amount_total or 0),
            "earning": float(self.earning or 0),
            "status": self.status,
            "occurredAt": self.occurred_at.isoformat() if self.occurred_at else None,
        }
