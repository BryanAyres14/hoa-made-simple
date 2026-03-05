"""
HOA Made Simple — Standalone Flask Server
==========================================
Minimal server that runs ONLY the HOA module.
Boot with: python hoa_app.py
Vercel: exports `app` for serverless deployment
"""
from flask import Flask, jsonify, send_from_directory, request
from config import ENVIRONMENT
import os

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('HOA_JWT_SECRET', 'dev-secret')

DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")


@app.after_request
def add_cors(response):
    origin = request.headers.get('Origin', '*')
    response.headers['Access-Control-Allow-Origin'] = origin
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, PUT, DELETE, OPTIONS'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response

# ============================================================
# HOA BLUEPRINTS ONLY
# ============================================================
from api.hoa.auth import auth_bp as hoa_auth_bp
from api.hoa.members import members_bp as hoa_members_bp
from api.hoa.accounting import accounting_bp as hoa_accounting_bp
from api.hoa.invoices import invoices_bp as hoa_invoices_bp
from api.hoa.communication import communication_bp as hoa_communication_bp
from api.hoa.reminders import reminders_bp as hoa_reminders_bp
from api.hoa.admin import admin_bp as hoa_admin_bp

app.register_blueprint(hoa_auth_bp)
app.register_blueprint(hoa_members_bp)
app.register_blueprint(hoa_accounting_bp)
app.register_blueprint(hoa_invoices_bp)
app.register_blueprint(hoa_communication_bp)
app.register_blueprint(hoa_reminders_bp)
app.register_blueprint(hoa_admin_bp)


# ============================================================
# ROUTES
# ============================================================

@app.route("/", methods=["GET"])
def root():
    return jsonify({
        "app": "HOA Made Simple",
        "version": "1.0.0",
        "status": "operational",
        "environment": ENVIRONMENT,
        "endpoints": {
            "login": "/hoa/login",
            "manager": "/hoa/manager",
            "portal": "/hoa/portal",
            "admin": "/hoa/admin",
            "api_auth": "/api/hoa/auth/login",
            "api_members": "/api/hoa/members",
        }
    })

@app.route("/health", methods=["GET"])
def health():
    checks = {"api": True}
    try:
        from lib.database import get_db
        get_db()
        checks["database"] = True
    except Exception as e:
        checks["database"] = False
        checks["db_error"] = str(e)

    all_healthy = all(v for k, v in checks.items() if k != "db_error")
    return jsonify({
        "status": "healthy" if all_healthy else "degraded",
        "checks": checks,
    }), 200 if all_healthy else 503


# HOA Dashboard pages
@app.route("/hoa/login", methods=["GET"])
@app.route("/hoa/login/", methods=["GET"])
def hoa_login_page():
    return send_from_directory(DASHBOARD_DIR, "hoa-login.html")

@app.route("/hoa/manager", methods=["GET"])
@app.route("/hoa/manager/", methods=["GET"])
def hoa_manager_dashboard():
    return send_from_directory(DASHBOARD_DIR, "hoa-manager.html")

@app.route("/hoa/portal", methods=["GET"])
@app.route("/hoa/portal/", methods=["GET"])
@app.route("/hoa/portal/<path:subpath>", methods=["GET"])
def hoa_member_portal(subpath=None):
    return send_from_directory(DASHBOARD_DIR, "hoa-portal.html")

@app.route("/hoa/admin", methods=["GET"])
@app.route("/hoa/admin/", methods=["GET"])
def hoa_admin_dashboard():
    return send_from_directory(DASHBOARD_DIR, "hoa-admin.html")


# ============================================================
# ERROR HANDLERS
# ============================================================

@app.errorhandler(404)
def not_found(e):
    return jsonify({"error": "Not found"}), 404

@app.errorhandler(500)
def server_error(e):
    return jsonify({"error": "Internal server error"}), 500


# ============================================================
# RUN
# ============================================================

if __name__ == "__main__":
    port = 5001
    print(f"🏠 HOA Made Simple starting on http://localhost:{port}")
    print(f"   Login: http://localhost:{port}/hoa/login")
    print(f"   Manager: http://localhost:{port}/hoa/manager")
    print(f"   API: http://localhost:{port}/api/hoa/auth/login")
    app.run(host="0.0.0.0", port=port, debug=True)
