"""Authentication, affiliate membership, and admin review routes.

Provides:
  * Public sign-up and sign-in API endpoints (/auth/signup, /auth/signin, /auth/me)
  * Affiliate member-guarded endpoints (/affiliate/*)
  * Admin affiliate review & approval endpoints (/admin/affiliates/*)
  * Built-in browser-friendly sign-up & sign-in HTML pages (/signup, /signin, /admin/approvals)
"""

import logging
from flask import Blueprint, g, jsonify, make_response, render_template_string, request
from sqlalchemy import func

from affiliate_service import create_link, earnings_by_link
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
from models import Affiliate, AffiliateLink, Earning, User, db

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
            promotional_channel=body.get("promotionalChannel") or body.get("promotional_channel") or body.get("channel", ""),
            channel_url=body.get("channelUrl") or body.get("channel_url") or body.get("link", ""),
            audience_size=body.get("audienceSize") or body.get("audience_size", ""),
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


@auth_bp.route("/affiliate/links", methods=["GET"])
@require_approved_affiliate
def list_affiliate_links_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"links": []}), 200
    links = AffiliateLink.query.filter_by(affiliate_id=user.affiliate.id).order_by(AffiliateLink.id.desc()).all()
    # Earnings are looked up for this affiliate only, so one affiliate's links
    # can never carry another's figures.
    stats = earnings_by_link(user.affiliate.id)
    payload = []
    for link in links:
        row = link.as_dict()
        row.update(stats.get(link.id) or {"sales": 0, "earnings": 0.0, "currency": None})
        payload.append(row)
    return jsonify({"links": payload}), 200


@auth_bp.route("/affiliate/links", methods=["POST"])
@require_approved_affiliate
def create_affiliate_link_route():
    user: User = g.current_user
    if not user.affiliate:
        return jsonify({"error": "not_found", "message": "Affiliate profile missing."}), 404

    body = request.get_json(silent=True) or {}
    link = create_link(
        affiliate=user.affiliate,
        markup_percent=body.get("markupPercent", 0),
        label=body.get("label", ""),
    )
    # A link nobody has clicked yet, stated rather than left for the caller to guess.
    return jsonify({"link": {**link.as_dict(), "sales": 0, "earnings": 0.0, "currency": None}}), 201


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
            "promotionalChannel": u.promotional_channel or "",
            "channelUrl": u.channel_url or "",
            "audienceSize": u.audience_size or "",
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
            func.coalesce(func.sum(Earning.earning), 0),
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
            "promotionalChannel": user.promotional_channel or "",
            "channelUrl": user.channel_url or "",
            "audienceSize": user.audience_size or "",
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
            <label class="block text-xs font-medium text-gray-300 mb-1">Promotional Channel *</label>
            <select name="promotionalChannel" required
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white text-sm focus:outline-none focus:border-emerald-500">
              <option value="">Select your channel...</option>
              <option value="Instagram">Instagram</option>
              <option value="TikTok">TikTok</option>
              <option value="YouTube">YouTube</option>
              <option value="Blog / Website">Blog / Website</option>
              <option value="WhatsApp Community">WhatsApp Community</option>
              <option value="Twitter/X">Twitter/X</option>
              <option value="Other">Other</option>
            </select>
          </div>
          <div>
            <label class="block text-xs font-medium text-gray-300 mb-1">Link to Channel *</label>
            <input type="url" name="channelUrl" required placeholder="https://instagram.com/yourhandle"
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
          </div>
        </div>

        <div class="grid grid-cols-1 sm:grid-cols-2 gap-4">
          <div>
            <label class="block text-xs font-medium text-gray-300 mb-1">Audience Size *</label>
            <input type="text" name="audienceSize" required placeholder="e.g. 10,000 followers"
              class="w-full px-3.5 py-2.5 rounded-lg bg-gray-800 border border-gray-700 text-white placeholder-gray-500 text-sm focus:outline-none focus:border-emerald-500">
          </div>
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
              ${a.promotionalChannel ? `
                <div class="mt-1 text-[11px] text-emerald-300">
                  <span class="font-semibold text-gray-300">Channel:</span> ${a.promotionalChannel}
                  ${a.channelUrl ? `(<a href="${a.channelUrl}" target="_blank" class="underline text-emerald-400 hover:text-emerald-300">Link</a>)` : ''}
                  ${a.audienceSize ? `• <span class="font-semibold text-gray-300">Audience:</span> ${a.audienceSize}` : ''}
                </div>
              ` : ''}
              ${a.whyJoin ? `
                <div class="mt-1 text-[11px] text-gray-400 italic bg-gray-800/40 p-1.5 rounded max-w-md">
                  "${a.whyJoin}"
                </div>
              ` : ''}
            </td>
            <td class="py-3 px-3">
              <span class="px-2 py-0.5 rounded text-[11px] font-semibold ${
                a.status === 'approved' ? 'bg-emerald-950 text-emerald-400 border border-emerald-800' :
                a.status === 'pending' ? 'bg-amber-950 text-amber-400 border border-amber-800' :
                'bg-red-950 text-red-400 border border-red-800'
              }">${a.status}</span>
            </td>
            <td class="py-3 px-3 text-gray-400">
              ${a.synced ? '<span class="text-emerald-400">Synced to Odoo</span>' : '<span class="text-gray-500">Not synced</span>'}
            </td>
            <td class="py-3 px-3 text-gray-400">${a.joinedAt ? new Date(a.joinedAt).toLocaleDateString() : 'N/A'}</td>
            <td class="py-3 px-3 text-right">
              ${a.status === 'pending' ? `
                <div class="flex justify-end gap-2">
                  <button onclick="approve(${a.id})" class="px-2.5 py-1 bg-emerald-500 hover:bg-emerald-400 text-black font-semibold rounded text-xs transition">
                    Approve
                  </button>
                  <button onclick="reject(${a.id})" class="px-2.5 py-1 bg-red-900/60 hover:bg-red-800 text-red-200 font-semibold rounded text-xs transition">
                    Reject
                  </button>
                </div>
              ` : `
                <span class="text-gray-500 text-xs">${a.status}</span>
              `}
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
