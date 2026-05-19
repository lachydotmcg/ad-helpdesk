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
import secrets
import string
from collections import defaultdict
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
db.migrate_db()


# ---------------------------------------------------------------------------
# Rate limiting (in-memory, resets on deploy — good enough for abuse prevention)
# ---------------------------------------------------------------------------

_rate_store: dict[str, list[float]] = defaultdict(list)

def _rate_limit(key: str, max_calls: int, window_seconds: int) -> bool:
    """Return True if allowed, False if rate-limited. key = e.g. 'signup:1.2.3.4'"""
    now  = time.time()
    hits = _rate_store[key]
    _rate_store[key] = [t for t in hits if now - t < window_seconds]
    if len(_rate_store[key]) >= max_calls:
        return False
    _rate_store[key].append(now)
    return True


def _gen_password(length: int = 16) -> str:
    """Generate a secure random password."""
    chars = string.ascii_letters + string.digits + "!@#$%"
    return "".join(secrets.choice(chars) for _ in range(length))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _normalize_domain(domain: str) -> str:
    """Normalise a trusted domain setting.
    Strips leading '@', lowercases, and appends '.com' if no dot is present.
    e.g. 'superlab' -> 'superlab.com'  |  '@acme.co.uk' -> 'acme.co.uk'
    """
    d = domain.strip().lstrip("@").lower()
    if d and "." not in d:
        d += ".com"
    return d


# ---------------------------------------------------------------------------
# Email helper
# ---------------------------------------------------------------------------

def _resolve_smtp(tenant_settings=None) -> dict:
    """Return SMTP config, preferring tenant settings over environment variables."""
    ts = tenant_settings or {}
    host = ts.get("smtp_host") or os.getenv("SMTP_HOST", "")
    port = int(ts.get("smtp_port") or os.getenv("SMTP_PORT", "587"))
    user = ts.get("smtp_user") or os.getenv("SMTP_USER", "")
    pwd  = ts.get("smtp_pass") or os.getenv("SMTP_PASS", "")
    frm  = ts.get("smtp_from") or os.getenv("SMTP_FROM", user)
    return {"host": host, "port": port, "user": user, "pass": pwd, "from": frm}


def send_ticket_email(to_email, to_name, ticket_title,
                      action_taken, message,
                      tenant_settings=None):
    """Send email notification to ticket requester. Silently skips if SMTP not configured."""
    smtp = _resolve_smtp(tenant_settings)
    smtp_host = smtp["host"]
    smtp_port = smtp["port"]
    smtp_user = smtp["user"]
    smtp_pass = smtp["pass"]
    smtp_from = smtp["from"]

    if not smtp_host or not smtp_user or not to_email:
        return  # SMTP not configured — skip silently

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = f"Re: {ticket_title} - Resolved"
        msg["From"]    = f"AID Helpdesk <{smtp_from}>"
        msg["To"]      = f"{to_name} <{to_email}>" if to_name else to_email

        greeting     = f"Hi {to_name.split()[0]}," if to_name else "Hi,"
        action_line  = f"\n\nAction taken: {action_taken}" if action_taken else ""

        plain = f"""{greeting}

Your request "{ticket_title}" has been resolved.{action_line}

{message}

- AID Helpdesk (powered by Janus AI)
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
  <p style="color:#64748b;font-size:12px;">Powered by Janus AI - AID Helpdesk</p>
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


def send_email(to_email, subject, body_text, body_html,
               tenant_settings=None):
    """Generic email helper for system emails (password reset, etc.). Skips silently if SMTP not configured."""
    smtp = _resolve_smtp(tenant_settings)
    smtp_host = smtp["host"]
    smtp_port = smtp["port"]
    smtp_user = smtp["user"]
    smtp_pass = smtp["pass"]
    smtp_from = smtp["from"]
    if not smtp_host or not smtp_user or not to_email:
        return
    try:
        msg            = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"AID Helpdesk <{smtp_from}>"
        msg["To"]      = to_email
        msg.attach(MIMEText(body_text, "plain"))
        msg.attach(MIMEText(body_html, "html"))
        with smtplib.SMTP(smtp_host, smtp_port) as server:
            server.ehlo(); server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_from, [to_email], msg.as_string())
    except Exception as e:
        print(f"[email] send_email failed to {to_email}: {e}")


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


@app.route("/robots.txt")
def robots():
    from flask import Response
    return Response("User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /admin\n",
                    mimetype="text/plain")


@app.route("/favicon.ico")
def favicon():
    return redirect(url_for("logo"), code=301)


_SVG_LOGO = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200">
  <defs>
    <filter id="g"><feGaussianBlur stdDeviation="8"/></filter>
    <radialGradient id="rg" cx="50%" cy="50%" r="50%">
      <stop offset="0%" stop-color="#6366f1" stop-opacity="0.45"/>
      <stop offset="100%" stop-color="#6366f1" stop-opacity="0"/>
    </radialGradient>
  </defs>
  <circle cx="100" cy="100" r="99" fill="url(#rg)" filter="url(#g)"/>
  <circle cx="100" cy="100" r="90" fill="#07061a"/>
  <circle cx="100" cy="100" r="90" fill="none" stroke="#6366f1" stroke-width="6"/>
  <circle cx="100" cy="100" r="83" fill="none" stroke="#818cf8" stroke-width="1.5" opacity="0.35"/>
  <!-- "AI" text -->
  <text x="85" y="121" text-anchor="middle" font-family="ui-monospace,monospace,system-ui"
        font-size="52" font-weight="800" fill="#ddd6fe" letter-spacing="-2">AI</text>
  <!-- D arc: right curved side of D, no left vertical stroke — same width as one character -->
  <path d="M 114,91 C 139,91 139,117 114,117"
        fill="none" stroke="#ddd6fe" stroke-width="7.5" stroke-linecap="round"/>
</svg>"""


@app.route("/logo.png")
@app.route("/logo.svg")
def logo():
    """Serve the AID logo. Tries PNG from static/, falls back to inline SVG."""
    from flask import Response
    static_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "static")
    png_path   = os.path.join(static_dir, "AIDLogo.png")
    if os.path.isfile(png_path):
        return send_file(png_path, mimetype="image/png")
    # Reliable fallback: inline SVG (always works even without the static file)
    return Response(_SVG_LOGO, mimetype="image/svg+xml")


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


# ---------------------------------------------------------------------------
# Email ticket intake webhook
# ---------------------------------------------------------------------------

