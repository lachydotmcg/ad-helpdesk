#!/usr/bin/env python3
"""
cloud/app.py -- AD Helpdesk cloud backend + hosted dashboard.

Runs on Railway / Render / fly.io / any VPS.
Agents installed on customer servers connect here to receive and execute AD commands.
Dashboard users log in at / to manage their AD from the browser.

Run:
    python cloud/app.py

Environment variables:
    ADMIN_KEY         -- secret key for admin endpoints (creating tenants / users)
    SECRET_KEY        -- Flask session secret
    PORT              -- port to listen on (default 5000)
    ANTHROPIC_API_KEY -- for the AI chat interface (v0.6)
"""

import os
import json
import time
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from functools import wraps
from flask import (
    Flask, request, jsonify, g,
    session, redirect, url_for, render_template, flash, send_file
)
from dotenv import load_dotenv
import db

load_dotenv()

app = Flask(__name__)
app.secret_key = os.getenv("SECRET_KEY", "change-this-in-production")

ADMIN_KEY = os.getenv("ADMIN_KEY", "")

db.init_db()


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------

def send_ticket_email(to_email: str, to_name: str, ticket_title: str,
                      action_taken: str, message: str):
    """Send email notification to ticket requester. Silently skips if SMTP not configured."""
    smtp_host = os.getenv("SMTP_HOST", "")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER", "")
    smtp_pass = os.getenv("SMTP_PASS", "")
    smtp_from = os.getenv("SMTP_FROM", smtp_user)

    if not smtp_host or not smtp_user or not to_email:
        return  # SMTP not configured — skip silently

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Re: {ticket_title} — Resolved"
        msg["From"]    = f"AID Helpdesk <{smtp_from}>"
        msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email

        greeting     = f"Hi {to_name.split()[0]}," if to_name else "Hi,"
        action_line  = f"\n\nAction taken: {action_taken}" if action_taken else ""

        plain = f"""{greeting}

Your request "{ticket_title}" has been resolved.{action_line}

{message}

— AID Helpdesk (powered by Janus AI)
"""
        html = f"""<!DOCTYPE html>
<html>
<body style="font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;color:#1a1a2e;padding:20px;">
  <div style="background:#1a1830;border-radius:12px;padding:20px 24px;margin-bottom:20px;">
    <span style="color:#818cf8;font-size:18px;font-weight:700;">AID</span>
    <span style="color:#e2e8f0;font-size:18px;"> Helpdesk</span>
  </div>
  <p>{greeting}</p>
  <p>Your request <strong>{ticket_title}</strong> has been resolved.</p>
  {'<p><strong>Action taken:</strong> ' + action_taken + '</p>' if action_taken else ''}
  <p>{message}</p>
  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
  <p style="color:#64748b;font-size:12px;">Powered by Janus AI — AID Helpdesk</p>
</body>
</html>"""

        msg.attach(MIMEText(plain, "plain"))
        msg.attach(MIMEText(html, "html"))

        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo()
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to_email], msg.as_string())
    except Exception as e:
        print(f"[email] Failed to send to {to_email}: {e}")


# ---------------------------------------------------------------------------
# Auth decorators
# ---------------------------------------------------------------------------

def require_tenant(f):
    """Authenticate agent requests using X-API-Key header. Sets g.tenant."""
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


