"""
HOA Reminder Automation API — Rules Engine for Auto-Emails Before/After Due Date
"""
from flask import Blueprint, request, jsonify
from datetime import datetime, date, timedelta
from lib.database import get_db

reminders_bp = Blueprint("hoa_reminders", __name__)


# ──────────────────────────────────────────────
# REMINDER RULES (CRUD)
# ──────────────────────────────────────────────
@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules", methods=["GET"])
def list_rules(hoa_id):
    """List all reminder rules."""
    try:
        result = get_db().table("hoa_reminder_rules").select(
            "*, hoa_email_templates(name, subject)"
        ).eq("hoa_id", hoa_id).order("trigger_days").execute()
        return jsonify({"rules": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules", methods=["POST"])
def create_rule(hoa_id):
    """
    Create a reminder rule.
    Body: {
        "name": "7 Days Before Due",
        "trigger_type": "before_due" | "after_due" | "on_due",
        "trigger_days": 7,
        "template_id": "...",
        "is_active": true,
        "target_status": "all" | "unpaid" | "partial",
        "max_sends": 1
    }
    """
    try:
        body = request.get_json()
        rule = {
            "hoa_id": hoa_id,
            "name": body["name"],
            "trigger_type": body["trigger_type"],
            "trigger_days": int(body["trigger_days"]),
            "template_id": body.get("template_id"),
            "is_active": body.get("is_active", True),
            "target_status": body.get("target_status", "unpaid"),
            "max_sends": body.get("max_sends", 1),
        }
        result = get_db().table("hoa_reminder_rules").insert(rule).execute()

        _log_audit(hoa_id, "reminder_rule_created", {"rule_id": result.data[0]["id"]})
        return jsonify({"rule": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules/<rule_id>", methods=["PATCH"])
def update_rule(hoa_id, rule_id):
    """Update a reminder rule."""
    try:
        body = request.get_json()
        allowed = ["name", "trigger_type", "trigger_days", "template_id", "is_active", "target_status", "max_sends"]
        updates = {k: v for k, v in body.items() if k in allowed}
        result = get_db().table("hoa_reminder_rules").update(updates).eq(
            "id", rule_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"rule": result.data[0] if result.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules/<rule_id>", methods=["DELETE"])
def delete_rule(hoa_id, rule_id):
    """Delete a reminder rule."""
    try:
        get_db().table("hoa_reminder_rules").delete().eq(
            "id", rule_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# SEED DEFAULT RULES
# ──────────────────────────────────────────────
@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules/seed", methods=["POST"])
def seed_rules(hoa_id):
    """Create default reminder rules for a new HOA."""
    try:
        # Get template IDs
        templates = get_db().table("hoa_email_templates").select("id, name").eq(
            "hoa_id", hoa_id
        ).execute()
        tpl_map = {t["name"]: t["id"] for t in templates.data}

        defaults = [
            {
                "hoa_id": hoa_id,
                "name": "7 Days Before Due — Friendly Reminder",
                "trigger_type": "before_due",
                "trigger_days": 7,
                "template_id": tpl_map.get("Payment Reminder (Friendly)"),
                "is_active": True,
                "target_status": "unpaid",
                "max_sends": 1,
            },
            {
                "hoa_id": hoa_id,
                "name": "On Due Date — Final Notice",
                "trigger_type": "on_due",
                "trigger_days": 0,
                "template_id": tpl_map.get("Payment Reminder (Friendly)"),
                "is_active": True,
                "target_status": "unpaid",
                "max_sends": 1,
            },
            {
                "hoa_id": hoa_id,
                "name": "7 Days Past Due — Urgent",
                "trigger_type": "after_due",
                "trigger_days": 7,
                "template_id": tpl_map.get("Payment Reminder (Urgent)"),
                "is_active": True,
                "target_status": "unpaid",
                "max_sends": 1,
            },
            {
                "hoa_id": hoa_id,
                "name": "30 Days Past Due — Final Warning",
                "trigger_type": "after_due",
                "trigger_days": 30,
                "template_id": tpl_map.get("Payment Reminder (Urgent)"),
                "is_active": True,
                "target_status": "unpaid",
                "max_sends": 1,
            },
        ]

        result = get_db().table("hoa_reminder_rules").insert(defaults).execute()
        return jsonify({"rules": result.data, "count": len(result.data)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PROCESS REMINDERS (called by JARVIS scheduler daily)
# ──────────────────────────────────────────────
@reminders_bp.route("/api/hoa/<hoa_id>/reminders/process", methods=["POST"])
def process_reminders(hoa_id):
    """
    Evaluate all active reminder rules and send emails where conditions are met.
    This is the main automation endpoint — called daily by JARVIS.
    """
    try:
        # Get active rules
        rules = get_db().table("hoa_reminder_rules").select(
            "*, hoa_email_templates(subject, body_html, merge_fields)"
        ).eq("hoa_id", hoa_id).eq("is_active", True).execute()

        if not rules.data:
            return jsonify({"message": "No active rules", "sent": 0})

        # Get org info
        org = get_db().table("hoa_organizations").select(
            "name, accent_color, contact_email"
        ).eq("id", hoa_id).single().execute()

        today = date.today()
        total_sent = 0
        results = []

        for rule in rules.data:
            trigger_type = rule["trigger_type"]
            trigger_days = rule["trigger_days"]
            template = rule.get("hoa_email_templates", {})

            # Calculate the target date based on rule
            if trigger_type == "before_due":
                target_due_date = (today + timedelta(days=trigger_days)).isoformat()
            elif trigger_type == "after_due":
                target_due_date = (today - timedelta(days=trigger_days)).isoformat()
            else:  # on_due
                target_due_date = today.isoformat()

            # Find matching invoices
            query = get_db().table("hoa_invoices").select(
                "id, member_id, invoice_number, total_amount, amount_paid, due_date, period"
            ).eq("hoa_id", hoa_id).eq("due_date", target_due_date)

            target_status = rule.get("target_status", "unpaid")
            if target_status == "unpaid":
                query = query.in_("status", ["sent", "draft", "overdue"])
            elif target_status == "partial":
                query = query.eq("status", "partial")

            invoices = query.execute()

            if not invoices.data:
                results.append({
                    "rule": rule["name"],
                    "matched": 0,
                    "sent": 0,
                })
                continue

            # Check send history to respect max_sends
            sent_for_rule = 0
            for inv in invoices.data:
                # Check if already sent for this rule + invoice combo
                already_sent = get_db().table("hoa_emails").select("id").eq(
                    "hoa_id", hoa_id
                ).like("subject", f"%{inv['invoice_number']}%").execute()

                max_sends = rule.get("max_sends", 1)
                if len(already_sent.data) >= max_sends:
                    continue

                # Get member info
                member = get_db().table("hoa_members").select(
                    "id, first_name, last_name, email, unit_id"
                ).eq("id", inv["member_id"]).single().execute()

                if not member.data or not member.data.get("email"):
                    continue

                # Get unit number
                unit_number = ""
                if member.data.get("unit_id"):
                    unit = get_db().table("hoa_units").select("unit_number").eq(
                        "id", member.data["unit_id"]
                    ).single().execute()
                    unit_number = unit.data.get("unit_number", "") if unit.data else ""

                # Merge and send
                outstanding = float(inv["total_amount"]) - float(inv.get("amount_paid", 0))
                days_overdue = (today - datetime.strptime(inv["due_date"], "%Y-%m-%d").date()).days

                subject = template.get("subject", f"Payment Reminder — {org.data['name']}")
                body_html = template.get("body_html", "<p>Payment reminder</p>")

                # Extended merge for invoice-specific fields
                merge = {
                    "{{first_name}}": member.data.get("first_name", ""),
                    "{{last_name}}": member.data.get("last_name", ""),
                    "{{unit_number}}": unit_number,
                    "{{amount_due}}": f"${outstanding:,.2f}",
                    "{{due_date}}": inv["due_date"],
                    "{{invoice_number}}": inv.get("invoice_number", ""),
                    "{{hoa_name}}": org.data.get("name", "Your HOA"),
                    "{{days_overdue}}": str(max(0, days_overdue)),
                    "{{portal_link}}": "https://portal.hoaledger.com",
                    "{{period}}": inv.get("period", ""),
                }

                for key, val in merge.items():
                    subject = subject.replace(key, val)
                    body_html = body_html.replace(key, val)

                # Send via Resend
                try:
                    from config import RESEND_API_KEY
                    import resend
                    resend.api_key = RESEND_API_KEY

                    accent = org.data.get("accent_color", "#2563eb")
                    wrapped = f"""
                    <div style="max-width:600px;margin:0 auto;font-family:sans-serif">
                        <div style="background:{accent};color:white;padding:16px 24px;text-align:center">
                            <h2 style="margin:0">{org.data.get('name', 'HOA')}</h2>
                        </div>
                        <div style="padding:24px">{body_html}</div>
                    </div>"""

                    resend.Emails.send({
                        "from": f"{org.data['name']} <reminders@{_get_send_domain()}>",
                        "to": member.data["email"],
                        "subject": subject,
                        "html": wrapped,
                    })

                    # Log the email
                    get_db().table("hoa_emails").insert({
                        "hoa_id": hoa_id,
                        "subject": subject,
                        "body_html": body_html,
                        "recipient_count": 1,
                        "sent_count": 1,
                        "status": "completed",
                        "sent_at": datetime.utcnow().isoformat(),
                        "template_id": rule.get("template_id"),
                    }).execute()

                    sent_for_rule += 1
                    total_sent += 1
                except Exception:
                    pass

            results.append({
                "rule": rule["name"],
                "matched": len(invoices.data),
                "sent": sent_for_rule,
            })

        _log_audit(hoa_id, "reminders_processed", {
            "total_sent": total_sent,
            "rules_evaluated": len(rules.data),
        })

        return jsonify({
            "total_sent": total_sent,
            "rules": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PREVIEW A RULE (show who would get emailed)
# ──────────────────────────────────────────────
@reminders_bp.route("/api/hoa/<hoa_id>/reminder-rules/<rule_id>/preview", methods=["GET"])
def preview_rule(hoa_id, rule_id):
    """Show which members would be emailed if this rule ran today."""
    try:
        rule = get_db().table("hoa_reminder_rules").select("*").eq(
            "id", rule_id
        ).single().execute()

        if not rule.data:
            return jsonify({"error": "Rule not found"}), 404

        today = date.today()
        trigger_type = rule.data["trigger_type"]
        trigger_days = rule.data["trigger_days"]

        if trigger_type == "before_due":
            target = (today + timedelta(days=trigger_days)).isoformat()
        elif trigger_type == "after_due":
            target = (today - timedelta(days=trigger_days)).isoformat()
        else:
            target = today.isoformat()

        query = get_db().table("hoa_invoices").select(
            "id, member_id, invoice_number, total_amount, amount_paid, due_date, "
            "hoa_members(first_name, last_name, email)"
        ).eq("hoa_id", hoa_id).eq("due_date", target)

        target_status = rule.data.get("target_status", "unpaid")
        if target_status == "unpaid":
            query = query.in_("status", ["sent", "draft", "overdue"])
        elif target_status == "partial":
            query = query.eq("status", "partial")

        invoices = query.execute()

        recipients = []
        for inv in invoices.data:
            member = inv.get("hoa_members", {})
            if member.get("email"):
                recipients.append({
                    "member_name": f"{member.get('first_name', '')} {member.get('last_name', '')}",
                    "email": member["email"],
                    "invoice_number": inv["invoice_number"],
                    "amount_due": float(inv["total_amount"]) - float(inv.get("amount_paid", 0)),
                    "due_date": inv["due_date"],
                })

        return jsonify({
            "rule": rule.data["name"],
            "target_date": target,
            "would_send_to": len(recipients),
            "recipients": recipients,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PROCESS ALL HOAS (global scheduler endpoint)
# ──────────────────────────────────────────────
@reminders_bp.route("/api/hoa/reminders/process-all", methods=["POST"])
def process_all_reminders():
    """
    Process reminders for ALL HOA organizations.
    Called by JARVIS daily scheduler.
    """
    try:
        orgs = get_db().table("hoa_organizations").select("id").eq(
            "is_active", True
        ).execute()

        results = []
        for org in orgs.data:
            try:
                # Use internal function to process
                hoa_id = org["id"]
                rules = get_db().table("hoa_reminder_rules").select("id").eq(
                    "hoa_id", hoa_id
                ).eq("is_active", True).execute()

                if rules.data:
                    # Trigger per-org processing
                    from flask import current_app
                    with current_app.test_client() as client:
                        resp = client.post(f"/api/hoa/{hoa_id}/reminders/process")
                        results.append({
                            "hoa_id": hoa_id,
                            "status": "processed",
                        })
            except Exception as org_err:
                results.append({
                    "hoa_id": org["id"],
                    "status": "error",
                    "error": str(org_err),
                })

        return jsonify({
            "orgs_processed": len(results),
            "results": results,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def _get_send_domain():
    try:
        from config import RESEND_DOMAIN
        return RESEND_DOMAIN
    except ImportError:
        return "updates.hoaledger.com"


def _log_audit(hoa_id, action, details=None):
    try:
        get_db().table("hoa_audit_log").insert({
            "hoa_id": hoa_id,
            "action": action,
            "details": details or {},
        }).execute()
    except Exception:
        pass
