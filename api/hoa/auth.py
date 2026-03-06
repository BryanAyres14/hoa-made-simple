"""
HOA Ledger — Authentication System
====================================
JWT-based auth for managers, board members, and homeowners.
Each HOA is a fully isolated tenant — logins are scoped per organization.

Super-admin (JARVIS/Bryan) gets a platform-level JWT that can access ANY HOA.
"""
from flask import Blueprint, request, jsonify, g
from datetime import datetime, timedelta, timezone
from lib.database import get_db
import hashlib
import hmac
import json
import base64
import secrets
import os

auth_bp = Blueprint("hoa_auth", __name__)

# ──────────────────────────────────────────────
# JWT CONFIG
# ──────────────────────────────────────────────
JWT_SECRET = os.environ.get("HOA_JWT_SECRET", "hoa-ledger-jwt-secret-change-in-production")
JWT_EXPIRY_HOURS = 24
JWT_REFRESH_DAYS = 30


# ──────────────────────────────────────────────
# JWT HELPERS (no external dependency)
# ──────────────────────────────────────────────
def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")


def _b64url_decode(s: str) -> bytes:
    padding = 4 - len(s) % 4
    s += "=" * padding
    return base64.urlsafe_b64decode(s)


def _create_jwt(payload: dict) -> str:
    """Create a JWT token with HS256 signing."""
    header = {"alg": "HS256", "typ": "JWT"}
    header_b64 = _b64url_encode(json.dumps(header).encode())
    payload_b64 = _b64url_encode(json.dumps(payload, default=str).encode())
    message = f"{header_b64}.{payload_b64}"
    signature = hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
    sig_b64 = _b64url_encode(signature)
    return f"{message}.{sig_b64}"


def _verify_jwt(token: str) -> dict | None:
    """Verify and decode a JWT token. Returns payload or None."""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return None
        header_b64, payload_b64, sig_b64 = parts
        # Verify signature
        message = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(JWT_SECRET.encode(), message.encode(), hashlib.sha256).digest()
        actual_sig = _b64url_decode(sig_b64)
        if not hmac.compare_digest(expected_sig, actual_sig):
            return None
        # Decode payload
        payload = json.loads(_b64url_decode(payload_b64))
        # Check expiry
        if "exp" in payload:
            exp = datetime.fromisoformat(payload["exp"])
            if exp < datetime.now(timezone.utc):
                return None
        return payload
    except Exception:
        return None


def _hash_password(password: str, salt: str = None) -> tuple:
    """Hash a password with salt using SHA-256 + PBKDF2."""
    if salt is None:
        salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000)
    return _b64url_encode(hashed), salt


def _verify_password(password: str, hashed: str, salt: str) -> bool:
    """Verify a password against a hash."""
    computed, _ = _hash_password(password, salt)
    return hmac.compare_digest(computed, hashed)


# ──────────────────────────────────────────────
# PUBLIC AUTH ENDPOINTS
# ──────────────────────────────────────────────

