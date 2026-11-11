"""The referral bonus: affiliates bringing in other affiliates (TPD-1875).

An approved affiliate has an invite code. Someone who signs up with it is
their recruit, fixed at sign-up. Every time the store confirms a sale by that
recruit, the inviter earns a bonus of `REFERRAL_BONUS_PERCENT` of the order's
goods before tax, delivery left out. CyberVilla pays it on top: the recruit's
own markup and commission are untouched.

Rules that are easy to get wrong, all deliberate:

* **One level.** Only the recruit's direct inviter earns. Nothing climbs
  further up, so this can never turn into a chain.
* **Nothing for sign-ups.** A bonus only ever comes from a confirmed sale.
* **Real customers only.** No bonus when the buyer's email is the inviter's
  or the recruit's own — buying from yourself through a friend's link is not
  a sale to a customer.
* **The inviter must still be in the programme** (approved) when the sale
  happens.
* **Frozen once earned.** The amount and rate are fixed when the sale is
  first recorded. The store re-sending the same order never pays twice, and a
  cancelled order takes the bonus back exactly as it takes back the
  recruit's own earning.

A bonus is an ordinary `Earning` row with `kind="referral"`, keyed
`REF-<order ref>`, so the hold period, the payable balance, payouts and their
audit trail all work on it unchanged.
"""

import logging
import random
import string

from sqlalchemy import func
from sqlalchemy.orm import aliased

from config import Config
from models import Affiliate, Earning, db

log = logging.getLogger(__name__)

PREFIX = "REF-"


def first_name(name: str | None) -> str:
    return ((name or "").strip().split(" ") or [""])[0] or "Someone"


# --------------------------------------------------------------------------- #
# Codes
# --------------------------------------------------------------------------- #

def _new_code(name: str) -> str:
    """INV-<NAME>-<4>: readable, and plainly not a shopping link code, so an
    affiliate does not hand an invite code to a customer by mistake."""
    from affiliate_service import _handle  # same readable handle as link codes
    handle = _handle(name)[:10]
    for _ in range(50):
        suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
        code = f"INV-{handle}-{suffix}"
        if not Affiliate.query.filter_by(referral_code=code).first():
            return code
    raise RuntimeError("Could not allocate a unique invite code.")


def ensure_code(affiliate: Affiliate) -> str | None:
    """The affiliate's invite code, given out the first time it is needed.

    Only approved affiliates get one. Never changes once given. The caller
    commits.
    """
    if not affiliate or affiliate.status != "approved":
        return None
    if not affiliate.referral_code:
        affiliate.referral_code = _new_code(affiliate.name)
    return affiliate.referral_code


def inviter_for_code(code: str | None) -> Affiliate | None:
    """The approved affiliate a code belongs to, or None."""
    cleaned = (code or "").strip().upper()
    if not cleaned:
        return None
    return Affiliate.query.filter_by(referral_code=cleaned, status="approved").first()


# --------------------------------------------------------------------------- #
# Bonuses
# --------------------------------------------------------------------------- #

def order_base(earning: Earning) -> float:
    """The order's goods before tax, delivery left out.

    Uses the lines the store reported. An order recorded without lines falls
    back to the untaxed total, which is the best figure there is for it.
    """
    goods = [line for line in earning.lines if not line.is_delivery]
    if goods:
        return round(sum(float(line.subtotal or 0) for line in goods), 2)
    if earning.lines:  # only a delivery line: nothing was bought to earn on
        return 0.0
    return float(earning.amount_untaxed or 0)


def bonus_row_for(earning: Earning) -> Earning | None:
    return Earning.query.filter_by(odoo_order_ref=PREFIX + earning.odoo_order_ref).first()


