"""Authentication, affiliate membership, and admin review routes.

Provides:
  * Public sign-up and sign-in API endpoints (/auth/signup, /auth/signin, /auth/me)
  * Affiliate member-guarded endpoints (/affiliate/*)
  * Admin affiliate review & approval endpoints (/admin/affiliates/*)
  * Built-in browser-friendly sign-up & sign-in HTML pages (/signup, /signin, /admin/approvals)
"""

import logging
from datetime import datetime, timezone

from flask import Blueprint, g, jsonify, make_response, render_template_string, request
from sqlalchemy import func

from affiliate_service import LinkLimitReached, create_link, delete_link, earnings_by_link
from catalog import StoreNotConfigured
from config import Config
from auth_service import (
    approve_affiliate,
    get_current_user,
    reject_affiliate,
    require_admin,
    require_approved_affiliate,
    require_auth,
    signin_user,
    signup_affiliate,
)
from models import (
    Affiliate, AffiliateLink, Campaign, CampaignProduct, Earning, Notification,
    Payout, User, amount_due, db, utcnow,
)
from campaign_service import announce_live_campaigns, live_campaigns, rewards_by_product
from notification_service import unread_count
from paystack_client import (
    PaystackError,
    PaystackNotConfigured,
    list_banks,
    resolve_account,
)
from payout_service import (
    PayoutError,
    confirm_payout_otp,
    fail_payout,
    mark_paid,
    payable_balance,
    reconcile_payout,
    request_payout,
    resend_payout_otp,
    send_payout,
)

log = logging.getLogger(__name__)

auth_bp = Blueprint("auth", __name__)

# How many earning rows an admin screen is handed at once. The money totals are
# summed in the database and are not affected by this.
EARNINGS_PAGE_LIMIT = 500


# --------------------------------------------------------------------------- #
# Public Auth API
# --------------------------------------------------------------------------- #

@auth_bp.route("/auth/signup", methods=["POST"])
@auth_bp.route("/auth/register", methods=["POST"])
def signup_route():
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        user, affiliate = signup_affiliate(
            name=body.get("name", ""),
            email=body.get("email", ""),
            password=body.get("password", ""),
            phone=body.get("phone", ""),
            why_join=body.get("whyJoin") or body.get("why_join") or body.get("pitch", ""),
        )
    except ValueError as exc:
        return jsonify({"error": "bad_request", "message": str(exc)}), 400

    from auth_service import generate_token
    token = generate_token(user)

    response_data = {
        "success": True,
        "message": (
            "Registration successful. Your application is pending admin approval. "
            "Sign-ups do not become members of the affiliate program until approved by the admin."
        ),
        "user": user.as_dict(),
        "token": token,
    }

    res = make_response(jsonify(response_data), 201)
    # Set cookie for browser sessions
    res.set_cookie("affiliate_token", token, httponly=False, samesite="Lax")
    return res


@auth_bp.route("/auth/signin", methods=["POST"])
@auth_bp.route("/auth/login", methods=["POST"])
def signin_route():
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    try:
        user, token = signin_user(
            email=body.get("email", ""),
            password=body.get("password", ""),
        )
    except ValueError as exc:
        return jsonify({"error": "unauthorized", "message": str(exc)}), 401

    status_message = "Signed in successfully."
    if user.role == "affiliate" and user.status == "pending":
        status_message = (
            "Signed in. Your account is pending admin approval before you can participate in the affiliate program."
        )

    res = make_response(jsonify({
        "success": True,
        "token": token,
        "user": user.as_dict(),
        "message": status_message,
    }), 200)
    res.set_cookie("affiliate_token", token, httponly=False, samesite="Lax")
    return res


@auth_bp.route("/auth/me", methods=["GET"])
@require_auth
def me_route():
    user: User = g.current_user
    affiliate_dict = user.affiliate.as_dict() if user.affiliate else None
    return jsonify({
        "user": user.as_dict(),
        "affiliate": affiliate_dict,
    }), 200


@auth_bp.route("/auth/logout", methods=["POST"])
def logout_route():
    res = make_response(jsonify({"success": True, "message": "Signed out successfully."}), 200)
    res.delete_cookie("affiliate_token")
    return res


# --------------------------------------------------------------------------- #
# Affiliate Member Routes (Restricted to Approved Members)
# --------------------------------------------------------------------------- #

@auth_bp.route("/affiliate/profile", methods=["GET"])
@require_approved_affiliate
def affiliate_profile_route():
    user: User = g.current_user
    affiliate = user.affiliate
    return jsonify({
        "user": user.as_dict(),
        "affiliate": affiliate.as_dict() if affiliate else None,
    }), 200


@auth_bp.route("/affiliate/rules", methods=["GET"])
@require_approved_affiliate
def affiliate_rules_route():
    """The numbers the affiliate-facing copy quotes back at people.

    Every one of them is configurable, and all of them are enforced
    elsewhere in this service. A help page that states a different cap from
    the one actually applied is worse than one that states nothing, so the
    rules travel to the dashboard rather than being written into it twice.
    """
    return jsonify({
        "maxMarkupPercent": float(Config.MAX_MARKUP_PERCENT),
        "maxLinks": int(Config.MAX_LINKS_PER_AFFILIATE),
        "minPayout": float(Config.MIN_PAYOUT_AMOUNT),
        "holdDays": int(Config.EARNING_HOLD_DAYS),
    }), 200