def require_dashboard_user(f):
    """Require an active dashboard session. Redirects to /login if not authenticated."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if "tenant_id" not in session or "user_email" not in session:
            return redirect(url_for("login"))
        g.tenant_id  = session["tenant_id"]
        g.user_email = session["user_email"]
        g.user_role  = session.get("user_role", "viewer")
        g.tenant_name = session.get("tenant_name", "")
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

@app.route("/health")
def health():
    return jsonify({"status": "ok", "service": "ad-helpdesk-cloud"})


@app.route("/logo.png")
def logo():
    """Serve the AID logo from whichever location it can be found."""
    _here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.join(_here, "static", "AIDLogo.png"),
        os.path.join(_here, "..", "AIDLogo.png"),
        os.path.join(_here, "..", "..", "ad-bridge", "AIDLogo.png"),
        os.path.join(_here, "AIDLogo.png"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            return send_file(path, mimetype="image/png")
    # Fallback: tiny transparent 1x1 PNG so <img> doesn't break layout
    import base64
    _1x1 = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
    )
    from flask import Response
    return Response(_1x1, mimetype="image/png")


# ---------------------------------------------------------------------------
# Dashboard -- login / logout
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    if "tenant_id" in session:
        return redirect(url_for("dashboard"))
    return render_template("landing.html")


@app.route("/login", methods=["GET", "POST"])
def login():
    if "tenant_id" in session:
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        user     = db.verify_tenant_user(email, password)
        if user:
            session.permanent = True
            session["tenant_id"]   = user["tenant_id"]
            session["user_email"]  = user["email"]
            session["user_role"]   = user["role"]
            session["tenant_name"] = user["tenant_name"]
            return redirect(url_for("dashboard"))
        error = "Invalid email or password."

    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Dashboard -- main page
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@require_dashboard_user
def dashboard():
    return render_template("dashboard.html",
                           tenant_name=g.tenant_name,
                           user_email=g.user_email,
                           user_role=g.user_role)


# ---------------------------------------------------------------------------
# Dashboard API -- async command execution
# All routes require a valid dashboard session (not the agent API key).
# ---------------------------------------------------------------------------

@app.route("/dashboard/api/exec", methods=["POST"])
@require_dashboard_user
def dashboard_exec():
    """
    Queue an AD command on behalf of the logged-in dashboard user.
    Body: { "action": "get_stats", "args": [] }
    Returns: { "command_id": "..." }
    """
    data   = request.get_json() or {}
    action = data.get("action", "").strip()
    args   = data.get("args", [])
    target = data.get("target", "")   # optional, for audit log

    if not action:
        return jsonify({"success": False, "message": "action is required."}), 400

    command = db.queue_command(g.tenant_id, action, args)

    # Log write operations
    write_actions = {
        "reset_password", "unlock_account", "enable_account",
        "disable_account", "add_to_group", "remove_from_group",
        "create_user", "move_user"
    }
    if action in write_actions:
        tgt = target or (args[0] if args else "")
        db.log_audit(g.tenant_id, g.user_email, action, tgt, "queued")
        db.log_activity(g.tenant_id, "ad_action", g.user_email, target=tgt,
                        detail=f"{action} queued via dashboard")

    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/dashboard/api/result/<command_id>")
@require_dashboard_user
def dashboard_result(command_id):
    """
    Poll for the result of a queued command.
    Returns 202 while pending, 200 with data when complete.
    """
    result = db.get_command_result(command_id, g.tenant_id)
    if not result:
        return jsonify({"success": False, "pending": True, "message": "Not ready yet."}), 202
    return jsonify({"success": True, "pending": False, "data": result})


@app.route("/dashboard/chat", methods=["POST"])
@require_dashboard_user
def dashboard_chat():
    """
    AI chat interface — takes a plain English message, asks Claude what AD
    command to run, queues it through the agent, and returns a natural language reply.
    """
    try:
        import anthropic
    except ImportError:
        return jsonify({"success": False, "message": "anthropic package not installed. Run: pip install anthropic"}), 500

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return jsonify({"success": False, "message": "ANTHROPIC_API_KEY not set in environment."}), 500

    data       = request.get_json() or {}
    message    = data.get("message", "").strip()
    history    = data.get("history", [])   # list of {role, content} for context
    session_id = data.get("session_id")    # existing session, or None to create new

    if not message:
        return jsonify({"success": False, "message": "message is required."}), 400

    # Get or create a chat session
    if session_id:
        chat_session = db.get_chat_session(session_id, g.tenant_id)
        if not chat_session:
            session_id = None
    if not session_id:
        new_session = db.create_chat_session(g.tenant_id, g.user_email)
        session_id  = new_session["id"]

    # Save the user message
    db.add_chat_message(session_id, g.tenant_id, "user", message)

    ai_name = os.getenv("AI_NAME", "Janus")

    system_prompt = f"""You are {ai_name}, an AI assistant built into AD Helpdesk — a tool for managing Windows Active Directory.
You are named after the Roman god of doorways and access, which suits your role perfectly.
The user is an IT admin. Your job is to understand what they want and either:
  (a) Execute an AD operation by responding with a JSON command block, OR
  (b) Answer a general question conversationally if no AD action is needed.

Available AD actions (use exact action names):
  get_user_info          args: [username]                  -- look up a user's full details, role, groups
  list_users             args: []                          -- list all users
  search_users           args: [search_term]               -- search by name
  list_ous               args: []                          -- list all Organisational Units
  reset_password         args: [username, new_password]    -- reset a user's password
  unlock_account         args: [username]                  -- unlock a locked account
  enable_account         args: [username]                  -- enable a disabled account
  disable_account        args: [username]                  -- disable an account
  add_to_group           args: [username, group_name]      -- add user to AD group
  remove_from_group      args: [username, group_name]      -- remove user from AD group
  create_user            args: [first, last, username, ou] -- create new AD user
  move_user              args: [username, ou_name]         -- move user to a different OU
  list_locked_accounts   args: []                          -- list all locked accounts
  list_expired_passwords args: []                          -- list expired passwords
  get_stats              args: []                          -- domain stats (total, locked, expired)