@app.route("/webhook/email/<api_key>", methods=["POST"])
def email_intake(api_key):
    """
    Inbound email webhook — compatible with Mailgun, SendGrid, Postmark, and generic JSON.

    Configure your email routing service to POST parsed emails to:
      https://<your-app>.railway.app/webhook/email/<tenant_api_key>

    Mailgun  → Inbound Routes → forward to this URL
    SendGrid → Inbound Parse → forward to this URL
    Postmark → Inbound Stream → forward to this URL
    Cloudflare Email Routing → forward to a Mailgun/SendGrid account, then here
    """
    tenant = db.get_tenant_by_key(api_key)
    if not tenant:
        return jsonify({"success": False, "message": "Invalid API key."}), 401

    tenant_id   = tenant["id"]
    tenant_name = tenant.get("name", "")

    # Check plan allows email intake
    plan   = db.get_tenant_plan(tenant_id)
    limits = db.get_plan_limits(plan)
    if not limits.get("email_intake"):
        return jsonify({"success": False,
                        "message": f"Email intake is not available on the {limits['label']} plan. Upgrade to Pro."}), 403

    # Rate limit: max 60 emails/hour per tenant
    if not _rate_limit(f"email_intake:{tenant_id}", 60, 3600):
        return jsonify({"success": False, "message": "Rate limit exceeded."}), 429

    # ----- Parse email fields from multiple webhook formats -----
    ct = (request.content_type or "").lower()

    if "application/json" in ct:
        # Postmark inbound / generic JSON
        data       = request.get_json(force=True, silent=True) or {}
        from_raw   = data.get("From") or data.get("from") or data.get("from_email") or ""
        from_name  = data.get("FromName") or data.get("from_name") or ""
        subject    = data.get("Subject") or data.get("subject") or "Support request"
        body       = (data.get("TextBody") or data.get("body_plain") or
                      data.get("text") or data.get("body") or "")
    else:
        # Mailgun / SendGrid multipart form-data
        from_raw  = (request.form.get("from") or request.form.get("sender") or
                     request.form.get("From") or "")
        from_name = ""
        subject   = (request.form.get("subject") or request.form.get("Subject") or "Support request")
        body      = (request.form.get("stripped-text") or request.form.get("body-plain") or
                     request.form.get("text") or request.form.get("TextBody") or "")

    # Parse "Name <email>" format
    from_email = from_raw.strip()
    if "<" in from_raw:
        parts      = from_raw.split("<", 1)
        from_name  = from_name or parts[0].strip().strip('"').strip("'")
        from_email = parts[1].rstrip(">").strip()

    # Skip emails from our own system (avoid reply loops)
    own_domain = os.getenv("SMTP_FROM", "")
    if own_domain and from_email.lower().endswith("@" + own_domain.split("@")[-1]):
        return jsonify({"success": True, "message": "Skipped — own domain."}), 200
    if "noreply" in from_email.lower() or "no-reply" in from_email.lower():
        return jsonify({"success": True, "message": "Skipped — noreply sender."}), 200

    if not from_email or not body.strip():
        return jsonify({"success": False, "message": "Could not parse email fields (from/body missing)."}), 400

    # Enforce ticket count plan limit
    cap = limits.get("tickets")
    if cap is not None:
        existing = db.list_tickets(tenant_id, limit=cap + 1)
        if len(existing) >= cap:
            return jsonify({"success": False, "message": "Ticket limit reached."}), 429

    title  = subject[:120].strip()
    desc   = body[:4000].strip()

    ticket = db.create_ticket(
        tenant_id,
        created_by  = "email-intake",
        title       = title,
        description = desc,
        priority    = "medium",
        requester_name  = from_name or None,
        requester_email = from_email or None,
        source      = "email",
    )

    db.log_activity(tenant_id, "ticket_created", from_email,
                    target=title, detail="via email intake")

    # Janus analysis
    result = _run_janus_analysis(
        tenant_id, tenant_name, ticket["id"],
        title, desc, from_name, from_email, limits
    )

    # Send auto-reply to requester acknowledging receipt
    if from_email:
        short_id = ticket["id"][:8].upper()
        auto_msg  = (
            f"Your request has been received and logged as ticket #{short_id}. "
            f"Our IT team has been notified"
        )
        if result["parsed"] and result["parsed"].get("analysis"):
            auto_msg += f" and our AI assistant has already begun analysing your issue"
        auto_msg += ". We'll be in touch shortly."

        if result.get("auto_resolved"):
            auto_msg = (
                f"Great news! Your request (#{short_id}) has been resolved automatically by Janus AI. "
                f"If you still have issues, please reply or submit a new request."
            )

        send_ticket_email(
            to_email    = from_email,
            to_name     = from_name or "",
            ticket_title = title,
            action_taken = None,
            message     = auto_msg,
            tenant_settings = db.get_settings(tenant_id),
        )

    return jsonify({"success": True, "ticket_id": ticket["id"]}), 201


