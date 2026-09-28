"""Authentication and affiliate approval service.

Handles:
  * Affiliate sign-up (creates user & affiliate profile in 'pending' status).
    Sign-ups do NOT become members of the affiliate program until approved by admin.
  * User sign-in (email + password verification, issuing JWTs).
  * Single admin account creation and seeding.
  * Admin affiliate review (approving or rejecting pending sign-ups).
  * Approved affiliate access control.
"""

from datetime import datetime, timedelta, timezone
from functools import wraps
import logging
import re
from typing import Optional, Tuple

from flask import g, jsonify, request
import jwt
from werkzeug.security import check_password_hash, generate_password_hash

from affiliate_service import _push_affiliate
from config import Config
from models import Affiliate, User, db, utcnow

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------- #
# JWT Token Helpers
# --------------------------------------------------------------------------- #

def generate_token(user: User) -> str:
    """Generate a JWT token for the given user."""
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "user_id": user.id,
        "email": user.email,
        "name": user.name,
        "role": user.role,
        "status": user.status,
        "is_member": user.is_member,
        "iat": now,
        "exp": now + timedelta(hours=Config.JWT_EXPIRY_HOURS),
    }
    return jwt.encode(payload, Config.JWT_SECRET, algorithm="HS256")


def decode_token(token: str) -> dict:
    """Decode and verify a JWT. Raises jwt exceptions on failure."""
    return jwt.decode(token, Config.JWT_SECRET, algorithms=["HS256"])


def get_current_user() -> Optional[User]:
    """Resolve the current user from Authorization header or cookie/session.
    
    Checks:
      1. 'Authorization: Bearer <token>' header
      2. 'token' query parameter (useful for web links)
      3. 'affiliate_token' cookie (for built-in web page navigation)
    """
    token = None
    auth_header = request.headers.get("Authorization", "").strip()
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
    elif "token" in request.args:
        token = request.args.get("token")
    elif "affiliate_token" in request.cookies:
        token = request.cookies.get("affiliate_token")

    if not token:
        return None

    try:
        payload = decode_token(token)
        raw_id = payload.get("user_id") or payload.get("sub")
        if not raw_id:
            return None
        return db.session.get(User, int(raw_id))
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Auth Decorators
# --------------------------------------------------------------------------- #

def require_auth(f):
    """Requires an authenticated user (admin or affiliate)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({
                "error": "unauthorized",
                "message": "Authentication required. Please provide a valid Bearer token.",
            }), 401
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Requires the authenticated user to be an admin."""
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({
                "error": "unauthorized",
                "message": "Admin authentication required.",
            }), 401
        if not user.is_admin:
            return jsonify({
                "error": "forbidden",
                "message": "Admin privileges required for this action.",
            }), 403
        g.current_user = user
        return f(*args, **kwargs)
    return decorated