@auth_bp.route("/api/hoa/auth/signup", methods=["POST"])
def signup():
    """
    Register a new HOA user.
    Body: {
        "hoa_id": "...",
        "email": "...",
        "password": "...",
        "first_name": "...",
        "last_name": "...",
        "role": "member"  (default, managers created via admin)
    }
    """
    try:
        body = request.get_json()
        hoa_id = body.get("hoa_id")
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        first_name = body.get("first_name", "")
        last_name = body.get("last_name", "")
        role = body.get("role", "member")

        if not hoa_id or not email or not password:
            return jsonify({"error": "hoa_id, email, and password are required"}), 400

        if len(password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400

        # Check if HOA exists
        org = get_db().table("hoa_organizations").select("id, name").eq(
            "id", hoa_id
        ).maybe_single().execute()
        if not org.data:
            return jsonify({"error": "HOA organization not found"}), 404

        # Check if email already registered for this HOA
        existing = get_db().table("hoa_users").select("id").eq(
            "hoa_id", hoa_id
        ).eq("email", email).maybe_single().execute()
        if existing.data:
            return jsonify({"error": "Email already registered for this HOA"}), 409

        # Only allow member signup via this endpoint — managers/board created by admins
        if role not in ("member",):
            role = "member"

        # Hash password
        pw_hash, pw_salt = _hash_password(password)

        # Create user
        user = get_db().table("hoa_users").insert({
            "hoa_id": hoa_id,
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "role": role,
            "portal_activated": True,
            "password_hash": pw_hash,
            "password_salt": pw_salt,
        }).execute()

        user_data = user.data[0]

        # Link to member record if exists
        member = get_db().table("hoa_members").select("id").eq(
            "hoa_id", hoa_id
        ).eq("email", email).maybe_single().execute()
        if member.data:
            get_db().table("hoa_members").update({
                "user_id": user_data["id"]
            }).eq("id", member.data["id"]).execute()

        # Generate tokens
        access_token = _create_jwt({
            "user_id": user_data["id"],
            "hoa_id": hoa_id,
            "email": email,
            "role": role,
            "is_admin": False,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat(),
        })
        refresh_token = secrets.token_urlsafe(48)

        # Store refresh token
        get_db().table("hoa_users").update({
            "refresh_token": refresh_token,
            "last_login_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", user_data["id"]).execute()

        _log_audit(hoa_id, "user_signup", {"email": email, "role": role})

        return jsonify({
            "user": {
                "id": user_data["id"],
                "email": email,
                "first_name": first_name,
                "last_name": last_name,
                "role": role,
                "hoa_id": hoa_id,
                "hoa_name": org.data["name"],
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": JWT_EXPIRY_HOURS * 3600,
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/login", methods=["POST"])
def login():
    """
    Login to an HOA portal.
    Supports two flows:

    FLOW 1 — Direct login (hoa_id known):
        Body: { "hoa_id": "...", "email": "...", "password": "..." }
        Returns: access_token + user data

    FLOW 2 — Multi-HOA resolution (management company):
        Body: { "email": "...", "password": "..." }  (no hoa_id)
        If email exists in 1 HOA → auto-login
        If email exists in 2+ HOAs → returns organizations list (pick one)
        Then call again with hoa_id to complete login.
    """
    try:
        body = request.get_json()
        hoa_id = body.get("hoa_id")
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")

        if not email or not password:
            return jsonify({"error": "email and password are required"}), 400

        # ── FLOW 2: No hoa_id — find all orgs this email belongs to ──
        if not hoa_id:
            all_users = get_db().table("hoa_users").select(
                "id, email, first_name, last_name, role, password_hash, password_salt, hoa_id"
            ).eq("email", email).execute()

            if not all_users.data:
                return jsonify({"error": "Invalid email or password"}), 401

            # Verify password against first account (same email = same person, same password)
            first_user = all_users.data[0]
            if not first_user.get("password_hash") or not first_user.get("password_salt"):
                return jsonify({"error": "Account not activated. Contact your HOA manager."}), 401
            if not _verify_password(password, first_user["password_hash"], first_user["password_salt"]):
                return jsonify({"error": "Invalid email or password"}), 401

            # Get all HOA orgs for this email
            hoa_ids = list(set(u["hoa_id"] for u in all_users.data))
            orgs = get_db().table("hoa_organizations").select(
                "id, name, city, state, logo_url, accent_color"
            ).in_("id", hoa_ids).execute()

            org_map = {o["id"]: o for o in orgs.data}

            organizations = []
            for u in all_users.data:
                org = org_map.get(u["hoa_id"], {})
                organizations.append({
                    "hoa_id": u["hoa_id"],
                    "hoa_name": org.get("name", "Unknown"),
                    "city": org.get("city"),
                    "state": org.get("state"),
                    "logo_url": org.get("logo_url"),
                    "accent_color": org.get("accent_color"),
                    "role": u["role"],
                    "user_id": u["id"],
                })

            # If only 1 org, auto-complete login
            if len(organizations) == 1:
                hoa_id = organizations[0]["hoa_id"]
                # Fall through to normal login below
            else:
                # Multiple orgs — return list for user to pick
                return jsonify({
                    "multi_org": True,
                    "message": "Multiple organizations found. Select one to continue.",
                    "organizations": organizations,
                    "email": email,
                })

        # ── FLOW 1: Direct login with hoa_id ──
        user = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, password_hash, password_salt, hoa_id"
        ).eq("hoa_id", hoa_id).eq("email", email).maybe_single().execute()

        if not user.data:
            return jsonify({"error": "Invalid email or password"}), 401

        if not user.data.get("password_hash") or not user.data.get("password_salt"):
            return jsonify({"error": "Account not activated. Please contact your HOA manager."}), 401

        if not _verify_password(password, user.data["password_hash"], user.data["password_salt"]):
            return jsonify({"error": "Invalid email or password"}), 401

        # Get HOA info
        org = get_db().table("hoa_organizations").select("name, logo_url, accent_color").eq(
            "id", hoa_id
        ).single().execute()

        # Check if this email has access to OTHER HOAs too (for org switcher)
        all_orgs = get_db().table("hoa_users").select(
            "hoa_id, role"
        ).eq("email", email).execute()

        other_org_ids = [u["hoa_id"] for u in all_orgs.data if u["hoa_id"] != hoa_id]
        accessible_orgs = []
        if other_org_ids:
            other_orgs = get_db().table("hoa_organizations").select(
                "id, name, logo_url"
            ).in_("id", other_org_ids).execute()
            role_map = {u["hoa_id"]: u["role"] for u in all_orgs.data}
            accessible_orgs = [{
                "hoa_id": o["id"],
                "hoa_name": o["name"],
                "logo_url": o.get("logo_url"),
                "role": role_map.get(o["id"], "member"),
            } for o in other_orgs.data]

        # Generate tokens
        access_token = _create_jwt({
            "user_id": user.data["id"],
            "hoa_id": hoa_id,
            "email": email,
            "role": user.data["role"],
            "is_admin": False,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat(),
        })
        refresh_token = secrets.token_urlsafe(48)

        # Update login timestamp and refresh token
        get_db().table("hoa_users").update({
            "last_login_at": datetime.now(timezone.utc).isoformat(),
            "refresh_token": refresh_token,
        }).eq("id", user.data["id"]).execute()

        # Get linked member record
        member = get_db().table("hoa_members").select("id, unit_id").eq(
            "user_id", user.data["id"]
        ).maybe_single().execute()

        _log_audit(hoa_id, "user_login", {"email": email, "role": user.data["role"]})

        return jsonify({
            "user": {
                "id": user.data["id"],
                "email": email,
                "first_name": user.data["first_name"],
                "last_name": user.data["last_name"],
                "role": user.data["role"],
                "hoa_id": hoa_id,
                "hoa_name": org.data["name"],
                "member_id": member.data["id"] if member.data else None,
                "unit_id": member.data.get("unit_id") if member.data else None,
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": JWT_EXPIRY_HOURS * 3600,
            "other_organizations": accessible_orgs,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/switch-org", methods=["POST"])
def switch_org():
    """
    Switch to a different HOA org (for management companies managing multiple HOAs).
    Requires a valid JWT. Issues a NEW JWT scoped to the target HOA.
    Data stays 100% separate — this just re-authenticates against the other org.

    Body: { "hoa_id": "<target hoa>" }
    """
    try:
        # Verify current JWT
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Authentication required"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = _verify_jwt(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        body = request.get_json()
        target_hoa_id = body.get("hoa_id")
        if not target_hoa_id:
            return jsonify({"error": "hoa_id is required"}), 400

        email = payload.get("email")

        # Look up the user in the TARGET HOA
        target_user = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, hoa_id"
        ).eq("hoa_id", target_hoa_id).eq("email", email).maybe_single().execute()

        if not target_user.data:
            return jsonify({"error": "You don't have access to this organization"}), 403

        # Get target org info
        org = get_db().table("hoa_organizations").select(
            "name, logo_url, accent_color"
        ).eq("id", target_hoa_id).single().execute()

        # Issue new JWT scoped to target HOA
        new_token = _create_jwt({
            "user_id": target_user.data["id"],
            "hoa_id": target_hoa_id,
            "email": email,
            "role": target_user.data["role"],
            "is_admin": False,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat(),
        })
        new_refresh = secrets.token_urlsafe(48)

        get_db().table("hoa_users").update({
            "last_login_at": datetime.now(timezone.utc).isoformat(),
            "refresh_token": new_refresh,
        }).eq("id", target_user.data["id"]).execute()

        # Get all accessible orgs for the switcher
        all_orgs = get_db().table("hoa_users").select("hoa_id, role").eq(
            "email", email
        ).execute()
        other_ids = [u["hoa_id"] for u in all_orgs.data if u["hoa_id"] != target_hoa_id]
        accessible_orgs = []
        if other_ids:
            others = get_db().table("hoa_organizations").select(
                "id, name, logo_url"
            ).in_("id", other_ids).execute()
            role_map = {u["hoa_id"]: u["role"] for u in all_orgs.data}
            accessible_orgs = [{
                "hoa_id": o["id"], "hoa_name": o["name"],
                "logo_url": o.get("logo_url"), "role": role_map.get(o["id"], "member"),
            } for o in others.data]

        _log_audit(target_hoa_id, "org_switch", {"email": email, "from_hoa": payload.get("hoa_id")})

        return jsonify({
            "user": {
                "id": target_user.data["id"],
                "email": email,
                "first_name": target_user.data["first_name"],
                "last_name": target_user.data["last_name"],
                "role": target_user.data["role"],
                "hoa_id": target_hoa_id,
                "hoa_name": org.data["name"],
            },
            "access_token": new_token,
            "refresh_token": new_refresh,
            "expires_in": JWT_EXPIRY_HOURS * 3600,
            "other_organizations": accessible_orgs,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/refresh", methods=["POST"])
def refresh_token():
    """
    Refresh an access token.
    Body: { "refresh_token": "..." }
    """
    try:
        body = request.get_json()
        token = body.get("refresh_token")
        if not token:
            return jsonify({"error": "refresh_token is required"}), 400

        # Find user with this refresh token
        user = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, hoa_id"
        ).eq("refresh_token", token).maybe_single().execute()

        if not user.data:
            return jsonify({"error": "Invalid refresh token"}), 401

        # Generate new tokens
        new_access = _create_jwt({
            "user_id": user.data["id"],
            "hoa_id": user.data["hoa_id"],
            "email": user.data["email"],
            "role": user.data["role"],
            "is_admin": False,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat(),
        })
        new_refresh = secrets.token_urlsafe(48)

        get_db().table("hoa_users").update({
            "refresh_token": new_refresh,
        }).eq("id", user.data["id"]).execute()

        return jsonify({
            "access_token": new_access,
            "refresh_token": new_refresh,
            "expires_in": JWT_EXPIRY_HOURS * 3600,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/logout", methods=["POST"])
def logout():
    """Invalidate refresh token."""
    try:
        body = request.get_json()
        token = body.get("refresh_token")
        if token:
            get_db().table("hoa_users").update({
                "refresh_token": None,
            }).eq("refresh_token", token).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/reset-password", methods=["POST"])
def request_password_reset():
    """
    Request a password reset. Generates a reset token and (optionally) sends email.
    Body: { "hoa_id": "...", "email": "..." }
    """
    try:
        body = request.get_json()
        hoa_id = body.get("hoa_id")
        email = body.get("email", "").strip().lower()

        if not hoa_id or not email:
            return jsonify({"error": "hoa_id and email are required"}), 400

        user = get_db().table("hoa_users").select("id").eq(
            "hoa_id", hoa_id
        ).eq("email", email).maybe_single().execute()

        # Always return success to prevent email enumeration
        if user.data:
            reset_token = secrets.token_urlsafe(32)
            reset_expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
            get_db().table("hoa_users").update({
                "reset_token": reset_token,
                "reset_expires": reset_expires,
            }).eq("id", user.data["id"]).execute()

            # TODO: Send reset email via Resend

        return jsonify({"message": "If an account exists, a reset link has been sent."})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/auth/reset-password/confirm", methods=["POST"])
def confirm_password_reset():
    """
    Confirm password reset with token.
    Body: { "reset_token": "...", "new_password": "..." }
    """
    try:
        body = request.get_json()
        reset_token = body.get("reset_token")
        new_password = body.get("new_password", "")

        if not reset_token or len(new_password) < 8:
            return jsonify({"error": "Valid reset_token and password (8+ chars) required"}), 400

        user = get_db().table("hoa_users").select(
            "id, reset_expires"
        ).eq("reset_token", reset_token).maybe_single().execute()

        if not user.data:
            return jsonify({"error": "Invalid or expired reset token"}), 400

        # Check expiry
        if user.data.get("reset_expires"):
            expires = datetime.fromisoformat(user.data["reset_expires"].replace("Z", "+00:00"))
            if expires < datetime.now(timezone.utc):
                return jsonify({"error": "Reset token has expired"}), 400

        pw_hash, pw_salt = _hash_password(new_password)
        get_db().table("hoa_users").update({
            "password_hash": pw_hash,
            "password_salt": pw_salt,
            "reset_token": None,
            "reset_expires": None,
        }).eq("id", user.data["id"]).execute()

        return jsonify({"message": "Password updated successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# ADMIN / JARVIS LOGIN
# ──────────────────────────────────────────────

@auth_bp.route("/api/hoa/auth/admin/login", methods=["POST"])
def admin_login():
    """
    Super-admin login (JARVIS / Bryan).
    Body: { "email": "...", "password": "..." }
    Returns a platform-level JWT with is_admin=True.
    """
    try:
        body = request.get_json()
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")

        if not email or not password:
            return jsonify({"error": "email and password required"}), 400

        # Look up in admin table
        admin = get_db().table("hoa_platform_admins").select(
            "id, email, name, password_hash, password_salt, role, is_active"
        ).eq("email", email).eq("is_active", True).maybe_single().execute()

        if not admin.data:
            return jsonify({"error": "Invalid credentials"}), 401

        if not _verify_password(password, admin.data["password_hash"], admin.data["password_salt"]):
            return jsonify({"error": "Invalid credentials"}), 401

        # Generate admin JWT — no hoa_id, has is_admin flag
        access_token = _create_jwt({
            "admin_id": admin.data["id"],
            "email": email,
            "role": admin.data["role"],  # super_admin | support
            "is_admin": True,
            "exp": (datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS)).isoformat(),
        })
        refresh_token = secrets.token_urlsafe(48)

        get_db().table("hoa_platform_admins").update({
            "last_login_at": datetime.now(timezone.utc).isoformat(),
            "refresh_token": refresh_token,
        }).eq("id", admin.data["id"]).execute()

        return jsonify({
            "admin": {
                "id": admin.data["id"],
                "email": email,
                "name": admin.data["name"],
                "role": admin.data["role"],
            },
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": JWT_EXPIRY_HOURS * 3600,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# USER MANAGEMENT (by managers)
# ──────────────────────────────────────────────

@auth_bp.route("/api/hoa/<hoa_id>/users", methods=["GET"])
def list_users(hoa_id):
    """List all users for an HOA (managers only)."""
    try:
        result = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, portal_activated, last_login_at, created_at"
        ).eq("hoa_id", hoa_id).order("role").execute()
        return jsonify({"users": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/<hoa_id>/users", methods=["POST"])
def create_user(hoa_id):
    """
    Create a user (manager creating board/member accounts).
    Body: {
        "email": "...",
        "first_name": "...",
        "last_name": "...",
        "role": "manager" | "board" | "member",
        "password": "..." (optional — can set later)
    }
    """
    try:
        body = request.get_json()
        email = body.get("email", "").strip().lower()
        role = body.get("role", "member")
        password = body.get("password")

        if not email:
            return jsonify({"error": "email is required"}), 400

        if role not in ("manager", "board", "member"):
            return jsonify({"error": "role must be manager, board, or member"}), 400

        # Check duplicate
        existing = get_db().table("hoa_users").select("id").eq(
            "hoa_id", hoa_id
        ).eq("email", email).maybe_single().execute()
        if existing.data:
            return jsonify({"error": "Email already registered"}), 409

        user_data = {
            "hoa_id": hoa_id,
            "email": email,
            "first_name": body.get("first_name", ""),
            "last_name": body.get("last_name", ""),
            "role": role,
            "portal_activated": password is not None,
        }

        if password:
            pw_hash, pw_salt = _hash_password(password)
            user_data["password_hash"] = pw_hash
            user_data["password_salt"] = pw_salt

        result = get_db().table("hoa_users").insert(user_data).execute()

        # Link to member if exists
        member = get_db().table("hoa_members").select("id").eq(
            "hoa_id", hoa_id
        ).eq("email", email).maybe_single().execute()
        if member.data:
            get_db().table("hoa_members").update({
                "user_id": result.data[0]["id"]
            }).eq("id", member.data["id"]).execute()

        _log_audit(hoa_id, "user_created", {
            "email": email,
            "role": role,
            "user_id": result.data[0]["id"],
        })

        return jsonify({"user": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/<hoa_id>/users/<user_id>", methods=["PATCH"])
def update_user(hoa_id, user_id):
    """Update a user's role or details."""
    try:
        body = request.get_json()
        allowed = ["first_name", "last_name", "role", "portal_activated", "phone"]
        updates = {k: v for k, v in body.items() if k in allowed}

        if "role" in updates and updates["role"] not in ("manager", "board", "member"):
            return jsonify({"error": "Invalid role"}), 400

        result = get_db().table("hoa_users").update(updates).eq(
            "id", user_id
        ).eq("hoa_id", hoa_id).execute()

        return jsonify({"user": result.data[0] if result.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/<hoa_id>/users/<user_id>/reset-password", methods=["POST"])
def admin_reset_password(hoa_id, user_id):
    """Manager resets a user's password."""
    try:
        body = request.get_json()
        new_password = body.get("password", "")
        if len(new_password) < 8:
            return jsonify({"error": "Password must be at least 8 characters"}), 400

        pw_hash, pw_salt = _hash_password(new_password)
        get_db().table("hoa_users").update({
            "password_hash": pw_hash,
            "password_salt": pw_salt,
            "portal_activated": True,
        }).eq("id", user_id).eq("hoa_id", hoa_id).execute()

        return jsonify({"success": True, "message": "Password reset successfully"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@auth_bp.route("/api/hoa/<hoa_id>/users/<user_id>", methods=["DELETE"])
def delete_user(hoa_id, user_id):
    """Delete a user."""
    try:
        get_db().table("hoa_users").delete().eq(
            "id", user_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# MY PROFILE
# ──────────────────────────────────────────────

@auth_bp.route("/api/hoa/auth/me", methods=["GET"])
def get_me():
    """Get current user profile from JWT."""
    try:
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "No token provided"}), 401

        token = auth_header.split(" ", 1)[1]
        payload = _verify_jwt(token)
        if not payload:
            return jsonify({"error": "Invalid or expired token"}), 401

        if payload.get("is_admin"):
            return jsonify({
                "is_admin": True,
                "admin_id": payload.get("admin_id"),
                "email": payload.get("email"),
                "role": payload.get("role"),
            })

        user = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, hoa_id, last_login_at"
        ).eq("id", payload["user_id"]).single().execute()

        org = get_db().table("hoa_organizations").select(
            "name, logo_url, accent_color"
        ).eq("id", payload["hoa_id"]).single().execute()

        return jsonify({
            "user": user.data,
            "organization": org.data,
            "is_admin": False,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def _log_audit(hoa_id, action, details=None):
    try:
        get_db().table("hoa_audit_log").insert({
            "hoa_id": hoa_id,
            "action": action,
            "details": details or {},
        }).execute()
    except Exception:
        pass
