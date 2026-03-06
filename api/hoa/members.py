"""
HOA Members API — CRUD, CSV Import, Notes, Search
"""
from flask import Blueprint, request, jsonify
import csv
import io
import uuid
from datetime import datetime
from lib.database import get_db

members_bp = Blueprint("hoa_members", __name__)


# ──────────────────────────────────────────────
# LIST MEMBERS (with filters, search, pagination)
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members", methods=["GET"])
def list_members(hoa_id):
    """List all members for an HOA with optional filters."""
    try:
        status = request.args.get("status")  # current | past_due | new
        search = request.args.get("search", "").strip()
        sort_by = request.args.get("sort", "last_name")
        sort_dir = request.args.get("dir", "asc")
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        offset = (page - 1) * per_page

        query = get_db().table("hoa_members").select(
            "*, hoa_units(unit_number, address)",
            count="exact"
        ).eq("hoa_id", hoa_id)

        if status:
            query = query.eq("status", status)

        if search:
            query = query.or_(
                f"first_name.ilike.%{search}%,"
                f"last_name.ilike.%{search}%,"
                f"email.ilike.%{search}%,"
                f"phone.ilike.%{search}%"
            )

        # Sort
        query = query.order(sort_by, desc=(sort_dir == "desc"))
        query = query.range(offset, offset + per_page - 1)

        result = query.execute()

        return jsonify({
            "members": result.data,
            "total": result.count,
            "page": page,
            "per_page": per_page,
            "pages": -(-result.count // per_page) if result.count else 0  # ceiling division
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# GET SINGLE MEMBER (with notes, payments)
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/<member_id>", methods=["GET"])
def get_member(hoa_id, member_id):
    """Get a single member with their notes and recent payments."""
    try:
        member = get_db().table("hoa_members").select(
            "*, hoa_units(unit_number, address)"
        ).eq("id", member_id).eq("hoa_id", hoa_id).single().execute()

        notes = get_db().table("hoa_member_notes").select("*").eq(
            "member_id", member_id
        ).order("created_at", desc=True).limit(20).execute()

        payments = get_db().table("hoa_payments").select("*").eq(
            "member_id", member_id
        ).order("payment_date", desc=True).limit(10).execute()

        newsletters = get_db().table("hoa_newsletters").select("*").eq(
            "hoa_id", hoa_id
        ).order("published_at", desc=True).limit(5).execute()

        return jsonify({
            "member": member.data,
            "notes": notes.data,
            "payments": payments.data,
            "newsletters": newsletters.data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CREATE MEMBER
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members", methods=["POST"])
def create_member(hoa_id):
    """Create a new member."""
    try:
        data = request.json
        data["hoa_id"] = hoa_id
        data["status"] = data.get("status", "new")

        # Get default dues from org if not specified
        if "monthly_dues" not in data or not data["monthly_dues"]:
            org = get_db().table("hoa_organizations").select(
                "default_monthly_dues"
            ).eq("id", hoa_id).single().execute()
            data["monthly_dues"] = org.data.get("default_monthly_dues", 0)

        result = get_db().table("hoa_members").insert(data).execute()

        # Log to audit
        _log_audit(hoa_id, None, "create", "member", result.data[0]["id"], new_values=data)

        return jsonify({"member": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# UPDATE MEMBER (inline edit from grid)
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/<member_id>", methods=["PATCH"])
def update_member(hoa_id, member_id):
    """Update a member's fields (supports partial update for inline editing)."""
    try:
        data = request.json

        # Get old values for audit
        old = get_db().table("hoa_members").select("*").eq(
            "id", member_id
        ).single().execute()

        result = get_db().table("hoa_members").update(data).eq(
            "id", member_id
        ).eq("hoa_id", hoa_id).execute()

        # Log to audit
        _log_audit(hoa_id, None, "update", "member", member_id,
                   old_values={k: old.data.get(k) for k in data},
                   new_values=data)

        return jsonify({"member": result.data[0]})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# DELETE MEMBER
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/<member_id>", methods=["DELETE"])
def delete_member(hoa_id, member_id):
    """Soft-delete a member (set status to inactive)."""
    try:
        result = get_db().table("hoa_members").update(
            {"status": "inactive"}
        ).eq("id", member_id).eq("hoa_id", hoa_id).execute()

        _log_audit(hoa_id, None, "delete", "member", member_id)

        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# BULK UPDATE (from grid selection)
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/bulk", methods=["PATCH"])
def bulk_update_members(hoa_id):
    """Bulk update selected members (apply late fee, change status, etc.)."""
    try:
        data = request.json
        member_ids = data.get("member_ids", [])
        updates = data.get("updates", {})

        if not member_ids or not updates:
            return jsonify({"error": "member_ids and updates required"}), 400

        results = []
        for mid in member_ids:
            r = get_db().table("hoa_members").update(updates).eq(
                "id", mid
            ).eq("hoa_id", hoa_id).execute()
            results.extend(r.data)

        return jsonify({"updated": len(results), "members": results})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CSV IMPORT
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/import/preview", methods=["POST"])
def preview_csv_import(hoa_id):
    """Upload CSV and preview the import (column mapping + data preview)."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))

        rows = []
        headers = reader.fieldnames or []

        for i, row in enumerate(reader):
            if i >= 200:  # Preview max 200 rows
                break
            rows.append(dict(row))

        # Auto-detect column mapping
        mapping = _auto_map_columns(headers)

        return jsonify({
            "headers": headers,
            "suggested_mapping": mapping,
            "preview_rows": rows[:10],
            "total_rows": len(rows),
            "filename": file.filename
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@members_bp.route("/api/hoa/<hoa_id>/members/import", methods=["POST"])
def execute_csv_import(hoa_id):
    """Execute the CSV import with the provided column mapping."""
    try:
        data = request.json
        rows = data.get("rows", [])
        mapping = data.get("mapping", {})
        filename = data.get("filename", "import.csv")

        if not rows or not mapping:
            return jsonify({"error": "rows and mapping required"}), 400

        # Get org default dues
        org = get_db().table("hoa_organizations").select(
            "default_monthly_dues"
        ).eq("id", hoa_id).single().execute()
        default_dues = org.data.get("default_monthly_dues", 0)

        successful = 0
        warnings = 0
        errors = 0
        error_details = []

        for i, row in enumerate(rows):
            try:
                member_data = {
                    "hoa_id": hoa_id,
                    "status": "new"
                }

                # Apply column mapping
                for csv_col, hoa_field in mapping.items():
                    if hoa_field and hoa_field != "skip" and csv_col in row:
                        value = row[csv_col].strip() if row[csv_col] else ""
                        if hoa_field == "monthly_dues":
                            value = float(value.replace("$", "").replace(",", "")) if value else default_dues
                        member_data[hoa_field] = value

                # Ensure required fields
                if not member_data.get("first_name") and not member_data.get("last_name"):
                    # Try splitting a "name" field
                    name = member_data.pop("name", "")
                    if name:
                        parts = name.split(" ", 1)
                        member_data["first_name"] = parts[0]
                        member_data["last_name"] = parts[1] if len(parts) > 1 else ""

                if not member_data.get("first_name"):
                    warnings += 1
                    error_details.append({"row": i + 1, "issue": "Missing name"})
                    continue

                if not member_data.get("monthly_dues"):
                    member_data["monthly_dues"] = default_dues

                # Create unit if unit number provided
                unit_number = member_data.pop("unit_number", None) or member_data.pop("unit", None)
                if unit_number:
                    # Upsert unit
                    unit_result = get_db().table("hoa_units").upsert({
                        "hoa_id": hoa_id,
                        "unit_number": str(unit_number).strip(),
                        "address": member_data.get("address", "")
                    }, on_conflict="hoa_id,unit_number").execute()
                    if unit_result.data:
                        member_data["unit_id"] = unit_result.data[0]["id"]

                # Remove non-member fields
                member_data.pop("unit", None)
                member_data.pop("name", None)

                get_db().table("hoa_members").insert(member_data).execute()
                successful += 1

            except Exception as row_error:
                errors += 1
                error_details.append({"row": i + 1, "issue": str(row_error)})

        # Log the import
        get_db().table("hoa_csv_imports").insert({
            "hoa_id": hoa_id,
            "filename": filename,
            "import_type": "members",
            "total_rows": len(rows),
            "successful": successful,
            "warnings": warnings,
            "errors": errors,
            "column_mapping": mapping,
            "status": "completed"
        }).execute()

        return jsonify({
            "success": True,
            "total": len(rows),
            "successful": successful,
            "warnings": warnings,
            "errors": errors,
            "error_details": error_details[:20]
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# CSV EXPORT
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/export", methods=["GET"])
def export_members_csv(hoa_id):
    """Export all members as CSV."""
    try:
        result = get_db().table("hoa_members").select(
            "first_name, last_name, email, phone, address, monthly_dues, current_balance, status, "
            "hoa_units(unit_number)"
        ).eq("hoa_id", hoa_id).order("last_name").execute()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["First Name", "Last Name", "Email", "Phone", "Address",
                         "Unit", "Monthly Dues", "Balance", "Status"])

        for m in result.data:
            unit = m.get("hoa_units", {})
            writer.writerow([
                m.get("first_name", ""),
                m.get("last_name", ""),
                m.get("email", ""),
                m.get("phone", ""),
                m.get("address", ""),
                unit.get("unit_number", "") if unit else "",
                m.get("monthly_dues", 0),
                m.get("current_balance", 0),
                m.get("status", "")
            ])

        from flask import Response
        return Response(
            output.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment;filename=members_{hoa_id}.csv"}
        )
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# MEMBER NOTES
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/members/<member_id>/notes", methods=["GET"])
def list_notes(hoa_id, member_id):
    """Get all notes for a member."""
    try:
        result = get_db().table("hoa_member_notes").select(
            "*, hoa_users(first_name, last_name)"
        ).eq("member_id", member_id).order("created_at", desc=True).execute()
        return jsonify({"notes": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@members_bp.route("/api/hoa/<hoa_id>/members/<member_id>/notes", methods=["POST"])
def add_note(hoa_id, member_id):
    """Add a manager note to a member."""
    try:
        data = request.json
        note_data = {
            "hoa_id": hoa_id,
            "member_id": member_id,
            "note": data.get("note", ""),
            "note_type": data.get("note_type", "general"),
            "author_id": data.get("author_id"),
            "is_pinned": data.get("is_pinned", False)
        }
        result = get_db().table("hoa_member_notes").insert(note_data).execute()
        return jsonify({"note": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@members_bp.route("/api/hoa/<hoa_id>/notes/<note_id>", methods=["DELETE"])
def delete_note(hoa_id, note_id):
    """Delete a note."""
    try:
        get_db().table("hoa_member_notes").delete().eq(
            "id", note_id
        ).eq("hoa_id", hoa_id).execute()
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# DASHBOARD STATS
# ──────────────────────────────────────────────
@members_bp.route("/api/hoa/<hoa_id>/dashboard", methods=["GET"])
def dashboard_stats(hoa_id):
    """Get dashboard summary statistics."""
    try:
        # Member counts
        members = get_db().table("hoa_members").select(
            "id, status, current_balance", count="exact"
        ).eq("hoa_id", hoa_id).execute()

        total = members.count or 0
        current = sum(1 for m in members.data if m["status"] == "current")
        past_due = sum(1 for m in members.data if m["status"] == "past_due")
        new_members = sum(1 for m in members.data if m["status"] == "new")
        outstanding = sum(float(m["current_balance"] or 0) for m in members.data if float(m["current_balance"] or 0) > 0)

        # Payments this month
        from datetime import date
        month_start = date.today().replace(day=1).isoformat()
        payments = get_db().table("hoa_payments").select(
            "amount"
        ).eq("hoa_id", hoa_id).eq("status", "completed").gte(
            "payment_date", month_start
        ).execute()
        collected = sum(float(p["amount"]) for p in payments.data)

        # Recent activity (last 10 audit entries)
        activity = get_db().table("hoa_audit_log").select("*").eq(
            "hoa_id", hoa_id
        ).order("created_at", desc=True).limit(10).execute()

        # Upcoming reminders
        reminders = get_db().table("hoa_reminder_rules").select("*").eq(
            "hoa_id", hoa_id
        ).eq("is_active", True).order("next_run_at").limit(5).execute()

        return jsonify({
            "stats": {
                "total_members": total,
                "current_members": current,
                "past_due_members": past_due,
                "new_members": new_members,
                "outstanding_balance": round(outstanding, 2),
                "collected_this_month": round(collected, 2),
                "collection_rate": round((collected / (collected + outstanding) * 100), 1) if (collected + outstanding) > 0 else 0
            },
            "recent_activity": activity.data,
            "upcoming_reminders": reminders.data
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def _auto_map_columns(headers):
    """Auto-detect column mapping from CSV headers."""
    mapping = {}
    field_hints = {
        "first_name": ["first name", "first", "fname", "given name"],
        "last_name": ["last name", "last", "lname", "surname", "family name"],
        "name": ["name", "full name", "owner", "owner name", "resident"],
        "email": ["email", "e-mail", "email address", "mail"],
        "phone": ["phone", "telephone", "cell", "mobile", "phone number"],
        "address": ["address", "street", "street address", "mailing address"],
        "unit_number": ["unit", "unit #", "unit number", "apt", "apartment", "suite", "unit no"],
        "monthly_dues": ["dues", "monthly dues", "amount", "monthly amount", "hoa dues", "assessment"],
    }

    for header in headers:
        h_lower = header.lower().strip()
        matched = False
        for field, hints in field_hints.items():
            if h_lower in hints or any(hint in h_lower for hint in hints):
                mapping[header] = field
                matched = True
                break
        if not matched:
            mapping[header] = "skip"

    return mapping


def _log_audit(hoa_id, user_id, action, entity_type, entity_id, old_values=None, new_values=None):
    """Log an action to the audit trail."""
    try:
        get_db().table("hoa_audit_log").insert({
            "hoa_id": hoa_id,
            "user_id": user_id,
            "action": action,
            "entity_type": entity_type,
            "entity_id": str(entity_id),
            "old_values": old_values,
            "new_values": new_values
        }).execute()
    except Exception:
        pass  # Don't fail the main operation if audit logging fails