@app.route("/signup", methods=["GET", "POST"])
def signup():
    if "tenant_id" in session:
        return redirect(url_for("dashboard"))

    error = None
    if request.method == "POST":
        ip       = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        if not _rate_limit(f"signup:{ip}", max_calls=5, window_seconds=3600):
            error = "Too many sign-up attempts. Please try again in an hour."
            return render_template("signup.html", error=error), 429

        company  = request.form.get("company", "").strip()
        email    = request.form.get("email", "").strip()
        password = request.form.get("password", "")
        confirm  = request.form.get("confirm", "")

        if not company or not email or not password:
            error = "All fields are required."
        elif len(password) < 8:
            error = "Password must be at least 8 characters."
        elif password != confirm:
            error = "Passwords do not match."
        else:
            try:
                tenant = db.create_tenant(company)
                user   = db.create_tenant_user(tenant["id"], email, password, role="admin")
                # Seed settings with trial start date
                import datetime as _dt
                db.update_settings(tenant["id"], {
                    **db._SETTINGS_DEFAULTS,
                    "trial_started_at": _dt.datetime.utcnow().isoformat(),
                })
                db.log_activity(tenant["id"], "tenant_created", email,
                                detail=f"New tenant: {company}")
                # Auto-login
                session.permanent = True
                session["tenant_id"]   = tenant["id"]
                session["user_email"]  = email
                session["user_role"]   = "admin"
                session["tenant_name"] = company
                return redirect(url_for("dashboard"))
            except Exception:
                error = "An account with that email already exists."

    return render_template("signup.html", error=error)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    """Show forgot-password form and send a reset email if SMTP is configured."""
    error   = None
    success = None
    if request.method == "POST":
        email  = request.form.get("email", "").strip().lower()
        if not email:
            error = "Please enter your email address."
        else:
            # Look up user across all tenants
            tenant = db.get_tenant_by_user_email(email)
            if tenant:
                user = db.get_tenant_user_by_email(tenant["id"], email)
                if user:
                    token = db.create_password_reset_token(user["id"], tenant["id"])
                    reset_url = request.host_url.rstrip("/") + f"/reset-password/{token}"
                    send_email(
                        to_email=email,
                        subject="Reset your AID Helpdesk password",
                        body_text=(
                            f"You requested a password reset for your AID Helpdesk account.\n\n"
                            f"Click the link below to set a new password. This link expires in 1 hour.\n\n"
                            f"{reset_url}\n\n"
                            f"If you didn't request this, you can safely ignore this email."
                        ),
                        body_html=(
                            f"<!DOCTYPE html><html><body style='font-family:system-ui,sans-serif;max-width:520px;margin:0 auto;color:#1a1a2e;padding:20px;'>"
                            f"<div style='background:#1a1830;border-radius:12px;padding:20px 24px;margin-bottom:20px;'>"
                            f"<span style='color:#818cf8;font-size:18px;font-weight:700;'>AID</span>"
                            f"<span style='color:#e2e8f0;font-size:18px;'> Helpdesk</span></div>"
                            f"<p>Hi,</p>"
                            f"<p>You requested a password reset for your <strong>AID Helpdesk</strong> account.</p>"
                            f"<p><a href='{reset_url}' style='background:#6366f1;color:#fff;padding:12px 24px;border-radius:8px;text-decoration:none;font-weight:700;display:inline-block;'>Reset Password</a></p>"
                            f"<p style='color:#64748b;font-size:13px;'>This link expires in 1 hour. If you didn't request this, you can safely ignore this email.</p>"
                            f"</body></html>"
                        )
                    )
            # Always show success to prevent email enumeration
            success = "If that email is registered, you'll receive a reset link shortly."
    return render_template("forgot_password.html", error=error, success=success)


@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    """Allow user to set a new password using a valid reset token."""
    error   = None
    success = None
    if request.method == "POST":
        pw      = request.form.get("password", "")
        pw_conf = request.form.get("confirm", "")
        if len(pw) < 8:
            error = "Password must be at least 8 characters."
        elif pw != pw_conf:
            error = "Passwords do not match."
        else:
            result = db.consume_password_reset_token(token)
            if not result:
                error = "This reset link is invalid or has expired. Please request a new one."
            else:
                db.update_user_password_by_id(result["user_id"], pw)
                success = "Password updated! You can now sign in."
    return render_template("reset_password.html", token=token, error=error, success=success)


# ---------------------------------------------------------------------------
# Dashboard -- main page
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@require_dashboard_user
def dashboard():
    plan = db.get_tenant_plan(g.tenant_id)
    return render_template("dashboard.html",
                           tenant_name=g.tenant_name,
                           user_email=g.user_email,
                           user_role=g.user_role,
                           tenant_plan=plan)


@app.route("/billing")
@require_dashboard_user
def billing():
    settings      = db.get_settings(g.tenant_id)
    stripe_key    = os.getenv("STRIPE_PUBLIC_KEY", "")
    plan          = db.get_tenant_plan(g.tenant_id)
    limits        = db.get_plan_limits(plan)
    usage         = db.get_usage(g.tenant_id) or {}
    used_janus    = usage.get("janus_calls", 0)
    used_commands = usage.get("ad_commands", 0)
    lim_janus     = limits["janus_calls"]
    lim_commands  = limits["ad_commands"]
    return render_template("billing.html",
                           tenant_name=g.tenant_name,
                           user_email=g.user_email,
                           settings=settings,
                           stripe_key=stripe_key,
                           tenant_plan=plan,
                           plan_label=limits["label"],
                           usage_janus=used_janus,
                           usage_commands=used_commands,
                           limits_janus=lim_janus,
                           limits_commands=lim_commands,
                           janus_pct=min(100, int(used_janus / lim_janus * 100)) if lim_janus else 0,
                           commands_pct=min(100, int(used_commands / lim_commands * 100)) if lim_commands else 0,
                           )


@app.route("/dashboard/api/onboarding")
@require_dashboard_user
def dashboard_onboarding():
    """Return checklist status for the getting-started panel."""
    import datetime as _dt
    settings   = db.get_settings(g.tenant_id)
    agent      = db.get_agent_status(g.tenant_id)
    tickets    = db.list_tickets(g.tenant_id, limit=1)
    team       = db.list_tenant_users(g.tenant_id)

    trial_start = settings.get("trial_started_at")
    trial_days_left = None
    if trial_start:
        try:
            start = _dt.datetime.fromisoformat(trial_start)
            elapsed = (_dt.datetime.utcnow() - start).days
            trial_days_left = max(0, 14 - elapsed)
        except Exception:
            pass

    return jsonify({
        "success": True,
        "data": {
            "agent_connected":    agent["online"],
            "email_domain_set":   bool(settings.get("email_domain")),
            "first_ticket":       len(tickets) > 0,
            "team_member_added":  len(team) > 1,
            "dismissed":          settings.get("onboarding_dismissed", False),
            "trial_days_left":    trial_days_left,
        }
    })


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

    # Enforce plan limits for write (AD-mutating) actions
    write_actions = {
        "reset_password", "unlock_account", "enable_account",
        "disable_account", "add_to_group", "remove_from_group",
        "create_user", "move_user", "force_password_change",
        "set_password_never_expires"
    }
    if action in write_actions:
        plan   = db.get_tenant_plan(g.tenant_id)
        limits = db.get_plan_limits(plan)
        usage  = db.get_usage(g.tenant_id) or {}
        used   = usage.get("ad_commands", 0)
        cap    = limits["ad_commands"]
        if used >= cap:
            return jsonify({
                "success": False,
                "limit_reached": True,
                "message": f"Monthly AD action limit reached ({cap} on {limits['label']} plan). Upgrade to Pro for {db.PLAN_LIMITS['pro']['ad_commands']} actions/month.",
            }), 429

    command = db.queue_command(g.tenant_id, action, args)

    if action in write_actions:
        tgt = target or (args[0] if args else "")
        db.log_audit(g.tenant_id, g.user_email, action, tgt, "queued")
        db.log_activity(g.tenant_id, "ad_action", g.user_email, target=tgt,
                        detail=f"{action} queued via dashboard")
        db.increment_usage(g.tenant_id, "ad_commands")

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
  USERS:
  get_user_info              args: [username]                      -- full user details, groups, attributes
  list_users                 args: []                              -- list all domain users
  search_users               args: [search_term]                   -- search by name or username (partial match)
  list_group_memberships     args: [username]                      -- all groups a user belongs to
  create_user                args: [first, last, username, ou]     -- create new AD account (ou can be plain name)
  move_user                  args: [username, ou_name]             -- move user to a different OU

  ACCOUNT STATE:
  unlock_account             args: [username]                      -- unlock a locked-out account
  enable_account             args: [username]                      -- re-enable a disabled account
  disable_account            args: [username]                      -- disable an account
  reset_password             args: [username, new_password]        -- reset password; user must change at logon
  force_password_change      args: [username]                      -- force password change at next logon
  set_password_never_expires args: [username, true|false]          -- toggle password expiry policy

  GROUPS:
  list_groups                args: []                              -- list all AD groups
  search_groups              args: [search_term]                   -- search groups by partial name
  get_group_members          args: [group_name]                    -- list members of a group
  add_to_group               args: [username, group_name]          -- add user to a group
  remove_from_group          args: [username, group_name]          -- remove user from a group

  OUs:
  list_ous                   args: []                              -- list all Organisational Units

  REPORTING:
  list_locked_accounts       args: []                              -- all currently locked accounts
  list_expired_passwords     args: []                              -- all accounts with expired passwords
  get_stats                  args: []                              -- domain summary (total, locked, expired)