def record_bonus(earning: Earning) -> tuple[Earning | None, bool]:
    """Add the inviter's bonus for this sale, if one is due.

    Returns (bonus row or None, whether it was created now). Never raises: a
    bonus is extra money for someone else and must never cost the recruit the
    recording of their own sale. The caller commits.
    """
    try:
        if earning.kind != "sale" or not earning.affiliate_id:
            return None, False
        existing = bonus_row_for(earning)
        if existing:
            # Already worked out once. Frozen, and a later "paid" for an order
            # that was cancelled in between does not bring it back.
            return existing, False
        if earning.status == "reversed":
            return None, False

        recruit = db.session.get(Affiliate, earning.affiliate_id)
        inviter = recruit.referred_by if recruit else None
        if not inviter or inviter.id == recruit.id:
            return None, False
        if inviter.status != "approved":
            log.info("No referral bonus on %s: inviter %s is %s",
                     earning.odoo_order_ref, inviter.backend_ref, inviter.status)
            return None, False
        buyer = (earning.customer_email or "").strip().lower()
        own = {(inviter.email or "").strip().lower(), (recruit.email or "").strip().lower()} - {""}
        if buyer and buyer in own:
            log.info("No referral bonus on %s: bought by the inviter or the recruit",
                     earning.odoo_order_ref)
            return None, False

        base = order_base(earning)
        rate = float(Config.REFERRAL_BONUS_PERCENT or 0)
        amount = round(base * rate / 100.0, 2)
        if amount <= 0:
            return None, False

        db.session.flush()  # the sale needs its id before the bonus can point at it
        row = Earning(
            odoo_order_ref=PREFIX + earning.odoo_order_ref,
            odoo_order_id=earning.odoo_order_id,
            kind="referral",
            source_earning_id=earning.id,
            affiliate_id=inviter.id,
            affiliate_backend_ref=inviter.backend_ref,
            currency=earning.currency,
            amount_total=base,
            amount_untaxed=base,
            earning=0,
            commission=0,
            bonus=amount,
            bonus_percent=rate,
            status="completed",
            last_event="order.paid",
            # The sale's own date, so the bonus waits out the same hold period.
            occurred_at=earning.occurred_at,
        )
        db.session.add(row)
        return row, True
    except Exception:  # noqa: BLE001 — never lose the recruit's sale over the bonus
        log.warning("Could not work out the referral bonus for %s",
                    getattr(earning, "odoo_order_ref", "?"), exc_info=True)
        return None, False


def reverse_bonus(earning: Earning) -> Earning | None:
    """Take the bonus back when the sale it came from is cancelled.

    Returns the row if it was reversed just now, None if there was nothing to
    reverse. The caller commits.
    """
    row = bonus_row_for(earning)
    if not row or row.status == "reversed":
        return None
    row.status = "reversed"
    row.last_event = "order.cancelled"
    return row


# --------------------------------------------------------------------------- #
# What an inviter (or an admin) sees
# --------------------------------------------------------------------------- #

def recruits_of(inviter: Affiliate) -> list[dict]:
    """Everyone who signed up with this affiliate's code.

    Per recruit: who, when, their account status, how many sales they have
    made, and what they have earned the inviter. Deliberately NOT what the
    recruit earned themselves — that is between them and CyberVilla.
    """
    recruits = (Affiliate.query.filter_by(referred_by_id=inviter.id)
                .order_by(Affiliate.created_at.desc()).all())
    if not recruits:
        return []
    ids = [r.id for r in recruits]

    sales = dict(
        db.session.query(Earning.affiliate_id, func.count(Earning.id))
        .filter(Earning.affiliate_id.in_(ids), Earning.kind == "sale",
                Earning.status != "reversed")
        .group_by(Earning.affiliate_id).all()
    )
    source = aliased(Earning)
    bonus = dict(
        db.session.query(source.affiliate_id, func.coalesce(func.sum(Earning.bonus), 0))
        .join(source, source.id == Earning.source_earning_id)
        .filter(Earning.affiliate_id == inviter.id, Earning.kind == "referral",
                Earning.status != "reversed", source.affiliate_id.in_(ids))
        .group_by(source.affiliate_id).all()
    )

    rows = []
    for r in recruits:
        user = getattr(r, "user", None)
        rows.append({
            "affiliateId": r.backend_ref,
            "userId": user.id if user else None,
            "name": r.name,
            "status": (user.status if user else r.status) or "pending",
            "joinedAt": r.created_at.isoformat() if r.created_at else None,
            "sales": int(sales.get(r.id, 0) or 0),
            "bonusEarned": float(bonus.get(r.id, 0) or 0),
        })
    return rows


def summary(inviter: Affiliate) -> dict:
    recruits = recruits_of(inviter)
    return {
        "code": inviter.referral_code,
        "ratePercent": float(Config.REFERRAL_BONUS_PERCENT or 0),
        "recruits": recruits,
        "totals": {
            "recruits": len(recruits),
            "active": sum(1 for r in recruits if r["status"] == "approved"),
            "sales": sum(r["sales"] for r in recruits),
            "bonusEarned": round(sum(r["bonusEarned"] for r in recruits), 2),
        },
    }
