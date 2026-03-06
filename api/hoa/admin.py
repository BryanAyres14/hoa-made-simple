"""
HOA Ledger — JARVIS Super-Admin API
=====================================
Platform-level operations for Bryan & JARVIS to:
  - Create/manage HOA organizations (tenants)
  - Monitor health across all HOAs
  - Switch into any HOA for debugging/repair
  - View cross-tenant analytics
  - Provision new HOAs with auto-seeding

All endpoints require `@require_admin` — platform admin JWT only.
"""
from flask import Blueprint, request, jsonify
from datetime import datetime, timezone, timedelta, date
from lib.database import get_db
from api.hoa.auth import _hash_password

admin_bp = Blueprint("hoa_admin", __name__)


# ──────────────────────────────────────────────
# TENANT PROVISIONING
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/organizations", methods=["GET"])
def list_organizations():
    """List all HOA organizations (tenants)."""
    try:
        result = get_db().table("hoa_organizations").select(
            "id, name, city, state, subscription_tier, default_monthly_dues, "
            "stripe_account_id, created_at"
        ).order("name").execute()

        # Get stats per org
        orgs = []
        for org in result.data:
            stats = _get_org_stats(org["id"])
            org["stats"] = stats
            orgs.append(org)

        return jsonify({"organizations": orgs, "total": len(orgs)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/organizations", methods=["POST"])
def create_organization():
    """
    Provision a new HOA — creates org + seeds accounts, templates, reminder rules.
    Body: {
        "name": "Sunset Ridge HOA",
        "address": "123 Main St",
        "city": "Phoenix",
        "state": "AZ",
        "zip": "85001",
        "phone": "602-555-1234",
        "email": "admin@sunsetridgehoa.com",
        "default_monthly_dues": 250.00,
        "dues_day_of_month": 1,
        "grace_period_days": 5,
        "accent_color": "#2563eb",
        "manager_email": "john@example.com",
        "manager_password": "SecurePass123",
        "manager_first_name": "John",
        "manager_last_name": "Smith"
    }
    """
    try:
        body = request.get_json()

        if not body.get("name"):
            return jsonify({"error": "Organization name is required"}), 400

        # 1. Create organization
        org_data = {
            "name": body["name"],
            "address": body.get("address"),
            "city": body.get("city"),
            "state": body.get("state"),
            "zip": body.get("zip"),
            "phone": body.get("phone"),
            "email": body.get("email"),
            "website": body.get("website"),
            "logo_url": body.get("logo_url"),
            "accent_color": body.get("accent_color", "#2563eb"),
            "default_monthly_dues": body.get("default_monthly_dues", 0),
            "dues_day_of_month": body.get("dues_day_of_month", 1),
            "grace_period_days": body.get("grace_period_days", 5),
            "subscription_tier": "active",
        }
        org_result = get_db().table("hoa_organizations").insert(org_data).execute()
        hoa_id = org_result.data[0]["id"]

        # 2. Seed default chart of accounts
        _seed_accounts(hoa_id)

        # 3. Seed default email templates
        _seed_templates(hoa_id)

        # 4. Seed default reminder rules
        _seed_reminder_rules(hoa_id)

        # 5. Create manager user if credentials provided
        manager_user = None
        if body.get("manager_email") and body.get("manager_password"):
            pw_hash, pw_salt = _hash_password(body["manager_password"])
            manager_data = {
                "hoa_id": hoa_id,
                "email": body["manager_email"].strip().lower(),
                "first_name": body.get("manager_first_name", ""),
                "last_name": body.get("manager_last_name", ""),
                "role": "manager",
                "portal_activated": True,
                "password_hash": pw_hash,
                "password_salt": pw_salt,
            }
            mgr_result = get_db().table("hoa_users").insert(manager_data).execute()
            manager_user = mgr_result.data[0] if mgr_result.data else None

        _log_audit(hoa_id, "organization_created", {"name": body["name"]})

        return jsonify({
            "organization": org_result.data[0],
            "manager": {
                "id": manager_user["id"] if manager_user else None,
                "email": body.get("manager_email"),
            } if manager_user else None,
            "seeded": {
                "accounts": True,
                "templates": True,
                "reminder_rules": True,
            },
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/organizations/<org_id>", methods=["GET"])
def get_organization(org_id):
    """Get full details of an HOA organization."""
    try:
        org = get_db().table("hoa_organizations").select("*").eq(
            "id", org_id
        ).single().execute()

        if not org.data:
            return jsonify({"error": "Organization not found"}), 404

        stats = _get_org_stats(org_id)
        users = get_db().table("hoa_users").select(
            "id, email, first_name, last_name, role, portal_activated, last_login_at"
        ).eq("hoa_id", org_id).execute()

        return jsonify({
            "organization": org.data,
            "stats": stats,
            "users": users.data,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/organizations/<org_id>", methods=["PATCH"])
def update_organization(org_id):
    """Update organization settings."""
    try:
        body = request.get_json()
        allowed = [
            "name", "address", "city", "state", "zip", "phone", "email",
            "website", "logo_url", "accent_color", "default_monthly_dues",
            "dues_day_of_month", "grace_period_days", "late_fee_rules",
            "subscription_tier", "stripe_account_id",
        ]
        updates = {k: v for k, v in body.items() if k in allowed}

        result = get_db().table("hoa_organizations").update(updates).eq(
            "id", org_id
        ).execute()

        return jsonify({"organization": result.data[0] if result.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/organizations/<org_id>", methods=["DELETE"])
def deactivate_organization(org_id):
    """Deactivate an organization (soft delete — sets tier to cancelled)."""
    try:
        get_db().table("hoa_organizations").update({
            "subscription_tier": "cancelled",
        }).eq("id", org_id).execute()

        _log_audit(org_id, "organization_deactivated", {})
        return jsonify({"success": True, "message": "Organization deactivated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PLATFORM ADMIN USERS (JARVIS / Bryan)
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/admins", methods=["GET"])
def list_admins():
    """List all platform admin users."""
    try:
        result = get_db().table("hoa_platform_admins").select(
            "id, email, name, role, is_active, last_login_at, created_at"
        ).order("name").execute()
        return jsonify({"admins": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/admins", methods=["POST"])
def create_admin():
    """
    Create a new platform admin.
    Body: { "email": "...", "name": "...", "password": "...", "role": "super_admin" | "support" }
    """
    try:
        body = request.get_json()
        email = body.get("email", "").strip().lower()
        password = body.get("password", "")
        name = body.get("name", "")
        role = body.get("role", "support")

        if not email or not password or not name:
            return jsonify({"error": "email, name, and password are required"}), 400

        if role not in ("super_admin", "support"):
            role = "support"

        pw_hash, pw_salt = _hash_password(password)

        result = get_db().table("hoa_platform_admins").insert({
            "email": email,
            "name": name,
            "role": role,
            "password_hash": pw_hash,
            "password_salt": pw_salt,
            "is_active": True,
        }).execute()

        return jsonify({"admin": {
            "id": result.data[0]["id"],
            "email": email,
            "name": name,
            "role": role,
        }}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CROSS-TENANT MONITORING & HEALTH
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/dashboard", methods=["GET"])
def admin_dashboard_stats():
    """Get platform-wide stats for the JARVIS dashboard."""
    try:
        # Organization counts
        orgs = get_db().table("hoa_organizations").select(
            "id, name, subscription_tier"
        ).execute()

        active_orgs = [o for o in orgs.data if o["subscription_tier"] in ("active", "trial")]
        total_orgs = len(orgs.data)

        # Total members across all HOAs
        members = get_db().table("hoa_members").select("id, hoa_id, status").execute()
        total_members = len(members.data)
        past_due_members = sum(1 for m in members.data if m.get("status") == "past_due")

        # Revenue this month
        month_start = date.today().replace(day=1).isoformat()
        payments = get_db().table("hoa_payments").select(
            "amount"
        ).gte("payment_date", month_start).eq("status", "completed").execute()
        monthly_revenue = sum(float(p["amount"]) for p in payments.data)

        # Recent audit log
        recent_activity = get_db().table("hoa_audit_log").select(
            "*, hoa_organizations(name)"
        ).order("created_at", desc=True).limit(20).execute()

        # Users summary
        users = get_db().table("hoa_users").select("id, role, hoa_id, last_login_at").execute()
        total_users = len(users.data)
        active_last_week = sum(
            1 for u in users.data
            if u.get("last_login_at") and
            datetime.fromisoformat(u["last_login_at"].replace("Z", "+00:00")) >
            datetime.now(timezone.utc) - timedelta(days=7)
        )

        return jsonify({
            "platform": {
                "total_organizations": total_orgs,
                "active_organizations": len(active_orgs),
                "total_members": total_members,
                "past_due_members": past_due_members,
                "total_users": total_users,
                "active_users_7d": active_last_week,
                "monthly_revenue": monthly_revenue,
            },
            "organizations": [{
                "id": o["id"],
                "name": o["name"],
                "tier": o["subscription_tier"],
            } for o in orgs.data],
            "recent_activity": recent_activity.data[:20],
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/health", methods=["GET"])
def platform_health():
    """Health check across all HOA tenants."""
    try:
        orgs = get_db().table("hoa_organizations").select(
            "id, name, subscription_tier"
        ).neq("subscription_tier", "cancelled").execute()

        health_reports = []
        for org in orgs.data:
            hoa_id = org["id"]
            report = {
                "hoa_id": hoa_id,
                "name": org["name"],
                "tier": org["subscription_tier"],
                "issues": [],
            }

            # Check member count
            members = get_db().table("hoa_members").select("id").eq(
                "hoa_id", hoa_id
            ).execute()
            report["member_count"] = len(members.data)

            if len(members.data) == 0:
                report["issues"].append("No members — needs setup")

            # Check manager exists
            managers = get_db().table("hoa_users").select("id").eq(
                "hoa_id", hoa_id
            ).eq("role", "manager").execute()
            report["has_manager"] = len(managers.data) > 0
            if not report["has_manager"]:
                report["issues"].append("No manager account")

            # Check accounts seeded
            accounts = get_db().table("hoa_accounts").select("id").eq(
                "hoa_id", hoa_id
            ).execute()
            report["accounts_count"] = len(accounts.data)
            if len(accounts.data) == 0:
                report["issues"].append("Chart of accounts not seeded")

            # Check email templates
            templates = get_db().table("hoa_email_templates").select("id").eq(
                "hoa_id", hoa_id
            ).execute()
            report["templates_count"] = len(templates.data)
            if len(templates.data) == 0:
                report["issues"].append("No email templates")

            report["status"] = "healthy" if not report["issues"] else "needs_attention"
            health_reports.append(report)

        healthy = sum(1 for r in health_reports if r["status"] == "healthy")

        return jsonify({
            "overall": "healthy" if healthy == len(health_reports) else "degraded",
            "healthy_count": healthy,
            "total_count": len(health_reports),
            "organizations": health_reports,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# REPAIR / MAINTENANCE TOOLS
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/organizations/<org_id>/repair", methods=["POST"])
def repair_organization(org_id):
    """
    Auto-repair an HOA org — re-seed missing accounts, templates, rules.
    Body: { "actions": ["seed_accounts", "seed_templates", "seed_reminders", "recalc_balances"] }
    """
    try:
        body = request.get_json() or {}
        actions = body.get("actions", ["seed_accounts", "seed_templates", "seed_reminders"])

        results = {}

        if "seed_accounts" in actions:
            _seed_accounts(org_id)
            results["seed_accounts"] = "done"

        if "seed_templates" in actions:
            _seed_templates(org_id)
            results["seed_templates"] = "done"

        if "seed_reminders" in actions:
            _seed_reminder_rules(org_id)
            results["seed_reminders"] = "done"

        if "recalc_balances" in actions:
            _recalculate_balances(org_id)
            results["recalc_balances"] = "done"

        _log_audit(org_id, "organization_repaired", {"actions": actions})

        return jsonify({"repaired": True, "results": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/organizations/<org_id>/impersonate", methods=["POST"])
def impersonate_user(org_id):
    """
    Generate a token to impersonate any user in an HOA (for debugging).
    Body: { "user_id": "..." }  (optional — defaults to first manager)
    """
    try:
        from api.hoa.auth import _create_jwt
        from datetime import timedelta

        body = request.get_json() or {}
        user_id = body.get("user_id")

        if user_id:
            user = get_db().table("hoa_users").select(
                "id, email, role, hoa_id, first_name, last_name"
            ).eq("id", user_id).eq("hoa_id", org_id).single().execute()
        else:
            user = get_db().table("hoa_users").select(
                "id, email, role, hoa_id, first_name, last_name"
            ).eq("hoa_id", org_id).eq("role", "manager").limit(1).single().execute()

        if not user.data:
            return jsonify({"error": "User not found"}), 404

        token = _create_jwt({
            "user_id": user.data["id"],
            "hoa_id": org_id,
            "email": user.data["email"],
            "role": user.data["role"],
            "is_admin": False,
            "impersonated_by": "admin",
            "exp": (datetime.now(timezone.utc) + timedelta(hours=4)).isoformat(),
        })

        _log_audit(org_id, "user_impersonated", {
            "user_id": user.data["id"],
            "email": user.data["email"],
        })

        return jsonify({
            "token": token,
            "user": {
                "id": user.data["id"],
                "email": user.data["email"],
                "name": f"{user.data['first_name']} {user.data['last_name']}",
                "role": user.data["role"],
            },
            "expires_in": 4 * 3600,
            "note": "Use this token as Bearer auth to act as this user.",
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CROSS-TENANT ANALYTICS
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/analytics", methods=["GET"])
def platform_analytics():
    """Revenue and usage analytics across all HOAs."""
    try:
        # Payments by month
        payments = get_db().table("hoa_payments").select(
            "amount, payment_date, hoa_id, status"
        ).eq("status", "completed").order("payment_date").execute()

        monthly_totals = {}
        for p in payments.data:
            month = p["payment_date"][:7]  # YYYY-MM
            monthly_totals[month] = monthly_totals.get(month, 0) + float(p["amount"])

        # Revenue by org
        org_revenue = {}
        for p in payments.data:
            hoa_id = p["hoa_id"]
            org_revenue[hoa_id] = org_revenue.get(hoa_id, 0) + float(p["amount"])

        # Members trend
        members = get_db().table("hoa_members").select(
            "created_at, hoa_id"
        ).order("created_at").execute()

        monthly_members = {}
        for m in members.data:
            if m.get("created_at"):
                month = m["created_at"][:7]
                monthly_members[month] = monthly_members.get(month, 0) + 1

        return jsonify({
            "revenue": {
                "monthly_totals": monthly_totals,
                "by_organization": org_revenue,
            },
            "members": {
                "monthly_signups": monthly_members,
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# BULK OPERATIONS
# ──────────────────────────────────────────────

@admin_bp.route("/api/hoa/admin/run-all-reminders", methods=["POST"])
def run_all_reminders():
    """Manually trigger reminder processing for all HOAs. Used by JARVIS scheduler."""
    try:
        orgs = get_db().table("hoa_organizations").select("id, name").eq(
            "subscription_tier", "active"
        ).execute()

        from flask import current_app
        results = []

        for org in orgs.data:
            try:
                with current_app.test_client() as client:
                    resp = client.post(f"/api/hoa/{org['id']}/reminders/process")
                    results.append({
                        "hoa_id": org["id"],
                        "name": org["name"],
                        "status": "processed",
                    })
            except Exception as err:
                results.append({
                    "hoa_id": org["id"],
                    "name": org["name"],
                    "status": "error",
                    "error": str(err),
                })

        return jsonify({
            "processed": len(results),
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@admin_bp.route("/api/hoa/admin/run-all-late-fees", methods=["POST"])
def run_all_late_fees():
    """Manually trigger late fee processing for all HOAs."""
    try:
        orgs = get_db().table("hoa_organizations").select("id, name").eq(
            "subscription_tier", "active"
        ).execute()

        from flask import current_app
        results = []

        for org in orgs.data:
            try:
                with current_app.test_client() as client:
                    resp = client.post(f"/api/hoa/{org['id']}/invoices/late-fees/apply")
                    results.append({
                        "hoa_id": org["id"],
                        "name": org["name"],
                        "status": "processed",
                    })
            except Exception as err:
                results.append({
                    "hoa_id": org["id"],
                    "name": org["name"],
                    "status": "error",
                    "error": str(err),
                })

        return jsonify({
            "processed": len(results),
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# SEED HELPERS
# ──────────────────────────────────────────────

def _seed_accounts(hoa_id):
    """Seed default chart of accounts for an HOA."""
    defaults = [
        ("1000", "Cash / Checking", "asset", "operating"),
        ("1100", "Savings", "asset", "operating"),
        ("1150", "Reserve Savings", "asset", "reserve"),
        ("1200", "Accounts Receivable", "asset", "operating"),
        ("1300", "Prepaid Expenses", "asset", "operating"),
        ("2000", "Accounts Payable", "liability", "operating"),
        ("2100", "Prepaid Assessments", "liability", "operating"),
        ("2200", "Accrued Expenses", "liability", "operating"),
        ("3000", "Operating Fund Balance", "equity", "operating"),
        ("3100", "Reserve Fund Balance", "equity", "reserve"),
        ("3200", "Special Assessment Fund", "equity", "special"),
        ("4000", "Assessment Revenue - Dues", "revenue", "operating"),
        ("4100", "Special Assessment Revenue", "revenue", "special"),
        ("4200", "Late Fee Revenue", "revenue", "operating"),
        ("4300", "Interest Income", "revenue", "operating"),
        ("4400", "Other Income", "revenue", "operating"),
        ("4500", "Transfer to Reserve", "revenue", "reserve"),
        ("5000", "Management Fees", "expense", "operating"),
        ("6000", "Utilities", "expense", "operating"),
        ("6100", "Landscaping", "expense", "operating"),
        ("6200", "Insurance", "expense", "operating"),
        ("6300", "Repairs & Maintenance", "expense", "operating"),
        ("6400", "Cleaning / Janitorial", "expense", "operating"),
        ("6500", "Pest Control", "expense", "operating"),
        ("6600", "Pool & Spa", "expense", "operating"),
        ("6700", "Security", "expense", "operating"),
        ("7000", "Legal & Professional", "expense", "operating"),
        ("7100", "Accounting & Tax Prep", "expense", "operating"),
        ("7200", "Bad Debt Expense", "expense", "operating"),
        ("8000", "Capital Improvements", "expense", "reserve"),
        ("8100", "Reserve Expenditures", "expense", "reserve"),
    ]

    rows = [{
        "hoa_id": hoa_id,
        "account_number": num,
        "account_name": name,
        "account_type": atype,
        "fund": fund,
    } for num, name, atype, fund in defaults]

    try:
        get_db().table("hoa_accounts").upsert(
            rows, on_conflict="hoa_id,account_number"
        ).execute()
    except Exception:
        # Fallback to individual inserts
        for row in rows:
            try:
                get_db().table("hoa_accounts").insert(row).execute()
            except Exception:
                pass


def _seed_templates(hoa_id):
    """Seed default email templates."""
    templates = [
        {
            "hoa_id": hoa_id,
            "name": "Payment Reminder (Friendly)",
            "subject": "Reminder: HOA Dues Due {{due_date}}",
            "body_html": "<p>Dear {{first_name}},</p><p>This is a friendly reminder that your HOA dues of <strong>{{amount_due}}</strong> for Unit {{unit_number}} are due on {{due_date}}.</p><p><a href='{{portal_link}}'>Pay Online</a></p><p>Thank you,<br/>{{hoa_name}}</p>",
            "template_type": "dues_reminder",
            "is_default": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "Payment Reminder (Urgent)",
            "subject": "PAST DUE: Your account has an outstanding balance",
            "body_html": "<p>Dear {{first_name}},</p><p>Your HOA account for Unit {{unit_number}} is <strong>{{days_overdue}} days past due</strong>. Outstanding balance: <strong>{{amount_due}}</strong>.</p><p>Late fees may apply. Please pay immediately.</p><p><a href='{{portal_link}}'>Pay Now</a></p><p>{{hoa_name}}</p>",
            "template_type": "past_due",
            "is_default": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "Payment Received",
            "subject": "Payment Received — Thank You!",
            "body_html": "<p>Dear {{first_name}},</p><p>We've received your payment. Thank you!</p><p>{{hoa_name}}</p>",
            "template_type": "custom",
            "is_default": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "Welcome New Member",
            "subject": "Welcome to {{hoa_name}}!",
            "body_html": "<p>Dear {{first_name}},</p><p>Welcome to {{hoa_name}}! Your portal account has been created.</p><p><a href='{{portal_link}}'>Access Your Portal</a></p><p>Best regards,<br/>{{hoa_name}} Management</p>",
            "template_type": "welcome",
            "is_default": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "Newsletter",
            "subject": "{{hoa_name}} Newsletter",
            "body_html": "<p>Dear {{first_name}},</p><p>Here's your latest community update.</p><p>{{hoa_name}}</p>",
            "template_type": "newsletter",
            "is_default": True,
        },
    ]

    for tpl in templates:
        try:
            get_db().table("hoa_email_templates").insert(tpl).execute()
        except Exception:
            pass


def _seed_reminder_rules(hoa_id):
    """Seed default reminder rules."""
    # Get template IDs
    templates = get_db().table("hoa_email_templates").select("id, name").eq(
        "hoa_id", hoa_id
    ).execute()
    tpl_map = {t["name"]: t["id"] for t in templates.data}

    rules = [
        {
            "hoa_id": hoa_id,
            "name": "7 Days Before Due — Friendly",
            "trigger_type": "before_due",
            "trigger_days": 7,
            "template_id": tpl_map.get("Payment Reminder (Friendly)"),
            "is_active": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "On Due Date",
            "trigger_type": "on_due",
            "trigger_days": 0,
            "template_id": tpl_map.get("Payment Reminder (Friendly)"),
            "is_active": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "7 Days Past Due — Urgent",
            "trigger_type": "after_due",
            "trigger_days": 7,
            "template_id": tpl_map.get("Payment Reminder (Urgent)"),
            "is_active": True,
        },
        {
            "hoa_id": hoa_id,
            "name": "30 Days Past Due — Final",
            "trigger_type": "after_due",
            "trigger_days": 30,
            "template_id": tpl_map.get("Payment Reminder (Urgent)"),
            "is_active": True,
        },
    ]

    for rule in rules:
        try:
            get_db().table("hoa_reminder_rules").insert(rule).execute()
        except Exception:
            pass


def _recalculate_balances(hoa_id):
    """Recalculate all member balances from invoice data."""
    members = get_db().table("hoa_members").select("id").eq(
        "hoa_id", hoa_id
    ).execute()

    for member in members.data:
        invoices = get_db().table("hoa_invoices").select(
            "balance_due, status"
        ).eq("member_id", member["id"]).in_(
            "status", ["sent", "partial", "overdue"]
        ).execute()

        balance = sum(float(inv.get("balance_due", 0)) for inv in invoices.data)

        get_db().table("hoa_members").update({
            "current_balance": balance,
            "status": "past_due" if balance > 0 else "current",
        }).eq("id", member["id"]).execute()


def _get_org_stats(hoa_id):
    """Get quick stats for an org."""
    try:
        members = get_db().table("hoa_members").select("id, status, current_balance").eq(
            "hoa_id", hoa_id
        ).execute()

        total_members = len(members.data)
        past_due = sum(1 for m in members.data if m.get("status") == "past_due")
        total_outstanding = sum(
            float(m.get("current_balance", 0))
            for m in members.data
            if float(m.get("current_balance", 0)) > 0
        )

        users = get_db().table("hoa_users").select("id, role").eq(
            "hoa_id", hoa_id
        ).execute()

        return {
            "total_members": total_members,
            "past_due_members": past_due,
            "total_outstanding": total_outstanding,
            "total_users": len(users.data),
            "managers": sum(1 for u in users.data if u["role"] == "manager"),
        }
    except Exception:
        return {}


def _log_audit(hoa_id, action, details=None):
    try:
        get_db().table("hoa_audit_log").insert({
            "hoa_id": hoa_id,
            "action": action,
            "details": details or {},
        }).execute()
    except Exception:
        pass