When you need to run an AD action, respond ONLY with this exact JSON (no preamble, no extra text):
{{"action": "action_name", "args": ["arg1", "arg2"], "message": "One sentence describing what you're doing"}}

SMART LOOKUP RULES (read these carefully):
- When a group name might not be exact (user used shorthand, caps vary, etc.), ALWAYS run search_groups first with the key word before attempting group operations. Only attempt add_to_group / remove_from_group once you have the exact group name from a search result.
- When a username looks uncertain or a previous action failed with "not found", run search_users first.
- When creating a user and the OU path is ambiguous or unclear, run list_ous first to find the correct OU name, then create the user with the exact OU name from the results. Parse human-readable paths like "Staff > Management" as the leaf "Management" and search for it.
- When a write action fails, your follow-up message MUST explain why and suggest what information to look up next (e.g. "Group not found — let me search for groups matching that name."). Then immediately issue the search action without waiting for the user to ask.

CRITICAL RULES:
- Output ONLY the raw JSON when taking an action — no "I'll look up..." or any other text before or after it.
- If the conversation history already contains the result of a previous lookup, answer the follow-up question directly from that — do NOT run the action again.
- When generating a temporary password, make it secure: uppercase + lowercase + numbers + symbol, 12+ chars. State it clearly so the admin can share it.
- Questions about a user's role, department, title, or groups → use get_user_info or list_group_memberships.
- Questions about expired passwords → always run list_expired_passwords (not stats cache).
- If no AD action is needed, respond conversationally in plain text — do NOT output JSON.
- Keep responses concise and direct. You are talking to an experienced IT admin."""

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

    # Lookup actions that should trigger auto-chaining to the intended follow-up action
    LOOKUP_ACTIONS = {
        'search_users', 'search_groups', 'list_ous', 'get_user_info',
        'list_users', 'list_groups', 'list_locked_accounts',
        'list_expired_passwords', 'get_stats', 'list_group_memberships',
        'get_group_members',
    }

    # If it's a command, queue it and wait for the agent
    if command_data:
        action      = command_data["action"]
        args        = command_data["args"]
        intent_msg  = command_data.get("message", f"Running {action}...")

        # Log and count write operations
        write_actions = {
            "reset_password", "unlock_account", "enable_account",
            "disable_account", "add_to_group", "remove_from_group",
            "create_user", "move_user", "force_password_change",
            "set_password_never_expires"
        }
        if action in write_actions:
            target = args[0] if args else ""
            db.log_audit(g.tenant_id, g.user_email, action, target, "queued")
            db.increment_usage(g.tenant_id, "ad_commands")

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
                "reply": f"{intent_msg}\n\nAgent did not respond in time. Is it running?",
                "action_taken": action
            })

        # ---------------------------------------------------------------
        # SEARCH CHAINING: if this was a lookup, automatically follow up
        # with the real action the user originally wanted (one chain max).
        # ---------------------------------------------------------------
        if action in LOOKUP_ACTIONS and result_data.get("success"):
            chain_prompt = f"""You are {ai_name}. The user originally asked: "{message}"
To prepare, you first ran: {action} {args}
The lookup returned: {json.dumps(result_data.get('data', {}))[:1400]}

Now complete the user's original request using the exact names/values from the lookup result above.
If the original request is fully answered by the lookup (e.g. they just wanted to see a list), reply conversationally.
If a follow-up write action is needed (e.g. add to group, create user, move OU), output ONLY the JSON command using the exact name from the result.
Do NOT run another lookup — use the data you already have.
Output format for action: {{"action": "action_name", "args": ["arg1"], "message": "what you are doing"}}"""

            chain_resp = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=300,
                messages=[{"role": "user", "content": chain_prompt}]
            )
            chain_text = chain_resp.content[0].text.strip()

            # Try to parse a chained command
            chained = None
            try:
                import re as _re
                clean2 = _re.sub(r'```(?:json)?', '', chain_text).strip()
                m2 = _re.search(r'\{.*?"action".*?\}', clean2, _re.DOTALL)
                if m2:
                    p2 = json.loads(m2.group())
                    if "action" in p2 and "args" in p2 and p2["action"] not in LOOKUP_ACTIONS:
                        chained = p2
            except Exception:
                pass

            if chained:
                action2 = chained["action"]
                args2   = chained["args"]
                if action2 in write_actions:
                    tgt2 = args2[0] if args2 else ""
                    db.log_audit(g.tenant_id, g.user_email, action2, tgt2, "queued")
                    db.increment_usage(g.tenant_id, "ad_commands")

                cmd2    = db.queue_command(g.tenant_id, action2, args2)
                result2 = None
                deadline2 = time.time() + 25
                while time.time() < deadline2:
                    time.sleep(0.6)
                    r2 = db.get_command_result(cmd2["id"], g.tenant_id)
                    if r2:
                        result2 = r2
                        break

                if result2:
                    # Summarise the chained result
                    pw_note2 = f"\nIMPORTANT: New password set: {args2[1]} - include it in your reply." if action2 == "reset_password" and len(args2) >= 2 else ""
                    sum2 = client.messages.create(
                        model="claude-haiku-4-5-20251001",
                        max_tokens=300,
                        messages=[{"role": "user", "content": f"""You are {ai_name}. The user asked: "{message}"
