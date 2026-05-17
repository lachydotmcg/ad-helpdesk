#!/usr/bin/env python3
"""
app.py -- AD Helpdesk Web Application
Run with: python app.py
Opens on http://localhost:8888

Set DASHBOARD_PASSWORD in .env to protect the login page.
Set API_KEY in .env to protect the REST API endpoints.
"""

import os
import json
import functools
from datetime import datetime
from flask import (
    Flask, render_template, request, jsonify,
    session, redirect, url_for, flash
)
from dotenv import load_dotenv
import ad_bridge

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", os.urandom(24).hex())

DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "admin")
API_KEY            = os.getenv("API_KEY", "")
AUDIT_LOG          = os.path.join(os.path.dirname(__file__), "ps-scripts", "audit.log")


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


def api_auth(f):
    @functools.wraps(f)
    def decorated(*args, **kwargs):
        if API_KEY and request.headers.get("X-API-Key") != API_KEY:
            return jsonify({"success": False, "message": "Unauthorized"}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Audit log helper
# ---------------------------------------------------------------------------

def read_audit_log(limit=50):
    entries = []
    if not os.path.exists(AUDIT_LOG):
        return entries
    with open(AUDIT_LOG, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()
    for line in reversed(lines[-limit:]):
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split("|")]
        if len(parts) >= 4:
            entries.append({
                "timestamp": parts[0],
                "action":    parts[1],
                "user":      parts[2].replace("User=", ""),
                "status":    parts[3].replace("Status=", ""),
                "detail":    parts[4] if len(parts) > 4 else "",
            })
    return entries


# ---------------------------------------------------------------------------
# Frontend routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            session["logged_in"] = True
            return redirect(url_for("dashboard"))
        flash("Incorrect password.")
    return render_template("login.html")


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/")
@login_required
def dashboard():
    return render_template("dashboard.html")


# ---------------------------------------------------------------------------
# REST API routes (used by the dashboard JS and external tools)
# ---------------------------------------------------------------------------

@app.route("/api/stats")
@login_required
def api_stats():
    return jsonify(ad_bridge.get_stats())


@app.route("/api/users")
@login_required
def api_users():
    ou = request.args.get("ou", "")
    return jsonify(ad_bridge.list_users(ou))


@app.route("/api/users/search")
@login_required
def api_search():
    q = request.args.get("q", "")
    if not q:
        return jsonify(ad_bridge.list_users())
    return jsonify(ad_bridge.search_users(q))


@app.route("/api/users/locked")
@login_required
def api_locked():
    return jsonify(ad_bridge.list_locked_accounts())


@app.route("/api/users/expired")
@login_required
def api_expired():
    return jsonify(ad_bridge.list_expired_passwords())


@app.route("/api/user/<username>")
@login_required
def api_user(username):
    return jsonify(ad_bridge.get_user_info(username))


@app.route("/api/user/<username>/reset_password", methods=["POST"])
@login_required
def api_reset_password(username):
    data = request.get_json() or {}
    pw = data.get("temp_password", "")
    if not pw:
        return jsonify({"success": False, "message": "temp_password required", "data": None}), 400
    return jsonify(ad_bridge.reset_password(username, pw))


@app.route("/api/user/<username>/unlock", methods=["POST"])
@login_required
def api_unlock(username):
    return jsonify(ad_bridge.unlock_account(username))


@app.route("/api/user/<username>/enable", methods=["POST"])
@login_required
def api_enable(username):
    return jsonify(ad_bridge.enable_account(username))


@app.route("/api/user/<username>/disable", methods=["POST"])
@login_required
def api_disable(username):
    return jsonify(ad_bridge.disable_account(username))


@app.route("/api/user/<username>/groups/<group>", methods=["POST"])
@login_required
def api_add_group(username, group):
    return jsonify(ad_bridge.add_to_group(username, group))


@app.route("/api/user/<username>/groups/<group>", methods=["DELETE"])
@login_required
def api_remove_group(username, group):
    return jsonify(ad_bridge.remove_from_group(username, group))


@app.route("/api/user", methods=["POST"])
@login_required
def api_create_user():
    data = request.get_json() or {}
    first = data.get("first", "")
    last  = data.get("last", "")
    uname = data.get("username", "")
    ou    = data.get("ou", "")
    if not all([first, last, uname]):
        return jsonify({"success": False, "message": "first, last, username required", "data": None}), 400
    return jsonify(ad_bridge.create_user(first, last, uname, ou))


@app.route("/api/audit")
@login_required
def api_audit():
    limit = int(request.args.get("limit", 50))
    return jsonify({"success": True, "data": read_audit_log(limit)})


@app.route("/health")
def health():
    return jsonify({"status": "ok", "app": "ad-helpdesk"})


# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("\n AD Helpdesk")
    print(" ---------------------------------")
    print(" http://localhost:8888")
    print(" Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=8888, debug=False)