@auth_bp.route("/affiliate/links", methods=["GET"])
@require_approved_affiliate
def list_affiliate_links_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"links": []}), 200
    links = (AffiliateLink.query
             .filter_by(affiliate_id=user.affiliate.id, active=True)
             .order_by(AffiliateLink.id.desc()).all())
    # Earnings are looked up for this affiliate only, so one affiliate's links
    # can never carry another's figures.
    stats = earnings_by_link(user.affiliate.id)
    payload = []
    for link in links:
        row = link.as_dict()
        row.update(stats.get(link.id) or {"sales": 0, "earnings": 0.0, "currency": None})
        payload.append(row)
    # The cap travels with the list so the dashboard never hardcodes its own copy.
    return jsonify({
        "links": payload,
        "used": len(payload),
        "maxLinks": Config.MAX_LINKS_PER_AFFILIATE,
    }), 200


@auth_bp.route("/affiliate/links", methods=["POST"])
@require_approved_affiliate
def create_affiliate_link_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"error": "not_found", "message": "Affiliate profile missing."}), 404

    body = request.get_json(silent=True) or {}
    product_id = body.get("productId")
    try:
        link = create_link(
            affiliate=user.affiliate,
            markup_percent=body.get("markupPercent", 0),
            label=body.get("label", ""),
            product_odoo_id=int(product_id) if product_id else None,
        )
    except LinkLimitReached as exc:
        return jsonify({"error": "link_limit_reached", "message": str(exc)}), 409
    # A link nobody has clicked yet, stated rather than left for the caller to guess.
    return jsonify({"link": {**link.as_dict(), "sales": 0, "earnings": 0.0, "currency": None}}), 201


@auth_bp.route("/affiliate/links/<backend_ref>", methods=["DELETE"])
@require_approved_affiliate
def delete_affiliate_link_route(backend_ref: str):
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"error": "not_found", "message": "Affiliate profile missing."}), 404

    # Scoped to this affiliate's own links, so a guessed reference from
    # someone else's account finds nothing.
    link = AffiliateLink.query.filter_by(
        backend_ref=backend_ref, affiliate_id=user.affiliate.id
    ).first()
    if not link:
        return jsonify({"error": "not_found", "message": "No such link."}), 404

    try:
        outcome = delete_link(link)
    except StoreNotConfigured as exc:
        return jsonify({"error": "store_not_configured", "message": str(exc)}), 503
    except Exception:
        log.warning("Could not retire link %s", backend_ref, exc_info=True)
        return jsonify({
            "error": "store_unreachable",
            "message": "We could not switch this link off in the store, so it has been left "
                       "active. Please try again shortly.",
        }), 502

    return jsonify({"outcome": outcome}), 200


@auth_bp.route("/notifications", methods=["GET"])
@require_auth
def notifications_route():
    """This person's notices. Serves affiliates and admins alike."""
    user: User = g.current_user
    limit = min(100, max(1, int(request.args.get("limit", 50))))
    rows = (Notification.query.filter_by(user_id=user.id)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit).all())
    return jsonify({
        "notifications": [r.as_dict() for r in rows],
        "unread": unread_count(user.id),
    }), 200


@auth_bp.route("/notifications/read", methods=["POST"])
@require_auth
def mark_notifications_read_route():
    """Mark some notices read, or all of them when none are named."""
    user: User = g.current_user
    body = request.get_json(silent=True) or {}
    ids = body.get("ids")
    query = Notification.query.filter_by(user_id=user.id, read_at=None)
    if ids:
        query = query.filter(Notification.id.in_(ids))
    now = utcnow()
    changed = 0
    for row in query.all():
        row.read_at = now
        changed += 1
    db.session.commit()
    return jsonify({"read": changed, "unread": unread_count(user.id)}), 200


@auth_bp.route("/banks", methods=["GET"])
@require_auth
def banks_route():
    """The banks a payout can actually be sent to.

    Signed-in users only. It is not secret, but it costs us a call to
    Paystack and there is no reason to let the open internet spend it.
    """
    try:
        return jsonify({"banks": list_banks()}), 200
    except PaystackError as exc:
        log.warning("bank list unavailable: %s", exc)
        return jsonify({
            "error": "banks_unavailable",
            "message": "We could not load the bank list just now. Please try again shortly.",
        }), 503


@auth_bp.route("/affiliate/payout-account/resolve", methods=["POST"])
@require_approved_affiliate
def resolve_payout_account_route():
    """Ask the bank who owns an account number, before anyone saves it.

    This is what stops money going to a mistyped account: the affiliate sees
    the name on the account and can tell at a glance whether it is theirs.
    Nothing is stored here — it is a question, not a change.
    """
    body = request.get_json(silent=True) or {}
    number = (body.get("accountNumber") or "").strip()
    code = (body.get("bankCode") or "").strip()
    if not (number and code):
        return jsonify({
            "error": "incomplete",
            "message": "We need both the account number and the bank.",
        }), 400
    if not number.isdigit() or not (8 <= len(number) <= 20):
        return jsonify({
            "error": "bad_account_number",
            "message": "An account number should be 8 to 20 digits.",
        }), 400
    try:
        name = resolve_account(number, code)
    except PaystackNotConfigured as exc:
        log.warning("account resolve unavailable: %s", exc)
        return jsonify({
            "error": "not_configured",
            "message": "Account checking is not switched on yet.",
        }), 503
    except PaystackError as exc:
        # Paystack's own wording here is the useful part ("Could not resolve
        # account name"), so it is passed through rather than replaced.
        return jsonify({"error": "unresolved", "message": str(exc)}), 400
    return jsonify({"accountName": name}), 200