You searched first ({action} {args}), then ran: {action2} {args2}
Final result: success={result2['success']}, message="{result2['message']}", data={json.dumps(result2.get('data',''))[:600]}
{pw_note2}
Write 2-3 sentences confirming what happened. Be direct and clear."""}]
                    )
                    final_reply = sum2.content[0].text.strip()
                    db.increment_usage(g.tenant_id, "janus_calls")
                    db.add_chat_message(session_id, g.tenant_id, "assistant", final_reply, action2)
                    db.touch_chat_session(session_id)
                    db.update_chat_session_title(session_id, message[:60])
                    db.log_activity(g.tenant_id, "janus_action", g.user_email,
                                    target=args2[0] if args2 else None,
                                    detail=f"{action} -> {action2} via Janus chat")
                    return jsonify({
                        "success": True, "reply": final_reply,
                        "action_taken": action2, "raw_data": result2.get("data"),
                        "session_id": session_id
                    })
            else:
                # Lookup was the full answer - chain_text is the conversational reply
                db.increment_usage(g.tenant_id, "janus_calls")
                db.add_chat_message(session_id, g.tenant_id, "assistant", chain_text, action)
                db.touch_chat_session(session_id)
                db.update_chat_session_title(session_id, message[:60])
                return jsonify({
                    "success": True, "reply": chain_text,
                    "action_taken": action, "raw_data": result_data.get("data"),
                    "session_id": session_id
                })

        # Build extra context for password resets so the password is never lost
        password_note = ""
        if action == "reset_password" and len(args) >= 2:
            password_note = f"\nIMPORTANT: The new password that was set is: {args[1]} - you MUST include this in your reply so the admin can share it."

        # Ask Claude to summarise the result in plain English
        summary_prompt = f"""You are {ai_name}, an AD Helpdesk AI assistant. The user asked: "{message}"
You ran this AD action: {action}
Arguments used: {args}
Agent result: success={result_data['success']}, message="{result_data['message']}", data={json.dumps(result_data['data'])[:1200]}
{password_note}
Write a short, friendly plain-English summary of what happened.
- If it succeeded: confirm clearly. Include key details (e.g. new password, group name, OU).
- If it failed because something wasn't found (user, group, OU): say so clearly, then suggest or offer to search.
- If there was data returned (user list, group list, stats): summarise the key points concisely.
- If this was a password reset: always state the new password clearly so the admin can share it.
Keep it under 5 sentences. Be direct."""

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
                        detail=f"{action} via Janus chat - {('success' if result_data['success'] else 'failed')}")
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
    """Return this tenant's usage, plan, and limits for the current month."""
    plan    = db.get_tenant_plan(g.tenant_id)
    limits  = db.get_plan_limits(plan)
    usage   = db.get_usage(g.tenant_id) or {}
    history = db.get_usage_history(g.tenant_id)
    return jsonify({"success": True, "data": {
        "current": usage,
        "history": history,
        "plan":    plan,
        "limits":  limits,
    }})


@app.route("/dashboard/api/settings", methods=["GET"])
@require_dashboard_user
def dashboard_get_settings():
    """Return this tenant's Janus / automation settings."""
    settings = db.get_settings(g.tenant_id)
    # Never expose smtp_pass in API response — redact it
    safe = dict(settings)
    if safe.get("smtp_pass"):
        safe["smtp_pass"] = ""   # client shows placeholder text instead
    return jsonify({"success": True, "data": safe})


@app.route("/dashboard/api/settings", methods=["PATCH"])
@require_dashboard_user
def dashboard_update_settings():
    """Update tenant settings (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data    = request.get_json() or {}
    current = db.get_settings(g.tenant_id)
    allowed = {"janus_enabled", "janus_scan_emails", "janus_auto_actions",
               "security_checks", "email_domain", "roles",
               "custom_statuses", "custom_priorities", "ticket_labels",
               "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from"}
    for k, v in data.items():
        if k in allowed:
            # Never overwrite smtp_pass with an empty string (blank = "keep existing")
            if k == "smtp_pass" and v == "":
                continue
            current[k] = v
    db.update_settings(g.tenant_id, current)
    db.log_activity(g.tenant_id, "settings_changed", g.user_email, detail="Settings updated")
    return jsonify({"success": True, "data": current})


@app.route("/dashboard/api/users/<user_id>", methods=["DELETE"])
@require_dashboard_user
def dashboard_delete_user(user_id):
    """Remove a team member (admin only). Cannot remove yourself."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    # Prevent self-deletion
    users = db.list_tenant_users(g.tenant_id)
    target = next((u for u in users if u["id"] == user_id), None)
    if not target:
        return jsonify({"success": False, "message": "User not found."}), 404
    if target["email"] == g.user_email:
        return jsonify({"success": False, "message": "You cannot remove your own account."}), 400
    db.delete_tenant_user(g.tenant_id, user_id)
    db.log_activity(g.tenant_id, "team_member_removed", g.user_email,
                    target=target["email"], detail=f"Removed team member: {target['email']}")
    return jsonify({"success": True})


@app.route("/dashboard/api/change-password", methods=["POST"])
@require_dashboard_user
def dashboard_change_password():
    """Allow the logged-in user to change their own password."""
    data         = request.get_json() or {}
    current_pw   = data.get("current_password", "")
    new_pw       = data.get("new_password", "")
    if not current_pw or not new_pw:
        return jsonify({"success": False, "message": "Both fields are required."}), 400
    if len(new_pw) < 8:
        return jsonify({"success": False, "message": "Password must be at least 8 characters."}), 400
    # Verify current password
    user = db.verify_tenant_user(g.user_email, current_pw)
    if not user:
        return jsonify({"success": False, "message": "Current password is incorrect."}), 403
    db.update_user_password(g.tenant_id, g.user_email, new_pw)
    db.log_activity(g.tenant_id, "password_changed", g.user_email, detail="Dashboard password changed")
    return jsonify({"success": True})


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
# Shared Janus analysis helper
# ---------------------------------------------------------------------------

