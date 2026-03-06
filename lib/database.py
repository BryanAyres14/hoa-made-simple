"""
NearMeHQ Database Layer (Supabase)
===================================
All database operations in one place. Every other module imports from here.
"""
from supabase import create_client as _supabase_create_client, Client
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY
from datetime import datetime, timezone
import json

# Use service key for server-side operations (full access)
# Use anon key for client-side / read-only operations
_supabase: Client = None


def get_db() -> Client:
    """Get or create Supabase client (singleton)."""
    global _supabase
    if _supabase is None:
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        if not key:
            raise ValueError("No Supabase key configured. Set SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY.")
        _supabase = _supabase_create_client(SUPABASE_URL, key)
    return _supabase


# ============================================================
# CLIENTS
# ============================================================

def get_client_by_twilio(twilio_number: str) -> dict | None:
    """Look up a business by its Twilio tracking number."""
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("twilio_number", twilio_number) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_id(client_id: str) -> dict | None:
    """Look up a business by UUID."""
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("id", client_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_stripe_customer(stripe_customer_id: str) -> dict | None:
    """Look up business by Stripe customer ID."""
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("stripe_customer_id", stripe_customer_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_stripe_subscription(subscription_id: str) -> dict | None:
    """Look up business by Stripe subscription ID."""
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("stripe_subscription_id", subscription_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_clients_by_city_category(city: str, category: str, status_filter: list = None) -> list:
    """Get all businesses in a city/category, optionally filtered by status."""
    query = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("city", city) \
        .eq("category", category)
    if status_filter:
        query = query.in_("status", status_filter)
    result = query.order("rank_position", desc=False).execute()
    return result.data or []


def get_paying_clients(city: str = None, category: str = None) -> list:
    """Get paying clients, optionally filtered."""
    query = get_db().table("nearmehq_clients") \
        .select("*") \
        .in_("status", ["starter", "premium", "exclusive"])
    if city:
        query = query.eq("city", city)
    if category:
        query = query.eq("category", category)
    result = query.execute()
    return result.data or []


def update_client(client_id: str, updates: dict) -> dict:
    """Update a client record."""
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = get_db().table("nearmehq_clients") \
        .update(updates) \
        .eq("id", client_id) \
        .execute()
    return result.data[0] if result.data else None


def create_client(data: dict) -> dict:
    """Insert a new client."""
    result = get_db().table("nearmehq_clients") \
        .insert(data) \
        .execute()
    return result.data[0] if result.data else None


def activate_client(client_id: str, tier: str, stripe_customer_id: str,
                    stripe_subscription_id: str, twilio_number: str, real_phone: str) -> dict:
    """Activate a client after payment — sets tier, Stripe IDs, Twilio number."""
    from config import TIERS
    tier_config = TIERS.get(tier, {})
    return update_client(client_id, {
        "status": tier,
        "tier_price": tier_config.get("price", 0) / 100,  # Convert cents to dollars
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "twilio_number": twilio_number,
        "real_phone": real_phone,
        "forward_hours": tier_config.get("forward_hours", "business"),
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    })


def deactivate_client(client_id: str) -> dict:
    """Deactivate when subscription canceled — downgrade to claimed, stop forwarding."""
    return update_client(client_id, {
        "status": "claimed",
        "tier_price": 0,
        "stripe_subscription_id": None,
        "forward_hours": None,
    })


# ============================================================
# CALLS
# ============================================================

def log_call(call_data: dict) -> dict:
    """Log a call event."""
    result = get_db().table("nearmehq_calls") \
        .insert(call_data) \
        .execute()
    return result.data[0] if result.data else None


def get_calls_for_client(twilio_number: str, days: int = 30) -> list:
    """Get recent calls for a client's tracking number."""
    from datetime import timedelta
    since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    result = get_db().table("nearmehq_calls") \
        .select("*") \
        .eq("twilio_number", twilio_number) \
        .gte("created_at", since) \
        .order("created_at", desc=True) \
        .execute()
    return result.data or []


def get_call_stats(twilio_number: str, days: int = 30) -> dict:
    """Get aggregated call stats for a tracking number."""
    calls = get_calls_for_client(twilio_number, days)
    total = len(calls)
    forwarded = sum(1 for c in calls if c.get("call_type") == "forwarded")
    captured = sum(1 for c in calls if c.get("call_type") == "captured")
    voicemail = sum(1 for c in calls if c.get("call_type") == "voicemail")
    missed = sum(1 for c in calls if c.get("call_type") == "missed")
    total_duration = sum(c.get("duration", 0) for c in calls)
    avg_duration = total_duration / max(forwarded, 1)

    return {
        "total_calls": total,
        "forwarded": forwarded,
        "captured": captured,
        "voicemail": voicemail,
        "missed": missed,
        "total_duration": total_duration,
        "avg_duration": round(avg_duration, 1),
        "period_days": days,
    }


def update_call(call_sid: str, updates: dict) -> dict:
    """Update a call record (e.g., add transcription, recording URL)."""
    result = get_db().table("nearmehq_calls") \
        .update(updates) \
        .eq("call_sid", call_sid) \
        .execute()
    return result.data[0] if result.data else None


# ============================================================
# LEADS
# ============================================================

def create_lead(lead_data: dict) -> dict:
    """Save a captured lead."""
    result = get_db().table("nearmehq_leads") \
        .insert(lead_data) \
        .execute()
    return result.data[0] if result.data else None


def get_leads(city: str = None, category: str = None, status: str = "new") -> list:
    """Get leads, optionally filtered."""
    query = get_db().table("nearmehq_leads") \
        .select("*") \
        .eq("status", status)
    if city:
        query = query.eq("city", city)
    if category:
        query = query.eq("category", category)
    result = query.order("created_at", desc=True).execute()
    return result.data or []


def assign_lead(lead_id: str, client_id: str) -> dict:
    """Assign a lead to a paying client."""
    return get_db().table("nearmehq_leads") \
        .update({
            "assigned_to": client_id,
            "status": "assigned",
            "assigned_at": datetime.now(timezone.utc).isoformat(),
        }) \
        .eq("id", lead_id) \
        .execute().data[0]


def sell_lead(lead_id: str, price: float) -> dict:
    """Mark a lead as sold."""
    return get_db().table("nearmehq_leads") \
        .update({
            "status": "sold",
            "sold_price": price,
            "sold_at": datetime.now(timezone.utc).isoformat(),
        }) \
        .eq("id", lead_id) \
        .execute().data[0]


# ============================================================
# REVENUE
# ============================================================

def log_revenue(client_id: str, amount: float, revenue_type: str,
                description: str = "", stripe_payment_id: str = "") -> dict:
    """Log a revenue event."""
    result = get_db().table("nearmehq_revenue") \
        .insert({
            "client_id": client_id,
            "amount": amount,
            "type": revenue_type,
            "description": description,
            "stripe_payment_id": stripe_payment_id,
        }) \
        .execute()
    return result.data[0] if result.data else None


def get_revenue_by_city() -> list:
    """Get revenue aggregated by city (uses the view)."""
    result = get_db().table("nearmehq_city_revenue") \
        .select("*") \
        .execute()
    return result.data or []


def get_revenue_for_client(client_id: str) -> list:
    """Get all revenue records for a client."""
    result = get_db().table("nearmehq_revenue") \
        .select("*") \
        .eq("client_id", client_id) \
        .order("created_at", desc=True) \
        .execute()
    return result.data or []
