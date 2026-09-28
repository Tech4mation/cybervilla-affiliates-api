"""Affiliates, links, and the earnings the store reports back.

This is where the affiliate half of the service actually happens. Three jobs:

  * make an affiliate and their links, and push them down to the store so its
    checkout can price and attribute a sale;
  * take the store's word for what an affiliate earned on an order, once, even
    when the same word is said twice;
  * keep the markup honest — the ceiling is applied here as well as in Odoo, so
    a bad number is stopped before it is ever pushed.

Pushing to the store can fail, because the store is a separate system reached
over a slow link. When it does, the local record still stands and is left
marked unsynced; nothing the affiliate did is lost, and `resync_pending` picks
it up later. A link that never reached the store simply will not price on the
store yet, which is visible as ``synced: false``.
"""

import logging
import random
import re
import string

from sqlalchemy import func

from catalog import StoreNotConfigured, build_client
from config import Config
from models import Affiliate, AffiliateLink, Earning, db, utcnow

log = logging.getLogger(__name__)


def _clamp_markup(value) -> float:
    """A markup, never below zero and never over the ceiling."""
    try:
        markup = float(value or 0)
    except (TypeError, ValueError):
        markup = 0.0
    return max(0.0, min(markup, float(Config.MAX_MARKUP_PERCENT)))


def _handle(name: str) -> str:
    """The letters-only first word of a name, upper-cased, for a readable code."""
    first = (name or "").strip().split(" ")[0]
    letters = re.sub(r"[^A-Za-z0-9]", "", first).upper()
    return letters or "AFF"


def _unique_code(name: str) -> str:
    """A short code that is not already taken. Readable, not secret — the code
    identifies an affiliate, it does not authorise anything."""
    handle = _handle(name)[:12]
    for _ in range(50):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        code = f"{handle}-{suffix}"
        if not AffiliateLink.query.filter_by(code=code).first():
            return code
    # Fifty collisions is not bad luck, it is a bug elsewhere; fail loudly.
    raise RuntimeError("Could not allocate a unique affiliate code.")


# --------------------------------------------------------------------------- #
# Affiliates
# --------------------------------------------------------------------------- #

def create_affiliate(name: str, email: str = "", phone: str = "") -> Affiliate:
    """Create an affiliate (pending) and push a copy to the store."""
    if not (name or "").strip():
        raise ValueError("An affiliate needs a name.")
    affiliate = Affiliate(name=name.strip(), email=(email or "").strip() or None,
                          phone=(phone or "").strip() or None, status="pending")
    db.session.add(affiliate)
    db.session.flush()  # assigns id
    affiliate.backend_ref = f"AFF-{affiliate.id}"
    _push_affiliate(affiliate)
    db.session.commit()
    return affiliate


def _push_affiliate(affiliate: Affiliate) -> bool:
    """Send the store our copy of this affiliate. Returns whether it landed."""
    try:
        client = build_client()
        affiliate.odoo_affiliate_id = client.upsert_affiliate({
            "backend_ref": affiliate.backend_ref,
            "name": affiliate.name,
            "email": affiliate.email or "",
        })
        affiliate.synced_at = utcnow()
        return True
    except StoreNotConfigured:
        log.warning("Affiliate %s not pushed: store not configured", affiliate.backend_ref)
    except Exception:
        log.warning("Affiliate %s push failed", affiliate.backend_ref, exc_info=True)
    return False


# --------------------------------------------------------------------------- #
# Links
# --------------------------------------------------------------------------- #

def create_link(affiliate: Affiliate, markup_percent, label: str = "") -> AffiliateLink:
    """Create a storewide link, price it at the (clamped) markup, and push it to the store."""
    link = AffiliateLink(
        affiliate=affiliate,
        code=_unique_code(affiliate.name),
        label=(label or "").strip() or None,
        target_type="storewide",
        markup_percent=_clamp_markup(markup_percent),
        active=True,
    )
    db.session.add(link)
    db.session.flush()
    link.backend_ref = f"LNK-{link.id}"
    _push_link(link)
    db.session.commit()
    return link


def _push_link(link: AffiliateLink) -> bool:
    """Send the store this link and its markup. Returns whether it landed.

    The affiliate must be on the store first, so a not-yet-synced affiliate is
    pushed here too. The store owns the pricelist; we keep the ids it hands back.
    """
    try:
        client = build_client()
        if not link.affiliate.odoo_affiliate_id:
            link.affiliate.odoo_affiliate_id = client.upsert_affiliate({
                "backend_ref": link.affiliate.backend_ref,
                "name": link.affiliate.name,
                "email": link.affiliate.email or "",
            })
            link.affiliate.synced_at = utcnow()

        vals = {
            "backend_ref": link.backend_ref,
            "code": link.code,
            "label": link.label or "",
            "affiliate_id": link.affiliate.odoo_affiliate_id,
            "markup_percent": float(link.markup_percent or 0),
        }

        result = client.upsert_affiliate_link(vals)
        link.odoo_link_id = result.get("link_id")
        link.odoo_pricelist_id = result.get("pricelist_id")
        # The store may have clamped the markup lower than we asked; believe it.
        if result.get("markup_percent") is not None:
            link.markup_percent = result["markup_percent"]
        link.synced_at = utcnow()
        return True
    except StoreNotConfigured:
        log.warning("Link %s not pushed: store not configured", link.backend_ref)
    except Exception:
        log.warning("Link %s push failed", link.backend_ref, exc_info=True)
    return False