def _run_janus_analysis(tenant_id, tenant_name, ticket_id, title, description,
                        requester_name, requester_email, limits):
    """
    Run Janus AI analysis on a ticket and update it in the DB.
    Returns a dict with keys: parsed, auto_resolved (bool), error (str or None).
    Silently skips if API key is missing, plan limit hit, or Janus disabled.
    """
    import anthropic as _anthropic
    api_key  = os.getenv("ANTHROPIC_API_KEY", "")
    ai_name  = os.getenv("AI_NAME", "Janus")
    settings = db.get_settings(tenant_id)

    janus_usage = db.get_usage(tenant_id) or {}
    janus_used  = janus_usage.get("janus_calls", 0)
    janus_cap   = limits["janus_calls"]
    janus_ok    = janus_cap is None or janus_used < janus_cap

    if not api_key or not settings.get("janus_enabled", True) or not janus_ok:
        return {"parsed": None, "auto_resolved": False, "error": None}

    try:
        client = _anthropic.Anthropic(api_key=api_key)

        configured_roles = settings.get("roles", [])
        roles_context = ""
        if configured_roles:
            roles_context = "\n\nCONFIGURED REQUESTER ROLES:\n"
            for r in configured_roles:
                roles_context += (
                    f"  - Email pattern: '{r.get('email_pattern','*')}'"
                    f" | Role: {r.get('role','user')}"
                    f" | Can request for others: {r.get('can_request_for_others', False)}"
                    f" | Allowed actions: {', '.join(r.get('allowed_actions', []) or ['all'])}\n"
                )

        trusted_domain  = settings.get("email_domain", "")
        security_checks = settings.get("security_checks", True)
        security_context = ""
        if security_checks:
            if trusted_domain:
                nd = _normalize_domain(trusted_domain)
                # Python does the domain check — Janus just gets the binary verdict
                domain_verified = False
                if requester_email and "@" in requester_email:
                    req_domain = requester_email.split("@")[-1].lower().strip()
                    domain_verified = (req_domain == nd or req_domain.endswith("." + nd))
                if domain_verified:
                    security_context = (
                        f"\n\nSECURITY: The system has already verified that the requester's email "
                        f"domain matches the organisation's trusted domain. "
                        f"Do NOT flag any domain mismatch — the email is legitimate. "
                        f"security_flag must be null unless there is a different, unrelated concern "
                        f"(e.g. the request itself is suspicious, or it targets someone other than the requester)."
                    )
                else:
                    unverified = requester_email or "none provided"
                    security_context = (
                        f"\n\nSECURITY: The requester's email '{unverified}' does NOT match the "
                        f"organisation's trusted domain '{nd}'. This is likely a spoofing or "
                        f"impersonation attempt. Set security_flag to describe this concern."
                    )
            else:
                security_context = (
                    "\n\nSECURITY: No trusted domain configured. "
                    "Flag only obviously suspicious or malformed requester emails."
                )

        prompt = f"""You are {ai_name}, an AI assistant for Active Directory management at {tenant_name or 'this organisation'}.
An IT support ticket has come in. Analyse it carefully.

Ticket title: {title}
Ticket description: {description}
Requester name: {requester_name or 'Unknown'}
Requester email: {requester_email or 'none provided'}
{roles_context}
{security_context}

Available AD actions for auto-resolution:
  unlock_account         args: [username]                   -- unlock a locked account
  reset_password         args: [username, new_password]     -- reset password (auto-generate 12-char secure password)
  enable_account         args: [username]                   -- re-enable a disabled account
  disable_account        args: [username]                   -- disable an account
  force_password_change  args: [username]                   -- flag account to require password change at next logon
  add_to_group           args: [username, group_name]       -- add user to an AD group
  remove_from_group      args: [username, group_name]       -- remove user from an AD group
  move_user              args: [username, ou_name]          -- move user to a different OU
  create_user            args: [first, last, username, ou]  -- create new AD account (ou optional)
  get_user_info          args: [username]                   -- look up user details (use when ticket needs review, not action)

Respond in EXACTLY this JSON format (no other text):
{{
  "analysis": "1-2 sentences: what the issue is and what action to take",
  "threat_score": 1,
  "threat_title": "Short label e.g. Account Lockout, Password Reset, Impersonation, Suspicious Request",
  "can_auto_resolve": true or false,
  "action": "action_name or null",
  "args": ["arg1"] or [],
  "confidence": "high / medium / low",
  "security_flag": null or "one sentence description of concern",
  "permission_ok": true or false,
  "notes": null or "one sentence caveat"
}}

THREAT SCORE GUIDE (1-10):
1-3  Routine, verified request (account lockout from trusted domain, simple password reset)
4-5  Unverified but plausible (no domain match configured, generic request)
6-7  Ambiguous or mildly suspicious (domain mismatch, vague identity, unusual scope)
8-10 Clear impersonation, spoofed domain, or high-risk request - flag prominently

RULES:
- Extract the username from the ticket body if stated explicitly.
- If no username given, use the requester email local part (before '@') as the username. Treat as confirmed with confidence "high". Do NOT say "inference required".
- If the email local part is generic (info, admin, support, noreply) fall back to confidence "low".
- Only set can_auto_resolve to true if confident about username AND action AND permission_ok is true.
- For password resets, generate a secure temporary password: uppercase + lowercase + numbers + symbol, 12+ chars.
- security_flag must be null if no concerns (not an empty string).
- threat_title must NOT say "typosquatting" - use "Impersonation" instead.
- For group operations, use the group name exactly as the user wrote it."""

        resp   = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw    = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()
        parsed = json.loads(raw)

        analysis_text = parsed.get("analysis", "")
        security_flag = parsed.get("security_flag")
        perm_ok       = parsed.get("permission_ok", True)
        threat_score  = int(parsed.get("threat_score") or 3)
        threat_title  = (parsed.get("threat_title") or "Analysis").replace("typosquatting", "Impersonation").replace("Typosquatting", "Impersonation")

        if security_flag:
            analysis_text += f"\n\nSecurity flag: {security_flag}"
        if not perm_ok:
            analysis_text += "\n\nPermission check failed - review before applying any fix."

        # Store structured blob so frontend can render the score card
        analysis_blob = json.dumps({
            "v": 2,
            "score": threat_score,
            "title": threat_title,
            "text":  analysis_text,
        })

        db.update_ticket(ticket_id, tenant_id,
                         janus_analysis=analysis_blob,
                         janus_action=parsed.get("action"),
                         janus_action_args=json.dumps(parsed.get("args", [])))
        db.add_ticket_action(ticket_id, tenant_id, "janus_analysis",
                             f"[Threat {threat_score}/10 - {threat_title}] {analysis_text}", "janus")
        db.increment_usage(tenant_id, "janus_calls")

        if security_flag:
            db.log_activity(tenant_id, "security_flag", "janus",
                            target=requester_email or requester_name,
                            detail=f"Ticket #{ticket_id[:8]}: {security_flag}")

        # Auto-action
        auto_resolved = False
        can_auto      = parsed.get("can_auto_resolve", False)
        janus_action  = parsed.get("action")
        janus_args    = parsed.get("args", [])
        auto_actions  = settings.get("janus_auto_actions", [])

        if can_auto and janus_action and janus_args and perm_ok and janus_action in auto_actions and limits.get("auto_actions", False):
            db.add_ticket_action(ticket_id, tenant_id, "janus_analysis",
                                 f"⚡ Janus is automatically applying fix: {janus_action} {janus_args}...", "janus")
            command    = db.queue_command(tenant_id, janus_action, janus_args)
            result_data = None
            deadline   = time.time() + 25
            while time.time() < deadline:
                time.sleep(0.6)
                result = db.get_command_result(command["id"], tenant_id)
                if result:
                    result_data = result
                    break

            if result_data and result_data["success"]:
                db.update_ticket(ticket_id, tenant_id, status="resolved")
                db.add_ticket_action(ticket_id, tenant_id, "ad_action",
                                     f"✅ Janus auto-resolved: {janus_action} on {janus_args[0] if janus_args else '?'} - {result_data['message']}", "janus")
                db.log_activity(tenant_id, "ticket_resolved", "janus", target=ticket_id,
                                detail=f"Auto-resolved via {janus_action}")
                auto_resolved = True
                if requester_email:
                    send_ticket_email(
                        to_email=requester_email,
                        to_name=requester_name or "",
                        ticket_title=title,
                        action_taken=f"{janus_action} on {janus_args[0] if janus_args else '?'}",
                        message="Your account issue has been resolved automatically by Janus AI.",
                        tenant_settings=db.get_settings(tenant_id),
                    )
            elif result_data:
                db.add_ticket_action(ticket_id, tenant_id, "ad_action",
                                     f"❌ Janus auto-apply failed: {result_data['message']}", "janus")
            else:
                db.add_ticket_action(ticket_id, tenant_id, "ad_action",
                                     "⚠️ Janus auto-apply timed out - agent may be offline.", "janus")

        return {"parsed": parsed, "auto_resolved": auto_resolved, "error": None}

    except Exception as e:
        print(f"[janus] Analysis error for ticket {ticket_id}: {e}", flush=True)
        return {"parsed": None, "auto_resolved": False, "error": str(e)}


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

    # Enforce ticket count plan limit
    plan   = db.get_tenant_plan(g.tenant_id)
    limits = db.get_plan_limits(plan)
    cap    = limits.get("tickets")
    if cap is not None:
        existing = db.list_tickets(g.tenant_id, limit=cap + 1)
        if len(existing) >= cap:
            return jsonify({
                "success": False,
                "limit_reached": True,
                "message": f"Ticket limit reached ({cap} on {limits['label']} plan). Upgrade to Pro for unlimited tickets.",
            }), 429

    ticket = db.create_ticket(
        g.tenant_id, g.user_email, title, description, priority,
        requester_name or None, requester_email or None
    )

    # Log activity
    db.log_activity(g.tenant_id, "ticket_created", g.user_email, target=ticket["id"],
                    detail=f"Ticket created: {title}")

    # Janus auto-analysis via shared helper
    result = _run_janus_analysis(
        g.tenant_id, g.tenant_name, ticket["id"],
        title, description, requester_name, requester_email, limits
    )
    if result["parsed"]:
        ticket["janus"] = result["parsed"]
    if result["auto_resolved"]:
        ticket["auto_resolved"] = True
    if result["error"]:
        ticket["janus_error"] = result["error"]

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
    allowed = {k: v for k, v in data.items() if k in {"status", "priority", "assigned_to", "labels"}}
    if allowed:
        db.update_ticket(ticket_id, g.tenant_id, **allowed)
        new_status = allowed.get("status")
        if new_status:
            db.add_ticket_action(ticket_id, g.tenant_id, "status_change",
                                 f"Status changed to {new_status}", g.user_email)
            # Send email notification when ticket is resolved or closed
            if new_status in ("resolved", "closed"):
                requester_email = ticket.get("requester_email", "")
                requester_name  = ticket.get("requester_name", "")
                title           = ticket.get("title", "Your request")
                janus_analysis  = ticket.get("janus_analysis", "")
                if requester_email:
                    if new_status == "resolved":
                        summary = "Your request has been reviewed and resolved by our IT helpdesk."
                        if janus_analysis:
                            # Extract plain text from structured JSON blob if present
                            try:
                                import json as _json
                                jd = _json.loads(janus_analysis)
                                clean = (jd.get("text") or "").split("\n\nSecurity flag")[0].split("\n\nPermission check")[0].strip()
                            except Exception:
                                clean = janus_analysis.split("\n\nSecurity flag")[0].split("\n\nPermission check")[0].strip()
                            if clean:
                                summary += f"\n\nSummary: {clean}"
                        summary += "\n\nIf you need further assistance, please submit a new ticket or reply to this email."
                    else:
                        summary = "Your support ticket has been closed. If your issue was not resolved, please submit a new ticket."
                    send_ticket_email(
                        to_email=requester_email,
                        to_name=requester_name or "",
                        ticket_title=title,
                        action_taken=new_status.capitalize(),
                        message=summary,
                        tenant_settings=db.get_settings(g.tenant_id),
                    )
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


