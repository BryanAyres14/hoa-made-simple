"""
HOA Communication API — Email Blasts, Templates, Newsletters, Manager Notes
"""
from flask import Blueprint, request, jsonify
from datetime import datetime, date
from lib.database import get_db

communication_bp = Blueprint("hoa_communication", __name__)


# ──────────────────────────────────────────────
# EMAIL TEMPLATES (CRUD)
# ──────────────────────────────────────────────
@communication_bp.route("/api/hoa/<hoa_id>/email-templates", methods=["GET"])
def list_templates(hoa_id):
    """List all email templates."""
    try:
        category = request.args.get("category")  # invoice | reminder | newsletter | custom
        query = get_db().table("hoa_email_templates").select("*").eq("hoa_id", hoa_id)
        if category:
            query = query.eq("category", category)
        result = query.order("name").execute()
        return jsonify({"templates": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@communication_bp.route("/api/hoa/<hoa_id>/email-templates", methods=["POST"])
def create_template(hoa_id):
    """
    Create email template with merge fields.
    Body: { "name", "subject", "body_html", "category", "merge_fields" }
    Merge fields: {{first_name}}, {{last_name}}, {{unit_number}}, {{amount_due}},
                  {{due_date}}, {{invoice_number}}, {{hoa_name}}, {{portal_link}}
    """
    try:
        body = request.get_json()
        template = {
            "hoa_id": hoa_id,
            "name": body["name"],
            "subject": body.get("subject", ""),
            "body_html": body.get("body_html", ""),
            "category": body.get("category", "custom"),
            "merge_fields": body.get("merge_fields", []),
            "is_active": True,
        }
        result = get_db().table("hoa_email_templates").insert(template).execute()
        return jsonify({"template": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@communication_bp.route("/api/hoa/<hoa_id>/email-templates/<template_id>", methods=["PATCH"])
def update_template(hoa_id, template_id):
    """Update an email template."""
    try:
        body = request.get_json()
        allowed = ["name", "subject", "body_html", "category", "merge_fields", "is_active"]
        updates = {k: v for k, v in body.items() if k in allowed}
        result = get_db().table("hoa_email_templates").update(updates).eq(
            "id", template_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"template": result.data[0] if result.data else {}})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@communication_bp.route("/api/hoa/<hoa_id>/email-templates/<template_id>", methods=["DELETE"])
def delete_template(hoa_id, template_id):
    """Soft-delete a template."""
    try:
        get_db().table("hoa_email_templates").update({"is_active": False}).eq(
            "id", template_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# SEED DEFAULT TEMPLATES
# ──────────────────────────────────────────────
@communication_bp.route("/api/hoa/<hoa_id>/email-templates/seed", methods=["POST"])
def seed_templates(hoa_id):
    """Seed default email templates for a new HOA."""
    try:
        defaults = [
            {
                "hoa_id": hoa_id,
                "name": "Monthly Invoice",
                "subject": "Your HOA Invoice for {{period}} — {{hoa_name}}",
                "body_html": _default_invoice_template(),
                "category": "invoice",
                "merge_fields": ["first_name", "last_name", "unit_number", "amount_due", "due_date", "invoice_number", "hoa_name", "portal_link"],
                "is_active": True,
            },
            {
                "hoa_id": hoa_id,
                "name": "Payment Reminder (Friendly)",
                "subject": "Friendly Reminder: Payment Due {{due_date}}",
                "body_html": _default_reminder_template("friendly"),
                "category": "reminder",
                "merge_fields": ["first_name", "amount_due", "due_date", "hoa_name", "portal_link"],
                "is_active": True,
            },
            {
                "hoa_id": hoa_id,
                "name": "Payment Reminder (Urgent)",
                "subject": "PAST DUE: Your HOA Payment of {{amount_due}}",
                "body_html": _default_reminder_template("urgent"),
                "category": "reminder",
                "merge_fields": ["first_name", "amount_due", "due_date", "days_overdue", "hoa_name", "portal_link"],
                "is_active": True,
            },
            {
                "hoa_id": hoa_id,
                "name": "Payment Confirmation",
                "subject": "Payment Received — Thank You!",
                "body_html": _default_receipt_template(),
                "category": "receipt",
                "merge_fields": ["first_name", "amount_paid", "payment_date", "invoice_number", "hoa_name"],
                "is_active": True,
            },
            {
                "hoa_id": hoa_id,
                "name": "Newsletter Template",
                "subject": "{{hoa_name}} — Community Update",
                "body_html": _default_newsletter_template(),
                "category": "newsletter",
                "merge_fields": ["first_name", "hoa_name"],
                "is_active": True,
            },
        ]

        result = get_db().table("hoa_email_templates").insert(defaults).execute()
        return jsonify({"templates": result.data, "count": len(result.data)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# COMPOSE & SEND EMAIL BLAST
# ──────────────────────────────────────────────
@communication_bp.route("/api/hoa/<hoa_id>/emails/send", methods=["POST"])
def send_email_blast(hoa_id):
    """
    Send emails to selected members or all members.
    Body: {
        "template_id": "...",     # or provide subject + body_html directly
        "subject": "...",
        "body_html": "...",
        "recipient_ids": [...],   # member IDs (omit for all active)
        "filter_status": "past_due",  # optional: only past_due members
        "include_newsletter_link": true,
        "newsletter_id": "..."
    }
    """
    try:
        body = request.get_json()

        # Get org info
        org = get_db().table("hoa_organizations").select(
            "name, logo_url, accent_color, contact_email"
        ).eq("id", hoa_id).single().execute()
        org_data = org.data

        # Get template if specified
        subject = body.get("subject", "")
        body_html = body.get("body_html", "")
        if body.get("template_id"):
            tpl = get_db().table("hoa_email_templates").select("*").eq(
                "id", body["template_id"]
            ).single().execute()
            subject = tpl.data.get("subject", subject)
            body_html = tpl.data.get("body_html", body_html)

        # Get recipients
        if body.get("recipient_ids"):
            members = get_db().table("hoa_members").select(
                "id, first_name, last_name, email, unit_id, monthly_dues_override"
            ).eq("hoa_id", hoa_id).in_("id", body["recipient_ids"]).execute()
        else:
            q = get_db().table("hoa_members").select(
                "id, first_name, last_name, email, unit_id, monthly_dues_override"
            ).eq("hoa_id", hoa_id).eq("status", "active")
            if body.get("filter_status") == "past_due":
                # Get members with overdue invoices
                overdue = get_db().table("hoa_invoices").select("member_id").eq(
                    "hoa_id", hoa_id
                ).eq("status", "overdue").execute()
                overdue_ids = list(set(i["member_id"] for i in overdue.data))
                if overdue_ids:
                    q = q.in_("id", overdue_ids)
                else:
                    return jsonify({"sent": 0, "message": "No past due members found"})
            members = q.execute()

        if not members.data:
            return jsonify({"error": "No recipients found"}), 400

        # Get unit numbers for merge
        unit_map = {}
        unit_ids = [m["unit_id"] for m in members.data if m.get("unit_id")]
        if unit_ids:
            units = get_db().table("hoa_units").select("id, unit_number").in_("id", unit_ids).execute()
            unit_map = {u["id"]: u["unit_number"] for u in units.data}

        # Send emails via Resend
        from config import RESEND_API_KEY
        import resend
        resend.api_key = RESEND_API_KEY

        sent = []
        failed = []

        # Create email log entry
        email_log = {
            "hoa_id": hoa_id,
            "subject": subject,
            "body_html": body_html,
            "recipient_count": len(members.data),
            "sent_count": 0,
            "failed_count": 0,
            "status": "sending",
            "sent_at": datetime.utcnow().isoformat(),
            "template_id": body.get("template_id"),
        }
        log_result = get_db().table("hoa_emails").insert(email_log).execute()
        email_id = log_result.data[0]["id"]

        for member in members.data:
            if not member.get("email"):
                failed.append({"id": member["id"], "reason": "no email"})
                continue

            # Merge fields
            merged_subject = _merge_fields(subject, member, org_data, unit_map)
            merged_body = _merge_fields(body_html, member, org_data, unit_map)

            # Wrap in branded container
            final_html = _wrap_branded_email(merged_body, org_data)

            try:
                resend.Emails.send({
                    "from": f"{org_data['name']} <updates@{_get_send_domain()}>",
                    "to": member["email"],
                    "subject": merged_subject,
                    "html": final_html,
                })
                sent.append(member["id"])
            except Exception as email_err:
                failed.append({"id": member["id"], "reason": str(email_err)})

        # Update email log
        get_db().table("hoa_emails").update({
            "sent_count": len(sent),
            "failed_count": len(failed),
            "status": "completed" if not failed else "partial",
        }).eq("id", email_id).execute()

        _log_audit(hoa_id, "email_blast_sent", {
            "email_id": email_id,
            "sent": len(sent),
            "failed": len(failed),
        })

        return jsonify({
            "email_id": email_id,
            "sent": len(sent),
            "failed": failed,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# EMAIL HISTORY (sent log)
# ──────────────────────────────────────────────
@communication_bp.route("/api/hoa/<hoa_id>/emails", methods=["GET"])
def list_emails(hoa_id):
    """List sent email history."""
    try:
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 25))
        offset = (page - 1) * per_page

        result = get_db().table("hoa_emails").select(
            "*", count="exact"
        ).eq("hoa_id", hoa_id).order("sent_at", desc=True).range(
            offset, offset + per_page - 1
        ).execute()

        return jsonify({
            "emails": result.data,
            "total": result.count,
            "page": page,
            "per_page": per_page,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# NEWSLETTERS (CRUD + link tracking)
# ──────────────────────────────────────────────
@communication_bp.route("/api/hoa/<hoa_id>/newsletters", methods=["GET"])
def list_newsletters(hoa_id):
    """List newsletters."""
    try:
        result = get_db().table("hoa_newsletters").select("*").eq(
            "hoa_id", hoa_id
        ).order("created_at", desc=True).execute()
        return jsonify({"newsletters": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@communication_bp.route("/api/hoa/<hoa_id>/newsletters", methods=["POST"])
def create_newsletter(hoa_id):
    """
    Create a newsletter entry (link or content).
    Body: { "title", "content_html" OR "external_url", "publish_date" }
    """
    try:
        body = request.get_json()
        newsletter = {
            "hoa_id": hoa_id,
            "title": body["title"],
            "content_html": body.get("content_html", ""),
            "external_url": body.get("external_url", ""),
            "publish_date": body.get("publish_date", date.today().isoformat()),
            "status": "draft",
        }
        result = get_db().table("hoa_newsletters").insert(newsletter).execute()
        return jsonify({"newsletter": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@communication_bp.route("/api/hoa/<hoa_id>/newsletters/<newsletter_id>/publish", methods=["POST"])
def publish_newsletter(hoa_id, newsletter_id):
    """Publish newsletter and optionally email it to all members."""
    try:
        body = request.get_json() or {}
        send_email = body.get("send_email", False)

        get_db().table("hoa_newsletters").update({
            "status": "published",
            "publish_date": date.today().isoformat(),
        }).eq("id", newsletter_id).eq("hoa_id", hoa_id).execute()

        email_result = None
        if send_email:
            # Trigger email blast with newsletter template
            newsletter = get_db().table("hoa_newsletters").select("*").eq(
                "id", newsletter_id
            ).single().execute()

            content = newsletter.data.get("content_html", "")
            if newsletter.data.get("external_url"):
                content += f'<p><a href="{newsletter.data["external_url"]}">Read full newsletter →</a></p>'

            # Use the send_email_blast endpoint logic
            from flask import current_app
            with current_app.test_request_context(
                "/api/hoa/{}/emails/send".format(hoa_id),
                method="POST",
                json={
                    "subject": f"Newsletter: {newsletter.data['title']}",
                    "body_html": content,
                },
            ):
                email_result = {"queued": True}

        return jsonify({
            "success": True,
            "email_sent": send_email,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# MERGE FIELD HELPERS
# ──────────────────────────────────────────────

def _merge_fields(text, member, org, unit_map=None):
    """Replace merge field placeholders with actual values."""
    if not text:
        return text

    unit_number = ""
    if unit_map and member.get("unit_id"):
        unit_number = unit_map.get(member["unit_id"], "")

    replacements = {
        "{{first_name}}": member.get("first_name", ""),
        "{{last_name}}": member.get("last_name", ""),
        "{{email}}": member.get("email", ""),
        "{{unit_number}}": unit_number,
        "{{hoa_name}}": org.get("name", "Your HOA"),
        "{{portal_link}}": f"https://portal.hoaledger.com",
    }

    for key, val in replacements.items():
        text = text.replace(key, str(val))

    return text


def _wrap_branded_email(body_html, org):
    """Wrap email content in branded template."""
    accent = org.get("accent_color", "#2563eb")
    name = org.get("name", "HOA")

    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif">
        <div style="background:{accent};color:white;padding:16px 24px;text-align:center">
            <h2 style="margin:0;font-weight:600">{name}</h2>
        </div>
        <div style="padding:24px;background:#ffffff">
            {body_html}
        </div>
        <div style="padding:16px 24px;background:#f9fafb;text-align:center;font-size:12px;color:#6b7280">
            <p>{name} • Powered by HOA Ledger</p>
            <p><a href="{{{{portal_link}}}}" style="color:{accent}">View in Portal</a></p>
        </div>
    </div>
    """


def _default_invoice_template():
    return """
    <p>Dear {{first_name}},</p>
    <p>Your monthly HOA invoice is ready.</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0">
        <tr style="background:#f3f4f6">
            <td style="padding:8px;font-weight:600">Invoice #</td>
            <td style="padding:8px">{{invoice_number}}</td>
        </tr>
        <tr>
            <td style="padding:8px;font-weight:600">Amount Due</td>
            <td style="padding:8px;font-size:18px;font-weight:700;color:#dc2626">{{amount_due}}</td>
        </tr>
        <tr style="background:#f3f4f6">
            <td style="padding:8px;font-weight:600">Due Date</td>
            <td style="padding:8px">{{due_date}}</td>
        </tr>
        <tr>
            <td style="padding:8px;font-weight:600">Unit</td>
            <td style="padding:8px">{{unit_number}}</td>
        </tr>
    </table>
    <p style="text-align:center;margin:24px 0">
        <a href="{{portal_link}}" style="background:#2563eb;color:white;padding:12px 32px;text-decoration:none;border-radius:6px;font-weight:600">
            Pay Now
        </a>
    </p>
    <p style="color:#6b7280;font-size:13px">If you've already paid, please disregard this notice.</p>
    """


def _default_reminder_template(tone="friendly"):
    if tone == "urgent":
        return """
        <p>Dear {{first_name}},</p>
        <p><strong>Your HOA payment of {{amount_due}} is past due.</strong></p>
        <p>This payment was due on {{due_date}} and is now <strong>{{days_overdue}} days overdue</strong>.
        Late fees may apply if payment is not received promptly.</p>
        <p style="text-align:center;margin:24px 0">
            <a href="{{portal_link}}" style="background:#dc2626;color:white;padding:12px 32px;text-decoration:none;border-radius:6px;font-weight:600">
                Pay Now
            </a>
        </p>
        <p style="color:#6b7280;font-size:13px">If you have questions about your balance, please contact your HOA manager.</p>
        """
    return """
    <p>Hi {{first_name}},</p>
    <p>Just a friendly reminder that your HOA payment of <strong>{{amount_due}}</strong> is due on <strong>{{due_date}}</strong>.</p>
    <p>You can pay online anytime through your member portal.</p>
    <p style="text-align:center;margin:24px 0">
        <a href="{{portal_link}}" style="background:#2563eb;color:white;padding:12px 32px;text-decoration:none;border-radius:6px;font-weight:600">
            Pay Now
        </a>
    </p>
    <p>Thank you for being a great neighbor!</p>
    """


def _default_receipt_template():
    return """
    <p>Dear {{first_name}},</p>
    <p>We've received your payment. Thank you!</p>
    <table style="width:100%;border-collapse:collapse;margin:16px 0">
        <tr style="background:#f0fdf4">
            <td style="padding:8px;font-weight:600">Amount Paid</td>
            <td style="padding:8px;font-size:18px;font-weight:700;color:#16a34a">{{amount_paid}}</td>
        </tr>
        <tr>
            <td style="padding:8px;font-weight:600">Date</td>
            <td style="padding:8px">{{payment_date}}</td>
        </tr>
        <tr style="background:#f0fdf4">
            <td style="padding:8px;font-weight:600">Invoice #</td>
            <td style="padding:8px">{{invoice_number}}</td>
        </tr>
    </table>
    <p style="color:#6b7280;font-size:13px">This is an automated receipt from {{hoa_name}}.</p>
    """


def _default_newsletter_template():
    return """
    <p>Hi {{first_name}},</p>
    <p>Here's the latest from your community.</p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
    <!-- Manager will replace this with actual content -->
    <p>[Newsletter content goes here]</p>
    <hr style="border:none;border-top:1px solid #e5e7eb;margin:20px 0">
    <p style="color:#6b7280;font-size:13px">From {{hoa_name}}</p>
    """


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