@auth_bp.route("/affiliate/payout-account", methods=["GET", "PUT"])
@require_approved_affiliate
def affiliate_payout_account_route():
    """Where this affiliate's money should be sent."""
    user: User = g.current_user
    affiliate = user.affiliate
    if not affiliate:
        return jsonify({"error": "not_found", "message": "Affiliate profile missing."}), 404

    if request.method == "PUT":
        body = request.get_json(silent=True) or {}
        number = (body.get("accountNumber") or "").strip()
        code = (body.get("bankCode") or "").strip()
        if not (number and code):
            return jsonify({
                "error": "incomplete",
                "message": "We need the account number and the bank.",
            }), 400
        if not number.isdigit() or not (8 <= len(number) <= 20):
            return jsonify({
                "error": "bad_account_number",
                "message": "An account number should be 8 to 20 digits.",
            }), 400

        # The bank's own record decides whose name is on the account. A name
        # sent by the browser is a claim, not a fact, and storing it would
        # let somebody label an account as anyone they liked — then point at
        # that label when the money went to the wrong place.
        try:
            resolved_name = resolve_account(number, code)
            bank_name = next((b["name"] for b in list_banks() if b["code"] == code), "")
        except PaystackNotConfigured as exc:
            log.warning("payout account not verifiable: %s", exc)
            return jsonify({
                "error": "not_configured",
                "message": "We can't check bank details just now, so we haven't saved them.",
            }), 503
        except PaystackError as exc:
            return jsonify({"error": "unresolved", "message": str(exc)}), 400

        if not bank_name:
            return jsonify({
                "error": "bad_bank",
                "message": "That isn't a bank we can send a payout to.",
            }), 400

        # Pointing at a different account makes the recipient Paystack already
        # holds for the old one wrong. Left in place it would quietly send the
        # next payout to the account this change was meant to replace.
        if affiliate.bank_account_number != number or affiliate.bank_code != code:
            affiliate.paystack_recipient_code = None

        affiliate.bank_account_name = resolved_name
        affiliate.bank_account_number = number
        affiliate.bank_code = code
        affiliate.bank_name = bank_name
        db.session.commit()

    return jsonify({"account": {
        "accountName": affiliate.bank_account_name or "",
        "bankName": affiliate.bank_name or "",
        "bankCode": affiliate.bank_code or "",
        # Never echoed in full once stored; enough to recognise, not to reuse.
        "accountNumberLast4": (affiliate.bank_account_number or "")[-4:],
        "complete": bool(affiliate.bank_account_number and affiliate.bank_code
                         and affiliate.bank_account_name),
    }}), 200


@auth_bp.route("/affiliate/payouts", methods=["GET"])
@require_approved_affiliate
def affiliate_payouts_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"payouts": [], "balance": None}), 200
    payouts = (Payout.query.filter_by(affiliate_id=user.affiliate.id)
               .order_by(Payout.id.desc()).all())
    return jsonify({
        "payouts": [p.as_dict() for p in payouts],
        "balance": payable_balance(user.affiliate.id),
    }), 200


@auth_bp.route("/affiliate/payouts", methods=["POST"])
@require_approved_affiliate
def request_payout_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"error": "not_found", "message": "Affiliate profile missing."}), 404
    body = request.get_json(silent=True) or {}
    try:
        payout = request_payout(user.affiliate, note=body.get("note", ""))
    except PayoutError as exc:
        return jsonify({"error": "not_payable", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 201


@auth_bp.route("/affiliate/earnings", methods=["GET"])
@require_approved_affiliate
def list_affiliate_earnings_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"earnings": []}), 200
    rows = Earning.query.filter_by(affiliate_id=user.affiliate.id).order_by(Earning.id.desc()).all()
    return jsonify({"earnings": [row.as_dict() for row in rows]}), 200


# --------------------------------------------------------------------------- #
# Admin Review & Approval Routes
# --------------------------------------------------------------------------- #

@auth_bp.route("/admin/affiliates", methods=["GET"])
@require_admin
def admin_list_affiliates():
    status = request.args.get("status", "").strip().lower()
    search = request.args.get("search", "").strip().lower()
    page = max(1, int(request.args.get("page", 1)))
    per_page = min(100, max(1, int(request.args.get("perPage", 20))))

    query = User.query.filter_by(role="affiliate")

    if status and status != "all":
        query = query.filter_by(status=status)

    if search:
        query = query.filter(
            (User.name.ilike(f"%{search}%")) | (User.email.ilike(f"%{search}%"))
        )

    total = query.count()
    users = query.order_by(User.id.desc()).offset((page - 1) * per_page).limit(per_page).all()

    items = []
    for u in users:
        aff = u.affiliate
        items.append({
            "id": u.id,
            "name": u.name,
            "email": u.email,
            "phone": u.phone or "",
            "role": u.role,
            "status": u.status,
            "isMember": u.is_member,
            "rejectionReason": u.rejection_reason,
            "whyJoin": u.why_join or "",
            "affiliateRef": aff.backend_ref if aff else None,
            "odooAffiliateId": aff.odoo_affiliate_id if aff else None,
            "synced": (aff.odoo_affiliate_id is not None) if aff else False,
            "joinedAt": u.created_at.isoformat() if u.created_at else None,
        })

    pending_count = User.query.filter_by(role="affiliate", status="pending").count()
    approved_count = User.query.filter_by(role="affiliate", status="approved").count()

    return jsonify({
        "affiliates": items,
        "total": total,
        "page": page,
        "perPage": per_page,
        "stats": {
            "pending": pending_count,
            "approved": approved_count,
        }
    }), 200


@auth_bp.route("/admin/affiliates/<int:target_id>/approve", methods=["POST"])
@require_admin
def admin_approve_affiliate(target_id: int):
    try:
        user, affiliate = approve_affiliate(target_id)
    except ValueError as exc:
        return jsonify({"error": "not_found", "message": str(exc)}), 404

    return jsonify({
        "success": True,
        "message": f"Affiliate {user.name} ({user.email}) approved successfully into the affiliate program.",
        "user": user.as_dict(),
        "affiliate": affiliate.as_dict(),
    }), 200