@app.route("/dashboard/api/tickets/<ticket_id>/analyse", methods=["POST"])
@require_dashboard_user
def rerun_janus_analysis(ticket_id):
    """Re-run Janus analysis on an existing ticket."""
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    plan   = db.get_tenant_plan(g.tenant_id)
    limits = db.get_plan_limits(plan)
    tenant = db.get_tenant_by_id(g.tenant_id)
    result = _run_janus_analysis(
        tenant_id      = g.tenant_id,
        tenant_name    = tenant["name"] if tenant else "",
        ticket_id      = ticket_id,
        title          = ticket["title"],
        description    = ticket["description"],
        requester_name = ticket.get("requester_name", ""),
        requester_email= ticket.get("requester_email", ""),
        limits         = limits,
    )
    if result.get("error"):
        return jsonify({"success": False, "message": result["error"]}), 500
    return jsonify({"success": True})


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
                             f"Fix applied: {action} {args} - {message}", g.user_email)
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
                message="Your account issue has been resolved. If you have further questions, please submit a new ticket.",
                tenant_settings=db.get_settings(g.tenant_id),
            )
    else:
        db.add_ticket_action(ticket_id, g.tenant_id, "ad_action",
                             f"Fix failed: {message}", g.user_email)
        db.log_activity(g.tenant_id, "fix_failed", g.user_email,
                        target=ticket_id, detail=f"Fix failed: {action} - {message}")

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


