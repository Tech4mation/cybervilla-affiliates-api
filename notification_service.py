"""Telling people what happened.

Every function here is a side effect of something that has already taken
place: an order was paid, a payout was sent, an application was approved. The
event is the important thing and the notice about it is not, so nothing in
this module is allowed to raise into its caller. A failure to write a notice
must never roll back a payout or reject a webhook — the worst acceptable
outcome is a missing line in someone's bell.

Only events this system genuinely detects are here. There are deliberately no
"commission milestone" or "promotion changed" notices, because the system has
no milestones and no promotions; inventing them would mean inventing the data.
"""

import logging

from models import Notification, User, db

log = logging.getLogger(__name__)


def notify(user_id: int | None, kind: str, title: str, body: str = "", href: str = "") -> None:
    """Add one notice for one person. Never raises."""
    if not user_id:
        return
    try:
        db.session.add(Notification(
            user_id=user_id, kind=kind, title=title,
            body=(body or "")[:1024] or None, href=href or None,
        ))
        db.session.commit()
    except Exception:  # noqa: BLE001 — a missing notice is not worth a failed payout
        db.session.rollback()
        log.warning("Could not write notification %s for user %s", kind, user_id, exc_info=True)


def notify_admins(kind: str, title: str, body: str = "", href: str = "") -> None:
    """Tell every admin. Used for things that need someone to act."""
    try:
        admins = User.query.filter_by(role="admin").all()
    except Exception:  # noqa: BLE001
        log.warning("Could not look up admins for %s", kind, exc_info=True)
        return
    for admin in admins:
        notify(admin.id, kind, title, body, href)


def user_for_affiliate(affiliate_id: int | None) -> int | None:
    """The login that belongs to an affiliate, if one does."""
    if not affiliate_id:
        return None
    try:
        found = User.query.filter_by(affiliate_id=affiliate_id).first()
        return found.id if found else None
    except Exception:  # noqa: BLE001
        return None


def notify_affiliate(affiliate_id: int | None, kind: str, title: str,
                     body: str = "", href: str = "") -> None:
    notify(user_for_affiliate(affiliate_id), kind, title, body, href)


def unread_count(user_id: int) -> int:
    return Notification.query.filter_by(user_id=user_id, read_at=None).count()