When you need to run an AD action, respond ONLY with this exact JSON (no preamble, no extra text):
{{"action": "action_name", "args": ["arg1", "arg2"], "message": "One sentence describing what you're doing"}}

CRITICAL RULES:
- Output ONLY the raw JSON when taking an action — no "I'll look up..." or any other text before or after it.
- If the conversation history already contains the result of a previous lookup, answer the follow-up question directly from that — do NOT run the action again.
- When generating a temporary password, make it secure: uppercase + lowercase + numbers + symbol, 12+ chars. State it in the message field so the admin can share it.
- Questions about a user's role, department, title, or group membership → use get_user_info.
- Questions about who has expired passwords → always run list_expired_passwords, never answer from a stats cache. Stats counts can lag behind individual user attributes.
- If no AD action is needed, respond conversationally in plain text — do NOT output JSON.
- Keep responses concise. You are talking to an experienced IT admin."""

    client   = anthropic.Anthropic(api_key=api_key)
    messages = []

    # Include conversation history for context (last 10 exchanges)
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})

    messages.append({"role": "user", "content": message})

    # Ask Claude what to do
    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=512,
        system=system_prompt,
        messages=messages
    )

    reply = response.content[0].text.strip()

    # Try to parse as a command — search for JSON anywhere in the reply
    # (model sometimes adds preamble text before the JSON block)
    command_data = None
    try:
        import re
        # Strip markdown code fences first
        clean = re.sub(r'```(?:json)?', '', reply).strip()
        # Find the first { ... } block that contains "action"
        match = re.search(r'\{.*?"action".*?\}', clean, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if "action" in parsed and "args" in parsed:
                command_data = parsed
    except (json.JSONDecodeError, ValueError):
        pass

    # If it's a command, queue it and wait for the agent
    if command_data:
        action      = command_data["action"]
        args        = command_data["args"]
        intent_msg  = command_data.get("message", f"Running {action}...")

        # Log write operations
        write_actions = {
            "reset_password", "unlock_account", "enable_account",
            "disable_account", "add_to_group", "remove_from_group",
            "create_user", "move_user"
        }
        if action in write_actions:
            target = args[0] if args else ""
            db.log_audit(g.tenant_id, g.user_email, action, target, "queued")

        command    = db.queue_command(g.tenant_id, action, args)
        command_id = command["id"]

        # Poll for result (up to 25s)
        result_data = None
        deadline    = time.time() + 25
        while time.time() < deadline:
            time.sleep(0.6)
            result = db.get_command_result(command_id, g.tenant_id)
            if result:
                result_data = result
                break

        if not result_data:
            return jsonify({
                "success": True,
                "reply": f"{intent_msg}\n\n⚠️ Agent didn't respond in time. Is it running?",
                "action_taken": action
            })

        # Build extra context for password resets so the password is never lost
        password_note = ""
        if action == "reset_password" and len(args) >= 2:
            password_note = f"\nIMPORTANT: The new password that was set is: {args[1]} — you MUST include this in your reply so the admin can share it."

        # Ask Claude to summarise the result in plain English
        summary_prompt = f"""You are {ai_name}, an AD Helpdesk AI assistant. The user asked: "{message}"