@app.route("/dashboard/api/insights")
@require_dashboard_user
def dashboard_insights():
    """Return a Janus AI health summary of recent AD stats + tickets."""
    try:
        import anthropic
        api_key  = os.getenv("ANTHROPIC_API_KEY", "")
        ai_name  = os.getenv("AI_NAME", "Janus")
        settings = db.get_settings(g.tenant_id)
        if not api_key or not settings.get("janus_enabled", True):
            return jsonify({"success": True, "data": {"message": None}})

        # Collect context: recent tickets + activity
        tickets     = db.list_tickets(g.tenant_id, limit=20)
        open_count  = sum(1 for t in tickets if t.get("status") == "open")
        recent_act  = db.get_activity_feed(g.tenant_id, limit=10)
        recent_desc = "; ".join(
            f"{a.get('event_type','?')} on {a.get('target','?')}"
            for a in recent_act[:5]
        ) or "none"

        prompt = (
            f"You are {ai_name}, an Active Directory assistant. "
            f"Summarise the current AD health for {g.tenant_name or 'this organisation'} "
            f"in exactly 1–2 sentences (≤30 words). Be specific, practical, and encouraging. "
            f"Data: {open_count} open support tickets; "
            f"recent activity: {recent_desc}. "
            f"Highlight the most important thing the admin should do or note right now."
        )
        client   = anthropic.Anthropic(api_key=api_key)
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            messages=[{"role": "user", "content": prompt}]
        )
        message = response.content[0].text.strip()
        return jsonify({"success": True, "data": {"message": message}})
    except Exception as e:
        return jsonify({"success": True, "data": {"message": None}})


@app.route("/dashboard/api/audit")
@require_dashboard_user
def dashboard_audit():
    """Return recent audit log entries for this tenant."""
    log = db.get_audit_log(g.tenant_id, limit=100)
    return jsonify({"success": True, "data": log})


@app.route("/dashboard/api/audit/export")
@require_dashboard_user
def dashboard_audit_export():
    """Download the full audit log as a CSV file."""
    import csv, io
    from flask import Response
    log = db.get_audit_log(g.tenant_id, limit=10000)
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["created_at", "user_email", "action", "target", "status"])
    writer.writeheader()
    for row in log:
        writer.writerow({k: row.get(k, "") for k in writer.fieldnames})
    filename = f"audit-log-{g.tenant_name.lower().replace(' ', '-')}.csv"
    return Response(
        output.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'}
    )


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
    """Create a new dashboard user for this tenant (admin only).
    If no password is provided, one is auto-generated and returned once."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data          = request.get_json() or {}
    email         = data.get("email", "").strip()
    password      = data.get("password", "").strip()
    role          = data.get("role", "viewer")
    auto_password = not bool(password)
    if not email:
        return jsonify({"success": False, "message": "email is required."}), 400
    if auto_password:
        password = _gen_password()
    if role not in ("admin", "viewer"):
        return jsonify({"success": False, "message": "role must be admin or viewer."}), 400

    # Enforce team member plan limit
    plan    = db.get_tenant_plan(g.tenant_id)
    limits  = db.get_plan_limits(plan)
    current = db.list_tenant_users(g.tenant_id)
    cap     = limits["team_members"]
    if cap is not None and len(current) >= cap:
        return jsonify({
            "success": False,
            "limit_reached": True,
            "message": f"Team member limit reached ({cap} on {limits['label']} plan). Upgrade to Pro for up to {db.PLAN_LIMITS['pro']['team_members']} members.",
        }), 429

    try:
        user = db.create_tenant_user(g.tenant_id, email, password, role)
        db.log_activity(g.tenant_id, "team_member_added", g.user_email,
                        target=email, detail=f"Added {role}: {email}")
        response = {"success": True, "data": user}
        if auto_password:
            response["generated_password"] = password  # shown once in UI, never stored plain
        return jsonify(response), 201
    except Exception:
        return jsonify({"success": False, "message": "An account with that email already exists."}), 409


@app.route("/dashboard/api/onboarding/dismiss", methods=["POST"])
@require_dashboard_user
def dashboard_onboarding_dismiss():
    """Mark the onboarding checklist as dismissed for this tenant."""
    settings = db.get_settings(g.tenant_id)
    settings["onboarding_dismissed"] = True
    db.update_settings(g.tenant_id, settings)
    return jsonify({"success": True})


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


@app.route("/admin/ui")
def admin_ui():
    """Browser-based admin panel — protected by ADMIN_KEY entered in the UI."""
    return render_template("admin_panel.html")


@app.route("/admin/tenants/<tenant_id>/set-plan", methods=["PATCH"])
@require_admin
def admin_set_tenant_plan(tenant_id):
    """Set plan for a specific tenant by ID."""
    plan = (request.get_json() or {}).get("plan", "").strip().lower()
    if plan not in ("free", "pro", "enterprise"):
        return jsonify({"success": False, "message": "plan must be free, pro, or enterprise"}), 400
    db.set_tenant_plan(tenant_id, plan)
    return jsonify({"success": True, "message": f"Plan set to '{plan}'"})


@app.route("/admin/set-plan", methods=["POST"])
@require_admin
def admin_set_plan():
    """
    Set a tenant's plan by user email.
    Body: { "email": "admin@example.com", "plan": "enterprise" }
    """
    data  = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    plan  = data.get("plan", "").strip().lower()
    if plan not in ("free", "pro", "enterprise"):
        return jsonify({"success": False, "message": "plan must be free, pro, or enterprise"}), 400
    if not email:
        return jsonify({"success": False, "message": "email is required"}), 400
    tenant = db.get_tenant_by_user_email(email)
    if not tenant:
        return jsonify({"success": False, "message": f"No user found with email {email}"}), 404
    db.set_tenant_plan(tenant["id"], plan)
    return jsonify({"success": True, "message": f"Plan set to '{plan}' for tenant of {email}"})


# ---------------------------------------------------------------------------
# Agent endpoints -- called by agent.py running on the customer's server
# ---------------------------------------------------------------------------

@app.route("/dashboard/api/agent/status")
@require_dashboard_user
def dashboard_agent_status():
    """Return whether the Windows agent is currently connected."""
    status = db.get_agent_status(g.tenant_id)
    return jsonify({"success": True, "data": status})


@app.route("/agent/poll", methods=["GET"])
@require_tenant
def agent_poll():
    """Agent calls this every 0.5s to check for pending commands."""
    db.update_agent_ping(g.tenant["id"])
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


# ---------------------------------------------------------------------------
# Error handlers
# ---------------------------------------------------------------------------

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(e):
    return render_template("500.html"), 500


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("\n AD Helpdesk -- Cloud Backend + Dashboard")
    print(" ------------------------------------------")
    print(f" Listening on port {port}")
    print(f" Dashboard: http://localhost:{port}/")
    print(" Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)
