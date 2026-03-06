"""
HOA Ledger — Auth Middleware
==============================
Decorators that protect API routes with JWT-based auth.

Usage:
    @require_auth              — Any authenticated user
    @require_role("manager")   — Manager only
    @require_role("board")     — Board or Manager
    @require_admin             — Platform super-admin (JARVIS/Bryan)

After decoration, `g.current_user` contains the JWT payload:
    {
        "user_id": "...",
        "hoa_id": "...",
        "email": "...",
        "role": "manager" | "board" | "member",
        "is_admin": False
    }

For admin tokens, `g.current_user` has `is_admin: True` and no `hoa_id`.
The admin can pass `?as_hoa=<uuid>` to operate on any HOA.
"""
from functools import wraps
from flask import request, jsonify, g

# Import JWT verifier from auth module
from api.hoa.auth import _verify_jwt


# Role hierarchy: manager > board > member
ROLE_HIERARCHY = {
    "manager": 3,
    "board": 2,
    "member": 1,
}


def _extract_token():
    """Extract and verify JWT from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None
    token = auth_header.split(" ", 1)[1]
    return _verify_jwt(token)


def require_auth(f):
    """Require any valid JWT (HOA user or admin)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        payload = _extract_token()
        if not payload:
            return jsonify({"error": "Authentication required"}), 401

        g.current_user = payload

        # If admin is accessing an HOA-specific route, allow via ?as_hoa=
        if payload.get("is_admin"):
            hoa_id_from_url = kwargs.get("hoa_id") or request.args.get("as_hoa")
            if hoa_id_from_url:
                g.current_user["hoa_id"] = hoa_id_from_url
                g.current_user["role"] = "manager"  # Admin gets manager-level access

        # Verify the user's hoa_id matches the route hoa_id
        route_hoa_id = kwargs.get("hoa_id")
        if route_hoa_id and not payload.get("is_admin"):
            if payload.get("hoa_id") != route_hoa_id:
                return jsonify({"error": "Access denied — wrong organization"}), 403

        return f(*args, **kwargs)
    return decorated


def require_role(min_role):
    """
    Require a minimum role level.
    "member"  — any authenticated user for this HOA
    "board"   — board member or manager
    "manager" — manager only
    """
    def decorator(f):
        @wraps(f)
        def decorated(*args, **kwargs):
            payload = _extract_token()
            if not payload:
                return jsonify({"error": "Authentication required"}), 401

            g.current_user = payload

            # Admins bypass role checks
            if payload.get("is_admin"):
                hoa_id_from_url = kwargs.get("hoa_id") or request.args.get("as_hoa")
                if hoa_id_from_url:
                    g.current_user["hoa_id"] = hoa_id_from_url
                    g.current_user["role"] = "manager"
                return f(*args, **kwargs)

            # Verify hoa_id match
            route_hoa_id = kwargs.get("hoa_id")
            if route_hoa_id and payload.get("hoa_id") != route_hoa_id:
                return jsonify({"error": "Access denied — wrong organization"}), 403

            # Check role level
            user_level = ROLE_HIERARCHY.get(payload.get("role"), 0)
            required_level = ROLE_HIERARCHY.get(min_role, 0)
            if user_level < required_level:
                return jsonify({
                    "error": f"Access denied — requires {min_role} role or higher"
                }), 403

            return f(*args, **kwargs)
        return decorated
    return decorator


def require_admin(f):
    """Require platform super-admin access (JARVIS/Bryan)."""
    @wraps(f)
    def decorated(*args, **kwargs):
        payload = _extract_token()
        if not payload:
            return jsonify({"error": "Authentication required"}), 401

        if not payload.get("is_admin"):
            return jsonify({"error": "Platform admin access required"}), 403

        g.current_user = payload
        return f(*args, **kwargs)
    return decorated


def require_self_or_manager(f):
    """
    Allow access if:
    - User is a manager/board for this HOA, OR
    - User is accessing their own record (member_id matches), OR
    - User is a platform admin
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        payload = _extract_token()
        if not payload:
            return jsonify({"error": "Authentication required"}), 401

        g.current_user = payload

        if payload.get("is_admin"):
            hoa_id_from_url = kwargs.get("hoa_id") or request.args.get("as_hoa")
            if hoa_id_from_url:
                g.current_user["hoa_id"] = hoa_id_from_url
                g.current_user["role"] = "manager"
            return f(*args, **kwargs)

        # Manager/board can access everything in their HOA
        if ROLE_HIERARCHY.get(payload.get("role"), 0) >= ROLE_HIERARCHY["board"]:
            route_hoa_id = kwargs.get("hoa_id")
            if route_hoa_id and payload.get("hoa_id") != route_hoa_id:
                return jsonify({"error": "Access denied — wrong organization"}), 403
            return f(*args, **kwargs)

        # Members can only access their own data
        member_id = kwargs.get("member_id")
        if member_id:
            from lib.database import get_db
            member = get_db().table("hoa_members").select("user_id").eq(
                "id", member_id
            ).maybe_single().execute()
            if member.data and member.data.get("user_id") == payload.get("user_id"):
                return f(*args, **kwargs)

        return jsonify({"error": "Access denied"}), 403
    return decorated


def get_current_hoa_id():
    """Helper: get the HOA ID from the current authenticated context."""
    user = getattr(g, "current_user", None)
    if not user:
        return None
    return user.get("hoa_id")


def get_current_user_id():
    """Helper: get the user ID from the current authenticated context."""
    user = getattr(g, "current_user", None)
    if not user:
        return None
    return user.get("user_id") or user.get("admin_id")


def is_admin():
    """Helper: check if current user is a platform admin."""
    user = getattr(g, "current_user", None)
    return user.get("is_admin", False) if user else False