You ran this AD action: {action}
Arguments used: {args}
Agent result: success={result_data['success']}, message="{result_data['message']}", data={json.dumps(result_data['data'])[:1000]}
{password_note}
Write a short, friendly plain-English summary of what happened. If it succeeded, confirm it clearly.
If there was data returned (like a user list or stats), summarise the key points concisely.
If this was a password reset, always state the new password clearly so the admin can pass it on. Keep it under 5 sentences."""

        summary_response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=300,
            messages=[{"role": "user", "content": summary_prompt}]
        )
        final_reply = summary_response.content[0].text.strip()

        db.increment_usage(g.tenant_id, "janus_calls")
        db.add_chat_message(session_id, g.tenant_id, "assistant", final_reply, action)
        db.touch_chat_session(session_id)
        db.update_chat_session_title(session_id, message[:60])
        db.log_activity(g.tenant_id, "janus_action", g.user_email,
                        target=args[0] if args else None,
                        detail=f"{action} via Janus chat — {('success' if result_data['success'] else 'failed')}")
        return jsonify({
            "success":      True,
            "reply":        final_reply,
            "action_taken": action,
            "raw_data":     result_data.get("data"),
            "session_id":   session_id
        })

    # Track usage for every Janus call
    db.increment_usage(g.tenant_id, "janus_calls")

    # No command — just a conversational reply
    db.add_chat_message(session_id, g.tenant_id, "assistant", reply)
    db.touch_chat_session(session_id)
    db.update_chat_session_title(session_id, message[:60])
    return jsonify({
        "success":      True,
        "reply":        reply,
        "action_taken": None,
        "session_id":   session_id
    })


@app.route("/dashboard/api/usage")
@require_dashboard_user
def dashboard_usage():
    """Return this tenant's Janus usage for the current month."""
    usage   = db.get_usage(g.tenant_id)
    history = db.get_usage_history(g.tenant_id)
    return jsonify({"success": True, "data": {"current": usage, "history": history}})


@app.route("/dashboard/api/settings", methods=["GET"])
@require_dashboard_user
def dashboard_get_settings():
    """Return this tenant's Janus / automation settings."""
    settings = db.get_settings(g.tenant_id)
    return jsonify({"success": True, "data": settings})


@app.route("/dashboard/api/settings", methods=["PATCH"])
@require_dashboard_user
def dashboard_update_settings():
    """Update tenant settings (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data    = request.get_json() or {}
    current = db.get_settings(g.tenant_id)
    allowed = {"janus_enabled", "janus_scan_emails", "janus_auto_actions",
               "security_checks", "email_domain", "roles"}
    for k, v in data.items():
        if k in allowed:
            current[k] = v
    db.update_settings(g.tenant_id, current)
    db.log_activity(g.tenant_id, "settings_changed", g.user_email, detail="Settings updated")
    return jsonify({"success": True, "data": current})


@app.route("/dashboard/api/tenant")
@require_dashboard_user
def dashboard_tenant_info():
    """Return current tenant info including api_key (for agent-config.json setup)."""
    tenant = db.get_tenant_by_id(g.tenant_id)
    if not tenant:
        return jsonify({"success": False, "message": "Tenant not found."}), 404
    return jsonify({
        "success": True,
        "data": {
            "id":      tenant["id"],
            "name":    tenant["name"],
            "api_key": tenant["api_key"],
        }
    })


@app.route("/dashboard/api/activity")
@require_dashboard_user
def dashboard_activity():
    """Return the activity feed for this tenant."""
    limit = int(request.args.get("limit", 100))
    feed  = db.get_activity_feed(g.tenant_id, limit=limit)
    return jsonify({"success": True, "data": feed})


# ---------------------------------------------------------------------------
# Tickets
# ---------------------------------------------------------------------------

@app.route("/dashboard/api/tickets", methods=["GET"])
@require_dashboard_user
def list_tickets():
    status  = request.args.get("status")
    tickets = db.list_tickets(g.tenant_id, status=status)
    return jsonify({"success": True, "data": tickets})


@app.route("/dashboard/api/tickets", methods=["POST"])
@require_dashboard_user
def create_ticket():
    """Create a ticket and trigger Janus auto-analysis."""
    data             = request.get_json() or {}
    title            = data.get("title", "").strip()
    description      = data.get("description", "").strip()
    priority         = data.get("priority", "medium")
    requester_name   = data.get("requester_name", "").strip()
    requester_email  = data.get("requester_email", "").strip()

    if not title or not description:
        return jsonify({"success": False, "message": "title and description are required."}), 400

    ticket = db.create_ticket(
        g.tenant_id, g.user_email, title, description, priority,
        requester_name or None, requester_email or None
    )

    # Log activity
    db.log_activity(g.tenant_id, "ticket_created", g.user_email, target=ticket["id"],
                    detail=f"Ticket created: {title}")

    # Janus auto-analysis — only runs if enabled in settings
    try:
        import anthropic
        api_key  = os.getenv("ANTHROPIC_API_KEY", "")
        ai_name  = os.getenv("AI_NAME", "Janus")
        settings = db.get_settings(g.tenant_id)

        if api_key and settings.get("janus_enabled", True):
            client = anthropic.Anthropic(api_key=api_key)

            # Build roles context for permission checking
            configured_roles = settings.get("roles", [])
            roles_context = ""
            if configured_roles:
                roles_context = "\n\nCONFIGURED REQUESTER ROLES (who is authorised to request actions on behalf of others):\n"
                for r in configured_roles:
                    roles_context += (
                        f"  - Email pattern: '{r.get('email_pattern','*')}'"
                        f" | Role: {r.get('role','user')}"
                        f" | Can request for others: {r.get('can_request_for_others', False)}"
                        f" | Allowed actions: {', '.join(r.get('allowed_actions', []) or ['all'])}\n"
                    )

            # Build security context
            trusted_domain   = settings.get("email_domain", "")
            security_checks  = settings.get("security_checks", True)
            security_context = ""
            if security_checks and trusted_domain:
                security_context = (
                    f"\n\nSECURITY: The trusted email domain for this organisation is '{trusted_domain}'. "
                    f"Check the requester email carefully. Flag typosquatting (swapped/extra letters like "
                    f"'{trusted_domain.split('.')[0][:3]}' variations), lookalike domains, or any email that "
                    f"looks suspicious. Also flag if the requester email has no matching role definition "
                    f"but is trying to take action on another user's account."
                )
            elif security_checks:
                security_context = (
                    "\n\nSECURITY: Flag any requester email that looks like it could be spoofed, "
                    "typosquatted, or suspicious. Also flag requests where someone appears to be "
                    "impersonating IT staff (e.g. 'it@' from an unusual domain)."
                )

            analysis_prompt = f"""You are {ai_name}, an AI assistant for Active Directory management.