@auth_bp.route("/admin/affiliates/<int:target_id>/reject", methods=["POST"])
@require_admin
def admin_reject_affiliate(target_id: int):
    body = request.get_json(silent=True) or request.form.to_dict() or {}
    reason = body.get("reason", "")
    try:
        user, affiliate = reject_affiliate(target_id, reason=reason)
    except ValueError as exc:
        return jsonify({"error": "not_found", "message": str(exc)}), 404

    return jsonify({
        "success": True,
        "message": f"Affiliate {user.name} rejected.",
        "user": user.as_dict(),
    }), 200


@auth_bp.route("/admin/earnings", methods=["GET"])
@require_admin
def admin_list_earnings():
    """Store-reported earnings, plus totals that are not capped by the page.

    The list is capped because nobody reads ten thousand rows in a browser, but
    the money figures must never be a sum of whatever happened to fit — that
    silently shrinks as the platform grows. So the totals are computed in the
    database over every row, and the caller is told how many it is seeing.
    """
    status = request.args.get("status", "").strip().lower()
    query = Earning.query
    if status and status != "all":
        query = query.filter_by(status=status)

    matching = query.order_by(None).count()
    rows = query.order_by(Earning.occurred_at.desc(), Earning.id.desc()).limit(EARNINGS_PAGE_LIMIT).all()

    # Every row, every status — what the dashboard tiles are built from.
    sums = (
        db.session.query(
            Earning.status,
            func.count(Earning.id),
            func.coalesce(func.sum(amount_due()), 0),
        )
        .group_by(Earning.status)
        .all()
    )

    return jsonify({
        "earnings": [row.as_dict() for row in rows],
        "returned": len(rows),
        "total": matching,
        "truncated": matching > len(rows),
        "totalsByStatus": {
            state: {"count": int(count or 0), "earning": float(amount or 0)}
            for state, count, amount in sums
        },
    }), 200


@auth_bp.route("/admin/payouts", methods=["GET"])
@require_admin
def admin_payouts_route():
    status = request.args.get("status", "").strip().lower()
    query = Payout.query
    if status and status != "all":
        query = query.filter_by(status=status)
    payouts = query.order_by(Payout.id.desc()).limit(200).all()
    return jsonify({"payouts": [
        {**p.as_dict(),
         "affiliateName": p.affiliate.name if p.affiliate else "",
         "affiliateRef": p.affiliate.backend_ref if p.affiliate else ""}
        for p in payouts
    ]}), 200


