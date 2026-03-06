"""
NearMeHQ Database Layer (Supabase REST API)
=============================================
Direct REST API wrapper — avoids supabase-py version conflicts on Vercel.
All database operations in one place. Every other module imports from here.
"""
import requests
from config import SUPABASE_URL, SUPABASE_SERVICE_KEY, SUPABASE_ANON_KEY
from datetime import datetime, timezone
import json

# ============================================================
# SUPABASE REST CLIENT (lightweight, no supabase-py dependency)
# ============================================================

_REST_URL = None
_API_KEY = None
_AUTH_HEADER = None


def _init_rest():
    global _REST_URL, _API_KEY, _AUTH_HEADER
    if _REST_URL is None:
        key = SUPABASE_SERVICE_KEY or SUPABASE_ANON_KEY
        if not key:
            raise ValueError("No Supabase key configured.")
        _REST_URL = f"{SUPABASE_URL}/rest/v1"
        _API_KEY = key
        _AUTH_HEADER = {
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "Prefer": "return=representation",
        }


class _APIResponse:
    """Mimics supabase-py APIResponse for compatibility."""
    def __init__(self, data, count=None):
        self.data = data
        self.count = count


class _QueryBuilder:
    """Chainable query builder that mirrors supabase-py's interface."""

    def __init__(self, table_name):
        _init_rest()
        self._table = table_name
        self._url = f"{_REST_URL}/{table_name}"
        self._method = "GET"
        self._params = {}
        self._headers = dict(_AUTH_HEADER)
        self._body = None
        self._select_cols = "*"
        self._single = False
        self._maybe_single = False

    def select(self, columns="*"):
        self._method = "GET"
        self._select_cols = columns
        self._params["select"] = columns
        return self

    def insert(self, data):
        self._method = "POST"
        self._body = data if isinstance(data, list) else data
        return self

    def update(self, data):
        self._method = "PATCH"
        self._body = data
        return self

    def delete(self):
        self._method = "DELETE"
        return self

    def eq(self, column, value):
        self._params[column] = f"eq.{value}"
        return self

    def neq(self, column, value):
        self._params[column] = f"neq.{value}"
        return self

    def gt(self, column, value):
        self._params[column] = f"gt.{value}"
        return self

    def gte(self, column, value):
        self._params[column] = f"gte.{value}"
        return self

    def lt(self, column, value):
        self._params[column] = f"lt.{value}"
        return self

    def lte(self, column, value):
        self._params[column] = f"lte.{value}"
        return self

    def in_(self, column, values):
        vals = ",".join(str(v) for v in values)
        self._params[column] = f"in.({vals})"
        return self

    def like(self, column, pattern):
        self._params[column] = f"like.{pattern}"
        return self

    def ilike(self, column, pattern):
        self._params[column] = f"ilike.{pattern}"
        return self

    def is_(self, column, value):
        self._params[column] = f"is.{value}"
        return self

    def order(self, column, desc=False):
        direction = "desc" if desc else "asc"
        self._params["order"] = f"{column}.{direction}"
        return self

    def limit(self, count):
        self._params["limit"] = str(count)
        return self

    def offset(self, count):
        self._params["offset"] = str(count)
        return self

    def single(self):
        self._single = True
        self._headers["Accept"] = "application/vnd.pgrst.object+json"
        return self

    def maybe_single(self):
        self._maybe_single = True
        self._headers["Accept"] = "application/vnd.pgrst.object+json"
        return self

    def execute(self):
        """Execute the query and return an _APIResponse."""
        try:
            if self._method == "GET":
                resp = requests.get(self._url, params=self._params, headers=self._headers, timeout=15)
            elif self._method == "POST":
                resp = requests.post(self._url, params=self._params, headers=self._headers,
                                     json=self._body, timeout=15)
            elif self._method == "PATCH":
                resp = requests.patch(self._url, params=self._params, headers=self._headers,
                                      json=self._body, timeout=15)
            elif self._method == "DELETE":
                resp = requests.delete(self._url, params=self._params, headers=self._headers, timeout=15)
            else:
                return _APIResponse(None)

            # Handle response
            if resp.status_code == 406 and self._maybe_single:
                # No rows found with maybe_single — return None data
                return _APIResponse(None)

            if resp.status_code >= 400:
                # For maybe_single, treat 404/406 as no data
                if self._maybe_single:
                    return _APIResponse(None)
                raise Exception(f"Supabase error {resp.status_code}: {resp.text}")

            data = resp.json() if resp.text else None

            # For single/maybe_single, data is a dict (single object) or None
            # For normal queries, data is a list
            return _APIResponse(data)

        except requests.exceptions.RequestException as e:
            if self._maybe_single:
                return _APIResponse(None)
            raise Exception(f"Database connection error: {str(e)}")


