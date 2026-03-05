"""
NearMeHQ Platform Configuration
================================
Single source of truth for all settings, keys, and tier definitions.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# ============================================================
# SUPABASE
# ============================================================
SUPABASE_URL = os.environ.get("SUPABASE_URL", "https://biveaiyzuocnfwhqylbw.supabase.co")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")

# ============================================================
# STRIPE
# ============================================================
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")

# ============================================================
# TWILIO
# ============================================================
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_SMS_NUMBER = os.environ.get("TWILIO_SMS_NUMBER", "+16236243129")

# ============================================================
# RESEND (Email)
# ============================================================
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
EMAIL_FROM = os.environ.get("EMAIL_FROM", "NearMeHQ <hello@nearmehq.com>")
EMAIL_REPLY_TO = os.environ.get("EMAIL_REPLY_TO", "support@nearmehq.com")

# ============================================================
# VAPI (AI Voice)
# ============================================================
VAPI_API_KEY = os.environ.get("VAPI_API_KEY", "")
VAPI_ORG_ID = os.environ.get("VAPI_ORG_ID", "")

# ============================================================
# APPLICATION
# ============================================================
SITE_DOMAIN = "https://nearmehq.com"
API_DOMAIN = os.environ.get("API_DOMAIN", "https://api.nearmehq.com")
BRAND_NAME = "NearMeHQ"
PARENT_BRAND = "Galileus Media"
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")

# ============================================================
# TIER DEFINITIONS — Single source of truth
# ============================================================
TIERS = {
    "unclaimed": {
        "price": 0,
        "stripe_price_id": None,
        "forward_calls": False,
        "forward_hours": None,
        "badge": None,
        "rank_boost": 0,
        "features": ["Basic listing"],
    },
    "claimed": {
        "price": 0,
        "stripe_price_id": None,
        "forward_calls": False,
        "forward_hours": None,
        "badge": "Claimed",
        "rank_boost": 1,
        "features": ["Claimed listing", "Business info display"],
    },
    "starter": {
        "price": 4900,  # cents
        "stripe_price_id": os.environ.get("STRIPE_PRICE_STARTER", ""),
        "forward_calls": True,
        "forward_hours": "business",  # 8am-6pm
        "badge": "Verified",
        "rank_boost": 10,
        "features": [
            "Verified badge", "Tracking number", "Call forwarding (business hours)",
            "Lead notifications", "Monthly report", "Dofollow backlink",
        ],
    },
    "premium": {
        "price": 14900,  # cents
        "stripe_price_id": os.environ.get("STRIPE_PRICE_PREMIUM", ""),
        "forward_calls": True,
        "forward_hours": "24/7",
        "badge": "Featured",
        "rank_boost": 20,
        "features": [
            "Featured badge", "Top 3 placement", "Tracking number",
            "24/7 call forwarding", "AI receptionist", "Missed call text-back",
            "Review requests", "GBP optimization", "Monthly SEO report",
            "Dofollow backlinks (3)", "Blog mention",
        ],
    },
    "exclusive": {
        "price": 29900,  # cents
        "stripe_price_id": os.environ.get("STRIPE_PRICE_EXCLUSIVE", ""),
        "forward_calls": True,
        "forward_hours": "24/7",
        "badge": "#1 Pick",
        "rank_boost": 50,
        "features": [
            "#1 guaranteed placement", "Exclusive badge", "Multiple tracking numbers",
            "24/7 call forwarding", "AI receptionist", "Missed call text-back",
            "Speed-to-lead alerts", "Review generation system", "GBP management",
            "Weekly SEO report", "Dofollow backlinks (7)", "Blog posts (2/mo)",
            "Social content pack", "Competitor monitoring", "Dedicated account manager (AI)",
        ],
    },
}

# ============================================================
# CATEGORIES
# ============================================================
CATEGORIES = [
    {"slug": "plumbers", "name": "Plumbers", "singular": "plumber"},
    {"slug": "electricians", "name": "Electricians", "singular": "electrician"},
    {"slug": "hvac_repair", "name": "HVAC Repair", "singular": "HVAC technician"},
    {"slug": "locksmiths", "name": "Locksmiths", "singular": "locksmith"},
    {"slug": "roofing", "name": "Roofing Contractors", "singular": "roofer"},
    {"slug": "pest_control", "name": "Pest Control", "singular": "pest control specialist"},
    {"slug": "landscaping", "name": "Landscaping", "singular": "landscaper"},
    {"slug": "accountants", "name": "Accountants & CPAs", "singular": "accountant"},
    {"slug": "auto_repair", "name": "Auto Repair", "singular": "auto mechanic"},
    {"slug": "pet_grooming", "name": "Pet Grooming", "singular": "pet groomer"},
    {"slug": "cleaning", "name": "Cleaning Services", "singular": "cleaning professional"},
    {"slug": "dentists", "name": "Dentists", "singular": "dentist"},
    {"slug": "garage_doors", "name": "Garage Door Repair", "singular": "garage door technician"},
    {"slug": "painting", "name": "Painters", "singular": "painter"},
    {"slug": "pool_builders", "name": "Pool Builders", "singular": "pool builder"},
]
