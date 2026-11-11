"""Move the programme onto a different store, starting the money from zero.

Used once, when the dashboard moved from the Odoo 16 test store to Odoo 19
(TPD-1874). Kept as a command rather than a loose script so it runs with the
app's own configuration and models.

What it does, in order:

1. Removes every earning, earning line and payout, and the bell notices
   about them. They were recorded against the old store; its order numbers
   would collide with the new store's (both count S00001 upwards), and a
   collision silently *updates* an old earning instead of recording a new
   sale.
2. Forgets the old store's ids for every affiliate and link, so each is
   pushed afresh. Affiliates, links, codes, markups, campaigns and bank
   details are all kept — those belong to this service, not to a store.
3. Clears the cached product pictures, then copies the catalogue across
   again. A product id can mean a different product in the new store, and a
   cached picture would keep showing the old one.
4. Checks every product a link or campaign points at still has the same name
   in the new store. One that changed is moved to the product with that exact
   name if there is exactly one; anything else is listed for a person.
5. Pushes every live affiliate and link to the new store.

Take a database backup first. Nothing here can be undone from inside the app.
"""

import logging

from models import (
    Affiliate, AffiliateLink, CampaignProduct, Earning, EarningLine,
    Notification, Payout, Product, ProductImage, db,
)

log = logging.getLogger(__name__)


def _product_names(ids) -> dict:
    ids = [i for i in ids if i]
    if not ids:
        return {}
    rows = Product.query.filter(Product.odoo_id.in_(ids)).all()
    return {row.odoo_id: row.name for row in rows}


def _unique_by_name(name: str):
    """The one live product with exactly this name in the new catalogue."""
    if not name:
        return None
    found = Product.query.filter(Product.name == name, Product.available.is_(True)).limit(2).all()
    return found[0] if len(found) == 1 else None


def switch_store(echo=print) -> dict:
    from affiliate_service import resync_pending
    from catalog import build_client, refresh_catalogue

    report = {"needs_attention": []}

    # What links and campaigns point at, by name, before the ids change meaning.
    links = AffiliateLink.query.all()
    campaign_products = CampaignProduct.query.all()
    before = _product_names([l.product_odoo_id for l in links])

    echo("1. Removing earnings, payouts and their notices")
    report["earning_lines"] = EarningLine.query.delete()
    # Earnings point at payouts, so they go first.
    report["earnings"] = Earning.query.delete()
    report["payouts"] = Payout.query.delete()
    report["notifications"] = Notification.query.filter(
        Notification.kind.like("earning.%") | Notification.kind.like("payout.%")
    ).delete(synchronize_session=False)

    echo("2. Forgetting the old store's ids")
    Affiliate.query.update({"odoo_affiliate_id": None, "synced_at": None})
    AffiliateLink.query.update({"odoo_link_id": None, "odoo_pricelist_id": None, "synced_at": None})
    report["pictures_cleared"] = ProductImage.query.delete()
    db.session.commit()
    echo(f"   removed {report['earnings']} earnings, {report['payouts']} payouts, "
         f"{report['notifications']} notices, {report['pictures_cleared']} cached pictures")

    echo("3. Copying the catalogue from the new store")
    report["catalogue"] = refresh_catalogue(with_images=False)
    echo(f"   {report['catalogue']}")

    echo("4. Checking what links and campaigns point at")
    # A product only counts as "still the same" if the new store actually
    # sent it in this sync. A row the new store did not send keeps its old
    # name in our copy, so comparing names alone would wave through an id
    # that now means a different, unpublished product in the new store.
    def same_product(pid, expected_name):
        found = Product.query.filter_by(odoo_id=pid).first() if pid else None
        return bool(found and found.available and expected_name and found.name == expected_name)

    for link in links:
        pid = link.product_odoo_id
        if not pid:
            continue
        old_name = before.get(pid)
        if same_product(pid, old_name):
            continue
        match = _unique_by_name(old_name)
        if match:
            echo(f"   link {link.code}: {old_name!r} moved {pid} -> {match.odoo_id}")
            link.product_odoo_id = match.odoo_id
        else:
            # Better the shop front than someone else's product.
            link.product_odoo_id = None
            link.target_type = "storewide"
            report["needs_attention"].append(
                f"link {link.code}: {old_name!r} (old id {pid}) is not on the new "
                f"store's website; the link now opens the shop")
    client = build_client()
    for cp in list(campaign_products):
        if not same_product(cp.product_odoo_id, cp.name):
            match = _unique_by_name(cp.name)
            if match:
                echo(f"   campaign {cp.campaign_id}: {cp.name!r} moved "
                     f"{cp.product_odoo_id} -> {match.odoo_id}")
                cp.product_odoo_id = match.odoo_id
            else:
                # Left in, the old id would pay this campaign on whatever
                # product the new store has under that number.
                report["needs_attention"].append(
                    f"campaign {cp.campaign_id}: {cp.name!r} (old id {cp.product_odoo_id}) "
                    f"is not on the new store's website; removed from the campaign")
                db.session.delete(cp)
                continue
        # Campaigns also match on the template, and template ids are numbered
        # separately in each store, so take it fresh from the new one.
        if cp.product_odoo_id:
            cp.product_tmpl_id = client.template_id_for(cp.product_odoo_id)
    db.session.commit()

    echo("5. Pushing affiliates and live links to the new store")
    report["pushed"] = resync_pending()
    echo(f"   {report['pushed']}")
    for line in report["needs_attention"]:
        echo(f"   NEEDS A PERSON: {line}")
    return report