@auth_bp.route("/admin/payouts/<reference>/send", methods=["POST"])
@require_admin
def admin_send_payout_route(reference: str):
    """Actually send this payout's money through Paystack.

    Unlike `/paid`, this one moves money. It either completes, or comes back
    waiting for the confirmation code Paystack has sent to the account owner.
    """
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    try:
        send_payout(payout)
    except PayoutError as exc:
        return jsonify({"error": "not_sendable", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 200


@auth_bp.route("/admin/payouts/<reference>/confirm-otp", methods=["POST"])
@require_admin
def admin_confirm_payout_otp_route(reference: str):
    """Release a transfer Paystack is holding, with the code it sent."""
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    body = request.get_json(silent=True) or {}
    try:
        confirm_payout_otp(payout, body.get("otp", ""))
    except PayoutError as exc:
        return jsonify({"error": "otp_rejected", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 200


@auth_bp.route("/admin/payouts/<reference>/resend-otp", methods=["POST"])
@require_admin
def admin_resend_payout_otp_route(reference: str):
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    try:
        resend_payout_otp(payout)
    except PayoutError as exc:
        return jsonify({"error": "not_resendable", "message": str(exc)}), 409
    return jsonify({"sent": True}), 200


@auth_bp.route("/admin/payouts/<reference>/reconcile", methods=["POST"])
@require_admin
def admin_reconcile_payout_route(reference: str):
    """Make our record agree with Paystack's. Never sends anything."""
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    try:
        reconcile_payout(payout)
    except PayoutError as exc:
        return jsonify({"error": "not_reconcilable", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 200


@auth_bp.route("/admin/payouts/<reference>/paid", methods=["POST"])
@require_admin
def admin_mark_payout_paid_route(reference: str):
    """Record that this payout has been sent from the bank.

    This does not move money; it states that money was moved. Keeping those
    apart is deliberate — see payout_service.mark_paid.
    """
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    body = request.get_json(silent=True) or {}
    try:
        mark_paid(payout, provider_reference=body.get("reference", ""),
                  note=body.get("note", ""))
    except PayoutError as exc:
        return jsonify({"error": "not_payable", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 200


@auth_bp.route("/admin/payouts/<reference>/failed", methods=["POST"])
@require_admin
def admin_mark_payout_failed_route(reference: str):
    payout = Payout.query.filter_by(reference=reference).first()
    if not payout:
        return jsonify({"error": "not_found", "message": "No such payout."}), 404
    body = request.get_json(silent=True) or {}
    try:
        fail_payout(payout, body.get("reason", ""))
    except PayoutError as exc:
        return jsonify({"error": "not_failable", "message": str(exc)}), 409
    return jsonify({"payout": payout.as_dict()}), 200


@auth_bp.route("/admin/affiliates/<int:target_id>", methods=["GET"])
@require_admin
def admin_affiliate_detail(target_id: int):
    """Return one affiliate with its backend-owned links and earnings."""
    user = User.query.filter_by(id=target_id, role="affiliate").first()
    if not user:
        return jsonify({"error": "not_found", "message": "No such affiliate."}), 404

    affiliate = user.affiliate
    return jsonify({
        "affiliate": {
            "id": user.id,
            "name": user.name,
            "email": user.email,
            "phone": user.phone or "",
            "role": user.role,
            "status": user.status,
            "isMember": user.is_member,
            "rejectionReason": user.rejection_reason,
            "whyJoin": user.why_join or "",
            "affiliateRef": affiliate.backend_ref if affiliate else None,
            "odooAffiliateId": affiliate.odoo_affiliate_id if affiliate else None,
            "synced": bool(affiliate and affiliate.odoo_affiliate_id),
            "joinedAt": user.created_at.isoformat() if user.created_at else None,
        },
        "links": [link.as_dict() for link in (affiliate.links if affiliate else [])],
        "earnings": [row.as_dict() for row in Earning.query.filter_by(
            affiliate_id=affiliate.id if affiliate else None
        ).order_by(Earning.id.desc()).all()] if affiliate else [],
    }), 200


# --------------------------------------------------------------------------- #
# Built-in Web Pages (HTML templates for Signup / Signin / Approvals)
# --------------------------------------------------------------------------- #

HTML_PAGE_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ title }} - CyberVilla Affiliate Program</title>
  <script src="https://cdn.tailwindcss.com"></script>
  <script>
    tailwind.config = {
      darkMode: 'class',
      theme: {
        extend: {
          colors: {
            brand: { 500: '#10b981', 600: '#059669', 700: '#047857' },
            dark: { 900: '#090d16', 800: '#111827', 700: '#1f2937' }
          }
        }
      }
    }
  </script>
  <style>
    body { background-color: #0b0f19; color: #f3f4f6; font-family: ui-sans-serif, system-ui, sans-serif; }
  </style>
</head>
<body class="min-h-screen flex flex-col justify-between">
  <!-- Header -->
  <header class="border-b border-gray-800 bg-gray-900/60 backdrop-blur px-6 py-4 flex items-center justify-between">
    <div class="flex items-center gap-3">
      <div class="w-8 h-8 rounded-lg bg-gradient-to-tr from-emerald-500 to-teal-400 flex items-center justify-center font-bold text-black text-sm">
        CV
      </div>
      <span class="font-bold text-lg tracking-tight text-white">CyberVilla <span class="text-emerald-400 font-medium">Affiliates</span></span>
    </div>
    <div class="flex items-center gap-4 text-sm">
      <a href="/signup" class="text-gray-300 hover:text-white transition">Sign Up</a>
      <a href="/signin" class="text-gray-300 hover:text-white transition">Sign In</a>
      <a href="/admin/approvals" class="text-xs bg-gray-800 text-gray-400 hover:text-emerald-400 border border-gray-700 px-2.5 py-1 rounded">Admin Portal</a>
    </div>
  </header>

  <!-- Main Content -->
  <main class="flex-1 flex items-center justify-center p-6">
    {{ content|safe }}
  </main>

  <!-- Footer -->
  <footer class="border-t border-gray-800/80 py-4 px-6 text-center text-xs text-gray-500">
    &copy; CyberVilla Affiliate Network. All sign-ups require admin approval before becoming program members.
  </footer>
</body>
</html>
"""


@auth_bp.route("/signup", methods=["GET"])
def signup_page():
    content = """
    <div class="w-full max-w-lg bg-gray-900 border border-gray-800 rounded-2xl p-6 sm:p-8 shadow-2xl my-8">
      <div class="text-center mb-6">
        <h1 class="text-2xl font-bold text-white tracking-tight">Become an Affiliate</h1>
        <p class="text-sm text-gray-400 mt-1">Join the CyberVilla affiliate community</p>
      </div>

      <!-- Policy Callout -->
      <div class="mb-6 p-3.5 rounded-xl bg-emerald-950/40 border border-emerald-800/50 flex gap-3 text-xs text-emerald-300">
        <svg class="w-5 h-5 flex-shrink-0 text-emerald-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 16h-1v-4h-1m1-4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z"></path>
        </svg>
        <div>
          <span class="font-semibold text-emerald-200">Approval Required:</span>
          Sign-ups do not become members of the affiliate program until approved by the admin.
        </div>
      </div>

      <div id="alertBox" class="hidden mb-4 p-3 rounded-lg text-xs font-medium"></div>

      <form id="signupForm" class="space-y-4">
        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-gray-300 mb-1">Full Name *</label>
            <input type="text" name="name" required placeholder="e.g. Tomiwa Adebayo"
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-300 mb-1">Email Address *</label>
            <input type="email" name="email" required placeholder="tomiwa@example.com"
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-gray-300 mb-1">Phone Number (Optional)</label>
            <input type="tel" name="phone" placeholder="+234 801 234 5678"
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
          </div>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-300 mb-1">Why do you want to join? *</label>
          <textarea name="whyJoin" required rows="3" placeholder="Tell us about your audience and how you plan to promote CyberVilla products..."
            class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500"></textarea>
        </div>

        <div>
          <label class="block text-xs font-medium text-gray-300 mb-1">Password *</label>
          <input type="password" name="password" required minlength="6" placeholder="At least 6 characters"
            class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
        </div>

        <button type="submit" id="submitBtn"
          class="w-full py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-black font-semibold text-sm transition">
          Submit Application
        </button>
      </form>

      <p class="text-center text-xs text-gray-400 mt-6">
        Already have an account? <a href="/signin" class="text-emerald-400 hover:underline">Sign In</a>
      </p>
    </div>

    <script>
      const form = document.getElementById('signupForm');
      const alertBox = document.getElementById('alertBox');
      const submitBtn = document.getElementById('submitBtn');

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        submitBtn.disabled = true;
        submitBtn.innerText = 'Submitting...';
        alertBox.className = 'hidden';

        const formData = new FormData(form);
        const data = Object.fromEntries(formData.entries());

        try {
          const res = await fetch('/auth/signup', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(data)
          });
          const result = await res.json();
          if (res.ok) {
            localStorage.setItem('affiliate_token', result.token);
            alertBox.className = 'mb-4 p-4 rounded-xl bg-amber-950/60 border border-amber-600/50 text-amber-200 text-xs leading-relaxed block';
            alertBox.innerHTML = `
              <div class="font-bold text-sm text-amber-300 mb-1">Application Received!</div>
              <p>Your affiliate account is currently <strong>pending admin approval</strong>.</p>
              <p class="mt-1 text-gray-300">You will become a full member of the affiliate program with access to commission links and products once the admin reviews and approves your application.</p>
            `;
            form.reset();
          } else {
            alertBox.className = 'mb-4 p-3 rounded-lg bg-red-950/60 border border-red-700 text-red-300 text-xs block';
            alertBox.innerText = result.message || 'Registration failed.';
          }
        } catch (err) {
          alertBox.className = 'mb-4 p-3 rounded-lg bg-red-950/60 border border-red-700 text-red-300 text-xs block';
          alertBox.innerText = 'Network error. Please try again.';
        } finally {
          submitBtn.disabled = false;
          submitBtn.innerText = 'Submit Application';
        }
      });
    </script>
    """
    return render_template_string(HTML_PAGE_TEMPLATE, title="Sign Up", content=content)


@auth_bp.route("/signin", methods=["GET"])
@auth_bp.route("/login", methods=["GET"])
def signin_page():
    content = """
    <div class="w-full max-w-md bg-gray-900 border border-gray-800 rounded-2xl p-8 shadow-2xl">
      <div class="text-center mb-6">
        <h1 class="text-2xl font-bold text-white tracking-tight">Affiliate Sign In</h1>
        <p class="text-sm text-gray-400 mt-1">Access your affiliate portal or check approval status</p>
      </div>

      <div id="alertBox" class="hidden mb-4 p-3 rounded-lg text-xs font-medium"></div>

      <form id="signinForm" class="space-y-4">
        <div>
          <label class="block text-xs font-medium text-gray-300 mb-1">Email Address</label>
          <input type="email" name="email" required placeholder="tomiwa@example.com"
            class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
        </div>
        <div>
          <label class="block text-xs font-medium text-gray-300 mb-1">Password</label>
          <input type="password" name="password" required placeholder="••••••••"
            class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
        </div>

        <button type="submit" id="submitBtn"
          class="w-full py-2.5 rounded-lg bg-emerald-500 hover:bg-emerald-400 text-black font-semibold text-sm transition">
          Sign In
        </button>
      </form>

      <p class="text-center text-xs text-gray-400 mt-6">
        Don't have an affiliate account? <a href="/signup" class="text-emerald-400 hover:underline">Apply now</a>
      </p>
    </div>

    <script>
      const form = document.getElementById('signinForm');
      const alertBox = document.getElementById('alertBox');
      const submitBtn = document.getElementById('submitBtn');

      form.addEventListener('submit', async (e) => {
        e.preventDefault();
        submitBtn.disabled = true;
        submitBtn.innerText = 'Signing in...';
        alertBox.className = 'hidden';

        const formData = new FormData(form);
        const data = Object.fromEntries(formData.entries());

        try {
          const res = await fetch('/auth/signin', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' },
            body: JSON.stringify(data)
          });
          const result = await res.json();
          if (res.ok) {
            localStorage.setItem('affiliate_token', result.token);
            const user = result.user;
            if (user.role === 'admin') {
              alertBox.className = 'mb-4 p-4 rounded-xl bg-blue-950/60 border border-blue-600/50 text-blue-200 text-xs block';
              alertBox.innerHTML = `
                <div class="font-bold text-sm text-blue-300">Signed In as Admin</div>
                <p class="mt-1">Welcome back, ${user.name}.</p>
                <div class="mt-3"><a href="/admin/approvals" class="px-3 py-1.5 bg-blue-500 text-black font-semibold rounded text-xs">Go to Admin Approvals Portal</a></div>
              `;
            } else if (user.status === 'pending') {
              alertBox.className = 'mb-4 p-4 rounded-xl bg-amber-950/60 border border-amber-600/50 text-amber-200 text-xs block';
              alertBox.innerHTML = `
                <div class="font-bold text-sm text-amber-300">Account Pending Admin Approval</div>
                <p class="mt-1">Hello ${user.name}. Your sign-up is recorded, but sign-ups do not become members of the affiliate program until approved by the admin.</p>
                <p class="mt-1 text-gray-400">Please check back once the administrator has approved your application.</p>
              `;
            } else if (user.status === 'rejected') {
              alertBox.className = 'mb-4 p-4 rounded-xl bg-red-950/60 border border-red-700/50 text-red-200 text-xs block';
              alertBox.innerHTML = `
                <div class="font-bold text-sm text-red-300">Application Not Approved</div>
                <p class="mt-1">Reason: ${user.rejectionReason || 'Application was declined.'}</p>
              `;
            } else {
              alertBox.className = 'mb-4 p-4 rounded-xl bg-emerald-950/60 border border-emerald-600/50 text-emerald-200 text-xs block';
              alertBox.innerHTML = `
                <div class="font-bold text-sm text-emerald-300">Welcome, ${user.name}!</div>
                <p class="mt-1">You are an approved member of the affiliate program.</p>
                <p class="mt-1 text-xs text-gray-300">Affiliate ID: ${user.affiliateId || 'Active'}</p>
              `;
            }
          } else {
            alertBox.className = 'mb-4 p-3 rounded-lg bg-red-950/60 border border-red-700 text-red-300 text-xs block';
            alertBox.innerText = result.message || 'Sign in failed.';
          }
        } catch (err) {
          alertBox.className = 'mb-4 p-3 rounded-lg bg-red-950/60 border border-red-700 text-red-300 text-xs block';
          alertBox.innerText = 'Network error. Please try again.';
        } finally {
          submitBtn.disabled = false;
          submitBtn.innerText = 'Sign In';
        }
      });
    </script>
    """
    return render_template_string(HTML_PAGE_TEMPLATE, title="Sign In", content=content)


@auth_bp.route("/admin/approvals", methods=["GET"])
def admin_approvals_page():
    content = """
    <div class="w-full max-w-4xl bg-gray-900 border border-gray-800 rounded-2xl p-6 sm:p-8 shadow-2xl">
      <div class="flex flex-col sm:flex-row sm:items-center justify-between pb-6 border-b border-gray-800 gap-4">
        <div>
          <h1 class="text-xl sm:text-2xl font-bold text-white tracking-tight">Admin Approval Portal</h1>
          <p class="text-xs sm:text-sm text-gray-400 mt-1">Review pending affiliate sign-ups and grant program membership</p>
        </div>
        <div id="adminBadge" class="text-xs bg-gray-800 text-gray-400 px-3 py-1.5 rounded-lg border border-gray-700">
          Checking credentials...
        </div>
      </div>

      <div id="authRequired" class="hidden my-8 p-6 text-center border border-dashed border-gray-800 rounded-xl">
        <p class="text-sm text-gray-300 mb-4">Admin authentication required to access this portal.</p>
        <a href="/signin" class="px-4 py-2 bg-emerald-500 hover:bg-emerald-400 text-black font-semibold text-xs rounded-lg transition inline-block">
          Sign In as Admin
        </a>
      </div>

      <div id="tableContainer" class="hidden mt-6">
        <div class="flex items-center justify-between mb-4">
          <div class="flex gap-2">
            <button onclick="loadAffiliates('pending')" class="px-3 py-1.5 text-xs font-semibold rounded-lg bg-emerald-500/20 text-emerald-300 border border-emerald-500/40">
              Pending Approvals
            </button>
            <button onclick="loadAffiliates('all')" class="px-3 py-1.5 text-xs font-semibold rounded-lg bg-gray-800 text-gray-400 hover:text-white border border-gray-700">
              All Affiliates
            </button>
          </div>
          <button onclick="loadAffiliates()" class="text-xs text-gray-400 hover:text-emerald-400">Refresh</button>
        </div>

        <div class="overflow-x-auto">
          <table class="w-full text-left text-xs">
            <thead>
              <tr class="border-b border-gray-800 text-gray-400 uppercase tracking-wider">
                <th class="py-3 px-3">Applicant</th>
                <th class="py-3 px-3">Status</th>
                <th class="py-3 px-3">Store Sync</th>
                <th class="py-3 px-3">Applied</th>
                <th class="py-3 px-3 text-right">Actions</th>
              </tr>
            </thead>
            <tbody id="affiliatesTable" class="divide-y divide-gray-800/60">
              <tr><td colspan="5" class="py-8 text-center text-gray-500">Loading applications...</td></tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>

    <script>
      let currentFilter = 'pending';

      async function checkAuth() {
        const token = localStorage.getItem('affiliate_token');
        try {
          const res = await fetch('/auth/me', {
            headers: token ? { 'Authorization': `Bearer ${token}` } : {}
          });
          const data = await res.json();
          if (res.ok && data.user && data.user.role === 'admin') {
            document.getElementById('adminBadge').innerHTML = `<span class="text-emerald-400 font-semibold">Logged in:</span> ${data.user.email}`;
            document.getElementById('tableContainer').classList.remove('hidden');
            loadAffiliates(currentFilter);
          } else {
            document.getElementById('authRequired').classList.remove('hidden');
            document.getElementById('adminBadge').innerHTML = `<span class="text-amber-400">Not Authenticated</span>`;
          }
        } catch (e) {
          document.getElementById('authRequired').classList.remove('hidden');
        }
      }

      async function loadAffiliates(status = currentFilter) {
        currentFilter = status;
        const token = localStorage.getItem('affiliate_token');
        const res = await fetch(`/admin/affiliates?status=${status}`, {
          headers: token ? { 'Authorization': `Bearer ${token}` } : {}
        });
        const data = await res.json();
        const tbody = document.getElementById('affiliatesTable');
        if (!data.affiliates || data.affiliates.length === 0) {
          tbody.innerHTML = `<tr><td colspan="5" class="py-8 text-center text-gray-500">No ${status} affiliate applications found.</td></tr>`;
          return;
        }

        tbody.innerHTML = data.affiliates.map(a => `
          <tr class="hover:bg-gray-800/30">
            <td class="py-3 px-3">
              <div class="font-medium text-white">${a.name}</div>
              <div class="text-gray-400 text-xs">${a.email} ${a.phone ? '• ' + a.phone : ''}</div>

            </td>
          </tr>
        `).join('');
      }

      async function approve(id) {
        if (!confirm('Approve this affiliate application? This will make them a member of the affiliate program and sync to the store.')) return;
        const token = localStorage.getItem('affiliate_token');
        const res = await fetch(`/admin/affiliates/${id}/approve`, {
          method: 'POST',
          headers: token ? { 'Authorization': `Bearer ${token}` } : {}
        });
        if (res.ok) {
          loadAffiliates(currentFilter);
        } else {
          const err = await res.json();
          alert(err.message || 'Approval failed.');
        }
      }

      async function reject(id) {
        const reason = prompt('Reason for rejection:');
        if (reason === null) return;
        const token = localStorage.getItem('affiliate_token');
        const res = await fetch(`/admin/affiliates/${id}/reject`, {
          method: 'POST',
          headers: { 'Content-Type': 'application/json', ...(token ? { 'Authorization': `Bearer ${token}` } : {}) },
          body: JSON.stringify({ reason })
        });
        if (res.ok) {
          loadAffiliates(currentFilter);
        } else {
          const err = await res.json();
          alert(err.message || 'Rejection failed.');
        }
      }

      checkAuth();
    </script>
    """
    return render_template_string(HTML_PAGE_TEMPLATE, title="Admin Approvals", content=content)


# --------------------------------------------------------------------------- #
# Campaigns
# --------------------------------------------------------------------------- #

def _parse_date_input(value):
    """A date or date-and-time as a browser submits it.

    Deliberately not `affiliate_service._parse_dt`, which only understands
    the two shapes Odoo sends. A date field posts "2026-10-10" and a
    datetime-local field posts "2026-10-10T14:30", neither of which that one
    accepts — it would return None and the campaign would silently have no
    start date.

    A bare date means the start of that day. Anything without a timezone is
    taken as UTC, which is what the rest of this service stores.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _campaign_from_body(campaign: Campaign, body: dict) -> str | None:
    """Apply a submitted campaign, or return why it cannot be applied."""
    name = (body.get("name") or "").strip()
    if not name:
        return "Give the campaign a name."
    reward_type = (body.get("rewardType") or "percent").strip()
    if reward_type not in ("percent", "fixed"):
        return "A reward is either a percentage of the sale or a fixed amount per item."
    try:
        reward_value = float(body.get("rewardValue") or 0)
    except (TypeError, ValueError):
        return "The reward must be a number."
    if reward_value <= 0:
        return "The reward must be more than zero."
    if reward_type == "percent" and reward_value > 100:
        return "A percentage reward cannot be over 100%."

    starts = _parse_date_input(body.get("startsAt"))
    ends = _parse_date_input(body.get("endsAt"))
    if starts and ends and ends < starts:
        return "The campaign cannot end before it starts."

    products = body.get("products")
    if not isinstance(products, list) or not products:
        return "Choose at least one product for this campaign to reward."

    campaign.name = name[:255]
    campaign.description = (body.get("description") or "").strip()[:1000] or None
    campaign.reward_type = reward_type
    campaign.reward_value = reward_value
    campaign.starts_at = starts
    campaign.ends_at = ends
    if "active" in body:
        campaign.active = bool(body.get("active"))

    campaign.products.clear()
    for row in products:
        if not isinstance(row, dict):
            continue
        product_id = row.get("productId")
        tmpl_id = row.get("productTmplId")
        if product_id is None and tmpl_id is None:
            continue
        campaign.products.append(CampaignProduct(
            product_odoo_id=int(product_id) if product_id is not None else None,
            product_tmpl_id=int(tmpl_id) if tmpl_id is not None else None,
            name=(row.get("name") or "")[:512] or None,
        ))
    if not campaign.products:
        return "None of those products could be used."
    return None


@auth_bp.route("/admin/campaigns", methods=["GET"])
@require_admin
def admin_list_campaigns_route():
    rows = Campaign.query.order_by(Campaign.id.desc()).all()
    return jsonify({"campaigns": [c.as_dict() for c in rows]}), 200


@auth_bp.route("/admin/campaigns", methods=["POST"])
@require_admin
def admin_create_campaign_route():
    body = request.get_json(silent=True) or {}
    campaign = Campaign()
    problem = _campaign_from_body(campaign, body)
    if problem:
        return jsonify({"error": "invalid", "message": problem}), 400
    db.session.add(campaign)
    db.session.commit()
    log.info("Campaign %s created (%s %s)", campaign.name,
             campaign.reward_value, campaign.reward_type)
    # A campaign nobody is told about changes nobody's behaviour. One that
    # starts later is announced by the sweep on the affiliate page instead.
    announce_live_campaigns()
    return jsonify({"campaign": campaign.as_dict()}), 201


@auth_bp.route("/admin/campaigns/<int:campaign_id>", methods=["PUT"])
@require_admin
def admin_update_campaign_route(campaign_id: int):
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({"error": "not_found", "message": "No such campaign."}), 404
    problem = _campaign_from_body(campaign, request.get_json(silent=True) or {})
    if problem:
        return jsonify({"error": "invalid", "message": problem}), 400
    db.session.commit()
    # Said plainly in the response: an edit changes what happens next, and
    # never what has already been earned.
    return jsonify({
        "campaign": campaign.as_dict(),
        "note": "Earnings already recorded keep the terms they were sold under.",
    }), 200


@auth_bp.route("/admin/campaigns/<int:campaign_id>", methods=["DELETE"])
@require_admin
def admin_stop_campaign_route(campaign_id: int):
    """Switch a campaign off. Never deletes it.

    Earnings point at the campaign that paid them, and that history has to
    stay readable, so stopping is a flag rather than a removal.
    """
    campaign = db.session.get(Campaign, campaign_id)
    if not campaign:
        return jsonify({"error": "not_found", "message": "No such campaign."}), 404
    campaign.active = False
    db.session.commit()
    return jsonify({"campaign": campaign.as_dict()}), 200


@auth_bp.route("/affiliate/product-rewards", methods=["GET"])
@require_approved_affiliate
def affiliate_product_rewards_route():
    """What each campaign product pays, keyed by product id.

    Separate from the campaigns list because the product grid needs it per
    product, and the amount depends on each product's own price.
    """
    return jsonify({"rewards": rewards_by_product()}), 200


@auth_bp.route("/affiliate/campaigns", methods=["GET"])
@require_approved_affiliate
def affiliate_campaigns_route():
    """What is on offer right now. A campaign nobody knows about changes nothing."""
    # Catches a campaign that was scheduled and has since started; there is
    # no clock on this service to do it on the hour.
    announce_live_campaigns()
    return jsonify({"campaigns": [c.as_dict() for c in live_campaigns()]}), 200
