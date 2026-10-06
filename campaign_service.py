"""Campaign commission: extra money for selling particular products.

An affiliate always earns their markup — the difference between CyberVilla's
price and the price they set. A campaign pays them *more* on top of that for
shifting specific goods, out of CyberVilla's own margin.

Two rules shape everything here.

**Only what the campaign names earns commission.** Commission is calculated
per order line, and a line earns only if its product is in a live campaign.
That is not merely a filter: it is what stops us paying commission on
delivery charges, which the store reports as an ordinary product line
("Lagos State Delivery") on every single order.

**The terms are frozen at the moment of sale.** The amount is written onto
the line, not recomputed from the campaign later, exactly as the markup is
frozen onto the order. Ending or editing a campaign tomorrow must never
change what somebody earned today.
"""

import logging

from models import Affiliate, Campaign, CampaignProduct, Product, db, utcnow
from notification_service import notify_affiliate

log = logging.getLogger(__name__)


def live_campaigns(when=None) -> list[Campaign]:
    """Every campaign that applies to something sold at `when`."""
    rows = Campaign.query.filter_by(active=True).all()
    return [c for c in rows if c.is_live(when)]


def _covers(campaign: Campaign, product_id, tmpl_id) -> bool:
    """Whether this campaign names this product.

    Either identifier matching is enough. An admin choosing a product means
    the template and all its variants; an order line names one variant.
    """
    for entry in campaign.products:
        if product_id is not None and entry.product_odoo_id == product_id:
            return True
        if tmpl_id is not None and entry.product_tmpl_id == tmpl_id:
            return True
    return False


def best_reward(product_id, tmpl_id, quantity, subtotal, when=None,
                campaigns: list[Campaign] | None = None):
    """The best commission available on one line, and which campaign pays it.

    Where two campaigns both cover a product the affiliate gets the better
    one, never both. Paying twice for one sale would make overlapping
    campaigns a way to multiply the cost of a sale by accident, and an
    affiliate cannot be expected to reason about which of our offers stack.

    Returns (amount, campaign) with campaign None when nothing applies.
    """
    pool = live_campaigns(when) if campaigns is None else campaigns
    best_amount, best_campaign = 0.0, None
    for campaign in pool:
        if not _covers(campaign, product_id, tmpl_id):
            continue
        amount = campaign.reward_for(quantity, subtotal)
        if amount > best_amount:
            best_amount, best_campaign = amount, campaign
    return best_amount, best_campaign


def apply_to_earning(earning) -> float:
    """Work out and store the commission on every line of this earning.

    Called whenever the store reports an order, so a re-delivery recalculates
    from the campaigns live *at the time of the order* rather than today.
    Returns the new total.

    Never raises: commission is money owed on top of the markup, and failing
    to work it out must not cost the affiliate the markup itself or make us
    reject the store's notification.
    """
    total = 0.0
    try:
        when = earning.occurred_at
        pool = live_campaigns(when)
        for line in earning.lines:
            amount, campaign = best_reward(
                line.product_odoo_id, line.product_tmpl_id,
                line.quantity, line.subtotal, when, pool,
            )
            line.commission = amount
            line.campaign_id = campaign.id if campaign else None
            total += amount
    except Exception:  # noqa: BLE001 — never lose the markup over the bonus
        log.warning("Could not calculate campaign commission for %s",
                    getattr(earning, "odoo_order_ref", "?"), exc_info=True)
        return float(earning.commission or 0)

    earning.commission = round(total, 2)
    return earning.commission


def recalculate_pending(campaign: Campaign) -> int:
    """Not implemented, deliberately.

    Changing a campaign does not reach back into earnings it has already
    paid. If that is ever wanted it must be an explicit, audited action with
    its own screen — not a silent side effect of an edit.
    """
    raise NotImplementedError(
        "Campaign changes never rewrite earnings that have already been recorded."
    )


def rewards_by_product() -> dict:
    """The best reward on offer for each product currently in a campaign.

    Worked out for a single unit at the product's own price, because that is
    what a product card shows: "5% commission (₦92,500)".

    The price is needed, not incidental: which campaign wins can depend on
    it. A flat ₦2,000 beats 5% on a ₦28,000 item and loses badly on a
    ₦1,850,000 one. Deciding that here keeps the "best one, never both" rule
    in the single place that already owns it, rather than repeating it in the
    browser where the two could drift apart.
    """
    pool = live_campaigns()
    if not pool:
        return {}

    wanted = {entry.product_odoo_id for c in pool for entry in c.products if entry.product_odoo_id}
    if not wanted:
        return {}

    priced = {
        row.odoo_id: (float(row.list_price or 0), row.currency)
        for row in Product.query.filter(Product.odoo_id.in_(wanted)).all()
    }

    rewards = {}
    for product_id in wanted:
        price, currency = priced.get(product_id, (0.0, None))
        amount, campaign = best_reward(product_id, None, 1, price, None, pool)
        if not campaign:
            continue
        rewards[str(product_id)] = {
            "amount": amount,
            "rewardType": campaign.reward_type,
            "rewardValue": float(campaign.reward_value or 0),
            "campaign": campaign.name,
            "currency": currency,
        }
    return rewards


def campaign_product_ids() -> set:
    """Products currently carrying a campaign reward.

    Used to float them to the front of the catalogue. Kept here rather than
    worked out by the caller so there is one answer to "is this product in a
    campaign right now", including the date and active checks.
    """
    return {
        entry.product_odoo_id
        for campaign in live_campaigns()
        for entry in campaign.products
        if entry.product_odoo_id
    }


def announce_live_campaigns() -> int:
    """Tell affiliates about any campaign that has started and not been announced.

    Called when a campaign is created and again whenever affiliates look at
    the campaigns page. That second call is what covers a campaign scheduled
    for a future date: there is no clock running on this service, so the
    announcement happens the next time somebody uses the system rather than
    at the stroke of midnight. A day late is a great deal better than never,
    and it needs no scheduler to go wrong quietly.

    Returns how many campaigns were announced. Never raises — an offer that
    goes unannounced is a missed opportunity, not a failure worth breaking a
    page over.
    """
    announced = 0
    try:
        due = [c for c in Campaign.query.filter_by(active=True).all()
               if c.announced_at is None and c.is_live()]
        if not due:
            return 0

        affiliates = Affiliate.query.filter_by(status="approved").all()
        for campaign in due:
            reward = (f"{campaign.reward_value:g}% extra"
                      if campaign.reward_type == "percent"
                      else f"{campaign.reward_value:,.0f} extra per item")
            count = len(campaign.products)
            for affiliate in affiliates:
                notify_affiliate(
                    affiliate.id, "campaign.started",
                    f"New campaign: {campaign.name}",
                    f"Earn {reward} on {count} product{'' if count == 1 else 's'}, "
                    "on top of your usual markup.",
                    "/campaigns",
                )
            campaign.announced_at = utcnow()
            announced += 1
        db.session.commit()
        log.info("Announced %d campaign(s) to %d affiliate(s)", announced, len(affiliates))
    except Exception:  # noqa: BLE001 — a missed announcement must not break a page
        db.session.rollback()
        log.warning("Could not announce campaigns", exc_info=True)
        return 0
    return announced