def require_approved_affiliate(f):
    """Requires an approved affiliate member (or admin).
    
    Sign-ups in 'pending' status are blocked with a clear message that their
    application is awaiting admin review before they become program members.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        user = get_current_user()
        if not user:
            return jsonify({
                "error": "unauthorized",
                "message": "Authentication required.",
            }), 401

        if user.is_admin:
            g.current_user = user
            return f(*args, **kwargs)

        if user.status == "pending":
            return jsonify({
                "error": "pending_approval",
                "message": (
                    "Your affiliate application is pending admin approval. "
                    "Sign-ups do not become members of the affiliate program until approved by the admin."
                ),
                "status": "pending",
                "isMember": False,
            }), 403

        if user.status == "rejected":
            return jsonify({
                "error": "application_rejected",
                "message": f"Your affiliate application was rejected: {user.rejection_reason or 'No reason provided.'}",
                "status": "rejected",
                "isMember": False,
            }), 403

        if user.status != "approved":
            return jsonify({
                "error": "forbidden",
                "message": f"Affiliate account is inactive ({user.status}).",
                "status": user.status,
                "isMember": False,
            }), 403

        g.current_user = user
        return f(*args, **kwargs)
    return decorated


# --------------------------------------------------------------------------- #
# Core Auth & Affiliate Flows
# --------------------------------------------------------------------------- #

def signup_affiliate(
    name: str,
    email: str,
    password: str,
    phone: str = "",
    promotional_channel: str = "",
    channel_url: str = "",
    audience_size: str = "",
    why_join: str = "",
) -> Tuple[User, Affiliate]:
    """Register a new affiliate applicant.
    
    Notice: Sign-ups do NOT become members of the affiliate program until
    they have been approved by the admin. The user and affiliate records
    are created with status='pending', and the affiliate is NOT pushed to Odoo
    until an admin explicitly approves them.
    """
    cleaned_name = (name or "").strip()
    if not cleaned_name:
        raise ValueError("Name is required.")

    cleaned_email = (email or "").strip().lower()
    if not cleaned_email or not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", cleaned_email):
        raise ValueError("A valid email address is required.")

    if not password or len(password) < 6:
        raise ValueError("Password must be at least 6 characters long.")

    existing_user = User.query.filter_by(email=cleaned_email).first()
    if existing_user:
        raise ValueError("An account with this email address already exists.")

    cleaned_phone = (phone or "").strip() or None
    cleaned_channel = (promotional_channel or "").strip() or None
    cleaned_url = (channel_url or "").strip() or None
    cleaned_size = str(audience_size or "").strip() or None
    cleaned_why = (why_join or "").strip() or None

    # Create affiliate row in pending state
    affiliate = Affiliate(
        name=cleaned_name,
        email=cleaned_email,
        phone=cleaned_phone,
        status="pending",
    )
    db.session.add(affiliate)
    db.session.flush()

    affiliate.backend_ref = f"AFF-{affiliate.id}"

    # Create user row linked to the affiliate with application details
    user = User(
        name=cleaned_name,
        email=cleaned_email,
        phone=cleaned_phone,
        role="affiliate",
        status="pending",
        promotional_channel=cleaned_channel,
        channel_url=cleaned_url,
        audience_size=cleaned_size,
        why_join=cleaned_why,
        affiliate_id=affiliate.id,
    )
    user.set_password(password)
    db.session.add(user)

    db.session.commit()
    log.info("New affiliate applicant registered: %s (%s), status=pending", user.name, user.email)
    return user, affiliate


def signin_user(email: str, password: str) -> Tuple[User, str]:
    """Verify user credentials and return the user and JWT token."""
    cleaned_email = (email or "").strip().lower()
    if not cleaned_email or not password:
        raise ValueError("Email and password are required.")

    user = User.query.filter_by(email=cleaned_email).first()
    if not user or not user.check_password(password):
        raise ValueError("Invalid email or password.")

    token = generate_token(user)
    return user, token


def ensure_admin_account(
    email: Optional[str] = None,
    password: Optional[str] = None,
    name: Optional[str] = None,
) -> User:
    """Ensure the single admin account exists in the database.
    
    If no admin user exists, one is created using the provided details
    or falling back to Config settings.
    """
    admin_email = (email or Config.ADMIN_EMAIL).strip().lower()
    admin_pass = password or Config.ADMIN_PASSWORD
    admin_name = (name or Config.ADMIN_NAME).strip()

    admin = User.query.filter_by(role="admin").first()
    if admin:
        return admin

    # Check if a user with that email already exists
    existing = User.query.filter_by(email=admin_email).first()
    if existing:
        existing.role = "admin"
        existing.status = "active"
        if password:
            existing.set_password(admin_pass)
        db.session.commit()
        log.info("Promoted existing user %s to admin", admin_email)
        return existing

    admin = User(
        email=admin_email,
        name=admin_name,
        role="admin",
        status="active",
    )
    admin.set_password(admin_pass)
    db.session.add(admin)
    db.session.commit()
    log.info("Created single admin account: %s", admin_email)
    return admin


def approve_affiliate(affiliate_or_user_id: int) -> Tuple[User, Affiliate]:
    """Admin approval of an affiliate sign-up.
    
    Marks user and affiliate status as 'approved', sets backend_ref if needed,
    and pushes the new affiliate to the CyberVilla Odoo store.
    """
    # Can be called with either user id or affiliate id
    user = User.query.filter(
        (User.id == affiliate_or_user_id) | (User.affiliate_id == affiliate_or_user_id)
    ).first()

    if not user:
        raise ValueError("Affiliate user not found.")

    affiliate = user.affiliate
    if not affiliate:
        affiliate = db.session.get(Affiliate, user.affiliate_id) if user.affiliate_id else None

    if not affiliate:
        # Fallback: create affiliate profile if missing
        affiliate = Affiliate(
            name=user.name,
            email=user.email,
            phone=user.phone,
            status="pending",
        )
        db.session.add(affiliate)
        db.session.flush()
        affiliate.backend_ref = f"AFF-{affiliate.id}"
        user.affiliate_id = affiliate.id

    user.status = "approved"
    user.rejection_reason = None
    affiliate.status = "approved"

    # Push to store now that admin has approved them as a member
    _push_affiliate(affiliate)

    db.session.commit()
    log.info("Affiliate approved by admin: %s (AFF-%s)", user.email, affiliate.id)
    return user, affiliate


def reject_affiliate(affiliate_or_user_id: int, reason: str = "") -> Tuple[User, Affiliate]:
    """Admin rejection of an affiliate application."""
    user = User.query.filter(
        (User.id == affiliate_or_user_id) | (User.affiliate_id == affiliate_or_user_id)
    ).first()

    if not user:
        raise ValueError("Affiliate user not found.")

    affiliate = user.affiliate
    user.status = "rejected"
    user.rejection_reason = reason.strip() or "Application did not meet requirements."

    if affiliate:
        affiliate.status = "rejected"

    db.session.commit()
    log.info("Affiliate rejected by admin: %s. Reason: %s", user.email, user.rejection_reason)
    return user, affiliate