def earnings_by_link(affiliate_id: int) -> dict:
    """Paid orders and money earned, per link, for one affiliate.

    A reversed earning is a sale that was cancelled or refunded, so it counts
    for neither the order tally nor the money. Links with nothing yet are
    absent from the result; the caller supplies the zero.
    """
    rows = (
        db.session.query(
            Earning.link_id,
            func.count(Earning.id),
            func.coalesce(func.sum(Earning.earning), 0),
            # What the store priced these in, so the dashboard never has to
            # guess a currency symbol.
            func.max(Earning.currency),
        )
        .filter(
            Earning.affiliate_id == affiliate_id,
            Earning.link_id.isnot(None),
            Earning.status != "reversed",
        )
        .group_by(Earning.link_id)
        .all()
    )
    return {
        link_id: {
            "sales": int(count or 0),
            "earnings": float(total or 0),
            "currency": currency,
        }
        for link_id, count, total, currency in rows
    }


def resync_pending() -> dict:
    """Retry pushing everything that has not reached the store yet.

    For a scheduler or an admin button after the store has been down. Each row
    is committed on its own, so one that still fails does not hold up the rest.
    """
    pushed_affiliates = pushed_links = failed = 0
    for affiliate in Affiliate.query.filter(Affiliate.odoo_affiliate_id.is_(None)).all():
        if _push_affiliate(affiliate):
            pushed_affiliates += 1
            db.session.commit()
        else:
            db.session.rollback()
            failed += 1
    for link in AffiliateLink.query.filter(AffiliateLink.odoo_link_id.is_(None)).all():
        if _push_link(link):
            pushed_links += 1
            db.session.commit()
        else:
            db.session.rollback()
            failed += 1
    return {"affiliates": pushed_affiliates, "links": pushed_links, "failed": failed}


# --------------------------------------------------------------------------- #
# Earnings reported back by the store
# --------------------------------------------------------------------------- #

def record_order_event(payload: dict) -> Earning:
    """Record one paid/cancelled notification from the store, idempotently.

    Keyed on the order reference, so the store retrying a delivery updates the
    one row rather than making a second. Resolves the affiliate and link to our
    own records where it can, and keeps the raw references either way so an
    earning is never dropped because a link was made in the store directly.
    """
    order_ref = (payload or {}).get("order_ref")
    if not order_ref:
        raise ValueError("A notification must carry an order_ref.")
    event = payload.get("event") or "order.paid"

    earning = Earning.query.filter_by(odoo_order_ref=order_ref).first()
    creating = earning is None
    if creating:
        earning = Earning(odoo_order_ref=order_ref, status="pending")
        db.session.add(earning)

    # Attribution and amounts, refreshed from the payload every time.
    link = None
    if payload.get("link_backend_ref"):
        link = AffiliateLink.query.filter_by(backend_ref=payload["link_backend_ref"]).first()
    if not link and payload.get("affiliate_code"):
        link = AffiliateLink.query.filter_by(code=payload["affiliate_code"]).first()
    affiliate = link.affiliate if link else (
        Affiliate.query.filter_by(backend_ref=payload.get("affiliate_backend_ref")).first()
    )

    earning.odoo_order_id = payload.get("order_id")
    earning.link_id = link.id if link else None
    earning.affiliate_id = affiliate.id if affiliate else None
    earning.affiliate_backend_ref = payload.get("affiliate_backend_ref")
    earning.link_backend_ref = payload.get("link_backend_ref")
    earning.affiliate_code = payload.get("affiliate_code")
    earning.currency = payload.get("currency")
    earning.markup_percent = payload.get("markup_percent")
    earning.amount_total = payload.get("amount_total")
    earning.amount_untaxed = payload.get("amount_untaxed")
    earning.earning = payload.get("affiliate_earning")
    earning.customer_email = payload.get("customer_email")
    earning.occurred_at = _parse_dt(payload.get("confirmed_at"))
    earning.last_event = event

    # A cancellation reverses whatever was earned; a payment leaves a new row
    # pending for the approval rules to move on later, and never un-reverses one.
    if event == "order.cancelled":
        earning.status = "reversed"
    elif creating:
        earning.status = "pending"

    db.session.commit()
    return earning


def _parse_dt(value):
    if not value:
        return None
    from datetime import datetime, timezone
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None
