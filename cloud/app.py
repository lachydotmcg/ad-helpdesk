#!/usr/bin/env python3
"""
cloud/app.py -- AD Helpdesk cloud backend.

Runs on a server (Railway, Render, fly.io, VPS, etc.).
Agents installed on customer servers connect here to receive and execute commands.

Run:
    python cloud/app.py

Environment variables:
    ADMIN_KEY   -- secret key for admin endpoints (creating tenants)
    SECRET_KEY  -- Flask session secret
    PORT        -- port to listen on (default 5000)
"""

import os
import json
from functools import wraps
from flask import Flask, request, jsonify, g
from dotenv import load_dotenv
import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-in-production")

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

db.init_db()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def require_tenant(f):
    """Authenticate a request using X-API-Key header. Sets g.tenant."""
    @wraps(f)
    def decorated(*args, **kwargs):
        api_key = request.headers.get("X-API-Key", "")
        tenant  = db.get_tenant_by_key(api_key)
        if not tenant:
            return jsonify({"success": False, "message": "Invalid API key."}), 401
        g.tenant = tenant
        return f(*args, **kwargs)
    return decorated


def require_admin(f):
    """Authenticate admin endpoints using X-Admin-Key header."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if ADMIN_KEY and request.headers.get("X-Admin-Key") != ADMIN_KEY:
            return jsonify({"success": False, "message": "Unauthorized."}), 401
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "ad-helpdesk-cloud"})


# ---------------------------------------------------------------------------
# Admin -- tenant management
# ---------------------------------------------------------------------------

@app.route("/admin/tenants", methods=["GET"])
@require_admin
def admin_list_tenants():
    return jsonify({"success": True, "data": db.list_tenants()})


@app.route("/admin/tenants", methods=["POST"])
@require_admin
def admin_create_tenant():
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"success": False, "message": "name is required."}), 400
    tenant = db.create_tenant(name)
    return jsonify({
        "success": True,
        "message": f"Tenant '{name}' created.",
        "data": tenant
    }), 201


# ---------------------------------------------------------------------------
# Agent endpoints -- called by agent.py running on the customer's server
# ---------------------------------------------------------------------------

@app.route("/agent/poll", methods=["GET"])
@require_tenant
def agent_poll():
    """
    Agent calls this every 0.5s to check for pending commands.
    Returns the next pending command, or null if none.
    """
    command = db.get_pending_command(g.tenant["id"])
    return jsonify({
        "success": True,
        "command": command  # None if nothing pending
    })


@app.route("/agent/result", methods=["POST"])
@require_tenant
def agent_result():
    """Agent calls this to post the result of a completed command."""
    data       = request.get_json() or {}
    command_id = data.get("command_id", "")
    success    = data.get("success", False)
    message    = data.get("message", "")
    result_data = data.get("data")

    if not command_id:
        return jsonify({"success": False, "message": "command_id is required."}), 400

    db.store_result(command_id, g.tenant["id"], success, message, result_data)
    return jsonify({"success": True, "message": "Result stored."})


# ---------------------------------------------------------------------------
# Dashboard / AI endpoints -- called by the frontend or Claude
# ---------------------------------------------------------------------------

@app.route("/api/command", methods=["POST"])
@require_tenant
def queue_command():
    """Queue an AD operation to be picked up by the tenant's agent."""
    data   = request.get_json() or {}
    action = data.get("action", "").strip()
    args   = data.get("args", [])

    if not action:
        return jsonify({"success": False, "message": "action is required."}), 400

    command = db.queue_command(g.tenant["id"], action, args)
    return jsonify({
        "success": True,
        "message": "Command queued.",
        "data": command
    }), 202


@app.route("/api/command/<command_id>/result", methods=["GET"])
@require_tenant
def get_result(command_id):
    """Poll for the result of a queued command."""
    result = db.get_command_result(command_id, g.tenant["id"])
    if not result:
        return jsonify({"success": False, "message": "Result not ready yet.", "data": None}), 202
    return jsonify({"success": True, "data": result})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("\n AD Helpdesk -- Cloud Backend")
    print(" --------------------------------")
    print(f" Listening on port {port}")
    print(" Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)
