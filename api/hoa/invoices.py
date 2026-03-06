"""
HOA Invoices & Payments API — Generate Invoices, Track Payments, Late Fees, Stripe
"""
from flask import Blueprint, request, jsonify
import uuid
from datetime import datetime, date, timedelta
from decimal import Decimal
from lib.database import get_db

invoices_bp = Blueprint("hoa_invoices", __name__)


# ──────────────────────────────────────────────
# LIST INVOICES (with filters, pagination)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices", methods=["GET"])
def list_invoices(hoa_id):
    """List invoices with optional filters for status, member, date range."""
    try:
        status = request.args.get("status")  # draft | sent | paid | partial | overdue | void
        member_id = request.args.get("member_id")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        sort_by = request.args.get("sort", "due_date")
        sort_dir = request.args.get("dir", "desc")
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        offset = (page - 1) * per_page

        query = get_db().table("hoa_invoices").select(
            "*, hoa_members(first_name, last_name, email, unit_id), "
            "hoa_units(unit_number, address)",
            count="exact"
        ).eq("hoa_id", hoa_id)

        if status:
            query = query.eq("status", status)
        if member_id:
            query = query.eq("member_id", member_id)
        if date_from:
            query = query.gte("due_date", date_from)
        if date_to:
            query = query.lte("due_date", date_to)

        # Sort
        ascending = sort_dir == "asc"
        query = query.order(sort_by, desc=not ascending)
        query = query.range(offset, offset + per_page - 1)

        result = query.execute()

        # Summary totals
        all_invoices = get_db().table("hoa_invoices").select(
            "total_amount, amount_paid, status"
        ).eq("hoa_id", hoa_id).execute()

        total_billed = sum(float(i.get("total_amount", 0)) for i in all_invoices.data)
        total_collected = sum(float(i.get("amount_paid", 0)) for i in all_invoices.data)
        total_outstanding = total_billed - total_collected
        overdue_count = sum(1 for i in all_invoices.data if i.get("status") == "overdue")

        return jsonify({
            "invoices": result.data,
            "total": result.count,
            "page": page,
            "per_page": per_page,
            "summary": {
                "total_billed": total_billed,
                "total_collected": total_collected,
                "total_outstanding": total_outstanding,
                "overdue_count": overdue_count,
            }
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# GET SINGLE INVOICE (with payment history)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices/<invoice_id>", methods=["GET"])
def get_invoice(hoa_id, invoice_id):
    """Get invoice detail with payment history."""
    try:
        invoice = get_db().table("hoa_invoices").select(
            "*, hoa_members(first_name, last_name, email, phone, unit_id), "
            "hoa_units(unit_number, address)"
        ).eq("id", invoice_id).eq("hoa_id", hoa_id).single().execute()

        payments = get_db().table("hoa_payments").select("*").eq(
            "invoice_id", invoice_id
        ).order("payment_date", desc=True).execute()

        return jsonify({
            "invoice": invoice.data,
            "payments": payments.data,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# GENERATE INVOICES (batch for all members or single)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices/generate", methods=["POST"])
def generate_invoices(hoa_id):
    """
    Generate monthly invoices for all active members (or a specific member).
    Body: { "period": "2026-03", "member_id": null, "include_late_fees": true }
    """
    try:
        body = request.get_json()
        period = body.get("period")  # "YYYY-MM"
        target_member = body.get("member_id")
        include_late_fees = body.get("include_late_fees", True)

        if not period:
            return jsonify({"error": "period is required (YYYY-MM)"}), 400

        # Get org settings
        org = get_db().table("hoa_organizations").select(
            "monthly_dues, late_fee_amount, late_fee_type, late_fee_grace_days, due_day"
        ).eq("id", hoa_id).single().execute()
        settings = org.data

        due_day = settings.get("due_day", 1)
        year, month = period.split("-")
        due_date = f"{year}-{month}-{str(due_day).zfill(2)}"

        # Check for existing invoices this period
        existing = get_db().table("hoa_invoices").select("member_id").eq(
            "hoa_id", hoa_id
        ).eq("period", period).execute()
        existing_members = {i["member_id"] for i in existing.data}

        # Get members to invoice
        members_query = get_db().table("hoa_members").select(
            "id, first_name, last_name, email, unit_id, monthly_dues_override"
        ).eq("hoa_id", hoa_id).eq("status", "active")

        if target_member:
            members_query = members_query.eq("id", target_member)

        members = members_query.execute()

        created = []
        skipped = []
        for member in members.data:
            if member["id"] in existing_members:
                skipped.append(member["id"])
                continue

            # Use member override or org default
            dues = member.get("monthly_dues_override") or settings.get("monthly_dues", 0)
            line_items = [{"description": f"Monthly HOA Dues — {period}", "amount": float(dues)}]

            # Check for past due invoices and apply late fees
            late_fee_total = 0
            if include_late_fees:
                late_fee_total = _calculate_late_fees(hoa_id, member["id"], settings)
                if late_fee_total > 0:
                    line_items.append({
                        "description": "Late Fee (past due balance)",
                        "amount": late_fee_total,
                    })

            total = float(dues) + late_fee_total

            invoice_data = {
                "hoa_id": hoa_id,
                "member_id": member["id"],
                "unit_id": member.get("unit_id"),
                "period": period,
                "due_date": due_date,
                "line_items": line_items,
                "total_amount": total,
                "amount_paid": 0,
                "status": "draft",
                "invoice_number": _next_invoice_number(hoa_id, period),
            }

            result = get_db().table("hoa_invoices").insert(invoice_data).execute()
            created.append(result.data[0])

        _log_audit(hoa_id, "invoices_generated", {
            "period": period,
            "created": len(created),
            "skipped": len(skipped),
        })

        return jsonify({
            "created": len(created),
            "skipped": len(skipped),
            "invoices": created,
        }), 201

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# SEND INVOICES (email via Resend)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices/send", methods=["POST"])
def send_invoices(hoa_id):
    """
    Send invoice emails to members.
    Body: { "invoice_ids": [...], "template": "default" }
    """
    try:
        body = request.get_json()
        invoice_ids = body.get("invoice_ids", [])

        if not invoice_ids:
            return jsonify({"error": "invoice_ids required"}), 400

        # Get org info for branding
        org = get_db().table("hoa_organizations").select(
            "name, logo_url, accent_color, contact_email"
        ).eq("id", hoa_id).single().execute()

        sent = []
        failed = []
        for inv_id in invoice_ids:
            inv = get_db().table("hoa_invoices").select(
                "*, hoa_members(first_name, last_name, email)"
            ).eq("id", inv_id).single().execute()
            invoice = inv.data
            member = invoice.get("hoa_members", {})

            if not member.get("email"):
                failed.append({"id": inv_id, "reason": "no email"})
                continue

            try:
                from config import RESEND_API_KEY
                import resend
                resend.api_key = RESEND_API_KEY

                html = _build_invoice_email(invoice, member, org.data)
                resend.Emails.send({
                    "from": f"{org.data['name']} <invoices@{_get_send_domain()}>",
                    "to": member["email"],
                    "subject": f"Invoice #{invoice['invoice_number']} — {org.data['name']}",
                    "html": html,
                })

                # Update invoice status to sent
                get_db().table("hoa_invoices").update({
                    "status": "sent",
                    "sent_at": datetime.utcnow().isoformat(),
                }).eq("id", inv_id).execute()

                sent.append(inv_id)
            except Exception as email_err:
                failed.append({"id": inv_id, "reason": str(email_err)})

        _log_audit(hoa_id, "invoices_sent", {"sent": len(sent), "failed": len(failed)})

        return jsonify({"sent": len(sent), "failed": failed})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# VOID AN INVOICE
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices/<invoice_id>/void", methods=["POST"])
def void_invoice(hoa_id, invoice_id):
    """Void an invoice (can't void if payments exist)."""
    try:
        payments = get_db().table("hoa_payments").select("id").eq(
            "invoice_id", invoice_id
        ).execute()

        if payments.data:
            return jsonify({"error": "Cannot void invoice with payments. Refund first."}), 400

        get_db().table("hoa_invoices").update({
            "status": "void",
        }).eq("id", invoice_id).eq("hoa_id", hoa_id).execute()

        _log_audit(hoa_id, "invoice_voided", {"invoice_id": invoice_id})
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PAYMENTS — LIST
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/payments", methods=["GET"])
def list_payments(hoa_id):
    """List all payments with filters."""
    try:
        method = request.args.get("method")  # stripe | check | ach | cash | other
        member_id = request.args.get("member_id")
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        offset = (page - 1) * per_page

        query = get_db().table("hoa_payments").select(
            "*, hoa_members(first_name, last_name), "
            "hoa_invoices(invoice_number, period, total_amount)",
            count="exact"
        ).eq("hoa_id", hoa_id)

        if method:
            query = query.eq("method", method)
        if member_id:
            query = query.eq("member_id", member_id)
        if date_from:
            query = query.gte("payment_date", date_from)
        if date_to:
            query = query.lte("payment_date", date_to)

        query = query.order("payment_date", desc=True)
        query = query.range(offset, offset + per_page - 1)
        result = query.execute()

        return jsonify({
            "payments": result.data,
            "total": result.count,
            "page": page,
            "per_page": per_page,
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# RECORD PAYMENT (manual — check, cash, ACH)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/payments", methods=["POST"])
def record_payment(hoa_id):
    """
    Record a manual payment.
    Body: { "invoice_id", "member_id", "amount", "method", "reference", "payment_date", "notes" }
    """
    try:
        body = request.get_json()
        required = ["invoice_id", "member_id", "amount", "method"]
        missing = [f for f in required if f not in body]
        if missing:
            return jsonify({"error": f"Missing fields: {missing}"}), 400

        payment_data = {
            "hoa_id": hoa_id,
            "invoice_id": body["invoice_id"],
            "member_id": body["member_id"],
            "amount": float(body["amount"]),
            "method": body["method"],
            "reference_number": body.get("reference", ""),
            "payment_date": body.get("payment_date", date.today().isoformat()),
            "notes": body.get("notes", ""),
            "status": "completed",
        }

        result = get_db().table("hoa_payments").insert(payment_data).execute()

        # Update invoice balance
        _update_invoice_balance(body["invoice_id"])

        # Create journal entry for the payment
        _create_payment_journal_entry(hoa_id, result.data[0], body)

        _log_audit(hoa_id, "payment_recorded", {
            "payment_id": result.data[0]["id"],
            "amount": body["amount"],
            "method": body["method"],
        })

        return jsonify({"payment": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# STRIPE PAYMENT — webhook handler for online payments
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/stripe-webhook", methods=["POST"])
def stripe_payment_webhook():
    """
    Handle Stripe Connect payment completion.
    Called when a homeowner pays online via Stripe Checkout.
    """
    try:
        import stripe
        from config import STRIPE_WEBHOOK_SECRET
        payload = request.get_data()
        sig = request.headers.get("Stripe-Signature")
        event = stripe.Webhook.construct_event(payload, sig, STRIPE_WEBHOOK_SECRET)

        if event["type"] == "checkout.session.completed":
            session = event["data"]["object"]
            metadata = session.get("metadata", {})
            invoice_id = metadata.get("invoice_id")
            hoa_id = metadata.get("hoa_id")
            member_id = metadata.get("member_id")

            if invoice_id and hoa_id:
                payment_data = {
                    "hoa_id": hoa_id,
                    "invoice_id": invoice_id,
                    "member_id": member_id,
                    "amount": session["amount_total"] / 100,
                    "method": "stripe",
                    "stripe_payment_id": session.get("payment_intent"),
                    "payment_date": date.today().isoformat(),
                    "status": "completed",
                }
                result = get_db().table("hoa_payments").insert(payment_data).execute()
                _update_invoice_balance(invoice_id)
                _create_payment_journal_entry(hoa_id, result.data[0], {
                    "amount": payment_data["amount"],
                    "method": "stripe",
                    "member_id": member_id,
                    "invoice_id": invoice_id,
                })

        return jsonify({"received": True}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


# ──────────────────────────────────────────────
# CREATE STRIPE CHECKOUT SESSION (for member portal)
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/invoices/<invoice_id>/pay", methods=["POST"])
def create_checkout(hoa_id, invoice_id):
    """Create Stripe Checkout session for online payment."""
    try:
        import stripe
        from config import STRIPE_SECRET_KEY

        stripe.api_key = STRIPE_SECRET_KEY

        invoice = get_db().table("hoa_invoices").select(
            "*, hoa_members(first_name, last_name, email), "
            "hoa_organizations(name, stripe_account_id)"
        ).eq("id", invoice_id).single().execute()
        inv = invoice.data
        member = inv.get("hoa_members", {})
        org = inv.get("hoa_organizations", {})

        outstanding = float(inv["total_amount"]) - float(inv.get("amount_paid", 0))
        if outstanding <= 0:
            return jsonify({"error": "Invoice already paid"}), 400

        # Destination charge — 1% platform fee
        platform_fee = int(outstanding * 0.01 * 100)  # cents

        session = stripe.checkout.Session.create(
            payment_method_types=["card", "us_bank_account"],
            line_items=[{
                "price_data": {
                    "currency": "usd",
                    "unit_amount": int(outstanding * 100),
                    "product_data": {
                        "name": f"Invoice #{inv['invoice_number']}",
                        "description": f"HOA Dues — {inv['period']}",
                    },
                },
                "quantity": 1,
            }],
            mode="payment",
            customer_email=member.get("email"),
            metadata={
                "hoa_id": hoa_id,
                "invoice_id": invoice_id,
                "member_id": inv["member_id"],
            },
            payment_intent_data={
                "application_fee_amount": platform_fee,
                "transfer_data": {
                    "destination": org.get("stripe_account_id"),
                },
            },
            success_url=f"{request.host_url}hoa/portal/payment-success?invoice={invoice_id}",
            cancel_url=f"{request.host_url}hoa/portal/invoices",
        )

        return jsonify({"checkout_url": session.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# LATE FEE ENGINE — auto-apply
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/late-fees/apply", methods=["POST"])
def apply_late_fees(hoa_id):
    """
    Scan for overdue invoices and apply late fees.
    Called by JARVIS scheduler daily or manually.
    """
    try:
        org = get_db().table("hoa_organizations").select(
            "late_fee_amount, late_fee_type, late_fee_grace_days"
        ).eq("id", hoa_id).single().execute()
        settings = org.data

        grace_days = settings.get("late_fee_grace_days", 15)
        cutoff = (date.today() - timedelta(days=grace_days)).isoformat()

        # Find overdue invoices without late fee already applied
        overdue = get_db().table("hoa_invoices").select("*").eq(
            "hoa_id", hoa_id
        ).in_("status", ["sent", "partial"]).lt("due_date", cutoff).execute()

        applied = []
        for inv in overdue.data:
            # Check if late fee already added
            line_items = inv.get("line_items", [])
            has_late_fee = any("late fee" in (li.get("description", "")).lower() for li in line_items)

            if has_late_fee:
                continue

            # Calculate fee
            fee_type = settings.get("late_fee_type", "flat")
            fee_amount = float(settings.get("late_fee_amount", 25))
            if fee_type == "percent":
                fee_amount = float(inv["total_amount"]) * (fee_amount / 100)

            line_items.append({
                "description": f"Late Fee (applied {date.today().isoformat()})",
                "amount": fee_amount,
            })
            new_total = float(inv["total_amount"]) + fee_amount

            get_db().table("hoa_invoices").update({
                "line_items": line_items,
                "total_amount": new_total,
                "status": "overdue",
            }).eq("id", inv["id"]).execute()

            applied.append(inv["id"])

        _log_audit(hoa_id, "late_fees_applied", {"count": len(applied)})
        return jsonify({"applied": len(applied), "invoice_ids": applied})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# SPECIAL ASSESSMENTS
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/assessments", methods=["GET"])
def list_assessments(hoa_id):
    """List all special assessments."""
    try:
        result = get_db().table("hoa_assessments").select("*").eq(
            "hoa_id", hoa_id
        ).order("created_at", desc=True).execute()
        return jsonify({"assessments": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@invoices_bp.route("/api/hoa/<hoa_id>/assessments", methods=["POST"])
def create_assessment(hoa_id):
    """
    Create a special assessment and generate invoices for all members.
    Body: { "name", "description", "total_amount", "per_unit_amount",
            "due_date", "allow_installments", "installment_count" }
    """
    try:
        body = request.get_json()
        assessment = {
            "hoa_id": hoa_id,
            "name": body["name"],
            "description": body.get("description", ""),
            "total_amount": float(body["total_amount"]),
            "per_unit_amount": float(body["per_unit_amount"]),
            "due_date": body["due_date"],
            "allow_installments": body.get("allow_installments", False),
            "installment_count": body.get("installment_count", 1),
            "status": "active",
        }

        result = get_db().table("hoa_assessments").insert(assessment).execute()
        assessment_id = result.data[0]["id"]

        # Generate invoices for all active members
        members = get_db().table("hoa_members").select("id, unit_id").eq(
            "hoa_id", hoa_id
        ).eq("status", "active").execute()

        installments = int(body.get("installment_count", 1))
        per_installment = float(body["per_unit_amount"]) / installments

        invoices_created = []
        for member in members.data:
            for i in range(installments):
                due_offset = timedelta(days=30 * i)
                base_due = datetime.strptime(body["due_date"], "%Y-%m-%d").date()
                inst_due = (base_due + due_offset).isoformat()

                inv_data = {
                    "hoa_id": hoa_id,
                    "member_id": member["id"],
                    "unit_id": member.get("unit_id"),
                    "period": f"SA-{assessment_id[:8]}",
                    "due_date": inst_due,
                    "line_items": [{
                        "description": f"{body['name']} ({i+1}/{installments})" if installments > 1 else body["name"],
                        "amount": per_installment,
                    }],
                    "total_amount": per_installment,
                    "amount_paid": 0,
                    "status": "draft",
                    "invoice_number": _next_invoice_number(hoa_id, "SA"),
                    "assessment_id": assessment_id,
                }
                inv_result = get_db().table("hoa_invoices").insert(inv_data).execute()
                invoices_created.append(inv_result.data[0]["id"])

        _log_audit(hoa_id, "assessment_created", {
            "assessment_id": assessment_id,
            "invoices": len(invoices_created),
        })

        return jsonify({
            "assessment": result.data[0],
            "invoices_created": len(invoices_created),
        }), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# PAYMENT SUMMARY / COLLECTION STATS
# ──────────────────────────────────────────────
@invoices_bp.route("/api/hoa/<hoa_id>/payments/summary", methods=["GET"])
def payment_summary(hoa_id):
    """Payment method breakdown and collection velocity."""
    try:
        period = request.args.get("period")  # optional "YYYY-MM"

        query = get_db().table("hoa_payments").select("*").eq("hoa_id", hoa_id)
        if period:
            query = query.gte("payment_date", f"{period}-01").lte("payment_date", f"{period}-31")
        payments = query.execute()

        by_method = {}
        total = 0
        for p in payments.data:
            m = p.get("method", "other")
            amt = float(p.get("amount", 0))
            by_method[m] = by_method.get(m, 0) + amt
            total += amt

        return jsonify({
            "total_collected": total,
            "by_method": by_method,
            "payment_count": len(payments.data),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────

def _calculate_late_fees(hoa_id, member_id, settings):
    """Calculate total late fees owed for a member's past-due invoices."""
    overdue = get_db().table("hoa_invoices").select(
        "total_amount, amount_paid"
    ).eq("hoa_id", hoa_id).eq("member_id", member_id).eq("status", "overdue").execute()

    if not overdue.data:
        return 0

    outstanding = sum(
        float(i["total_amount"]) - float(i.get("amount_paid", 0))
        for i in overdue.data
    )

    fee_type = settings.get("late_fee_type", "flat")
    fee_amount = float(settings.get("late_fee_amount", 25))

    if fee_type == "percent":
        return outstanding * (fee_amount / 100)
    return fee_amount if outstanding > 0 else 0


def _update_invoice_balance(invoice_id):
    """Recalculate invoice paid amount and update status."""
    payments = get_db().table("hoa_payments").select("amount").eq(
        "invoice_id", invoice_id
    ).eq("status", "completed").execute()

    total_paid = sum(float(p["amount"]) for p in payments.data)

    invoice = get_db().table("hoa_invoices").select(
        "total_amount"
    ).eq("id", invoice_id).single().execute()

    total_due = float(invoice.data["total_amount"])

    if total_paid >= total_due:
        status = "paid"
    elif total_paid > 0:
        status = "partial"
    else:
        status = "sent"

    get_db().table("hoa_invoices").update({
        "amount_paid": total_paid,
        "status": status,
    }).eq("id", invoice_id).execute()


def _create_payment_journal_entry(hoa_id, payment, body):
    """Create double-entry journal entry for a payment."""
    try:
        # Debit: Cash/Bank (1000)
        # Credit: Accounts Receivable (1200) or Assessment Revenue (4000)
        cash_account = get_db().table("hoa_accounts").select("id").eq(
            "hoa_id", hoa_id
        ).eq("account_number", "1000").single().execute()

        ar_account = get_db().table("hoa_accounts").select("id").eq(
            "hoa_id", hoa_id
        ).eq("account_number", "1200").single().execute()

        if cash_account.data and ar_account.data:
            entry = {
                "hoa_id": hoa_id,
                "date": payment.get("payment_date", date.today().isoformat()),
                "description": f"Payment received — {body.get('method', 'unknown')}",
                "reference": payment.get("reference_number", ""),
                "fund": "operating",
                "status": "posted",
            }
            je = get_db().table("hoa_journal_entries").insert(entry).execute()

            lines = [
                {
                    "journal_entry_id": je.data[0]["id"],
                    "account_id": cash_account.data["id"],
                    "debit": float(body["amount"]),
                    "credit": 0,
                    "description": f"Payment from member",
                },
                {
                    "journal_entry_id": je.data[0]["id"],
                    "account_id": ar_account.data["id"],
                    "debit": 0,
                    "credit": float(body["amount"]),
                    "description": f"Applied to invoice",
                },
            ]
            get_db().table("hoa_journal_lines").insert(lines).execute()
    except Exception:
        pass  # Don't fail payment if journal entry fails


def _next_invoice_number(hoa_id, period):
    """Generate next sequential invoice number: HOA-2026-03-0001"""
    existing = get_db().table("hoa_invoices").select("invoice_number").eq(
        "hoa_id", hoa_id
    ).like("invoice_number", f"%-{period}-%").execute()

    seq = len(existing.data) + 1
    return f"HOA-{period}-{str(seq).zfill(4)}"


def _build_invoice_email(invoice, member, org):
    """Build branded HTML invoice email."""
    accent = org.get("accent_color", "#2563eb")
    name = f"{member.get('first_name', '')} {member.get('last_name', '')}"
    outstanding = float(invoice["total_amount"]) - float(invoice.get("amount_paid", 0))

    lines_html = ""
    for item in invoice.get("line_items", []):
        lines_html += f"""
        <tr>
            <td style="padding:8px;border-bottom:1px solid #eee">{item['description']}</td>
            <td style="padding:8px;border-bottom:1px solid #eee;text-align:right">${item['amount']:.2f}</td>
        </tr>"""

    return f"""
    <div style="max-width:600px;margin:0 auto;font-family:sans-serif">
        <div style="background:{accent};color:white;padding:20px;text-align:center">
            <h1 style="margin:0">{org.get('name', 'HOA')}</h1>
        </div>
        <div style="padding:30px">
            <p>Dear {name},</p>
            <p>Here is your invoice for the period of <strong>{invoice.get('period', '')}</strong>.</p>
            <table style="width:100%;border-collapse:collapse;margin:20px 0">
                <tr style="background:#f9f9f9">
                    <th style="padding:8px;text-align:left">Description</th>
                    <th style="padding:8px;text-align:right">Amount</th>
                </tr>
                {lines_html}
                <tr style="font-weight:bold">
                    <td style="padding:12px 8px">Total Due</td>
                    <td style="padding:12px 8px;text-align:right">${outstanding:.2f}</td>
                </tr>
            </table>
            <p><strong>Due Date:</strong> {invoice.get('due_date', '')}</p>
            <p><strong>Invoice #:</strong> {invoice.get('invoice_number', '')}</p>
            <div style="text-align:center;margin:30px 0">
                <a href="#" style="background:{accent};color:white;padding:14px 40px;text-decoration:none;border-radius:6px;font-size:16px">
                    Pay Now
                </a>
            </div>
            <p style="color:#888;font-size:12px;margin-top:30px">
                Questions? Contact {org.get('contact_email', 'your HOA manager')}.
            </p>
        </div>
    </div>
    """


def _get_send_domain():
    """Get email sending domain from config."""
    try:
        from config import RESEND_DOMAIN
        return RESEND_DOMAIN
    except ImportError:
        return "updates.hoaledger.com"


def _log_audit(hoa_id, action, details=None):
    """Write to audit log."""
    try:
        get_db().table("hoa_audit_log").insert({
            "hoa_id": hoa_id,
            "action": action,
            "details": details or {},
        }).execute()
    except Exception:
        pass