An IT support ticket has just come in. Analyse it carefully.

Ticket title: {title}
Ticket description: {description}
Requester name: {requester_name or 'Unknown'}
Requester email: {requester_email or 'none provided'}
{roles_context}
{security_context}

Available AD actions:
  unlock_account    args: [username]
  reset_password    args: [username, new_password]
  enable_account    args: [username]
  disable_account   args: [username]
  get_user_info     args: [username]
  add_to_group      args: [username, group_name]
  move_user         args: [username, ou_name]
  create_user       args: [first, last, username, ou]

Respond in EXACTLY this JSON format (no other text):
{{
  "analysis": "2-3 sentence plain English summary of the issue and your recommendation",
  "can_auto_resolve": true or false,
  "action": "action_name or null",
  "args": ["arg1"] or [],
  "confidence": "high / medium / low",
  "security_flag": null or "description of security concern (typosquatting, impersonation, unauthorised request, etc.)",
  "permission_ok": true or false,
  "notes": "any caveats or things the admin should verify before applying the fix"
}}

RULES:
- Extract the username from the ticket if mentioned explicitly.
- Extract the username from the ticket body if stated explicitly (e.g. "my username is jake.miller").
- If no username is in the body, use the requester email local part (before '@') as the username — e.g. "sarah.chen" from "sarah.chen@company.com". If it looks like a real name, treat it as confirmed: set confidence "high" and write the analysis as if the username is known. Do NOT say "inference required" or "must be verified" — the email IS their identity proof.
- If the email local part is generic (info, admin, support, noreply, it) or the requester name is suspicious, then fall back to name-based inference with confidence "low" and note it needs verification.
- Do NOT infer anything if the requester is obviously fake (hacker, test, anonymous).
- Only set can_auto_resolve to true if confident about the username AND the action AND permission_ok is true. Inferred usernames should generally be can_auto_resolve: false.
- If roles are configured and the requester has no matching role, set permission_ok to false.
- If the requester's role doesn't allow the requested action, set permission_ok to false and explain in security_flag.
- security_flag must be null if there are no concerns — do not return an empty string."""

            resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=500,
                messages=[{"role": "user", "content": analysis_prompt}]
            )
            raw    = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
            parsed = json.loads(raw)

            # Build a rich analysis string that includes security flags
            analysis_text = parsed.get("analysis", "")
            security_flag = parsed.get("security_flag")
            perm_ok       = parsed.get("permission_ok", True)

            if security_flag:
                analysis_text += f"\n\n⚠️ Security flag: {security_flag}"
            if not perm_ok:
                analysis_text += "\n\n🔒 Permission check failed — review before applying any fix."

            db.update_ticket(
                ticket["id"], g.tenant_id,
                janus_analysis=analysis_text,
                janus_action=parsed.get("action"),
                janus_action_args=json.dumps(parsed.get("args", []))
            )
            db.add_ticket_action(ticket["id"], g.tenant_id, "janus_analysis", analysis_text, "janus")
            db.increment_usage(g.tenant_id, "janus_calls")

            # Log security flags to activity feed
            if security_flag:
                db.log_activity(g.tenant_id, "security_flag", "janus",
                                target=requester_email or requester_name,
                                detail=f"Ticket #{ticket['id'][:8]}: {security_flag}")

            ticket["janus"] = parsed

            # Auto-action: apply the fix automatically if enabled in settings
            can_auto    = parsed.get("can_auto_resolve", False)
            janus_action = parsed.get("action")
            janus_args   = parsed.get("args", [])
            auto_actions = settings.get("janus_auto_actions", [])

            if can_auto and janus_action and janus_args and perm_ok and janus_action in auto_actions:
                db.add_ticket_action(ticket["id"], g.tenant_id, "janus_analysis",
                                     f"⚡ Janus is automatically applying fix: {janus_action} {janus_args}...", "janus")

                command    = db.queue_command(g.tenant_id, janus_action, janus_args)
                result_data = None
                deadline   = time.time() + 25
                while time.time() < deadline:
                    time.sleep(0.6)
                    result = db.get_command_result(command["id"], g.tenant_id)
                    if result:
                        result_data = result
                        break

                if result_data and result_data["success"]:
                    db.update_ticket(ticket["id"], g.tenant_id, status="resolved")
                    db.add_ticket_action(ticket["id"], g.tenant_id, "ad_action",
                                         f"✅ Janus auto-resolved: {janus_action} on {janus_args[0] if janus_args else '?'} — {result_data['message']}", "janus")
                    db.log_activity(g.tenant_id, "ticket_resolved", "janus",
                                    target=ticket["id"],
                                    detail=f"Auto-resolved via {janus_action} on {janus_args[0] if janus_args else '?'}")
                    ticket["auto_resolved"] = True
                    # Email the requester
                    if requester_email:
                        send_ticket_email(
                            to_email=requester_email,
                            to_name=requester_name or "",
                            ticket_title=title,
                            action_taken=f"{janus_action} on {janus_args[0] if janus_args else '?'}",
                            message="Your account issue has been resolved automatically by Janus AI. If you have further questions, please submit a new ticket."
                        )
                elif result_data:
                    db.add_ticket_action(ticket["id"], g.tenant_id, "ad_action",
                                         f"❌ Janus auto-apply failed: {result_data['message']}", "janus")
                else:
                    db.add_ticket_action(ticket["id"], g.tenant_id, "ad_action",
                                         "⚠️ Janus auto-apply timed out — agent may be offline. Fix can be applied manually.", "janus")

    except Exception as e:
        ticket["janus_error"] = str(e)

    return jsonify({"success": True, "data": ticket}), 201


@app.route("/dashboard/api/tickets/<ticket_id>", methods=["GET"])
@require_dashboard_user
def get_ticket(ticket_id):
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    actions = db.get_ticket_actions(ticket_id, g.tenant_id)
    return jsonify({"success": True, "data": {**ticket, "actions": actions}})


@app.route("/dashboard/api/tickets/<ticket_id>", methods=["PATCH"])
@require_dashboard_user
def update_ticket(ticket_id):
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    data = request.get_json() or {}
    allowed = {k: v for k, v in data.items() if k in {"status", "priority", "assigned_to"}}
    if allowed:
        db.update_ticket(ticket_id, g.tenant_id, **allowed)
        if "status" in allowed:
            db.add_ticket_action(ticket_id, g.tenant_id, "status_change",
                                 f"Status changed to {allowed['status']}", g.user_email)
    return jsonify({"success": True})


@app.route("/dashboard/api/tickets/<ticket_id>/comment", methods=["POST"])
@require_dashboard_user
def add_ticket_comment(ticket_id):
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    content = (request.get_json() or {}).get("content", "").strip()
    if not content:
        return jsonify({"success": False, "message": "content is required."}), 400
    action = db.add_ticket_action(ticket_id, g.tenant_id, "comment", content, g.user_email)
    return jsonify({"success": True, "data": action}), 201


@app.route("/dashboard/api/tickets/<ticket_id>/apply-fix", methods=["POST"])
@require_dashboard_user
def apply_ticket_fix(ticket_id):
    """
    Queue the Janus-suggested AD action for this ticket.
    Returns a command_id immediately — frontend polls /dashboard/api/result/<id>
    and calls /dashboard/api/tickets/<id>/apply-fix/complete when done.
    """
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    if not ticket.get("janus_action"):
        return jsonify({"success": False, "message": "No Janus suggestion available."}), 400

    action  = ticket["janus_action"]
    args    = json.loads(ticket.get("janus_action_args") or "[]")
    command = db.queue_command(g.tenant_id, action, args)

    db.add_ticket_action(ticket_id, g.tenant_id, "ad_action",
                         f"Fix queued: {action} {args}", g.user_email)

    return jsonify({
        "success":    True,
        "command_id": command["id"],
        "action":     action,
        "args":       args
    }), 202


@app.route("/dashboard/api/tickets/<ticket_id>/apply-fix/complete", methods=["POST"])
@require_dashboard_user
def apply_ticket_fix_complete(ticket_id):
    """Called by frontend once the agent result has come back."""
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404

    data       = request.get_json() or {}
    ad_success = data.get("success", False)
    message    = data.get("message", "")
    action     = data.get("action", "")
    args       = data.get("args", [])

    if ad_success:
        db.update_ticket(ticket_id, g.tenant_id, status="resolved")
        db.add_ticket_action(ticket_id, g.tenant_id, "ad_action",
                             f"Fix applied: {action} {args} — {message}", g.user_email)
        db.log_audit(g.tenant_id, g.user_email, action, args[0] if args else "", "success")
        db.log_activity(g.tenant_id, "ticket_resolved", g.user_email,
                        target=ticket_id, detail=f"Fix applied: {action} on {args[0] if args else '?'}")
        # Email the requester
        if ticket.get("requester_email"):
            send_ticket_email(
                to_email=ticket["requester_email"],
                to_name=ticket.get("requester_name", ""),
                ticket_title=ticket["title"],
                action_taken=f"{action} on {args[0] if args else '?'}",
                message="Your account issue has been resolved. If you have further questions, please submit a new ticket."
            )
    else:
        db.add_ticket_action(ticket_id, g.tenant_id, "ad_action",
                             f"Fix failed: {message}", g.user_email)
        db.log_activity(g.tenant_id, "fix_failed", g.user_email,
                        target=ticket_id, detail=f"Fix failed: {action} — {message}")

    return jsonify({"success": True})


@app.route("/webhook/email", methods=["POST"])
def email_webhook():
    """
    Inbound email webhook — compatible with Mailgun and SendGrid.
    Creates a ticket from an inbound support email.
    Requires X-Webhook-Key header matching WEBHOOK_KEY env var.
    Future: route to correct tenant by To: address or subdomain.
    """
    webhook_key = os.getenv("WEBHOOK_KEY", "")
    if webhook_key and request.headers.get("X-Webhook-Key") != webhook_key:
        return jsonify({"success": False, "message": "Unauthorized."}), 401

    data = request.get_json() or request.form.to_dict()

    subject  = data.get("subject", data.get("Subject", "Support request"))
    body     = data.get("body-plain", data.get("text", data.get("plain", "")))
    sender   = data.get("sender", data.get("from", ""))
    from_name = data.get("from-name", data.get("name", ""))

    # For now, create ticket on the first tenant (multi-tenant routing comes with v1.0)
    tenant_id = db.get_first_tenant_id()
    if not tenant_id:
        return jsonify({"success": False, "message": "No tenants configured."}), 500
    db.create_ticket(
        tenant_id, "email-webhook",
        title=subject[:200],
        description=body[:2000] or subject,
        priority="medium",
        requester_name=from_name or sender,
        requester_email=sender,
        source="email"
    )
    return jsonify({"success": True, "message": "Ticket created."}), 201


@app.route("/dashboard/api/chat/sessions")
@require_dashboard_user
def chat_sessions():
    """Return recent chat sessions for this tenant."""
    sessions = db.list_chat_sessions(g.tenant_id)
    return jsonify({"success": True, "data": sessions})


@app.route("/dashboard/api/chat/sessions/<session_id>/messages")
@require_dashboard_user
def chat_session_messages(session_id):
    """Return all messages in a chat session."""
    chat_session = db.get_chat_session(session_id, g.tenant_id)
    if not chat_session:
        return jsonify({"success": False, "message": "Session not found."}), 404
    messages = db.get_chat_messages(session_id, g.tenant_id)
    return jsonify({"success": True, "data": messages, "session": chat_session})


@app.route("/dashboard/api/audit")
@require_dashboard_user
def dashboard_audit():
    """Return recent audit log entries for this tenant."""
    log = db.get_audit_log(g.tenant_id, limit=100)
    return jsonify({"success": True, "data": log})


@app.route("/dashboard/api/users")
@require_dashboard_user
def dashboard_users():
    """Return the list of dashboard users for this tenant (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    users = db.list_tenant_users(g.tenant_id)
    return jsonify({"success": True, "data": users})


@app.route("/dashboard/api/users", methods=["POST"])
@require_dashboard_user
def dashboard_create_user():
    """Create a new dashboard user for this tenant (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data     = request.get_json() or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    role     = data.get("role", "viewer")
    if not email or not password:
        return jsonify({"success": False, "message": "email and password are required."}), 400
    if role not in ("admin", "viewer"):
        return jsonify({"success": False, "message": "role must be admin or viewer."}), 400
    try:
        user = db.create_tenant_user(g.tenant_id, email, password, role)
        return jsonify({"success": True, "data": user}), 201
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 409


# ---------------------------------------------------------------------------
# Admin -- tenant management (called with X-Admin-Key)
# ---------------------------------------------------------------------------

@app.route("/admin/tenants", methods=["GET"])
@require_admin
def admin_list_tenants():
    return jsonify({"success": True, "data": db.list_tenants()})


@app.route("/admin/tenants", methods=["POST"])
@require_admin
def admin_create_tenant():
    """
    Create a new tenant. Optionally create the first dashboard user.
    Body: { "name": "Acme Corp", "email": "admin@acme.com", "password": "..." }
    """
    data     = request.get_json() or {}
    name     = data.get("name", "").strip()
    email    = data.get("email", "").strip()
    password = data.get("password", "")

    if not name:
        return jsonify({"success": False, "message": "name is required."}), 400

    tenant = db.create_tenant(name)

    user = None
    if email and password:
        user = db.create_tenant_user(tenant["id"], email, password, role="admin")

    return jsonify({
        "success": True,
        "message": f"Tenant '{name}' created.",
        "data": {
            "tenant":       tenant,
            "dashboard_user": user
        }
    }), 201


@app.route("/admin/tenants/<tenant_id>/users", methods=["POST"])
@require_admin
def admin_create_tenant_user(tenant_id):
    """Add a dashboard user to an existing tenant."""
    tenant = db.get_tenant_by_id(tenant_id)
    if not tenant:
        return jsonify({"success": False, "message": "Tenant not found."}), 404
    data     = request.get_json() or {}
    email    = data.get("email", "").strip()
    password = data.get("password", "")
    role     = data.get("role", "admin")
    if not email or not password:
        return jsonify({"success": False, "message": "email and password are required."}), 400
    user = db.create_tenant_user(tenant_id, email, password, role)
    return jsonify({"success": True, "data": user}), 201


# ---------------------------------------------------------------------------
# Agent endpoints -- called by agent.py running on the customer's server
# ---------------------------------------------------------------------------

@app.route("/agent/poll", methods=["GET"])
@require_tenant
def agent_poll():
    """Agent calls this every 0.5s to check for pending commands."""
    command = db.get_pending_command(g.tenant["id"])
    return jsonify({"success": True, "command": command})


@app.route("/agent/result", methods=["POST"])
@require_tenant
def agent_result():
    """Agent calls this to post the result of a completed command."""
    data        = request.get_json() or {}
    command_id  = data.get("command_id", "")
    success     = data.get("success", False)
    message     = data.get("message", "")
    result_data = data.get("data")

    if not command_id:
        return jsonify({"success": False, "message": "command_id is required."}), 400

    db.store_result(command_id, g.tenant["id"], success, message, result_data)
    return jsonify({"success": True, "message": "Result stored."})


# ---------------------------------------------------------------------------
# Legacy API -- direct command queue (kept for backward compat / webhooks)
# ---------------------------------------------------------------------------

@app.route("/api/command", methods=["POST"])
@require_tenant
def queue_command():
    data   = request.get_json() or {}
    action = data.get("action", "").strip()
    args   = data.get("args", [])
    if not action:
        return jsonify({"success": False, "message": "action is required."}), 400
    command = db.queue_command(g.tenant["id"], action, args)
    return jsonify({"success": True, "message": "Command queued.", "data": command}), 202


@app.route("/api/command/<command_id>/result", methods=["GET"])
@require_tenant
def get_result(command_id):
    result = db.get_command_result(command_id, g.tenant["id"])
    if not result:
        return jsonify({"success": False, "message": "Result not ready yet.", "data": None}), 202
    return jsonify({"success": True, "data": result})


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("\n AD Helpdesk -- Cloud Backend + Dashboard")
    print(" ------------------------------------------")
    print(f" Listening on port {port}")
    print(f" Dashboard: http://localhost:{port}/")
    print(" Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)
