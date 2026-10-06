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
from sqlalchemy import func
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
    bank_name = db.Column(db.String(128))
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
    # Payouts are money that moved; they are never cascade-deleted with an
    # affiliate, because the record of a payment has to outlive the account.
    payouts = db.relationship("Payout", back_populates="affiliate")

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
    # No longer asked for at sign-up. Kept so applications made before that
    # change keep their answers; nothing writes these now.
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
            "whyJoin": self.why_join or "",
            "rejectionReason": self.rejection_reason,
            "affiliateId": self.affiliate.backend_ref if self.affiliate else None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class AffiliateLink(db.Model):
    """A shareable code that prices the shop at one affiliate's markup.

    A link may name a product, but that only decides where the visitor lands.
    The markup is never narrowed to it: whatever the buyer ends up with is
    priced at the link's markup, and the affiliate earns on it. The store
    builds the actual pricelist; here we only remember what was asked for and
    what the store said it applied.
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

    def product_name(self) -> str | None:
        """The name of the product this link lands on, if it names one.

        Looked up rather than stored, so a renamed product reads correctly.
        A product the store has since withdrawn is still in our mirror, but
        guard for it having gone entirely.
        """
        if not self.product_odoo_id:
            return None
        found = Product.query.filter_by(odoo_id=self.product_odoo_id).first()
        return found.name if found else None

    def as_dict(self) -> dict:
        return {
            "id": self.backend_ref,
            "code": self.code,
            "label": self.label or "",
            "url": self.url(),
            "targetType": "Product" if self.target_type == "product" else "Storewide",
            "productId": self.product_odoo_id,
            "productName": self.product_name(),
            "markupPercent": float(self.markup_percent or 0),
            "active": bool(self.active),
            "synced": self.odoo_link_id is not None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class Notification(db.Model):
    """Something worth telling one person about, shown in their bell.

    Addressed to a `User` rather than an affiliate, so the same table serves
    admins ("a payout is waiting") and affiliates ("you were paid") without
    two of everything.

    These are written as a side effect of things that have already happened,
    so writing one must never be allowed to fail the thing it describes —
    see `notification_service.notify`.
    """

    __tablename__ = "notification"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)

    # A stable machine name for the event, e.g. "payout.paid". Kept separate
    # from the wording so the text can be reworded without breaking anything
    # that counts or filters by type.
    kind = db.Column(db.String(48), nullable=False, index=True)
    title = db.Column(db.String(255), nullable=False)
    body = db.Column(db.String(1024))
    # Where clicking it should go, if anywhere.
    href = db.Column(db.String(255))

    read_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow, index=True)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "body": self.body or "",
            "href": self.href,
            "read": self.read_at is not None,
            "createdAt": self.created_at.isoformat() if self.created_at else None,
        }


class Payout(db.Model):
    """One payment of an affiliate's approved earnings.

    A payout is a record of money owed and then moved, and it names exactly
    which earnings it covers — `Earning.payout_id` points back here. That
    link is what makes the figures auditable: every naira paid can be traced
    to the orders it came from, and an earning can never be paid twice
    because attaching it to a payout is what marks it paid.

    The provider fields are left empty when someone pays by hand and filled
    in when a transfer is made through Paystack, so both ways of paying leave
    the same shape of record behind.
    """

    __tablename__ = "payout"

    id = db.Column(db.Integer, primary_key=True)
    # Ours, shown to people: "PO-12".
    reference = db.Column(db.String(64), unique=True, index=True)

    affiliate_id = db.Column(db.Integer, db.ForeignKey("affiliate.id"),
                             nullable=False, index=True)
    affiliate = db.relationship("Affiliate", back_populates="payouts")

    amount = db.Column(db.Numeric(14, 2), nullable=False)
    currency = db.Column(db.String(8))

    # "bank_transfer" once money moves through Paystack; "manual" when someone
    # pays from the bank themselves and records it here.
    method = db.Column(db.String(32), nullable=False, default="manual")

    # requested -> [awaiting_otp] -> processing -> paid, or failed / cancelled.
    # awaiting_otp only occurs while Paystack is configured to require a
    # confirmation code per transfer; with that off, sending goes straight to
    # processing. Nothing is ever deleted; a failed payout releases its
    # earnings again.
    status = db.Column(db.String(16), nullable=False, default="requested", index=True)
    failure_reason = db.Column(db.String(512))

    # Where the money went, frozen at the time of payment, so a later change
    # to the affiliate's bank details never rewrites what actually happened.
    bank_account_name = db.Column(db.String(255))
    bank_account_number = db.Column(db.String(32))
    bank_name = db.Column(db.String(128))
    bank_code = db.Column(db.String(16))

    # Paystack's own identifiers, when a transfer was used.
    provider_reference = db.Column(db.String(64), index=True)
    provider_status = db.Column(db.String(32))

    note = db.Column(db.String(512))

    requested_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    paid_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    earnings = db.relationship("Earning", back_populates="payout")

    def as_dict(self) -> dict:
        return {
            "id": self.reference,
            "amount": float(self.amount or 0),
            "currency": self.currency,
            "method": self.method,
            "status": self.status,
            "orderCount": len(self.earnings),
            "accountName": self.bank_account_name or "",
            "bankName": self.bank_name or "",
            # Only the last four digits — enough to recognise the account,
            # not enough to be worth leaking.
            "accountNumberLast4": (self.bank_account_number or "")[-4:],
            "failureReason": self.failure_reason,
            # Paystack is holding this one until somebody supplies the code
            # it sent to the account owner.
            "awaitingOtp": self.status == "awaiting_otp",
            "providerStatus": self.provider_status,
            "note": self.note,
            "requestedAt": self.requested_at.isoformat() if self.requested_at else None,
            "paidAt": self.paid_at.isoformat() if self.paid_at else None,
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

    # Campaign commission across this order's lines, summed here so the
    # money queries do not have to join the lines every time. The markup
    # stays in `earning`: an affiliate is owed the two added together, and
    # keeping them apart is what lets either be reported on its own.
    commission = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    # When a person decided this earning is real — i.e. past the point where
    # the order is likely to come back. Only approved earnings can be paid.
    approved_at = db.Column(db.DateTime(timezone=True))
    # The payout that settled this earning, once one has. This is the audit
    # trail: every paid earning can name the payment it went out in.
    payout_id = db.Column(db.Integer, db.ForeignKey("payout.id"), index=True)
    payout = db.relationship("Payout", back_populates="earnings")
    # Joined rather than lazy: the transactions table reads this for every
    # row, and a lazy load there is one query per transaction.
    link = db.relationship("AffiliateLink", lazy="joined")
    # The goods on the order. Empty for anything recorded before the store
    # began reporting them, which is why nothing may assume they exist.
    lines = db.relationship("EarningLine", back_populates="earning",
                            cascade="all, delete-orphan", lazy="selectin")

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
            "commission": float(self.commission or 0),
            # What the affiliate is actually owed for this order.
            "totalDue": float(self.earning or 0) + float(self.commission or 0),
            "status": self.status,
            "occurredAt": self.occurred_at.isoformat() if self.occurred_at else None,
            "approvedAt": self.approved_at.isoformat() if self.approved_at else None,
            "payoutRef": self.payout.reference if self.payout else None,
            # Which link brought this sale in. Deliberately describes the
            # LINK, not the goods: a product link earns on whatever the
            # customer ends up buying, so naming the link's product as "the
            # product sold" would be wrong. The goods are not reported to us
            # at all yet — the store sends totals only.
            "sourceCode": self.affiliate_code,
            "sourceLabel": (self.link.label or None) if self.link else None,
            "sourceKind": (
                ("product" if self.link.product_odoo_id else "storewide")
                if self.link else None
            ),
            # Empty for orders recorded before the store started sending the
            # goods. A reader must treat [] as "not reported", not "nothing
            # was bought".
            "lines": [line.as_dict() for line in self.lines],
        }


class EarningLine(db.Model):
    """One product on an attributed order, as the store reported it.

    Separate from `Earning` because an order has many lines and we need to
    ask questions per product — "what did this campaign pay out on?" — which
    a blob of JSON on the earning could not answer without scanning
    everything.

    These rows are a copy of what the store said at the time, not a link to
    our catalogue: a product can be renamed, repriced or withdrawn later, and
    the history of what was actually bought must not change with it. The
    product ids are kept so the rows can still be matched to a campaign.
    """

    __tablename__ = "earning_line"

    id = db.Column(db.Integer, primary_key=True)
    earning_id = db.Column(db.Integer, db.ForeignKey("earning.id", ondelete="CASCADE"),
                           nullable=False, index=True)
    earning = db.relationship("Earning", back_populates="lines")

    # product.product — the variant, which is what our Product.odoo_id holds
    # and so what a campaign matches on.
    product_odoo_id = db.Column(db.Integer, index=True)
    # product.template — the grouping, for campaigns aimed at a product
    # rather than one of its variants.
    product_tmpl_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(512))

    quantity = db.Column(db.Numeric(14, 3))
    unit_price = db.Column(db.Numeric(14, 2))
    subtotal = db.Column(db.Numeric(14, 2))

    # What a campaign paid on this line, and which campaign paid it. Frozen
    # at the time of sale: editing or ending the campaign afterwards must not
    # change what was earned. Null campaign means no campaign covered it.
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="SET NULL"),
                            index=True)
    campaign = db.relationship("Campaign")
    commission = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)

    def as_dict(self) -> dict:
        return {
            "productId": self.product_odoo_id,
            "productTmplId": self.product_tmpl_id,
            "name": self.name or "",
            "quantity": float(self.quantity or 0),
            "unitPrice": float(self.unit_price or 0),
            "subtotal": float(self.subtotal or 0),
            "commission": float(self.commission or 0),
            "campaign": self.campaign.name if self.campaign else None,
        }


class Campaign(db.Model):
    """A standing offer: sell these products, earn this on top of your markup.

    A campaign rewards an affiliate for pushing particular goods. It does not
    replace the markup — the affiliate still keeps the difference between
    CyberVilla's price and theirs — it is paid alongside it, out of
    CyberVilla's margin.

    The terms are frozen onto each earning when a sale happens, not read back
    from here later, for the same reason the markup is frozen on the order:
    changing a campaign tomorrow must never rewrite what somebody earned
    today.
    """

    __tablename__ = "campaign"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False)
    description = db.Column(db.String(1000))

    # "percent" — of the line's subtotal; "fixed" — this much per unit sold.
    reward_type = db.Column(db.String(16), nullable=False, default="percent")
    reward_value = db.Column(db.Numeric(14, 2), nullable=False, default=0)

    # Null means open-ended at that end. An order qualifies on the date it
    # was placed, so ending a campaign never claws back what it already paid.
    starts_at = db.Column(db.DateTime(timezone=True))
    ends_at = db.Column(db.DateTime(timezone=True))

    # Switched off by hand, separately from the dates, so a campaign can be
    # stopped immediately without rewriting its schedule.
    active = db.Column(db.Boolean, nullable=False, default=True, index=True)

    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at = db.Column(db.DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    # When affiliates were told about this campaign. Stamped once and never
    # cleared, so editing a running campaign does not announce it again —
    # nobody wants the same offer in their bell three times.
    announced_at = db.Column(db.DateTime(timezone=True))

    products = db.relationship("CampaignProduct", back_populates="campaign",
                               cascade="all, delete-orphan", lazy="selectin")

    def is_live(self, when=None) -> bool:
        """Whether this campaign applies to something sold at `when`."""
        if not self.active:
            return False
        moment = when or utcnow()
        if self.starts_at and moment < self.starts_at:
            return False
        if self.ends_at and moment > self.ends_at:
            return False
        return True

    def reward_for(self, quantity, subtotal) -> float:
        """What this campaign pays on one order line."""
        value = float(self.reward_value or 0)
        if value <= 0:
            return 0.0
        if self.reward_type == "fixed":
            return round(value * float(quantity or 0), 2)
        return round(float(subtotal or 0) * value / 100.0, 2)

    def as_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description or "",
            "rewardType": self.reward_type,
            "rewardValue": float(self.reward_value or 0),
            "startsAt": self.starts_at.isoformat() if self.starts_at else None,
            "endsAt": self.ends_at.isoformat() if self.ends_at else None,
            "active": bool(self.active),
            "live": self.is_live(),
            "productCount": len(self.products),
            "products": [p.as_dict() for p in self.products],
        }


class CampaignProduct(db.Model):
    """One product a campaign rewards.

    Both ids are kept because the store counts them differently: an order
    line names a variant, while an admin picking "the Redmi 15C" means the
    template and all its variants. Matching on either lets a campaign be
    aimed at whichever the admin meant.
    """

    __tablename__ = "campaign_product"

    id = db.Column(db.Integer, primary_key=True)
    campaign_id = db.Column(db.Integer, db.ForeignKey("campaign.id", ondelete="CASCADE"),
                            nullable=False, index=True)
    campaign = db.relationship("Campaign", back_populates="products")

    product_odoo_id = db.Column(db.Integer, index=True)
    product_tmpl_id = db.Column(db.Integer, index=True)
    name = db.Column(db.String(512))

    def as_dict(self) -> dict:
        return {
            "productId": self.product_odoo_id,
            "productTmplId": self.product_tmpl_id,
            "name": self.name or "",
        }


def amount_due():
    """What an affiliate is owed on an earning, as a SQL expression.

    Markup plus campaign commission. Defined once because three separate
    places total this up — the payable balance, the per-link figures and the
    admin totals — and a sum that forgot the commission would quietly
    underpay somebody.
    """
    return func.coalesce(Earning.earning, 0) + func.coalesce(Earning.commission, 0)