class _SupabaseREST:
    """Lightweight Supabase REST client that mimics supabase-py's interface."""

    def table(self, table_name):
        return _QueryBuilder(table_name)


# Singleton
_db_instance = None


def get_db():
    """Get the database client (singleton)."""
    global _db_instance
    if _db_instance is None:
        _db_instance = _SupabaseREST()
    return _db_instance


# ============================================================
# CLIENTS (NearMeHQ helpers — unchanged interface)
# ============================================================

def get_client_by_twilio(twilio_number: str) -> dict | None:
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("twilio_number", twilio_number) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_id(client_id: str) -> dict | None:
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("id", client_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_stripe_customer(stripe_customer_id: str) -> dict | None:
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("stripe_customer_id", stripe_customer_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_client_by_stripe_subscription(subscription_id: str) -> dict | None:
    result = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("stripe_subscription_id", subscription_id) \
        .maybe_single() \
        .execute()
    return result.data


def get_clients_by_city_category(city: str, category: str, status_filter: list = None) -> list:
    query = get_db().table("nearmehq_clients") \
        .select("*") \
        .eq("city", city) \
        .eq("category", category)
    if status_filter:
        query = query.in_("status", status_filter)
    result = query.order("rank_position", desc=False).execute()
    return result.data or []


def get_paying_clients(city: str = None, category: str = None) -> list:
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
    updates["updated_at"] = datetime.now(timezone.utc).isoformat()
    result = get_db().table("nearmehq_clients") \
        .update(updates) \
        .eq("id", client_id) \
        .execute()
    return result.data[0] if result.data else None


def create_client(data: dict) -> dict:
    result = get_db().table("nearmehq_clients") \
        .insert(data) \
        .execute()
    return result.data[0] if result.data else None


def activate_client(client_id: str, tier: str, stripe_customer_id: str,
                    stripe_subscription_id: str, twilio_number: str, real_phone: str) -> dict:
    from config import TIERS
    tier_config = TIERS.get(tier, {})
    return update_client(client_id, {
        "status": tier,
        "tier_price": tier_config.get("price", 0) / 100,
        "stripe_customer_id": stripe_customer_id,
        "stripe_subscription_id": stripe_subscription_id,
        "twilio_number": twilio_number,
        "real_phone": real_phone,
        "forward_hours": tier_config.get("forward_hours", "business"),
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    })


def deactivate_client(client_id: str) -> dict:
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
    result = get_db().table("nearmehq_calls") \
        .insert(call_data) \
        .execute()
    return result.data[0] if result.data else None


def get_calls_for_client(twilio_number: str, days: int = 30) -> list:
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
    result = get_db().table("nearmehq_calls") \
        .update(updates) \
        .eq("call_sid", call_sid) \
        .execute()
    return result.data[0] if result.data else None


# ============================================================
# LEADS
# ============================================================

def create_lead(lead_data: dict) -> dict:
    result = get_db().table("nearmehq_leads") \
        .insert(lead_data) \
        .execute()
    return result.data[0] if result.data else None


def get_leads(city: str = None, category: str = None, status: str = "new") -> list:
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
    return get_db().table("nearmehq_leads") \
        .update({
            "assigned_to": client_id,
            "status": "assigned",
            "assigned_at": datetime.now(timezone.utc).isoformat(),
        }) \
        .eq("id", lead_id) \
        .execute().data[0]


def sell_lead(lead_id: str, price: float) -> dict:
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
    result = get_db().table("nearmehq_city_revenue") \
        .select("*") \
        .execute()
    return result.data or []


def get_revenue_for_client(client_id: str) -> list:
    result = get_db().table("nearmehq_revenue") \
        .select("*") \
        .eq("client_id", client_id) \
        .order("created_at", desc=True) \
        .execute()
    return result.data or []
