"""
HOA Accounting API — Ledger, Journal Entries, Bank Import, Chart of Accounts
"""
from flask import Blueprint, request, jsonify
import csv
import io
from datetime import datetime, date
from lib.database import get_db

accounting_bp = Blueprint("hoa_accounting", __name__)


# ──────────────────────────────────────────────
# CHART OF ACCOUNTS
# ──────────────────────────────────────────────
@accounting_bp.route("/api/hoa/<hoa_id>/accounts", methods=["GET"])
def list_accounts(hoa_id):
    """List all accounts in the chart of accounts."""
    try:
        result = get_db().table("hoa_accounts").select("*").eq(
            "hoa_id", hoa_id
        ).eq("is_active", True).order("account_number").execute()
        return jsonify({"accounts": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/accounts", methods=["POST"])
def create_account(hoa_id):
    """Add a new account to the chart of accounts."""
    try:
        data = request.json
        data["hoa_id"] = hoa_id
        result = get_db().table("hoa_accounts").insert(data).execute()
        return jsonify({"account": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/accounts/seed", methods=["POST"])
def seed_accounts(hoa_id):
    """Seed the default chart of accounts for a new HOA."""
    try:
        get_db().rpc("seed_default_accounts", {"p_hoa_id": hoa_id}).execute()
        return jsonify({"success": True, "message": "Default accounts created"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# LEDGER / JOURNAL ENTRIES
# ──────────────────────────────────────────────
@accounting_bp.route("/api/hoa/<hoa_id>/ledger", methods=["GET"])
def list_journal_entries(hoa_id):
    """List journal entries with optional filters (date range, fund, account)."""
    try:
        fund = request.args.get("fund")
        account_id = request.args.get("account_id")
        start_date = request.args.get("start_date")
        end_date = request.args.get("end_date")
        page = int(request.args.get("page", 1))
        per_page = int(request.args.get("per_page", 50))
        offset = (page - 1) * per_page

        query = get_db().table("hoa_journal_entries").select(
            "*, hoa_journal_lines(*, hoa_accounts(account_number, account_name, fund), "
            "hoa_members(first_name, last_name))",
            count="exact"
        ).eq("hoa_id", hoa_id).eq("status", "posted")

        if start_date:
            query = query.gte("entry_date", start_date)
        if end_date:
            query = query.lte("entry_date", end_date)

        query = query.order("entry_date", desc=True).range(offset, offset + per_page - 1)
        result = query.execute()

        # Calculate running totals
        total_debits = 0
        total_credits = 0
        for entry in result.data:
            for line in entry.get("hoa_journal_lines", []):
                total_debits += float(line.get("debit", 0))
                total_credits += float(line.get("credit", 0))

        return jsonify({
            "entries": result.data,
            "total": result.count,
            "total_debits": round(total_debits, 2),
            "total_credits": round(total_credits, 2),
            "balanced": abs(total_debits - total_credits) < 0.01,
            "page": page,
            "per_page": per_page
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/ledger", methods=["POST"])
def create_journal_entry(hoa_id):
    """Create a new journal entry with debit/credit lines."""
    try:
        data = request.json
        lines = data.pop("lines", [])

        if not lines:
            return jsonify({"error": "At least one journal line required"}), 400

        # Validate debits = credits
        total_debit = sum(float(l.get("debit", 0)) for l in lines)
        total_credit = sum(float(l.get("credit", 0)) for l in lines)
        if abs(total_debit - total_credit) > 0.01:
            return jsonify({"error": f"Debits ({total_debit}) must equal credits ({total_credit})"}), 400

        # Create the entry
        entry_data = {
            "hoa_id": hoa_id,
            "entry_date": data.get("entry_date", date.today().isoformat()),
            "description": data.get("description", ""),
            "reference": data.get("reference"),
            "source": data.get("source", "manual"),
            "posted_by": data.get("posted_by"),
            "status": "posted"
        }
        entry_result = get_db().table("hoa_journal_entries").insert(entry_data).execute()
        entry_id = entry_result.data[0]["id"]

        # Create lines
        for line in lines:
            line_data = {
                "journal_entry_id": entry_id,
                "account_id": line["account_id"],
                "debit": float(line.get("debit", 0)),
                "credit": float(line.get("credit", 0)),
                "member_id": line.get("member_id"),
                "memo": line.get("memo")
            }
            get_db().table("hoa_journal_lines").insert(line_data).execute()

        return jsonify({"entry": entry_result.data[0], "lines_created": len(lines)}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# BANK STATEMENT IMPORT
# ──────────────────────────────────────────────
@accounting_bp.route("/api/hoa/<hoa_id>/bank-import/upload", methods=["POST"])
def upload_bank_statement(hoa_id):
    """Upload a bank CSV and auto-match transactions."""
    try:
        if "file" not in request.files:
            return jsonify({"error": "No file uploaded"}), 400

        file = request.files["file"]
        bank_name = request.form.get("bank_name", "")
        content = file.read().decode("utf-8-sig")
        reader = csv.DictReader(io.StringIO(content))

        # Create import batch
        batch = get_db().table("hoa_bank_imports").insert({
            "hoa_id": hoa_id,
            "filename": file.filename,
            "bank_name": bank_name,
            "status": "reviewing"
        }).execute()
        batch_id = batch.data[0]["id"]

        # Get matching rules
        rules = get_db().table("hoa_matching_rules").select("*").eq(
            "hoa_id", hoa_id
        ).eq("is_active", True).order("priority", desc=True).execute()

        # Get members for name matching
        members = get_db().table("hoa_members").select(
            "id, first_name, last_name"
        ).eq("hoa_id", hoa_id).execute()

        transactions = []
        auto_matched = 0
        needs_review = 0

        for row in reader:
            # Parse the transaction (flexible column detection)
            txn = _parse_bank_row(row)
            if not txn:
                continue

            # Try to auto-match
            match = _match_transaction(txn, rules.data, members.data)

            txn_data = {
                "import_id": batch_id,
                "hoa_id": hoa_id,
                "transaction_date": txn["date"],
                "description": txn["description"],
                "amount": txn["amount"],
                "suggested_account_id": match.get("account_id"),
                "suggested_member_id": match.get("member_id"),
                "match_confidence": match.get("confidence", 0),
                "match_rule": match.get("rule"),
                "status": "matched" if match.get("confidence", 0) > 0.7 else "unmatched"
            }

            result = get_db().table("hoa_bank_transactions").insert(txn_data).execute()
            transactions.append(result.data[0])

            if match.get("confidence", 0) > 0.7:
                auto_matched += 1
            else:
                needs_review += 1

        # Update batch totals
        get_db().table("hoa_bank_imports").update({
            "total_transactions": len(transactions),
            "auto_matched": auto_matched,
            "needs_review": needs_review
        }).eq("id", batch_id).execute()

        return jsonify({
            "batch_id": batch_id,
            "total_transactions": len(transactions),
            "auto_matched": auto_matched,
            "needs_review": needs_review,
            "transactions": transactions[:20]  # Preview first 20
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/bank-import/<batch_id>/approve", methods=["POST"])
def approve_bank_import(hoa_id, batch_id):
    """Approve matched transactions and post them as journal entries."""
    try:
        data = request.json
        transaction_ids = data.get("transaction_ids", [])
        approve_all = data.get("approve_all", False)

        query = get_db().table("hoa_bank_transactions").select("*").eq("import_id", batch_id)
        if not approve_all:
            query = query.in_("id", transaction_ids)
        else:
            query = query.eq("status", "matched")

        transactions = query.execute()
        posted = 0

        for txn in transactions.data:
            if not txn.get("suggested_account_id"):
                continue

            # Create journal entry from bank transaction
            amount = abs(float(txn["amount"]))
            is_debit = float(txn["amount"]) < 0  # Negative = money out = debit expense

            entry = get_db().table("hoa_journal_entries").insert({
                "hoa_id": hoa_id,
                "entry_date": txn["transaction_date"],
                "description": txn["description"],
                "reference": f"BANK-{batch_id[:8]}",
                "source": "bank_import",
                "status": "posted"
            }).execute()
            entry_id = entry.data[0]["id"]

            # Cash account (1000)
            cash = get_db().table("hoa_accounts").select("id").eq(
                "hoa_id", hoa_id
            ).eq("account_number", "1000").single().execute()

            if is_debit:
                # Money out: Debit expense, Credit cash
                get_db().table("hoa_journal_lines").insert([
                    {"journal_entry_id": entry_id, "account_id": txn["suggested_account_id"],
                     "debit": amount, "credit": 0, "member_id": txn.get("suggested_member_id")},
                    {"journal_entry_id": entry_id, "account_id": cash.data["id"],
                     "debit": 0, "credit": amount}
                ]).execute()
            else:
                # Money in: Debit cash, Credit revenue
                get_db().table("hoa_journal_lines").insert([
                    {"journal_entry_id": entry_id, "account_id": cash.data["id"],
                     "debit": amount, "credit": 0},
                    {"journal_entry_id": entry_id, "account_id": txn["suggested_account_id"],
                     "debit": 0, "credit": amount, "member_id": txn.get("suggested_member_id")}
                ]).execute()

            # Update transaction status
            get_db().table("hoa_bank_transactions").update({
                "status": "posted",
                "journal_entry_id": entry_id
            }).eq("id", txn["id"]).execute()
            posted += 1

        # Update batch status if all done
        remaining = get_db().table("hoa_bank_transactions").select(
            "id", count="exact"
        ).eq("import_id", batch_id).in_("status", ["matched", "unmatched"]).execute()

        if remaining.count == 0:
            get_db().table("hoa_bank_imports").update({"status": "posted"}).eq("id", batch_id).execute()

        return jsonify({"posted": posted})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# MATCHING RULES
# ──────────────────────────────────────────────
@accounting_bp.route("/api/hoa/<hoa_id>/matching-rules", methods=["GET"])
def list_matching_rules(hoa_id):
    try:
        result = get_db().table("hoa_matching_rules").select(
            "*, hoa_accounts(account_number, account_name)"
        ).eq("hoa_id", hoa_id).order("priority", desc=True).execute()
        return jsonify({"rules": result.data})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/matching-rules", methods=["POST"])
def create_matching_rule(hoa_id):
    try:
        data = request.json
        data["hoa_id"] = hoa_id
        result = get_db().table("hoa_matching_rules").insert(data).execute()
        return jsonify({"rule": result.data[0]}), 201
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# FINANCIAL REPORTS
# ──────────────────────────────────────────────
@accounting_bp.route("/api/hoa/<hoa_id>/reports/income-statement", methods=["GET"])
def income_statement(hoa_id):
    """Generate income statement for a period."""
    try:
        start_date = request.args.get("start_date", date.today().replace(month=1, day=1).isoformat())
        end_date = request.args.get("end_date", date.today().isoformat())
        fund = request.args.get("fund")

        # Get all posted journal lines with accounts
        query = get_db().table("hoa_journal_lines").select(
            "debit, credit, hoa_accounts(account_number, account_name, account_type, fund), "
            "hoa_journal_entries!inner(entry_date, status, hoa_id)"
        ).eq("hoa_journal_entries.hoa_id", hoa_id).eq(
            "hoa_journal_entries.status", "posted"
        ).gte("hoa_journal_entries.entry_date", start_date).lte(
            "hoa_journal_entries.entry_date", end_date
        )

        result = query.execute()

        revenue = {}
        expenses = {}
        total_revenue = 0
        total_expenses = 0

        for line in result.data:
            acct = line.get("hoa_accounts", {})
            if not acct:
                continue

            if fund and acct.get("fund") != fund:
                continue

            acct_key = f"{acct['account_number']} - {acct['account_name']}"

            if acct["account_type"] == "revenue":
                amount = float(line.get("credit", 0)) - float(line.get("debit", 0))
                revenue[acct_key] = revenue.get(acct_key, 0) + amount
                total_revenue += amount

            elif acct["account_type"] == "expense":
                amount = float(line.get("debit", 0)) - float(line.get("credit", 0))
                expenses[acct_key] = expenses.get(acct_key, 0) + amount
                total_expenses += amount

        return jsonify({
            "period": {"start": start_date, "end": end_date, "fund": fund},
            "revenue": {k: round(v, 2) for k, v in sorted(revenue.items())},
            "expenses": {k: round(v, 2) for k, v in sorted(expenses.items())},
            "total_revenue": round(total_revenue, 2),
            "total_expenses": round(total_expenses, 2),
            "net_income": round(total_revenue - total_expenses, 2)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@accounting_bp.route("/api/hoa/<hoa_id>/reports/collections", methods=["GET"])
def collections_report(hoa_id):
    """Aging report — who owes what, grouped by 30/60/90+ days."""
    try:
        members = get_db().table("hoa_members").select(
            "id, first_name, last_name, current_balance, status, "
            "hoa_units(unit_number)"
        ).eq("hoa_id", hoa_id).gt("current_balance", 0).order(
            "current_balance", desc=True
        ).execute()

        # Get overdue invoices for aging
        invoices = get_db().table("hoa_invoices").select(
            "member_id, balance_due, due_date"
        ).eq("hoa_id", hoa_id).in_(
            "status", ["sent", "overdue", "partial"]
        ).execute()

        # Build aging buckets per member
        today = date.today()
        aging = {}
        for inv in invoices.data:
            mid = inv["member_id"]
            if mid not in aging:
                aging[mid] = {"current": 0, "days_31_60": 0, "days_61_90": 0, "over_90": 0}

            due = date.fromisoformat(inv["due_date"])
            days_late = (today - due).days
            amount = float(inv["balance_due"])

            if days_late <= 30:
                aging[mid]["current"] += amount
            elif days_late <= 60:
                aging[mid]["days_31_60"] += amount
            elif days_late <= 90:
                aging[mid]["days_61_90"] += amount
            else:
                aging[mid]["over_90"] += amount

        # Combine
        report = []
        for m in members.data:
            a = aging.get(m["id"], {})
            unit = m.get("hoa_units", {})
            report.append({
                "member_id": m["id"],
                "name": f"{m['first_name']} {m['last_name']}",
                "unit": unit.get("unit_number", "") if unit else "",
                "total_balance": float(m["current_balance"]),
                "current": round(a.get("current", 0), 2),
                "days_31_60": round(a.get("days_31_60", 0), 2),
                "days_61_90": round(a.get("days_61_90", 0), 2),
                "over_90": round(a.get("over_90", 0), 2)
            })

        total_outstanding = sum(r["total_balance"] for r in report)

        return jsonify({
            "report": report,
            "total_outstanding": round(total_outstanding, 2),
            "total_accounts": len(report)
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ──────────────────────────────────────────────
# HELPERS
# ──────────────────────────────────────────────
def _parse_bank_row(row):
    """Parse a bank CSV row with flexible column detection."""
    date_val = None
    desc_val = None
    amount_val = None

    for key, val in row.items():
        k = key.lower().strip()
        v = (val or "").strip()

        if not v:
            continue

        if k in ["date", "transaction date", "post date", "posting date"]:
            try:
                for fmt in ["%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d", "%m-%d-%Y"]:
                    try:
                        date_val = datetime.strptime(v, fmt).date().isoformat()
                        break
                    except ValueError:
                        continue
            except Exception:
                pass

        elif k in ["description", "memo", "payee", "transaction description", "name"]:
            desc_val = v

        elif k in ["amount", "debit", "credit", "withdrawal", "deposit"]:
            try:
                clean = v.replace("$", "").replace(",", "").replace("(", "-").replace(")", "")
                amount_val = float(clean)
                if k in ["debit", "withdrawal"] and amount_val > 0:
                    amount_val = -amount_val
            except ValueError:
                pass

    if date_val and desc_val and amount_val is not None:
        return {"date": date_val, "description": desc_val, "amount": amount_val}
    return None


def _match_transaction(txn, rules, members):
    """Try to match a bank transaction to an account and/or member."""
    desc_upper = txn["description"].upper()
    amount = float(txn["amount"])

    # Check rules first
    for rule in rules:
        pattern = rule["pattern"].upper()
        if rule["match_type"] == "contains" and pattern in desc_upper:
            return {
                "account_id": rule.get("account_id"),
                "member_id": rule.get("member_id"),
                "confidence": 0.9,
                "rule": rule["pattern"]
            }
        elif rule["match_type"] == "starts_with" and desc_upper.startswith(pattern):
            return {
                "account_id": rule.get("account_id"),
                "member_id": rule.get("member_id"),
                "confidence": 0.85,
                "rule": rule["pattern"]
            }

    # Try member name matching (for incoming payments)
    if amount > 0:
        for member in members:
            name = f"{member['first_name']} {member['last_name']}".upper()
            last = member["last_name"].upper()
            if name in desc_upper or last in desc_upper:
                return {
                    "member_id": member["id"],
                    "confidence": 0.75,
                    "rule": f"Name match: {member['first_name']} {member['last_name']}"
                }

    return {"confidence": 0}
