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
import re
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
import action_policy
from graph_client import GraphClient, ACTIONS as GRAPH_ACTIONS

load_dotenv()

app = Flask(__name__)

_IS_DEV = os.getenv("FLASK_ENV", "production") == "development"

# Fail closed on required secrets. A hardcoded fallback signing key in a public
# repo lets anyone forge a session cookie and impersonate any tenant, so in
# production we refuse to start without an explicit SECRET_KEY rather than
# silently running on a known default.
_SECRET_KEY = os.getenv("SECRET_KEY", "")
if not _SECRET_KEY:
    if _IS_DEV:
        _SECRET_KEY = "dev-only-insecure-key-do-not-use-in-prod"
    else:
        raise RuntimeError(
            "SECRET_KEY is not set. Refusing to start in production with a "
            "default signing key (would allow session forgery / account "
            "takeover). Set the SECRET_KEY environment variable."
        )
app.secret_key = _SECRET_KEY

# Session cookie security:
# - HttpOnly:  JS cannot read the cookie (Flask default is True)
# - SameSite:  Lax prevents cross-site POST from carrying the session
#              → provides CSRF protection for all JSON dashboard endpoints
# - Secure:    only send over HTTPS (disabled in dev so localhost works)
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"]   = not _IS_DEV

# Admin endpoints must never be reachable without an explicit key. If ADMIN_KEY
# is unset the require_admin decorator below denies every request (fail closed),
# and in production we refuse to boot at all so the misconfig can't slip by.
ADMIN_KEY = os.getenv("ADMIN_KEY", "")
if not ADMIN_KEY and not _IS_DEV:
    raise RuntimeError(
        "ADMIN_KEY is not set. Refusing to start in production: every /admin "
        "endpoint would be publicly accessible. Set the ADMIN_KEY environment "
        "variable."
    )
DEFAULT_AI_NAME = "Assistant"

# Assistant intelligence tiers. Tenants pick one in Configure AI; it selects the
# Claude model the assistant reasons with. Cost multipliers are relative to the
# Normal (Haiku) tier and are used to weight monthly AI-scan usage accordingly.
AI_MODELS = {
    "normal": {"model": "claude-haiku-4-5-20251001", "label": "Normal", "cost": 1.0},
    "high":   {"model": "claude-sonnet-5",           "label": "High",   "cost": 2.5},
    "super":  {"model": "claude-opus-4-8",            "label": "Super",  "cost": 5.0},
}
DEFAULT_AI_TIER = "normal"


def _ai_tier(tenant_id):
    """Return the tenant's chosen intelligence tier key (validated)."""
    try:
        tier = (db.get_settings(tenant_id) or {}).get("ai_model", DEFAULT_AI_TIER)
    except Exception:
        tier = DEFAULT_AI_TIER
    return tier if tier in AI_MODELS else DEFAULT_AI_TIER


def _ai_model(tenant_id):
    """Return the Claude model id for the tenant's intelligence tier."""
    return AI_MODELS[_ai_tier(tenant_id)]["model"]


def _ai_cost(tenant_id):
    """Usage weight (rounded up) for one assistant call at the tenant's tier."""
    import math
    return int(math.ceil(AI_MODELS[_ai_tier(tenant_id)]["cost"]))


# Standard security headers applied to every response.
@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
    resp.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if not _IS_DEV:
        resp.headers.setdefault(
            "Strict-Transport-Security", "max-age=31536000; includeSubDomains"
        )
    return resp

db.init_db()
db.migrate_db()


# ---------------------------------------------------------------------------
# Rate limiting (DB-backed — shared across dynos, survives deploys)
# ---------------------------------------------------------------------------

def _rate_limit(key: str, max_calls: int, window_seconds: int) -> bool:
    """Return True if allowed, False if rate-limited. key = e.g. 'signup:1.2.3.4'.
    Backed by the shared rate_hits table so limits hold across instances."""
    return db.rate_limit_allow(key, max_calls, window_seconds)


# ---------------------------------------------------------------------------
# Destructive action confirmation tokens
# High-risk actions require the admin to confirm a 6-digit challenge code
# before the command is queued. Tokens expire after 5 minutes, single-use.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Time saved per action (realistic "AD already open" estimates, in minutes)
# These intentionally lean conservative -- the goal is credibility, not hype.
# ---------------------------------------------------------------------------

TIME_SAVED_MINUTES: dict[str, float] = {
    # Write ops
    "unlock_account":             1.5,   # find user in ADUC, right-click, unlock
    "reset_password":             2.0,   # find user, reset, communicate temp password
    "enable_account":             1.0,
    "disable_account":            1.5,
    "force_password_change":      1.0,
    "add_to_group":               2.0,   # navigate to group, add member
    "remove_from_group":          2.0,
    "set_password_never_expires": 1.0,
    # Destructive / structural ops
    "create_user":                8.0,   # wizard, groups, OU, notify requester
    "move_user":                  2.0,
    "create_ou":                  3.0,
    "bulk_move_users":           15.0,   # per batch (would be per-user manually)
    # Read ops -- minor but real (spares opening ADUC / running reports)
    "get_user_info":              0.5,
    "list_users_in_ou":           0.5,
    "search_users":               0.5,
    "list_locked_accounts":       1.0,
    "list_expired_passwords":     1.0,
    "get_stats":                  0.5,
    "list_ous":                   0.5,
    "list_groups":                0.5,
    "get_group_members":          0.5,
    # DNS
    "add_dns_record":             1.5,
    "update_dns_record":          1.5,
    "remove_dns_record":          1.0,
    "set_dns_scavenging":         1.0,
    "list_dns_zones":             0.5,
    "list_dns_records":           0.5,
    "get_dns_zone":               0.5,
    "get_dns_scavenging":         0.5,
    # DHCP
    "add_dhcp_reservation":       1.5,
    "remove_dhcp_reservation":    1.0,
    "add_dhcp_exclusion":         1.0,
    "remove_dhcp_exclusion":      1.0,
    "list_dhcp_scopes":           0.5,
    "get_dhcp_scope":             0.5,
    "get_dhcp_scope_stats":       0.5,
    "list_dhcp_leases":           0.5,
    "list_dhcp_reservations":     0.5,
    "list_dhcp_exclusions":       0.5,
    # GPO
    "link_gpo":                   1.5,
    "unlink_gpo":                 1.0,
    "set_gpo_status":             1.0,
    "set_gpo_link_enforced":      1.0,
    "list_gpos":                  0.5,
    "get_gpo":                    0.5,
    "get_gpo_report":             1.0,
    "list_gpo_links":             0.5,
    "get_gpo_inheritance":        0.5,
}


# Actions that require an explicit 6-digit confirmation challenge.
#
# Philosophy: only gate actions that are HARD TO UNDO and HIGH BLAST RADIUS.
# Routine helpdesk operations (password reset, unlock, group add/remove,
# enable/disable, create user) should NOT require a code — the user confirmed
# their identity via the ticket or chat session, and blocking those with a
# 6-digit prompt defeats the purpose of an auto-resolution system.
#
# The gate is reserved for bulk / structural changes that can break many users
# at once and are annoying to roll back: bulk_move_users and create_ou.
# disable_account is kept because silently disabling an account for the wrong
# user is a significant incident.
#
# All other action_policy.DESTRUCTIVE actions (remove_from_group, move_user,
# create_user) are intentionally NOT gated — they are reviewed by the admin
# in the chat/ticket flow, which is confirmation enough.
DESTRUCTIVE_ACTIONS: frozenset[str] = frozenset({
    "disable_account",
    "bulk_move_users",
    "create_ou",
    # DNS: removing a record or changing scavenging can break name resolution
    # for a whole zone, so both require the 6-digit confirmation challenge.
    "remove_dns_record",
    "set_dns_scavenging",
    # DHCP: removing a reservation or changing an exclusion range can knock
    # a device off the network, so all three require the confirmation challenge.
    "remove_dhcp_reservation",
    "add_dhcp_exclusion",
    "remove_dhcp_exclusion",
    # GPO: linking/unlinking a GPO, changing its status, or toggling link
    # enforcement can change policy for an entire OU (and everything under
    # it) at once, so all four require the confirmation challenge -- none
    # of the GPO writes are in action_policy.WRITE.
    "link_gpo",
    "unlink_gpo",
    "set_gpo_status",
    "set_gpo_link_enforced",
})

_CONFIRM_TTL = 300                     # 5 minutes

def _issue_confirm_token(tenant_id: str, action: str, args: list) -> str:
    """Generate a 6-digit confirmation code and store the pending action.
    Backed by the shared confirm_tokens table (survives deploys / multi-dyno)."""
    return db.issue_confirm_token(tenant_id, action, args, _CONFIRM_TTL)

def _consume_confirm_token(tenant_id: str, code: str) -> dict | None:
    """Validate and single-use-consume a confirmation token. Returns the pending
    action {action, args, tenant_id} or None."""
    return db.consume_confirm_token(tenant_id, code)


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


def _get_ai_name(settings: dict | None = None) -> str:
    """Return the configured AI display name with a generic default."""
    settings = settings or {}
    return str(settings.get("ai_name") or os.getenv("AI_NAME") or DEFAULT_AI_NAME).strip() or DEFAULT_AI_NAME


def _get_ai_context(settings: dict | None = None) -> str:
    """Return tenant-provided AI context, supporting the previous settings key."""
    settings = settings or {}
    return str(settings.get("ai_context") or settings.get("janus_context") or "").strip()


def _tenant_plan_limits(tenant_id: str) -> tuple[str, dict]:
    """Return the effective plan and limits. Paid plans require active Stripe billing."""
    plan = db.get_tenant_plan(tenant_id)
    return plan, db.get_plan_limits(plan)


def _paid_feature_block(tenant_id: str, feature_key: str, feature_label: str) -> str | None:
    """Return an error message when a plan feature is unavailable."""
    plan, limits = _tenant_plan_limits(tenant_id)
    value = limits.get(feature_key)
    if isinstance(value, bool):
        allowed = value
    elif feature_key.endswith("_limit"):
        allowed = value is None or int(value or 0) > 0
    else:
        allowed = bool(value)
    if allowed:
        return None
    return (
        f"{feature_label} is not available on the {limits['label']} plan. "
        "Upgrade to an active Pro or Enterprise subscription to use it."
    )


def _custom_scripts_limit(tenant_id: str) -> tuple[int | None, str, dict]:
    plan, limits = _tenant_plan_limits(tenant_id)
    return limits.get("custom_scripts_limit", 0), plan, limits


AI_CHAT_BURST_LIMIT_PER_HOUR = 60
BULK_MOVE_USERS_PER_AD_UNIT = 25
BULK_MOVE_RULES_PER_AD_UNIT = 10


def _is_ad_mutation(action: str) -> bool:
    return action_policy.is_write(action) or action_policy.is_destructive(action)


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _bulk_move_total(result_data) -> int | None:
    """Extract the actual moved user count returned by bulk_move_users."""
    if not isinstance(result_data, dict):
        return None
    if "total" not in result_data:
        return None
    total = _safe_int(result_data.get("total"), default=-1)
    return total if total >= 0 else None


def _bulk_move_rule_count(args: list | None) -> int:
    if not args or len(args) < 2:
        return 0
    rules_raw = args[1]
    if isinstance(rules_raw, list):
        return len(rules_raw)
    if not isinstance(rules_raw, str) or not rules_raw.strip():
        return 0
    try:
        rules = json.loads(rules_raw)
    except (TypeError, ValueError):
        return 0
    return len(rules) if isinstance(rules, list) else 0


def _bulk_move_rule_units(args: list | None) -> int:
    rule_count = _bulk_move_rule_count(args)
    if rule_count <= 0:
        return 1
    return max(1, (rule_count + BULK_MOVE_RULES_PER_AD_UNIT - 1) // BULK_MOVE_RULES_PER_AD_UNIT)


def _ad_usage_units(action: str, args: list | None = None, result_data=None) -> int:
    """
    Return monthly AD quota units for this action.

    Bulk moves are not charged per user, but they are not a one-action loophole
    either: a batch costs at least one unit, then scales by moved users and very
    large rule sets.
    """
    if not _is_ad_mutation(action):
        return 0
    if action == "bulk_move_users":
        rule_units = _bulk_move_rule_units(args)
        total = _bulk_move_total(result_data)
        if total is None:
            return rule_units
        user_units = max(1, (total + BULK_MOVE_USERS_PER_AD_UNIT - 1) // BULK_MOVE_USERS_PER_AD_UNIT)
        return max(rule_units, user_units)
    return 1


def _usage_units_label(units: int) -> str:
    return "1 AD action" if units == 1 else f"{units} AD actions"


def _ad_usage_limit_block(tenant_id: str, action: str, args: list | None = None,
                          result_data=None) -> dict | None:
    units = _ad_usage_units(action, args, result_data)
    if units <= 0:
        return None
    plan, limits = _tenant_plan_limits(tenant_id)
    usage = db.get_usage(tenant_id) or {}
    used = _safe_int(usage.get("ad_commands"), 0)
    cap = limits.get("ad_commands")
    if cap is not None and used + units > int(cap):
        return {
            "limit_reached": True,
            "units": units,
            "used": used,
            "cap": int(cap),
            "label": limits["label"],
            "message": (
                f"Monthly AD action limit would be exceeded "
                f"({used}/{cap} used; this needs {_usage_units_label(units)})."
            ),
        }
    return None


def _charge_ad_usage(tenant_id: str, action: str, args: list | None = None,
                     result_data=None, already_charged_units: int = 0) -> int:
    units = _ad_usage_units(action, args, result_data)
    delta = max(0, units - max(0, _safe_int(already_charged_units, 0)))
    if delta:
        db.increment_usage(tenant_id, "ad_commands", amount=delta)
    return delta


def _queue_args_with_ad_budget(tenant_id: str, action: str, args: list | None) -> list:
    """
    Add internal runtime guards to commands before they reach the agent.

    bulk_move_users accepts a hidden fourth argument: max users that may be
    moved under the tenant's remaining AD quota. The agent checks this before
    it mutates anything.
    """
    out = list(args or [])
    if action != "bulk_move_users":
        return out

    _, limits = _tenant_plan_limits(tenant_id)
    cap = limits.get("ad_commands")
    if cap is None:
        return out

    usage = db.get_usage(tenant_id) or {}
    used = _safe_int(usage.get("ad_commands"), 0)
    remaining_units = max(0, int(cap) - used)
    max_users = remaining_units * BULK_MOVE_USERS_PER_AD_UNIT

    while len(out) < 3:
        out.append("true" if len(out) == 2 else "")
    if len(out) >= 4:
        out[3] = str(max_users)
    else:
        out.append(str(max_users))
    return out


def _reconcile_bulk_ad_usage(tenant_id: str, command: dict | None,
                             success: bool, result_data) -> int:
    """
    Bulk moves are charged once before queueing using rule count. Once the
    agent reports how many users moved, charge any extra 25-user blocks.
    """
    if not success or not command or command.get("action") != "bulk_move_users":
        return 0
    reserved_units = _ad_usage_units(command["action"], command.get("args") or [])
    return _charge_ad_usage(
        tenant_id,
        command["action"],
        command.get("args") or [],
        result_data=result_data,
        already_charged_units=reserved_units,
    )


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
    ai_name = _get_ai_name(tenant_settings)
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

- AID Helpdesk (powered by {ai_name})
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
  <p style="color:#64748b;font-size:12px;">Powered by {ai_name} - AID Helpdesk</p>
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
# Slack / Teams webhook notifications
# ---------------------------------------------------------------------------

def _send_webhook_notification(webhook_url: str, text: str, platform: str = "slack") -> None:
    """POST a simple notification to a Slack or Teams incoming webhook. Silently skips on failure."""
    if not webhook_url:
        return
    try:
        import urllib.request
        if platform == "teams":
            payload = json.dumps({"text": text}).encode()
        else:
            payload = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            webhook_url,
            data=payload,
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"[webhook] {platform} notification failed: {e}")


def notify_integrations(tenant_id: str, text: str) -> None:
    """Send a notification to all configured Slack/Teams webhooks for this tenant."""
    if _paid_feature_block(tenant_id, "integrations", "Slack/Teams integrations"):
        return
    settings = db.get_settings(tenant_id)
    slack_url = settings.get("slack_webhook_url", "")
    teams_url = settings.get("teams_webhook_url", "")
    if slack_url:
        _send_webhook_notification(slack_url, text, platform="slack")
    if teams_url:
        _send_webhook_notification(teams_url, text, platform="teams")


# ---------------------------------------------------------------------------
# AI reflection pass (organisational memory extraction)
# ---------------------------------------------------------------------------

def _run_reflection_pass(tenant_id: str, ai_name: str, user_message: str,
                         action: str, args: list, result_success: bool) -> None:
    """
    After a meaningful action, ask Claude to extract any org-specific facts worth remembering.
    Runs in a background thread so it never blocks the chat response.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key or not result_success:
        return
    try:
        import anthropic as _ant
        client = _ant.Anthropic(api_key=api_key)
        prompt = f"""An IT admin asked their AI assistant ("{ai_name}") to: "{user_message}"
The action taken was: {action} with args {args}
The action succeeded.

Extract any organisation-specific facts from this interaction that would help the AI do its job better in the future.
Examples of useful facts:
  - "Username suffix 08 = Year 8 students"
  - "Bulk moves happen at semester rollover"
  - "Finance OU requires extra confirmation"
  - "Admin prefers dry-run summary before bulk ops"
  - "OU named 'Teachers' contains all teaching staff"

Reply with a JSON array of facts, or an empty array [] if nothing meaningful was learned.
Each fact: {{"category": "org|preference|user|ou_structure", "key": "short label", "value": "the fact"}}
Output ONLY the JSON array, no other text."""

        resp = client.messages.create(
            model=_ai_model(tenant_id),
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = resp.content[0].text.strip()
        import re as _re
        match = _re.search(r'\[.*\]', raw, _re.DOTALL)
        if not match:
            return
        facts = json.loads(match.group())
        for f in facts:
            if isinstance(f, dict) and f.get("key") and f.get("value"):
                db.upsert_memory(
                    tenant_id,
                    category=f.get("category", "general"),
                    key=f["key"],
                    value=f["value"],
                    confidence=0.75,
                    source="auto"
                )
    except Exception as e:
        print(f"[reflection] Failed: {e}")


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
    """Authenticate admin endpoints using X-Admin-Key header. Fails closed."""
    @wraps(f)
    def decorated(*args, **kwargs):
        # Fail closed: if no admin key is configured, deny rather than allow.
        if not ADMIN_KEY:
            return jsonify({"success": False, "message": "Admin access is not configured."}), 503
        if request.headers.get("X-Admin-Key") != ADMIN_KEY:
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
    # Verify the database is actually reachable so uptime monitors catch a DB
    # outage instead of seeing a green "ok" from an app that can't serve anyone.
    db_ok = db.ping()
    return jsonify({
        "status":  "ok" if db_ok else "degraded",
        "service": "ad-helpdesk-cloud",
        "db":      "up" if db_ok else "down",
    }), (200 if db_ok else 503)


@app.route("/robots.txt")
def robots():
    from flask import Response
    return Response("User-agent: *\nAllow: /\nDisallow: /dashboard\nDisallow: /admin\n",
                    mimetype="text/plain")


@app.route("/privacy")
def privacy():
    return render_template("privacy.html")


@app.route("/terms")
def terms():
    return render_template("terms.html")


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
        ip    = request.headers.get("X-Forwarded-For", request.remote_addr or "").split(",")[0].strip()
        email = request.form.get("email", "").strip().lower()
        # Rate limit: 10 attempts per IP per 15 minutes, plus 5 per email per 15 minutes
        if not _rate_limit(f"login_ip:{ip}", 10, 900):
            error = "Too many login attempts. Please wait 15 minutes and try again."
        elif email and not _rate_limit(f"login_email:{email}", 5, 900):
            error = "Too many login attempts for this account. Please wait 15 minutes."
        else:
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

    # AI analysis
    result = _run_janus_analysis(
        tenant_id, tenant_name, ticket["id"],
        title, desc, from_name, from_email, limits
    )

    # Send auto-reply to requester acknowledging receipt
    if from_email:
        tenant_settings = db.get_settings(tenant_id)
        ai_name = _get_ai_name(tenant_settings)
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
                f"Great news! Your request (#{short_id}) has been resolved automatically by {ai_name}. "
                f"If you still have issues, please reply or submit a new request."
            )

        send_ticket_email(
            to_email    = from_email,
            to_name     = from_name or "",
            ticket_title = title,
            action_taken = None,
            message     = auto_msg,
            tenant_settings = tenant_settings,
        )

    return jsonify({"success": True, "ticket_id": ticket["id"]}), 201


# ---------------------------------------------------------------------------
# Zoho Desk integration — inbound ticket webhook
# ---------------------------------------------------------------------------

_ZOHO_PRIORITY_MAP = {"urgent": "urgent", "high": "high", "medium": "medium", "low": "low"}


def _parse_zoho_ticket(data: dict) -> dict:
    """Map a Zoho Desk webhook payload to AID ticket fields, defensively.
    Zoho's payload shape varies with how the webhook / workflow rule is set up,
    so we check a few likely locations for each field and unwrap common envelopes."""
    t = data if isinstance(data, dict) else {}
    for key in ("ticket", "payload", "data"):
        if isinstance(t.get(key), dict):
            t = t[key]
            break

    def pick(*keys):
        for k in keys:
            v = t.get(k)
            if v:
                return v
        return None

    subject = pick("subject", "Subject", "ticketSubject") or "Support request"
    desc    = pick("description", "Description", "content", "ticketDescription") or ""

    email = pick("email", "Email", "fromEmailAddress", "contactEmail")
    name  = pick("contactName", "customerName")
    contact = t.get("contact") if isinstance(t.get("contact"), dict) else {}
    if not email:
        email = contact.get("email") or contact.get("emailId")
    if not name:
        name = (f"{contact.get('firstName','')} {contact.get('lastName','')}".strip() or None)

    zoho_id   = str(pick("id", "ticketId") or "").strip() or None
    ticket_no = pick("ticketNumber", "ticketNo")
    priority  = _ZOHO_PRIORITY_MAP.get(str(pick("priority", "Priority") or "").strip().lower(), "medium")

    return {
        "subject":     str(subject)[:120].strip(),
        "description": str(desc)[:4000].strip(),
        "email":       (str(email).strip() if email else None),
        "name":        (str(name).strip() if name else None),
        "zoho_id":     zoho_id,
        "ticket_no":   (str(ticket_no) if ticket_no else None),
        "priority":    priority,
    }


@app.route("/webhook/zoho/<api_key>", methods=["POST"])
def zoho_intake(api_key):
    """Inbound Zoho Desk webhook — turns Zoho Desk tickets into AID tickets and
    runs the same AI-analysis pipeline as email intake.

    Set up in Zoho Desk (Setup → Automation → Webhooks, fired by a workflow rule
    on ticket creation), POSTing the ticket as JSON to:
      https://<your-app>/webhook/zoho/<tenant_api_key>
    """
    tenant = db.get_tenant_by_key(api_key)
    if not tenant:
        return jsonify({"success": False, "message": "Invalid API key."}), 401
    tenant_id   = tenant["id"]
    tenant_name = tenant.get("name", "")

    plan   = db.get_tenant_plan(tenant_id)
    limits = db.get_plan_limits(plan)
    if not limits.get("integrations"):
        return jsonify({"success": False,
                        "message": f"Integrations are not available on the {limits['label']} plan."}), 403

    # Webhooks can be chatty (create + update events); allow a generous window.
    if not _rate_limit(f"zoho_intake:{tenant_id}", 120, 3600):
        return jsonify({"success": False, "message": "Rate limit exceeded."}), 429

    data = request.get_json(force=True, silent=True) or {}
    z = _parse_zoho_ticket(data)
    if not z["description"] and z["subject"] == "Support request":
        return jsonify({"success": False,
                        "message": "Could not parse Zoho ticket (subject/description missing)."}), 400

    # Dedupe: a Zoho ticket we've already imported (workflow can fire repeatedly).
    if z["zoho_id"]:
        existing = db.find_ticket_by_external_ref(tenant_id, z["zoho_id"])
        if existing:
            return jsonify({"success": True, "message": "Already imported.",
                            "ticket_id": existing["id"]}), 200

    # Enforce the ticket plan cap.
    cap = limits.get("tickets")
    if cap is not None and len(db.list_tickets(tenant_id, limit=cap + 1)) >= cap:
        return jsonify({"success": False, "message": "Ticket limit reached."}), 429

    title = z["subject"] or (f"Zoho ticket {z['ticket_no']}".strip() if z["ticket_no"] else "Support request")
    desc  = z["description"] or title

    ticket = db.create_ticket(
        tenant_id,
        created_by      = "zoho-intake",
        title           = title,
        description     = desc,
        priority        = z["priority"],
        requester_name  = z["name"],
        requester_email = z["email"],
        source          = "zoho",
        external_ref    = z["zoho_id"],
    )

    db.log_activity(tenant_id, "ticket_created", z["email"] or "zoho",
                    target=title, detail="via Zoho Desk")

    _run_janus_analysis(
        tenant_id, tenant_name, ticket["id"],
        title, desc, z["name"], z["email"], limits
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
# Public API v1 — direct action endpoints (no AI layer).
# API-key authenticated via X-API-Key header → require_tenant.
# Claude Code's own session handles natural-language → action mapping;
# the backend just queues commands and returns raw results.
# ---------------------------------------------------------------------------

# Ordered parameter spec for each action: (field_name, required)
_ACTION_PARAMS: dict[str, list[tuple[str, bool]]] = {
    # Users
    "get_user_info":              [("username", True)],
    "list_users":                 [],
    "list_users_in_ou":           [("ou", True)],
    "search_users":               [("query", True)],
    "list_group_memberships":     [("username", True)],
    "create_user":                [("first_name", True), ("last_name", True), ("username", True), ("ou", False)],
    "move_user":                  [("username", True), ("ou", True)],
    # Account state
    "unlock_account":             [("username", True)],
    "enable_account":             [("username", True)],
    "disable_account":            [("username", True)],
    "reset_password":             [("username", True), ("password", True)],
    "force_password_change":      [("username", True)],
    "set_password_never_expires": [("username", True), ("enabled", False)],
    # Groups
    "list_groups":                [],
    "search_groups":              [("query", True)],
    "get_group_members":          [("group", True)],
    "add_to_group":               [("username", True), ("group", True)],
    "remove_from_group":          [("username", True), ("group", True)],
    # OUs
    "list_ous":                   [],
    "create_ou":                  [("name", True), ("parent_ou", False)],
    # Reporting
    "list_locked_accounts":       [],
    "list_expired_passwords":     [],
    "get_stats":                  [],
    # Bulk
    "bulk_move_users":            [("source_ou", True), ("rules", False), ("create_missing_ous", False)],
}


def _build_args(action: str, data: dict) -> list | None:
    """
    Map a flat JSON/query-param dict to the positional args list the agent expects.
    Returns None if a required parameter is missing.
    """
    spec = _ACTION_PARAMS.get(action)
    if spec is None:
        return None
    args = []
    for field, required in spec:
        val = data.get(field)
        if val is None:
            if required:
                return None
            args.append("")
        else:
            args.append(str(val))
    # Trim trailing empty strings (optional args not supplied)
    while args and args[-1] == "":
        args.pop()
    return args


@app.route("/api/v1/status", methods=["GET"])
@require_tenant
def api_v1_status():
    """Return tenant info and agent online/offline status."""
    tenant   = g.tenant
    settings = db.get_settings(tenant["id"])
    import datetime as _dt
    last_seen = tenant.get("agent_last_seen") or ""
    try:
        ls_dt  = _dt.datetime.fromisoformat(last_seen)
        online = (_dt.datetime.utcnow() - ls_dt).total_seconds() < 90
    except Exception:
        online = False
    return jsonify({
        "success": True,
        "tenant":  tenant.get("name", ""),
        "plan":    db.get_tenant_plan(tenant["id"]),
        "agent":   "online" if online else "offline",
    })


@app.route("/api/v1/tickets", methods=["GET"])
@require_tenant
def api_v1_tickets():
    """Return tickets for this tenant. Optional ?status= filter."""
    tenant_id = g.tenant["id"]
    status    = request.args.get("status", "")
    tickets   = db.list_tickets(tenant_id, status=status or None, limit=50)
    return jsonify({"success": True, "data": tickets})


@app.route("/api/v1/actions", methods=["GET"])
@require_tenant
def api_v1_list_actions():
    """List all available actions with their parameter spec and classification."""
    actions = []
    for act, params in _ACTION_PARAMS.items():
        actions.append({
            "action":         act,
            "classification": action_policy.classify(act),
            "params":         [{"name": f, "required": r} for f, r in params],
        })
    return jsonify({"success": True, "data": actions})


@app.route("/api/v1/actions/<action>", methods=["GET", "POST"])
@require_tenant
def api_v1_action(action):
    """
    Execute a direct AD action — no AI layer.

    POST: JSON body supplies parameters  {"username": "john.smith", ...}
    GET:  query-string supplies parameters  ?username=john.smith

    Queues the command to the Windows agent, polls up to 25 s, returns the
    raw result.  Claude Code's session is the intelligence layer; this
    endpoint is just a thin, audited queue.
    """
    if action not in action_policy.ALL_ACTIONS:
        return jsonify({
            "success": False,
            "message": f"Unknown action '{action}'. Call GET /api/v1/actions for the full list.",
        }), 404

    tenant_id = g.tenant["id"]

    if request.method == "POST":
        data = request.get_json() or {}
    else:
        data = dict(request.args)

    args = _build_args(action, data)
    if args is None:
        spec    = _ACTION_PARAMS.get(action, [])
        missing = [f for f, req in spec if req and not data.get(f)]
        return jsonify({
            "success": False,
            "message": f"Missing required parameter(s): {', '.join(missing)}",
            "params":  [{"name": f, "required": r} for f, r in spec],
        }), 400

    ok, reason = action_policy.validate(action, source="human")
    if not ok:
        return jsonify({"success": False, "message": reason}), 403

    is_mutation = _is_ad_mutation(action)

    # ----- Destructive-action confirmation handshake --------------------
    # Destructive actions require a two-step confirm: the first call returns a
    # 6-digit token, the caller must echo it back to actually run the action.
    # This prevents an accidental single-call destructive op (e.g. Claude Code
    # misreading intent). Same token machinery the dashboard uses.
    if action == "run_custom_script":
        block = _paid_feature_block(tenant_id, "custom_scripts_limit", "Custom scripts")
        if block:
            return jsonify({"success": False, "message": block}), 403

    if action in DESTRUCTIVE_ACTIONS:
        supplied = str(data.get("confirm_token", "")).strip()
        if not supplied:
            code = _issue_confirm_token(tenant_id, action, args)
            return jsonify({
                "success":               False,
                "confirmation_required": True,
                "confirm_token":         code,
                "message": (
                    f"'{action}' is a destructive action. Re-send the same request "
                    f"with \"confirm_token\": \"{code}\" within 5 minutes to proceed."
                ),
            }), 409
        entry = _consume_confirm_token(tenant_id, supplied)
        if not entry or entry.get("action") != action:
            return jsonify({
                "success": False,
                "message": "Invalid or expired confirm_token. Re-request the action to get a fresh code.",
            }), 403
        args = entry["args"]

    # ----- Monthly plan-quota enforcement (the real abuse guard) --------
    # Mirrors the dashboard /api/command path: write/destructive actions count
    # against the tenant's monthly ad_commands cap. Reads are uncapped.
    queue_args = _queue_args_with_ad_budget(tenant_id, action, args)

    if is_mutation:
        limit_block = _ad_usage_limit_block(tenant_id, action, args)
        if limit_block:
            return jsonify({
                "success":       False,
                "limit_reached": True,
                "message":       limit_block["message"],
            }), 429

        # Burst guard on top of the monthly cap.
        if not _rate_limit(f"api_writes:{tenant_id}", 30, 3600):
            return jsonify({"success": False, "message": "Rate limit: 30 write actions/hour."}), 429

        db.log_audit(tenant_id, "api-plugin", action, data.get("username", ""), "queued")
        _charge_ad_usage(tenant_id, action, args)

    command    = db.queue_command(tenant_id, action, queue_args)
    command_id = command["id"]

    result_data = None
    deadline    = time.time() + 25
    while time.time() < deadline:
        time.sleep(0.6)
        res = db.get_command_result(command_id, tenant_id)
        if res:
            result_data = res
            break

    if not result_data:
        return jsonify({
            "success": False,
            "message": "Agent did not respond in time. Is the Windows agent running?",
        }), 504

    return jsonify({
        "success": result_data.get("success", False),
        "message": result_data.get("message", ""),
        "data":    result_data.get("data"),
    })


# ---------------------------------------------------------------------------
# Dashboard -- main page
# ---------------------------------------------------------------------------

@app.route("/dashboard")
@require_dashboard_user
def dashboard():
    plan = db.get_tenant_plan(g.tenant_id)
    settings = db.get_settings(g.tenant_id)
    capabilities = db.get_tenant_capabilities(g.tenant_id)
    return render_template("dashboard.html",
                           tenant_name=g.tenant_name,
                           user_email=g.user_email,
                           user_role=g.user_role,
                           tenant_plan=plan,
                           tenant_limits=db.get_plan_limits(plan),
                           ai_name=_get_ai_name(settings),
                           capabilities=capabilities,
                           entra_configured=_entra_configured(g.tenant_id))


@app.route("/billing")
@require_dashboard_user
def billing():
    settings      = db.get_settings(g.tenant_id)
    billing_enabled = bool(os.getenv("STRIPE_SECRET_KEY", ""))
    billing_customer = db.get_billing_customer(g.tenant_id)
    billing_sub  = db.get_billing_subscription(g.tenant_id) or {}
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
                           user_role=g.user_role,
                           settings=settings,
                           billing_enabled=billing_enabled,
                           billing_customer=billing_customer,
                           billing_status=billing_sub.get("status", "none"),
                           billing_current_period_end=billing_sub.get("current_period_end"),
                           billing_portal_available=bool(billing_customer),
                           tenant_plan=plan,
                           plan_label=limits["label"],
                           plan_price=limits["price"],
                           usage_janus=used_janus,
                           usage_commands=used_commands,
                           limits_janus=lim_janus,
                           limits_commands=lim_commands,
                           ai_name=_get_ai_name(settings),
                           janus_pct=min(100, int(used_janus / lim_janus * 100)) if lim_janus else 0,
                           commands_pct=min(100, int(used_commands / lim_commands * 100)) if lim_commands else 0,
                           )


# ---------------------------------------------------------------------------
# Stripe billing
# ---------------------------------------------------------------------------
# Required env vars:
#   STRIPE_SECRET_KEY      — secret key from Stripe dashboard
#   STRIPE_WEBHOOK_SECRET  — from `stripe listen --forward-to ...` or dashboard
#   STRIPE_PRICE_PRO       — recurring Price ID for the Pro plan (e.g. price_xxx)
#   STRIPE_PRICE_ENTERPRISE— recurring Price ID for the Enterprise plan
# Optional:
#   APP_BASE_URL           — public base URL, e.g. https://app.aidhelpdesk.com
#                            used to build success/cancel redirect URLs

def _stripe_client():
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        raise RuntimeError("STRIPE_SECRET_KEY is not set")
    try:
        import stripe as _stripe
    except ImportError as exc:
        raise RuntimeError("Stripe Python SDK is not installed") from exc
    _stripe.api_key = key
    return _stripe


def _app_base_url() -> str:
    base = os.getenv("APP_BASE_URL", "").rstrip("/")
    if not base:
        # Fall back to the request's own origin when APP_BASE_URL is unset.
        from flask import request as _req
        base = _req.host_url.rstrip("/")
    return base


# plan slug → Stripe Price ID (set these in Railway env vars)
_CHECKOUT_PRICE_ENV = {
    "pro": "STRIPE_PRICE_PRO",
    "enterprise": "STRIPE_PRICE_ENTERPRISE",
}


def _stripe_get(obj, key: str, default=None):
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, default)
    try:
        return obj.get(key, default)
    except Exception:
        return getattr(obj, key, default)


def _stripe_metadata(obj) -> dict:
    meta = _stripe_get(obj, "metadata", {}) or {}
    try:
        return dict(meta)
    except Exception:
        return {}


def _stripe_timestamp_to_iso(value) -> str | None:
    if not value:
        return None
    try:
        from datetime import datetime
        return datetime.utcfromtimestamp(int(value)).isoformat()
    except Exception:
        return None


def _configured_price_to_plan() -> dict:
    return {
        os.getenv(env): plan
        for plan, env in _CHECKOUT_PRICE_ENV.items()
        if os.getenv(env)
    }


def _plan_from_price_id(price_id: str | None) -> str | None:
    if not price_id:
        return None
    return _configured_price_to_plan().get(price_id)


def _subscription_items(subscription) -> list:
    items = _stripe_get(subscription, "items", {}) or {}
    data = _stripe_get(items, "data", []) or []
    return list(data)


def _subscription_price_id(subscription) -> str | None:
    for item in _subscription_items(subscription):
        price = _stripe_get(item, "price", {}) or {}
        price_id = _stripe_get(price, "id")
        if price_id:
            return price_id
    return None


def _subscription_period_end(subscription) -> str | None:
    period_end = _stripe_get(subscription, "current_period_end")
    if not period_end:
        for item in _subscription_items(subscription):
            period_end = _stripe_get(item, "current_period_end")
            if period_end:
                break
    return _stripe_timestamp_to_iso(period_end)


def _resolve_subscription_tenant(subscription, tenant_hint: str | None = None) -> str | None:
    if tenant_hint:
        return tenant_hint
    metadata = _stripe_metadata(subscription)
    tenant_id = metadata.get("tenant_id")
    if tenant_id:
        return tenant_id
    sub_id = _stripe_get(subscription, "id")
    tenant_id = db.get_tenant_id_by_stripe_subscription(sub_id)
    if tenant_id:
        return tenant_id
    customer_id = _stripe_get(subscription, "customer")
    return db.get_tenant_id_by_stripe_customer(customer_id)


def _sync_subscription_from_stripe(subscription, tenant_hint: str | None = None) -> dict | None:
    """Persist a Stripe subscription and update the tenant plan from active status."""
    sub_id = _stripe_get(subscription, "id")
    if not sub_id:
        return None

    price_id = _subscription_price_id(subscription)
    if not price_id:
        try:
            stripe = _stripe_client()
            subscription = stripe.Subscription.retrieve(sub_id, expand=["items.data.price"])
            price_id = _subscription_price_id(subscription)
        except Exception as e:
            print(f"[stripe] Could not retrieve subscription {sub_id}: {e}")

    tenant_id = _resolve_subscription_tenant(subscription, tenant_hint)
    if not tenant_id:
        print(f"[stripe] Could not map subscription {sub_id} to a tenant")
        return None

    customer_id = _stripe_get(subscription, "customer")
    if customer_id:
        db.upsert_billing_customer(tenant_id, customer_id)

    metadata = _stripe_metadata(subscription)
    plan = _plan_from_price_id(price_id)
    if plan is None:
        metadata_plan = metadata.get("plan")
        if metadata_plan in ("pro", "enterprise"):
            plan = metadata_plan
        else:
            existing = db.get_billing_subscription(tenant_id) or {}
            plan = existing.get("plan") or "free"

    status = str(_stripe_get(subscription, "status", "none") or "none")
    current_period_end = _subscription_period_end(subscription)
    row = db.upsert_billing_subscription(
        tenant_id=tenant_id,
        stripe_subscription_id=sub_id,
        plan=plan,
        status=status,
        current_period_end=current_period_end,
    )

    if status == "active" and plan in ("pro", "enterprise"):
        db.set_tenant_plan(tenant_id, plan)
    else:
        db.set_tenant_plan(tenant_id, "free")
    return row


def _create_portal_url(tenant_id: str) -> str | None:
    customer = db.get_billing_customer(tenant_id)
    if not customer:
        return None
    stripe = _stripe_client()
    portal = stripe.billing_portal.Session.create(
        customer=customer["stripe_customer_id"],
        return_url=f"{_app_base_url()}/billing",
    )
    return portal.url


@app.route("/create-checkout-session", methods=["POST"])
@app.route("/billing/create-checkout-session", methods=["POST"])
@require_dashboard_user
def billing_create_checkout_session():
    """Create a Stripe Checkout session and return its URL."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Only tenant admins can manage billing."}), 403

    data = request.get_json(silent=True) or {}
    plan = str(data.get("plan", "pro")).strip().lower()
    if plan not in ("free", "pro", "enterprise"):
        return jsonify({"success": False, "message": "plan must be free, pro, or enterprise"}), 400

    if plan == "free":
        return jsonify({
            "success": False,
            "message": "The Free plan does not require Stripe Checkout.",
        }), 400

    try:
        stripe = _stripe_client()
    except RuntimeError as e:
        return jsonify({"success": False, "message": str(e)}), 503

    active_sub = db.get_billing_subscription(g.tenant_id)
    if active_sub and active_sub.get("status") == "active":
        portal_url = _create_portal_url(g.tenant_id)
        if portal_url:
            return jsonify({"success": True, "url": portal_url, "portal": True})
        return jsonify({"success": False, "message": "This tenant already has an active subscription."}), 409

    base = _app_base_url()
    price_env = _CHECKOUT_PRICE_ENV[plan]
    price_id = os.getenv(price_env, "")
    if not price_id:
        return jsonify({
            "success": False,
            "message": f"{price_env} is not configured.",
        }), 503

    metadata = {"tenant_id": g.tenant_id, "plan": plan}
    billing_customer = db.get_billing_customer(g.tenant_id)
    customer_id = billing_customer["stripe_customer_id"] if billing_customer else ""
    if not customer_id:
        customer = stripe.Customer.create(
            email=g.user_email,
            name=g.tenant_name or None,
            metadata={"tenant_id": g.tenant_id},
        )
        customer_id = customer.id
        db.upsert_billing_customer(g.tenant_id, customer_id)

    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price_id, "quantity": 1}],
        customer=customer_id,
        client_reference_id=g.tenant_id,
        metadata=metadata,
        subscription_data={"metadata": metadata},
        allow_promotion_codes=True,
        success_url=f"{base}/billing/success?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base}/billing?cancelled=1",
    )
    return jsonify({"success": True, "url": session.url, "session_id": session.id})


@app.route("/billing/portal", methods=["GET"])
@require_dashboard_user
def billing_portal():
    """Open Stripe Customer Portal for subscription management."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Only tenant admins can manage billing."}), 403
    try:
        portal_url = _create_portal_url(g.tenant_id)
    except RuntimeError as e:
        return jsonify({"success": False, "message": str(e)}), 503
    if not portal_url:
        return jsonify({"success": False, "message": "No Stripe customer exists for this tenant yet."}), 404
    return redirect(portal_url)


@app.route("/billing/success")
@require_dashboard_user
def billing_success():
    """Redirect target after a completed Stripe checkout."""
    from flask import redirect
    return redirect("/billing?success=1")


@app.route("/webhook/stripe", methods=["POST"])
@app.route("/billing/webhook", methods=["POST"])
def billing_webhook():
    """
    Stripe webhook receiver.  Automatically upgrades (or downgrades) the
    tenant's plan when Stripe confirms payment or cancellation.

    Configure in Stripe dashboard → Developers → Webhooks:
      endpoint: https://<your-domain>/billing/webhook
      events:   checkout.session.completed
                customer.subscription.deleted
    """
    import stripe as _stripe
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    if not secret:
        return jsonify({"error": "STRIPE_WEBHOOK_SECRET is not configured"}), 503

    payload = request.get_data()
    sig     = request.headers.get("Stripe-Signature", "")

    try:
        event = _stripe.Webhook.construct_event(payload, sig, secret)
    except ValueError:
        return jsonify({"error": "bad payload"}), 400
    except _stripe.error.SignatureVerificationError:
        return jsonify({"error": "invalid signature"}), 400

    event_id = _stripe_get(event, "id")
    etype = _stripe_get(event, "type")
    if not event_id or not etype:
        return jsonify({"error": "invalid event"}), 400

    if not db.record_stripe_event(event_id, etype):
        return jsonify({"received": True, "duplicate": True})

    try:
        obj = event["data"]["object"]

        if etype == "checkout.session.completed":
            metadata = _stripe_metadata(obj)
            tenant_id = metadata.get("tenant_id") or _stripe_get(obj, "client_reference_id")
            customer_id = _stripe_get(obj, "customer")
            subscription_id = _stripe_get(obj, "subscription")
            if tenant_id and customer_id:
                db.upsert_billing_customer(tenant_id, customer_id)
            if subscription_id:
                stripe = _stripe_client()
                subscription = stripe.Subscription.retrieve(
                    subscription_id,
                    expand=["items.data.price"],
                )
                _sync_subscription_from_stripe(subscription, tenant_hint=tenant_id)

        elif etype in ("customer.subscription.created",
                       "customer.subscription.updated",
                       "customer.subscription.deleted"):
            _sync_subscription_from_stripe(obj)

        db.mark_stripe_event_processed(event_id)
        return jsonify({"received": True})
    except Exception as e:
        db.release_unprocessed_stripe_event(event_id)
        print(f"[stripe] Webhook processing failed for {event_id} ({etype}): {e}")
        return jsonify({"error": "webhook processing failed"}), 500


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
    data          = request.get_json() or {}
    action        = data.get("action", "").strip()
    args          = data.get("args", [])
    target        = data.get("target", "")      # optional, for audit log
    confirm_token = data.get("confirm_token", "").strip()

    if not action:
        return jsonify({"success": False, "message": "action is required."}), 400

    # ── Hard action whitelist (policy layer) ─────────────────────────────────
    if action not in action_policy.ALL_ACTIONS:
        return jsonify({
            "success": False,
            "message": f"'{action}' is not a recognised action.",
        }), 400

    # ── Destructive action confirmation ──────────────────────────────────────
    # High-risk actions require a short-lived 6-digit challenge code to be
    # confirmed by the admin before the command is queued.
    ok, reason = action_policy.validate(action, source="human")
    if not ok:
        return jsonify({"success": False, "message": reason}), 403

    if action == "run_custom_script":
        block = _paid_feature_block(g.tenant_id, "custom_scripts_limit", "Custom scripts")
        if block:
            return jsonify({"success": False, "message": block}), 403

    if action in DESTRUCTIVE_ACTIONS:
        if not confirm_token:
            # Issue a challenge — return the code for the admin to confirm
            code = _issue_confirm_token(g.tenant_id, action, args)
            subject = args[0] if args else "?"
            action_labels = {
                "disable_account":          f"Disable account for {subject}",
                "remove_from_group":        f"Remove {subject} from group {args[1] if len(args)>1 else '?'}",
                "move_user":                f"Move {subject} to OU: {args[1] if len(args)>1 else '?'}",
                "create_user":              f"Create new user: {subject} {args[1] if len(args)>1 else ''}",
                "reset_password":           f"Reset password for {subject}",
                "set_password_never_expires": f"Set password-never-expires for {subject}",
                "create_ou":                f"Create OU: {subject}",
                "bulk_move_users":          "Bulk move users — review the rules carefully before confirming",
                "remove_dns_record":        f"Remove DNS record '{args[1] if len(args)>1 else '?'}' ({args[2] if len(args)>2 else '?'}) from zone {subject}",
                "set_dns_scavenging":       "Change DNS scavenging state",
                "remove_dhcp_reservation":  f"Remove DHCP reservation for {args[1] if len(args)>1 else '?'} from scope {subject}",
                "add_dhcp_exclusion":       f"Add DHCP exclusion range {args[1] if len(args)>1 else '?'}-{args[2] if len(args)>2 else '?'} to scope {subject}",
                "remove_dhcp_exclusion":    f"Remove DHCP exclusion range {args[1] if len(args)>1 else '?'}-{args[2] if len(args)>2 else '?'} from scope {subject}",
                "link_gpo":                 f"Link GPO '{subject}' to OU {args[1] if len(args)>1 else '?'}",
                "unlink_gpo":               f"Unlink GPO '{subject}' from OU {args[1] if len(args)>1 else '?'}",
                "set_gpo_status":           f"Set GPO '{subject}' status to {args[1] if len(args)>1 else '?'}",
                "set_gpo_link_enforced":    f"Set enforcement of GPO '{subject}' link on OU {args[1] if len(args)>1 else '?'} to {args[2] if len(args)>2 else '?'}",
            }
            return jsonify({
                "success":              False,
                "requires_confirmation": True,
                "confirm_token":        code,
                "action_label":         action_labels.get(action, action),
                "message":              "This action requires confirmation.",
            }), 202

        # Token supplied — validate it
        pending = _consume_confirm_token(g.tenant_id, confirm_token)
        if not pending:
            return jsonify({
                "success": False,
                "message": "Invalid or expired confirmation code. Please try again.",
            }), 403
        # Use the action/args from the stored token (prevents tampering)
        action = pending["action"]
        args   = pending["args"]

    # ── Plan limits for write actions ────────────────────────────────────────
    is_mutation = _is_ad_mutation(action)
    queue_args = _queue_args_with_ad_budget(g.tenant_id, action, args)
    if is_mutation:
        limit_block = _ad_usage_limit_block(g.tenant_id, action, args)
        if limit_block:
            return jsonify({
                "success": False,
                "limit_reached": True,
                "message": limit_block["message"],
            }), 429

    command = db.queue_command(g.tenant_id, action, queue_args)

    if is_mutation:
        tgt = target or (args[0] if args else "")
        db.log_audit(g.tenant_id, g.user_email, action, tgt, "queued")
        db.log_activity(g.tenant_id, "ad_action", g.user_email, target=tgt,
                        detail=f"{action} queued via dashboard")
        _charge_ad_usage(g.tenant_id, action, args)

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


# ---------------------------------------------------------------------------
# Dashboard API -- DNS
# Thin, capability-gated wrappers around the same queue_command / confirm-token
# machinery dashboard_exec uses for AD actions. Reads and the routine record
# add queue immediately; remove_dns_record and set_dns_scavenging (both in
# DESTRUCTIVE_ACTIONS) go through the same 6-digit challenge as disable_account.
# ---------------------------------------------------------------------------

def _require_dns_capability() -> str | None:
    """Return a friendly error message if this tenant's agent hasn't reported
    the 'dns' capability, else None."""
    caps = db.get_tenant_capabilities(g.tenant_id)
    if "dns" not in caps:
        return (
            "DNS management isn't available yet. Your Windows agent hasn't reported "
            "the DNS Server role as installed, or hasn't connected since it was added."
        )
    return None


@app.route("/api/dns/zones", methods=["GET"])
@require_dashboard_user
def api_dns_zones():
    """Queue a DNS zone list. Body: none. Returns { command_id }."""
    block = _require_dns_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_dns_zones", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dns/records", methods=["GET"])
@require_dashboard_user
def api_dns_records_list():
    """Queue a DNS record list for a zone. Query: ?zone=...&type=... (type optional)."""
    block = _require_dns_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    zone = request.args.get("zone", "").strip()
    record_type = request.args.get("type", "").strip()
    if not zone:
        return jsonify({"success": False, "message": "A zone name is required."}), 400
    command = db.queue_command(g.tenant_id, "list_dns_records", [zone, record_type])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dns/records", methods=["POST"])
@require_dashboard_user
def api_dns_records_add():
    """
    Queue a DNS record add (routine write, no confirmation token).
    Body: { "zone": "...", "name": "...", "type": "A", "value": "...", "ttl": 3600 }
    """
    block = _require_dns_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data   = request.get_json() or {}
    zone   = str(data.get("zone", "")).strip()
    name   = str(data.get("name", "")).strip()
    rtype  = str(data.get("type", "")).strip()
    value  = str(data.get("value", "")).strip()
    ttl    = data.get("ttl", 3600)

    if not (zone and name and rtype and value):
        return jsonify({"success": False, "message": "Zone, name, type, and value are all required."}), 400

    action = "add_dns_record"
    args   = [zone, name, rtype, value, ttl]

    ok, reason = action_policy.validate(action, source="human")
    if not ok:
        return jsonify({"success": False, "message": reason}), 403

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, f"{name}.{zone}", "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=f"{name}.{zone}",
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dns/records/delete", methods=["POST"])
@require_dashboard_user
def api_dns_records_delete():
    """
    Queue a DNS record removal. High-caution -- gated behind the same 6-digit
    confirmation challenge as disable_account (remove_dns_record is in
    DESTRUCTIVE_ACTIONS).
    Body: { "zone": "...", "name": "...", "type": "A", "value": "...", "confirm_token": "" }
    """
    block = _require_dns_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data          = request.get_json() or {}
    zone          = str(data.get("zone", "")).strip()
    name          = str(data.get("name", "")).strip()
    rtype         = str(data.get("type", "")).strip()
    value         = str(data.get("value", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    action = "remove_dns_record"
    args   = [zone, name, rtype, value]

    if not confirm_token:
        if not (zone and name and rtype and value):
            return jsonify({"success": False, "message": "Zone, name, type, and value are all required."}), 400
        ok, reason = action_policy.validate(action, source="human")
        if not ok:
            return jsonify({"success": False, "message": reason}), 403
        code = _issue_confirm_token(g.tenant_id, action, args)
        apex_note = " (this is the zone apex record -- double-check before confirming)" if name == "@" else ""
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Remove {rtype} record '{name}' from zone {zone}{apex_note}",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending:
        return jsonify({
            "success": False,
            "message": "Invalid or expired confirmation code. Please try again.",
        }), 403

    action = pending["action"]
    args   = pending["args"]
    zone, name = (args[0], args[1]) if len(args) >= 2 else (zone, name)

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, f"{name}.{zone}", "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=f"{name}.{zone}",
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


# ---------------------------------------------------------------------------
# Dashboard API -- DHCP
# Same shape as the DNS routes above: thin, capability-gated wrappers around
# queue_command / the confirm-token flow. Reads and add_dhcp_reservation
# queue immediately; remove_dhcp_reservation, add_dhcp_exclusion, and
# remove_dhcp_exclusion (all in DESTRUCTIVE_ACTIONS) go through the same
# 6-digit challenge as disable_account.
# ---------------------------------------------------------------------------

def _require_dhcp_capability() -> str | None:
    """Return a friendly error message if this tenant's agent hasn't reported
    the 'dhcp' capability, else None."""
    caps = db.get_tenant_capabilities(g.tenant_id)
    if "dhcp" not in caps:
        return (
            "DHCP management isn't available yet. Your Windows agent hasn't reported "
            "the DHCP Server role as installed, or hasn't connected since it was added."
        )
    return None


@app.route("/api/dhcp/scopes", methods=["GET"])
@require_dashboard_user
def api_dhcp_scopes():
    """Queue a DHCP scope list. Body: none. Returns { command_id }."""
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_dhcp_scopes", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/scopes/stats", methods=["GET"])
@require_dashboard_user
def api_dhcp_scopes_stats():
    """Queue utilisation stats for every scope (Free/InUse/PercentageInUse).
    The dashboard merges this with /api/dhcp/scopes by scope_id."""
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "get_dhcp_scope_stats", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/leases", methods=["GET"])
@require_dashboard_user
def api_dhcp_leases():
    """Queue a lease list for a scope. Query: ?scope=<scope_id>."""
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    scope = request.args.get("scope", "").strip()
    if not scope:
        return jsonify({"success": False, "message": "A scope ID is required."}), 400
    command = db.queue_command(g.tenant_id, "list_dhcp_leases", [scope])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/reservations", methods=["GET"])
@require_dashboard_user
def api_dhcp_reservations_list():
    """Queue a reservation list for a scope. Query: ?scope=<scope_id>."""
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    scope = request.args.get("scope", "").strip()
    if not scope:
        return jsonify({"success": False, "message": "A scope ID is required."}), 400
    command = db.queue_command(g.tenant_id, "list_dhcp_reservations", [scope])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/exclusions", methods=["GET"])
@require_dashboard_user
def api_dhcp_exclusions_list():
    """Queue an exclusion-range list for a scope. Query: ?scope=<scope_id>."""
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    scope = request.args.get("scope", "").strip()
    if not scope:
        return jsonify({"success": False, "message": "A scope ID is required."}), 400
    command = db.queue_command(g.tenant_id, "list_dhcp_exclusions", [scope])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/reservations", methods=["POST"])
@require_dashboard_user
def api_dhcp_reservations_add():
    """
    Queue a DHCP reservation add (routine write, no confirmation token).
    Also used to convert a lease to a reservation -- the dashboard pre-fills
    ip/mac/hostname from the lease row before calling this.
    Body: { "scope": "...", "ip": "...", "mac": "...", "name": "...", "description": "" }
    """
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data        = request.get_json() or {}
    scope       = str(data.get("scope", "")).strip()
    ip          = str(data.get("ip", "")).strip()
    mac         = str(data.get("mac", "")).strip()
    name        = str(data.get("name", "")).strip()
    description = str(data.get("description", "")).strip()

    if not (scope and ip and mac and name):
        return jsonify({"success": False, "message": "Scope, IP, MAC, and name are all required."}), 400

    action = "add_dhcp_reservation"
    args   = [scope, ip, mac, name, description]

    ok, reason = action_policy.validate(action, source="human")
    if not ok:
        return jsonify({"success": False, "message": reason}), 403

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, ip, "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=ip,
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/reservations/delete", methods=["POST"])
@require_dashboard_user
def api_dhcp_reservations_delete():
    """
    Queue a DHCP reservation removal. High-caution -- gated behind the same
    6-digit confirmation challenge as disable_account (remove_dhcp_reservation
    is in DESTRUCTIVE_ACTIONS).
    Body: { "scope": "...", "ip": "...", "confirm_token": "" }
    """
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data          = request.get_json() or {}
    scope         = str(data.get("scope", "")).strip()
    ip            = str(data.get("ip", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    action = "remove_dhcp_reservation"
    args   = [scope, ip]

    if not confirm_token:
        if not (scope and ip):
            return jsonify({"success": False, "message": "Scope and IP are required."}), 400
        ok, reason = action_policy.validate(action, source="human")
        if not ok:
            return jsonify({"success": False, "message": reason}), 403
        code = _issue_confirm_token(g.tenant_id, action, args)
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Remove DHCP reservation for {ip} from scope {scope}",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending:
        return jsonify({
            "success": False,
            "message": "Invalid or expired confirmation code. Please try again.",
        }), 403

    action = pending["action"]
    args   = pending["args"]
    scope, ip = (args[0], args[1]) if len(args) >= 2 else (scope, ip)

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, ip, "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=ip,
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/exclusions", methods=["POST"])
@require_dashboard_user
def api_dhcp_exclusions_add():
    """
    Queue a DHCP exclusion range add. High-caution -- can knock devices off
    the network if it overlaps an active lease, so it is gated behind the
    same 6-digit confirmation challenge as disable_account.
    Body: { "scope": "...", "start": "...", "end": "...", "confirm_token": "" }
    """
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data          = request.get_json() or {}
    scope         = str(data.get("scope", "")).strip()
    start         = str(data.get("start", "")).strip()
    end           = str(data.get("end", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    action = "add_dhcp_exclusion"
    args   = [scope, start, end]

    if not confirm_token:
        if not (scope and start and end):
            return jsonify({"success": False, "message": "Scope, start, and end are all required."}), 400
        ok, reason = action_policy.validate(action, source="human")
        if not ok:
            return jsonify({"success": False, "message": reason}), 403
        code = _issue_confirm_token(g.tenant_id, action, args)
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Add DHCP exclusion range {start}-{end} to scope {scope}",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending:
        return jsonify({
            "success": False,
            "message": "Invalid or expired confirmation code. Please try again.",
        }), 403

    action = pending["action"]
    args   = pending["args"]
    scope, start, end = (args[0], args[1], args[2]) if len(args) >= 3 else (scope, start, end)

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, f"{start}-{end}", "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=f"{start}-{end}",
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/dhcp/exclusions/delete", methods=["POST"])
@require_dashboard_user
def api_dhcp_exclusions_delete():
    """
    Queue a DHCP exclusion range removal. High-caution -- gated behind the
    same 6-digit confirmation challenge as disable_account.
    Body: { "scope": "...", "start": "...", "end": "...", "confirm_token": "" }
    """
    block = _require_dhcp_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data          = request.get_json() or {}
    scope         = str(data.get("scope", "")).strip()
    start         = str(data.get("start", "")).strip()
    end           = str(data.get("end", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    action = "remove_dhcp_exclusion"
    args   = [scope, start, end]

    if not confirm_token:
        if not (scope and start and end):
            return jsonify({"success": False, "message": "Scope, start, and end are all required."}), 400
        ok, reason = action_policy.validate(action, source="human")
        if not ok:
            return jsonify({"success": False, "message": reason}), 403
        code = _issue_confirm_token(g.tenant_id, action, args)
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Remove DHCP exclusion range {start}-{end} from scope {scope}",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending:
        return jsonify({
            "success": False,
            "message": "Invalid or expired confirmation code. Please try again.",
        }), 403

    action = pending["action"]
    args   = pending["args"]
    scope, start, end = (args[0], args[1], args[2]) if len(args) >= 3 else (scope, start, end)

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, f"{start}-{end}", "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=f"{start}-{end}",
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


# ---------------------------------------------------------------------------
# Dashboard API -- Group Policy (GPO)
# Same shape as the DNS/DHCP routes above: thin, capability-gated wrappers
# around queue_command / the confirm-token machinery. All reads (list, report,
# links, inheritance) queue immediately. All four writes (link, unlink,
# status, enforce) are in DESTRUCTIVE_ACTIONS -- none of them are in
# action_policy.WRITE, per OVERNIGHT_PLAN.md 3.3's explicit safety stance --
# so they always go through the same 6-digit challenge as disable_account.
# ---------------------------------------------------------------------------

def _require_gpo_capability() -> str | None:
    """Return a friendly error message if this tenant's agent hasn't reported
    the 'gpo' capability, else None."""
    caps = db.get_tenant_capabilities(g.tenant_id)
    if "gpo" not in caps:
        return (
            "Group Policy management isn't available yet. Your Windows agent hasn't "
            "reported the GroupPolicy module as installed, or hasn't connected since it was added."
        )
    return None


@app.route("/api/gpo/list", methods=["GET"])
@require_dashboard_user
def api_gpo_list():
    """Queue a GPO list. Body: none. Returns { command_id }."""
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_gpos", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/gpo/report", methods=["GET"])
@require_dashboard_user
def api_gpo_report():
    """Queue a GPO report fetch (parsed summary). Query: ?gpo=<name or GUID>."""
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    gpo = request.args.get("gpo", "").strip()
    if not gpo:
        return jsonify({"success": False, "message": "A GPO name or GUID is required."}), 400
    command = db.queue_command(g.tenant_id, "get_gpo_report", [gpo])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/gpo/links", methods=["GET"])
@require_dashboard_user
def api_gpo_links():
    """Queue a GPO link list for an OU. Query: ?ou=<name or DN>."""
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    ou = request.args.get("ou", "").strip()
    if not ou:
        return jsonify({"success": False, "message": "An OU name or distinguished name is required."}), 400
    command = db.queue_command(g.tenant_id, "list_gpo_links", [ou])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/gpo/inheritance", methods=["GET"])
@require_dashboard_user
def api_gpo_inheritance():
    """Queue a GPO inheritance lookup for an OU. Query: ?ou=<name or DN>."""
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    ou = request.args.get("ou", "").strip()
    if not ou:
        return jsonify({"success": False, "message": "An OU name or distinguished name is required."}), 400
    command = db.queue_command(g.tenant_id, "get_gpo_inheritance", [ou])
    return jsonify({"success": True, "command_id": command["id"]}), 202


def _gpo_destructive_write(action: str, args: list, target: str, action_label: str, data: dict):
    """Shared confirm-token flow for the four GPO write routes below. Returns
    a Flask response tuple. `data` is the already-parsed request JSON body
    (used only to read confirm_token, since args are otherwise fixed by the caller)."""
    confirm_token = str(data.get("confirm_token", "")).strip()

    if not confirm_token:
        ok, reason = action_policy.validate(action, source="human")
        if not ok:
            return jsonify({"success": False, "message": reason}), 403
        code = _issue_confirm_token(g.tenant_id, action, args)
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          action_label,
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending:
        return jsonify({
            "success": False,
            "message": "Invalid or expired confirmation code. Please try again.",
        }), 403

    action = pending["action"]
    args   = pending["args"]

    command = db.queue_command(g.tenant_id, action, args)
    db.log_audit(g.tenant_id, g.user_email, action, target, "queued")
    db.log_activity(g.tenant_id, "ad_action", g.user_email, target=target,
                     detail=f"{action} queued via dashboard")
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/gpo/link", methods=["POST"])
@require_dashboard_user
def api_gpo_link():
    """
    Queue a GPO link to an OU. DESTRUCTIVE -- gated behind the same 6-digit
    confirmation challenge as disable_account.
    Body: { "gpo": "...", "ou": "...", "confirm_token": "" }
    """
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data = request.get_json() or {}
    gpo  = str(data.get("gpo", "")).strip()
    ou   = str(data.get("ou", "")).strip()

    if not str(data.get("confirm_token", "")).strip() and not (gpo and ou):
        return jsonify({"success": False, "message": "GPO and OU are both required."}), 400

    return _gpo_destructive_write(
        "link_gpo", [gpo, ou], ou,
        f"Link GPO '{gpo}' to OU {ou}", data,
    )


@app.route("/api/gpo/unlink", methods=["POST"])
@require_dashboard_user
def api_gpo_unlink():
    """
    Queue a GPO unlink from an OU. DESTRUCTIVE -- gated behind the same
    6-digit confirmation challenge as disable_account.
    Body: { "gpo": "...", "ou": "...", "confirm_token": "" }
    """
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data = request.get_json() or {}
    gpo  = str(data.get("gpo", "")).strip()
    ou   = str(data.get("ou", "")).strip()

    if not str(data.get("confirm_token", "")).strip() and not (gpo and ou):
        return jsonify({"success": False, "message": "GPO and OU are both required."}), 400

    return _gpo_destructive_write(
        "unlink_gpo", [gpo, ou], ou,
        f"Unlink GPO '{gpo}' from OU {ou}", data,
    )


@app.route("/api/gpo/status", methods=["POST"])
@require_dashboard_user
def api_gpo_status():
    """
    Queue a GPO status change (enable/disable computer/user settings).
    DESTRUCTIVE -- gated behind the same 6-digit confirmation challenge as
    disable_account.
    Body: { "gpo": "...", "status": "AllSettingsEnabled", "confirm_token": "" }
    """
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data   = request.get_json() or {}
    gpo    = str(data.get("gpo", "")).strip()
    status = str(data.get("status", "")).strip()

    if not str(data.get("confirm_token", "")).strip() and not (gpo and status):
        return jsonify({"success": False, "message": "GPO and status are both required."}), 400

    return _gpo_destructive_write(
        "set_gpo_status", [gpo, status], gpo,
        f"Set GPO '{gpo}' status to {status}", data,
    )


@app.route("/api/gpo/enforce", methods=["POST"])
@require_dashboard_user
def api_gpo_enforce():
    """
    Queue a GPO link enforcement toggle for an OU. DESTRUCTIVE -- gated
    behind the same 6-digit confirmation challenge as disable_account.
    Body: { "gpo": "...", "ou": "...", "enforced": true, "confirm_token": "" }
    """
    block = _require_gpo_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403

    data     = request.get_json() or {}
    gpo      = str(data.get("gpo", "")).strip()
    ou       = str(data.get("ou", "")).strip()
    enforced = bool(data.get("enforced", False))

    if not str(data.get("confirm_token", "")).strip() and not (gpo and ou):
        return jsonify({"success": False, "message": "GPO and OU are both required."}), 400

    return _gpo_destructive_write(
        "set_gpo_link_enforced", [gpo, ou, enforced], ou,
        f"Set enforcement of GPO '{gpo}' link on OU {ou} to {enforced}", data,
    )


# ---------------------------------------------------------------------------
# Dashboard API -- NPS (Network Policy Server / RADIUS)
#
# Read-only for now, per OVERNIGHT_PLAN.md 5.1 -- no write routes exist here.
# Follows the exact same queue-and-poll shape as DNS/DHCP/GPO above.
# ---------------------------------------------------------------------------

def _require_nps_capability() -> str | None:
    """Return a friendly error message if this tenant's agent hasn't reported
    the 'nps' capability, else None."""
    caps = db.get_tenant_capabilities(g.tenant_id)
    if "nps" not in caps:
        return (
            "NPS (RADIUS) management isn't available yet. Your Windows agent hasn't "
            "reported the Network Policy Server role as installed, or hasn't connected since it was added."
        )
    return None


@app.route("/api/nps/summary", methods=["GET"])
@require_dashboard_user
def api_nps_summary():
    """Queue an NPS configuration summary fetch. Body: none. Returns { command_id }."""
    block = _require_nps_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "get_nps_summary", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/nps/clients", methods=["GET"])
@require_dashboard_user
def api_nps_clients():
    """Queue an NPS RADIUS client list. Body: none. Returns { command_id }."""
    block = _require_nps_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_nps_radius_clients", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/nps/policies", methods=["GET"])
@require_dashboard_user
def api_nps_policies():
    """Queue an NPS network policy list. Body: none. Returns { command_id }."""
    block = _require_nps_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_nps_network_policies", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


@app.route("/api/nps/connection-policies", methods=["GET"])
@require_dashboard_user
def api_nps_connection_policies():
    """Queue an NPS connection request policy list. Body: none. Returns { command_id }."""
    block = _require_nps_capability()
    if block:
        return jsonify({"success": False, "message": block}), 403
    command = db.queue_command(g.tenant_id, "list_nps_connection_policies", [])
    return jsonify({"success": True, "command_id": command["id"]}), 202


# ---------------------------------------------------------------------------
# Dashboard API -- Entra ID (Microsoft Graph)
#
# Unlike DNS/DHCP/GPO, these routes never go through db.queue_command -- there
# is no agent involved. Entra ID is a cloud service, so the cloud backend talks
# to Microsoft Graph directly via GraphClient using this tenant's stored app
# registration credentials. That also means these actions never pass through
# action_policy.validate() (that gate only applies to the agent action-name
# queue); the equivalent safety tiering is enforced right here instead:
#   - reads (list/get users, list/get groups, list group members) are free
#   - add_group_member is routine (no confirm token)
#   - remove_group_member, revoke_sessions, reset_password require the same
#     6-digit human-confirm token flow as disable_account, validated the same
#     way (db.issue_confirm_token / db.consume_confirm_token), just executed
#     against GraphClient instead of queued for the Windows agent.
# ---------------------------------------------------------------------------

def _entra_settings(tenant_id: str) -> dict:
    settings = db.get_settings(tenant_id)
    return {
        "tenant_id":    str(settings.get("graph_tenant_id") or "").strip(),
        "client_id":    str(settings.get("graph_client_id") or "").strip(),
        "client_secret": str(settings.get("graph_client_secret") or "").strip(),
    }


def _entra_configured(tenant_id: str) -> bool:
    creds = _entra_settings(tenant_id)
    return bool(creds["tenant_id"] and creds["client_id"] and creds["client_secret"])


def _get_graph_client():
    """Build a GraphClient for the current dashboard tenant, or return an
    error message if credentials aren't configured. Every /api/entra/* route
    should call this first so an unconfigured tenant gets a friendly 400
    instead of an exception."""
    creds = _entra_settings(g.tenant_id)
    if not (creds["tenant_id"] and creds["client_id"] and creds["client_secret"]):
        return None, "Entra ID is not configured. Add your Entra credentials in Settings."
    try:
        return GraphClient(creds["tenant_id"], creds["client_id"], creds["client_secret"]), None
    except Exception as e:
        return None, str(e)


def _generate_temp_password() -> str:
    """Generate a secure temporary password: uppercase + lowercase + numbers +
    symbol, 12+ chars. Mirrors how the AD side generates reset passwords."""
    alphabet = string.ascii_uppercase + string.ascii_lowercase + string.digits
    symbols  = "!@#$%^&*"
    chars = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(symbols),
    ]
    chars += [secrets.choice(alphabet + symbols) for _ in range(8)]
    secrets.SystemRandom().shuffle(chars)
    return "".join(chars)


@app.route("/api/entra/users", methods=["GET"])
@require_dashboard_user
def api_entra_users():
    """List Entra users, optionally filtered by ?q= search term."""
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400
    search = request.args.get("q", "").strip()
    result = client.list_users(search=search, top=100)
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/users/<user_id>", methods=["GET"])
@require_dashboard_user
def api_entra_user_detail(user_id):
    """Get a single Entra user by id or userPrincipalName."""
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400
    result = client.get_user(user_id)
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/groups", methods=["GET"])
@require_dashboard_user
def api_entra_groups():
    """List Entra groups, optionally filtered by ?q= search term."""
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400
    search = request.args.get("q", "").strip()
    result = client.list_groups(search=search, top=100)
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/groups/<group_id>/members", methods=["GET"])
@require_dashboard_user
def api_entra_group_members(group_id):
    """List the direct members of an Entra group."""
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400
    result = client.get_group_members(group_id)
    return jsonify(result), (200 if result["success"] else 502)


def _entra_sync_guard(client, user_id):
    """Return a friendly error message if the target user is synced from
    on-prem AD, else None. Entra mutations (group changes, password reset,
    session revoke) must not run against synced identities -- those attributes
    are owned by AD and Graph writes to them are rejected or silently
    overwritten on the next sync. Routes must use the AD tab instead."""
    lookup = client.get_user(user_id)
    if not lookup["success"]:
        return None  # let the real operation surface the Graph error
    data = lookup.get("data") or {}
    if data.get("onPremisesSyncEnabled"):
        return (
            "This user syncs from on-prem AD. Use the Active Directory tab so the change persists."
        )
    return None


@app.route("/api/entra/groups/member", methods=["POST"])
@require_dashboard_user
def api_entra_group_member_add():
    """Add a user to an Entra group. ROUTINE write -- no confirm token needed.
    Body: { "group_id": "...", "user_id": "..." }"""
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400

    data     = request.get_json() or {}
    group_id = str(data.get("group_id", "")).strip()
    user_id  = str(data.get("user_id", "")).strip()
    if not group_id or not user_id:
        return jsonify({"success": False, "message": "group_id and user_id are both required."}), 400

    sync_block = _entra_sync_guard(client, user_id)
    if sync_block:
        return jsonify({"success": False, "message": sync_block}), 403

    result = client.add_group_member(group_id, user_id)
    if result["success"]:
        db.log_audit(g.tenant_id, g.user_email, "add_entra_group_member", f"{user_id}->{group_id}", "done")
        db.log_activity(g.tenant_id, "entra_action", g.user_email, target=user_id,
                        detail=f"Added to Entra group {group_id}")
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/groups/member/delete", methods=["POST"])
@require_dashboard_user
def api_entra_group_member_remove():
    """
    Remove a user from an Entra group. HIGH CAUTION -- gated behind the same
    6-digit confirmation challenge as disable_account.
    Body: { "group_id": "...", "user_id": "...", "confirm_token": "" }
    """
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400

    data          = request.get_json() or {}
    group_id      = str(data.get("group_id", "")).strip()
    user_id       = str(data.get("user_id", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    if not confirm_token:
        if not (group_id and user_id):
            return jsonify({"success": False, "message": "group_id and user_id are both required."}), 400
        sync_block = _entra_sync_guard(client, user_id)
        if sync_block:
            return jsonify({"success": False, "message": sync_block}), 403
        code = _issue_confirm_token(g.tenant_id, "remove_entra_group_member", [group_id, user_id])
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Remove user '{user_id}' from Entra group '{group_id}'",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending or pending["action"] != "remove_entra_group_member":
        return jsonify({"success": False, "message": "Invalid or expired confirmation code. Please try again."}), 403

    group_id, user_id = pending["args"]
    result = client.remove_group_member(group_id, user_id)
    if result["success"]:
        db.log_audit(g.tenant_id, g.user_email, "remove_entra_group_member", f"{user_id}->{group_id}", "done")
        db.log_activity(g.tenant_id, "entra_action", g.user_email, target=user_id,
                        detail=f"Removed from Entra group {group_id}")
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/sessions/revoke", methods=["POST"])
@require_dashboard_user
def api_entra_revoke_sessions():
    """
    Revoke all sign-in sessions for an Entra user. HIGH CAUTION -- gated
    behind the same 6-digit confirmation challenge as disable_account.
    Body: { "user_id": "...", "confirm_token": "" }
    """
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400

    data          = request.get_json() or {}
    user_id       = str(data.get("user_id", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    if not confirm_token:
        if not user_id:
            return jsonify({"success": False, "message": "user_id is required."}), 400
        sync_block = _entra_sync_guard(client, user_id)
        if sync_block:
            return jsonify({"success": False, "message": sync_block}), 403
        code = _issue_confirm_token(g.tenant_id, "revoke_entra_sessions", [user_id])
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Revoke all sign-in sessions for '{user_id}'",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending or pending["action"] != "revoke_entra_sessions":
        return jsonify({"success": False, "message": "Invalid or expired confirmation code. Please try again."}), 403

    (user_id,) = pending["args"]
    result = client.revoke_sessions(user_id)
    if result["success"]:
        db.log_audit(g.tenant_id, g.user_email, "revoke_entra_sessions", user_id, "done")
        db.log_activity(g.tenant_id, "entra_action", g.user_email, target=user_id,
                        detail="Revoked Entra sign-in sessions")
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/password/reset", methods=["POST"])
@require_dashboard_user
def api_entra_password_reset():
    """
    Reset an Entra user's password to a freshly generated temporary password
    (forceChangePasswordNextSignIn). HIGH CAUTION -- gated behind the same
    6-digit confirmation challenge as disable_account. The temp password is
    generated server-side, right before the confirmed write, so it never sits
    around in a pending confirm-token row.
    Body: { "user_id": "...", "confirm_token": "" }
    """
    client, err = _get_graph_client()
    if err:
        return jsonify({"success": False, "message": err}), 400

    data          = request.get_json() or {}
    user_id       = str(data.get("user_id", "")).strip()
    confirm_token = str(data.get("confirm_token", "")).strip()

    if not confirm_token:
        if not user_id:
            return jsonify({"success": False, "message": "user_id is required."}), 400
        sync_block = _entra_sync_guard(client, user_id)
        if sync_block:
            return jsonify({"success": False, "message": sync_block}), 403
        code = _issue_confirm_token(g.tenant_id, "reset_entra_password", [user_id])
        return jsonify({
            "success":               False,
            "requires_confirmation": True,
            "confirm_token":         code,
            "action_label":          f"Reset password for '{user_id}'",
            "message":               "This action requires confirmation.",
        }), 202

    pending = _consume_confirm_token(g.tenant_id, confirm_token)
    if not pending or pending["action"] != "reset_entra_password":
        return jsonify({"success": False, "message": "Invalid or expired confirmation code. Please try again."}), 403

    (user_id,) = pending["args"]
    new_password = _generate_temp_password()
    result = client.reset_password(user_id, new_password, force_change=True)
    if result["success"]:
        result["data"] = {"temp_password": new_password}
        db.log_audit(g.tenant_id, g.user_email, "reset_entra_password", user_id, "done")
        db.log_activity(g.tenant_id, "entra_action", g.user_email, target=user_id,
                        detail="Reset Entra password")
    return jsonify(result), (200 if result["success"] else 502)


@app.route("/api/entra/test", methods=["POST"])
@require_dashboard_user
def api_entra_test_connection():
    """
    Test Entra credentials without saving them first (used by the Settings
    page's "Test connection" button). Body may supply tenant_id/client_id/
    client_secret directly (e.g. before Save is clicked); falls back to the
    tenant's saved settings for any field left blank.
    """
    data  = request.get_json() or {}
    saved = _entra_settings(g.tenant_id)
    tenant_id     = str(data.get("graph_tenant_id") or "").strip() or saved["tenant_id"]
    client_id     = str(data.get("graph_client_id") or "").strip() or saved["client_id"]
    client_secret = str(data.get("graph_client_secret") or "").strip() or saved["client_secret"]

    if not (tenant_id and client_id and client_secret):
        return jsonify({"success": False, "message": "Tenant ID, Client ID, and Client Secret are all required."}), 400

    client = GraphClient(tenant_id, client_id, client_secret)
    result = client.test_connection()
    return jsonify(result), (200 if result["success"] else 502)


def _run_chat(tenant_id: str, user_email: str, message: str,
              history: list = None, session_id: str = None) -> dict:
    """
    Core AI chat pipeline shared by the dashboard and the public API.
    Returns a plain dict (suitable for jsonify). Never raises — errors are
    returned as {"success": False, "message": "..."}.
    """
    try:
        import anthropic
    except ImportError:
        return {"success": False, "message": "anthropic package not installed."}

    api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not api_key:
        return {"success": False, "message": "ANTHROPIC_API_KEY not set in environment."}

    history = history or []

    if not message:
        return {"success": False, "message": "message is required."}

    plan, limits = _tenant_plan_limits(tenant_id)
    usage = db.get_usage(tenant_id) or {}
    ai_cap = limits.get("janus_calls")
    if ai_cap is not None and usage.get("janus_calls", 0) >= ai_cap:
        return {
            "success": False,
            "message": (
                f"Monthly AI usage limit reached ({ai_cap} on the {limits['label']} plan). "
                "Upgrade to an active Pro or Enterprise subscription to continue."
            ),
        }
    if not _rate_limit(f"ai_chat:{tenant_id}", AI_CHAT_BURST_LIMIT_PER_HOUR, 3600):
        return {
            "success": False,
            "message": "AI assistant rate limit reached. Please wait before sending more requests.",
        }

    # ---- session -------------------------------------------------------
    if session_id:
        chat_session = db.get_chat_session(session_id, tenant_id)
        if not chat_session:
            session_id = None
    if not session_id:
        new_session = db.create_chat_session(tenant_id, user_email)
        session_id  = new_session["id"]

    db.add_chat_message(session_id, tenant_id, "user", message)

    # ---- system prompt -------------------------------------------------
    custom_scripts_cap, _, _ = _custom_scripts_limit(tenant_id)
    custom_scripts  = db.list_custom_scripts(tenant_id) if custom_scripts_cap != 0 else []
    enabled_scripts = [s for s in custom_scripts if s.get("enabled")]
    tenant_settings = db.get_settings(tenant_id)
    ai_context      = _get_ai_context(tenant_settings)
    ai_name         = _get_ai_name(tenant_settings)

    custom_scripts_block = ""
    if enabled_scripts:
        lines = ["", "  CUSTOM SCRIPTS (tenant-defined, call with run_custom_script):"]
        for s in enabled_scripts:
            args_note = f"  args: {s['args_description']}" if s.get("args_description") else "  args: []"
            lines.append(
                f"  run_custom_script  slug={s['slug']}  -- {s['description']}"
                f"\n    {args_note}"
                f"\n    classification: {s.get('classification','write')}"
                f"\n    To invoke: {{\"action\":\"run_custom_script\",\"args\":[\"{s['slug']}\",\"arg1\",...]}}"
            )
        custom_scripts_block = "\n".join(lines)

    tenant_context_block = f"\n\nADMIN-PROVIDED CONTEXT (read this for org-specific knowledge):\n{ai_context}" if ai_context else ""

    memories     = db.get_memories_for_context(tenant_id, limit=20)
    memory_block = ""
    if memories:
        lines = ["\n\nORGANISATIONAL MEMORY (facts learned about this environment; use these to inform your responses):"]
        for m in memories:
            lines.append(f"  [{m['category']}] {m['key']}: {m['value']}")
        memory_block = "\n".join(lines)
        for m in memories:
            db.touch_memory(tenant_id, m["id"])

    system_prompt = f"""You are {ai_name}, an AI assistant built into AD Helpdesk, a tool for managing Windows Active Directory.
The user is an IT admin. Your job is to understand what they want and either:
  (a) Execute an AD operation by responding with a JSON command block, OR
  (b) Answer a general question conversationally if no AD action is needed.

Available AD actions (use exact action names):
  USERS:
  get_user_info              args: [username]                      -- full user details including all group memberships, OU, status
  list_users                 args: []                              -- list all domain users
  list_users_in_ou           args: [ou_name]                      -- list all users in a specific OU (use this before bulk ops)
  search_users               args: [search_term]                   -- search by name or username (partial match)
  list_group_memberships     args: [username]                      -- detailed group list (use only if get_user_info groups aren't enough)
  create_user                args: [first, last, username, ou]     -- create new AD account (ou can be plain name)
  move_user                  args: [username, ou_name]             -- move a single user to a different OU

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
  create_ou                  args: [ou_name, parent_ou]           -- create a new OU (parent_ou can be plain name or omit for domain root)

  BULK / CONDITIONAL OPERATIONS:
  bulk_move_users            args: [source_ou, rules_json, create_missing_ous]

  REPORTING:
  list_locked_accounts       args: []                              -- all currently locked accounts
  list_expired_passwords     args: []                              -- all accounts with expired passwords
  get_stats                  args: []                              -- domain summary (total, locked, expired)

  DNS (only offer these if the tenant's agent has the 'dns' capability -- if a DNS
  action fails with a capability error, tell the user DNS management isn't connected yet):
  list_dns_zones              args: []                                          -- LOW RISK, read-only. List all DNS zones.
  get_dns_zone                args: [zone]                                      -- LOW RISK, read-only. Zone details (type, dynamic update, reverse-lookup).
  list_dns_records            args: [zone, type]                                -- LOW RISK, read-only. type is optional (A/AAAA/CNAME/MX/TXT/PTR), pass "" for all.
  get_dns_scavenging          args: []                                          -- LOW RISK, read-only. Current scavenging settings.
  add_dns_record               args: [zone, name, type, value, ttl_seconds]     -- ROUTINE WRITE. Adds one record. ttl_seconds defaults to 3600 if unsure.
  update_dns_record            args: [zone, name, type, old_value, new_value]   -- ROUTINE WRITE. Changes an existing record's value.
  remove_dns_record            args: [zone, name, type, value]                  -- HIGH CAUTION. Deletes a record; can break name resolution. The dashboard
                                                                                    always makes the admin confirm this with a 6-digit code before it runs --
                                                                                    you do not need to ask for extra confirmation yourself, just queue it plainly.
  set_dns_scavenging           args: [zone_or_scope, true|false]                -- HIGH CAUTION. Changes stale-record cleanup for the whole DNS server.
                                                                                    Also gated behind a 6-digit confirmation code before it runs.

  DNS SAFETY RULE -- zone apex records: if `name` is "@" (the zone apex, i.e. the
  domain's root record), treat any add/update/remove on it as HIGH RISK regardless
  of the action's normal tier -- apex records often carry mail routing (MX/TXT/SPF)
  or the primary A record for the whole domain. Call this out explicitly in your
  "message" field so the admin sees the warning before confirming, e.g.
  "Warning: this is the zone apex record -- changing it affects the whole domain."

  DHCP (only offer these if the tenant's agent has the 'dhcp' capability -- if a DHCP
  action fails with a capability error, tell the user DHCP management isn't connected yet):
  list_dhcp_scopes            args: []                                          -- LOW RISK, read-only. List all DHCP scopes.
  get_dhcp_scope              args: [scope_id]                                  -- LOW RISK, read-only. Scope details plus utilisation stats.
  get_dhcp_scope_stats        args: []                                          -- LOW RISK, read-only. Utilisation (Free/InUse/PercentageInUse) for every scope.
  list_dhcp_leases            args: [scope_id]                                  -- LOW RISK, read-only. Active leases in a scope.
  list_dhcp_reservations       args: [scope_id]                                 -- LOW RISK, read-only. Reservations in a scope.
  list_dhcp_exclusions         args: [scope_id]                                 -- LOW RISK, read-only. Excluded address ranges in a scope.
  add_dhcp_reservation          args: [scope_id, ip, mac, name, description]    -- ROUTINE WRITE. Pins an IP to a MAC (e.g. converting a lease). description can be "".
  remove_dhcp_reservation       args: [scope_id, ip]                            -- HIGH CAUTION. Drops the device back into the general lease pool. The dashboard
                                                                                    always makes the admin confirm this with a 6-digit code before it runs --
                                                                                    you do not need to ask for extra confirmation yourself, just queue it plainly.
  add_dhcp_exclusion            args: [scope_id, start_ip, end_ip]              -- HIGH CAUTION. Removes an address range from the assignable pool; can starve
                                                                                    the scope or clash with existing leases. Also gated behind a 6-digit code.
  remove_dhcp_exclusion         args: [scope_id, start_ip, end_ip]              -- HIGH CAUTION. Frees a previously excluded range back into the pool; can hand
                                                                                    out addresses that were reserved for static infrastructure. Also gated behind a 6-digit code.

  GROUP POLICY (GPO) (only offer these if the tenant's agent has the 'gpo' capability -- if a
  GPO action fails with a capability error, tell the user Group Policy management isn't connected yet):
  list_gpos                   args: []                                          -- LOW RISK, read-only. List all GPOs with status and dates.
  get_gpo                     args: [name_or_guid]                              -- LOW RISK, read-only. Basic GPO details (owner, domain, WMI filter).
  get_gpo_report               args: [name_or_guid]                             -- LOW RISK, read-only. Parsed report: Computer/User Configuration enabled
                                                                                    state plus extensions with setting counts.
  list_gpo_links               args: [ou_name_or_dn]                            -- LOW RISK, read-only. GPOs directly linked to an OU.
  get_gpo_inheritance          args: [ou_name_or_dn]                            -- LOW RISK, read-only. Direct + inherited GPO links for an OU, plus whether
                                                                                    inheritance is blocked.
  link_gpo                     args: [name_or_guid, ou_name_or_dn]              -- DESTRUCTIVE. Links a GPO to an OU, applying its policy to every user/
                                                                                    computer under that OU. This action ALWAYS requires human confirmation
                                                                                    with a 6-digit token before it runs -- never auto-resolve it, and never
                                                                                    tell the admin it has completed until they have confirmed the code.
  unlink_gpo                   args: [name_or_guid, ou_name_or_dn]              -- DESTRUCTIVE. Removes a GPO's link from an OU, so its policy stops applying
                                                                                    there. This action ALWAYS requires human confirmation with a 6-digit token
                                                                                    before it runs -- never auto-resolve it, and never tell the admin it has
                                                                                    completed until they have confirmed the code.
  set_gpo_status                args: [name_or_guid, status]                    -- DESTRUCTIVE. Changes whether a GPO's Computer and/or User settings apply
                                                                                    at all (status is one of AllSettingsEnabled, AllSettingsDisabled,
                                                                                    ComputerSettingsDisabled, UserSettingsDisabled). This action ALWAYS
                                                                                    requires human confirmation with a 6-digit token before it runs -- never
                                                                                    auto-resolve it, and never tell the admin it has completed until they
                                                                                    have confirmed the code.
  set_gpo_link_enforced         args: [name_or_guid, ou_name_or_dn, true|false] -- DESTRUCTIVE. Toggles whether a GPO link overrides "Block Inheritance" on
                                                                                    child OUs. This action ALWAYS requires human confirmation with a 6-digit
                                                                                    token before it runs -- never auto-resolve it, and never tell the admin it
                                                                                    has completed until they have confirmed the code.

  GPO SAFETY RULE: every GPO write above (link_gpo, unlink_gpo, set_gpo_status,
  set_gpo_link_enforced) can change policy for an entire OU -- and everything under
  it -- in one action. Unlike the routine DNS/DHCP writes, none of these are ever
  auto-resolved by you or queued as "already confirmed". Always queue them plainly
  (the dashboard shows the 6-digit confirmation modal automatically) and say so in
  your "message" field, e.g. "This will require you to confirm a 6-digit code before it runs."

  NPS (Network Policy Server / RADIUS) (only offer these if the tenant's agent has the 'nps'
  capability -- if an NPS action fails with a capability error, tell the user NPS management
  isn't connected yet). READ-ONLY: there are no NPS write actions, ever mention or invent one.
  list_nps_radius_clients      args: []                                          -- LOW RISK, read-only. Lists configured RADIUS clients (name, address,
                                                                                    vendor, enabled). Never includes shared secrets.
  list_nps_network_policies    args: []                                          -- LOW RISK, read-only. Lists network policies with enabled state and
                                                                                    processing order.
  list_nps_connection_policies args: []                                          -- LOW RISK, read-only. Lists connection request policies with enabled
                                                                                    state and processing order.
  get_nps_summary              args: []                                          -- LOW RISK, read-only. Counts of RADIUS clients, network policies, and
                                                                                    connection request policies for a quick overview.

  ENTRA ID (Microsoft Graph) (only offer these if the tenant has entered Entra credentials
  in Settings -- if an Entra action fails saying Entra isn't configured, tell the user to
  add their app registration details in Settings):
  list_entra_users             args: [search_term_or_""]                        -- LOW RISK, read-only. Lists cloud (Entra) users, optionally filtered by
                                                                                    display name or UPN. Runs directly against Microsoft Graph, not the
                                                                                    on-prem agent -- results are cloud identities, separate from AD ones.
  get_entra_user                args: [upn_or_id]                               -- LOW RISK, read-only. Full Entra user details including onPremisesSyncEnabled.
  list_entra_groups             args: [search_term_or_""]                       -- LOW RISK, read-only. Lists Entra (cloud) security/Microsoft 365 groups.
  get_entra_group_members       args: [group_id]                                -- LOW RISK, read-only. Members of an Entra group.

  These four reads execute inline against Microsoft Graph (no agent involved, no
  queueing delay). Every other Entra operation -- add/remove group membership,
  revoke sign-in sessions, reset password -- is only available from the Entra ID
  tab in the dashboard, not through chat, because those go through a human
  confirmation challenge in the UI.

  HYBRID IDENTITY ROUTING RULE: a user can exist in AD, in Entra, or both (a
  "synced" identity). When a user's Entra record has onPremisesSyncEnabled = true,
  that user is managed by on-prem AD and synced up to Entra -- any write made in
  Entra for that user is either rejected by Microsoft Graph or silently overwritten
  on the next sync cycle. If the admin asks you to change something (group
  membership, password, sessions) for a user who turns out to be synced from AD,
  tell them to use the Active Directory tab/actions instead of Entra, and do NOT
  attempt or recommend the Entra write action for that user. Only cloud-only
  users (onPremisesSyncEnabled = false or absent) can be safely mutated via Entra.
  When you look up a user (in AD or Entra) and report back, say which system the
  data came from (AD or Entra ID) so the admin knows which one they are looking at.

When you need to run an AD action, respond ONLY with this exact JSON (no preamble, no extra text):
{{"action": "action_name", "args": ["arg1", "arg2"], "message": "One sentence describing what you're doing"}}

CRITICAL RULES:
- Output ONLY the raw JSON when taking an action — no preamble, no trailing text.
- If the conversation history already contains the result of a previous lookup, answer directly from that — do NOT run the action again.
- When generating a temporary password, make it secure: uppercase + lowercase + numbers + symbol, 12+ chars. State it clearly.
- Questions about a user's groups, role, department, title, status → ALWAYS use get_user_info first.
- If no AD action is needed, respond conversationally in plain text — do NOT output JSON.
- Keep responses concise and direct. You are talking to an experienced IT admin.{custom_scripts_block}{tenant_context_block}{memory_block}"""

    LOOKUP_ACTIONS = {
        'search_users', 'search_groups', 'list_ous', 'get_user_info',
        'list_users', 'list_users_in_ou', 'list_groups', 'list_locked_accounts',
        'list_expired_passwords', 'get_stats', 'list_group_memberships',
        'get_group_members',
        'list_dns_zones', 'get_dns_zone', 'list_dns_records', 'get_dns_scavenging',
        'list_dhcp_scopes', 'get_dhcp_scope', 'get_dhcp_scope_stats',
        'list_dhcp_leases', 'list_dhcp_reservations', 'list_dhcp_exclusions',
        'list_gpos', 'get_gpo', 'get_gpo_report', 'list_gpo_links', 'get_gpo_inheritance',
        'list_nps_radius_clients', 'list_nps_network_policies', 'list_nps_connection_policies', 'get_nps_summary',
    }

    # Entra reads run inline via GraphClient (see the branch below), not via
    # db.queue_command -- there is no agent involved for a cloud service. They
    # still count as "lookups" for the single-hop follow-up chain below.
    ENTRA_LOOKUP_ACTIONS = {
        'list_entra_users', 'get_entra_user', 'list_entra_groups', 'get_entra_group_members',
    }
    LOOKUP_ACTIONS |= ENTRA_LOOKUP_ACTIONS

    client   = anthropic.Anthropic(api_key=api_key)
    messages = []
    for h in history[-10:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": h["content"]})
    messages.append({"role": "user", "content": message})

    response     = client.messages.create(
        model=_ai_model(tenant_id), max_tokens=512,
        system=system_prompt, messages=messages
    )
    reply        = response.content[0].text.strip()
    db.increment_usage(tenant_id, "janus_calls", _ai_cost(tenant_id))
    command_data = None
    try:
        import re
        clean = re.sub(r'```(?:json)?', '', reply).strip()
        match = re.search(r'\{.*?"action".*?\}', clean, re.DOTALL)
        if match:
            parsed = json.loads(match.group())
            if "action" in parsed and "args" in parsed:
                command_data = parsed
    except (json.JSONDecodeError, ValueError):
        pass

    if command_data:
        action     = command_data["action"]
        args       = command_data["args"]
        intent_msg = command_data.get("message", f"Running {action}...")

        if action == "run_custom_script" and args:
            block = _paid_feature_block(tenant_id, "custom_scripts_limit", "Custom scripts")
            if block:
                return {"success": True, "reply": block, "action_taken": None}
            slug   = args[0]
            script = db.get_custom_script_by_slug(tenant_id, slug)
            if not script:
                return {"success": True, "reply": f"Custom script '{slug}' not found or is disabled.", "action_taken": None}
            args       = [script["ps_content"]] + list(args[1:])
            intent_msg = intent_msg or f"Running custom script: {script['name']}"

        # Entra reads bypass action_policy entirely -- that gate is for the
        # agent action-name queue, and Entra actions never touch the agent.
        # They execute inline against Microsoft Graph right here.
        if action in ENTRA_LOOKUP_ACTIONS:
            if not _entra_configured(tenant_id):
                return {
                    "success": True,
                    "reply": "Entra ID is not configured. Add your Entra credentials in Settings to look up cloud users and groups.",
                    "action_taken": None,
                }
            creds       = _entra_settings(tenant_id)
            graph       = GraphClient(creds["tenant_id"], creds["client_id"], creds["client_secret"])
            graph_method = GRAPH_ACTIONS[action]
            try:
                result_data = getattr(graph, graph_method)(*args)
            except TypeError:
                result_data = {"success": False, "message": f"Invalid arguments for {action}.", "data": None}
            command_id = None
        else:
            policy_ok, policy_reason = action_policy.validate(action, source="ai_chat")
            if not policy_ok:
                db.log_activity(tenant_id, "security_flag", user_email,
                                detail=f"Chat policy blocked '{action}': {policy_reason}")
                return {"success": True, "reply": f"I can't run that action: {policy_reason}", "action_taken": None}

            if _is_ad_mutation(action):
                if not _rate_limit(f"ai_writes:{tenant_id}", 20, 3600):
                    return {"success": True, "reply": "Rate limit reached (20 write actions/hour). Please wait.", "action_taken": None}

            if action in DESTRUCTIVE_ACTIONS:
                code = _issue_confirm_token(tenant_id, action, args)
                return {
                    "success": True,
                    "reply": f"{intent_msg}\n\nThis action requires confirmation before it is queued.",
                    "requires_confirmation": True,
                    "confirm_token": code,
                    "action_label": f"{action} {args}",
                    "pending_action": {"action": action, "args": args},
                    "action_taken": None,
                    "session_id": session_id,
                }

            queue_args = _queue_args_with_ad_budget(tenant_id, action, args)

            # Monthly plan-quota enforcement. The dashboard (/dashboard/api/exec) and
            # the public API (/api/v1/actions) both hard-block write/destructive
            # actions once the tenant hits its ad_commands cap; without the same gate
            # here the limit could be bypassed simply by routing the action through
            # AI chat. Reads stay uncapped.
            if _is_ad_mutation(action):
                limit_block = _ad_usage_limit_block(tenant_id, action, args)
                if limit_block:
                    return {
                        "success": True,
                        "reply": (
                            f"{limit_block['message']} Upgrade to Pro for "
                            f"{db.PLAN_LIMITS['pro']['ad_commands']} AD actions/month to continue."
                        ),
                        "action_taken": None,
                    }

            if _is_ad_mutation(action):
                target = args[0] if args else ""
                db.log_audit(tenant_id, user_email, action, target, "queued")
                _charge_ad_usage(tenant_id, action, args)

            command    = db.queue_command(tenant_id, action, queue_args)
            command_id = command["id"]

            result_data = None
            deadline    = time.time() + 25
            while time.time() < deadline:
                time.sleep(0.6)
                result = db.get_command_result(command_id, tenant_id)
                if result:
                    result_data = result
                    break

            if not result_data:
                return {"success": True, "reply": f"{intent_msg}\n\nAgent did not respond in time. Is it running?", "action_taken": action}

        # Label lookup results with their source system so the model (and the
        # admin, via its reply) can tell AD data from Entra data apart.
        if result_data is not None and result_data.get("success"):
            result_data = dict(result_data)
            result_data["source"] = "Entra ID (Microsoft Graph)" if action in ENTRA_LOOKUP_ACTIONS else "Active Directory"

        if action in LOOKUP_ACTIONS and result_data.get("success"):
            chain_prompt = f"""You are {ai_name}. The user originally asked: "{message}"
To prepare, you first ran: {action} {args} (source: {result_data.get('source', 'Active Directory')})
The lookup returned: {json.dumps(result_data.get('data', {}))[:1400]}

Mention the source system (AD or Entra ID) in your reply so the admin knows which directory this data came from.
Now complete the user's original request using the exact names/values from the lookup result above.
If the original request is fully answered by the lookup, reply conversationally.
If a follow-up write action is needed, output ONLY the JSON command.
Do NOT run another lookup — use the data you already have.
Output format for action: {{"action": "action_name", "args": ["arg1"], "message": "what you are doing"}}"""

            chain_resp = client.messages.create(
                model=_ai_model(tenant_id), max_tokens=300,
                messages=[{"role": "user", "content": chain_prompt}]
            )
            chain_text = chain_resp.content[0].text.strip()

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
                policy_ok2, policy_reason2 = action_policy.validate(action2, source="ai_chat")
                if not policy_ok2:
                    return {"success": True, "reply": f"I can't run that follow-up action: {policy_reason2}", "action_taken": action}
                if action2 == "run_custom_script" and args2:
                    block = _paid_feature_block(tenant_id, "custom_scripts_limit", "Custom scripts")
                    if block:
                        return {"success": True, "reply": block, "action_taken": action}
                    slug2 = args2[0]
                    script2 = db.get_custom_script_by_slug(tenant_id, slug2)
                    if not script2:
                        return {"success": True, "reply": f"Custom script '{slug2}' not found or is disabled.", "action_taken": action}
                    args2 = [script2["ps_content"]] + list(args2[1:])
                if action2 in DESTRUCTIVE_ACTIONS:
                    code2 = _issue_confirm_token(tenant_id, action2, args2)
                    return {
                        "success": True,
                        "reply": f"The follow-up action {action2} requires confirmation before it is queued.",
                        "requires_confirmation": True,
                        "confirm_token": code2,
                        "action_label": f"{action2} {args2}",
                        "pending_action": {"action": action2, "args": args2},
                        "action_taken": action,
                        "raw_data": result_data.get("data"),
                        "session_id": session_id,
                    }
                queue_args2 = _queue_args_with_ad_budget(tenant_id, action2, args2)
                if _is_ad_mutation(action2):
                    limit_block2 = _ad_usage_limit_block(tenant_id, action2, args2)
                    if limit_block2:
                        return {
                            "success": True,
                            "reply": limit_block2["message"],
                            "action_taken": action,
                        }
                    db.log_audit(tenant_id, user_email, action2, args2[0] if args2 else "", "queued")
                    _charge_ad_usage(tenant_id, action2, args2)
                cmd2      = db.queue_command(tenant_id, action2, queue_args2)
                result2   = None
                deadline2 = time.time() + 25
                while time.time() < deadline2:
                    time.sleep(0.6)
                    r2 = db.get_command_result(cmd2["id"], tenant_id)
                    if r2:
                        result2 = r2
                        break
                if result2:
                    pw_note2 = f"\nIMPORTANT: New password set: {args2[1]} - include it in your reply." if action2 == "reset_password" and len(args2) >= 2 else ""
                    sum2 = client.messages.create(
                        model=_ai_model(tenant_id), max_tokens=300,
                        messages=[{"role": "user", "content": f"""You are {ai_name}. The user asked: "{message}"
You searched first ({action} {args}), then ran: {action2} {args2}
Final result: success={result2['success']}, message="{result2['message']}", data={json.dumps(result2.get('data',''))[:600]}
{pw_note2}
Write 2-3 sentences confirming what happened. Be direct and clear."""}]
                    )
                    final_reply = sum2.content[0].text.strip()
                    db.add_chat_message(session_id, tenant_id, "assistant", final_reply, action2)
                    db.touch_chat_session(session_id)
                    db.update_chat_session_title(session_id, message[:60])
                    db.log_activity(tenant_id, "janus_action", user_email,
                                    target=args2[0] if args2 else None,
                                    detail=f"{action} -> {action2} via chat")
                    return {"success": True, "reply": final_reply, "action_taken": action2,
                            "raw_data": result2.get("data"), "session_id": session_id}
            else:
                db.add_chat_message(session_id, tenant_id, "assistant", chain_text, action)
                db.touch_chat_session(session_id)
                db.update_chat_session_title(session_id, message[:60])
                return {"success": True, "reply": chain_text, "action_taken": action,
                        "raw_data": result_data.get("data"), "session_id": session_id}

        password_note = ""
        if action == "reset_password" and len(args) >= 2:
            password_note = f"\nIMPORTANT: The new password that was set is: {args[1]} - you MUST include this in your reply so the admin can share it."

        summary_response = client.messages.create(
            model=_ai_model(tenant_id), max_tokens=300,
            messages=[{"role": "user", "content": f"""You are {ai_name}, an AD Helpdesk AI assistant. The user asked: "{message}"
You ran this AD action: {action}
Arguments used: {args}
Agent result: success={result_data['success']}, message="{result_data['message']}", data={json.dumps(result_data['data'])[:1200]}
{password_note}
Write a short, friendly plain-English summary of what happened. Keep it under 5 sentences. Be direct."""}]
        )
        final_reply = summary_response.content[0].text.strip()
        db.add_chat_message(session_id, tenant_id, "assistant", final_reply, action)
        db.touch_chat_session(session_id)
        db.update_chat_session_title(session_id, message[:60])
        db.log_activity(tenant_id, "janus_action", user_email,
                        target=args[0] if args else None,
                        detail=f"{action} via chat - {('success' if result_data['success'] else 'failed')}")
        if result_data.get("success") and action not in LOOKUP_ACTIONS:
            import threading
            threading.Thread(
                target=_run_reflection_pass,
                args=(tenant_id, ai_name, message, action, args, True),
                daemon=True
            ).start()
        return {"success": True, "reply": final_reply, "action_taken": action,
                "raw_data": result_data.get("data"), "session_id": session_id}

    # Conversational — no AD action
    db.add_chat_message(session_id, tenant_id, "assistant", reply)
    db.touch_chat_session(session_id)
    db.update_chat_session_title(session_id, message[:60])
    return {"success": True, "reply": reply, "action_taken": None, "session_id": session_id}


@app.route("/dashboard/chat", methods=["POST"])
@require_dashboard_user
def dashboard_chat():
    """
    AI chat interface — takes a plain English message, asks Claude what AD
    command to run, queues it through the agent, and returns a natural language reply.
    """
    data       = request.get_json() or {}
    message    = data.get("message", "").strip()
    history    = data.get("history", [])
    session_id = data.get("session_id")
    result     = _run_chat(g.tenant_id, g.user_email, message, history, session_id)
    status     = 200 if result.get("success") else 400
    return jsonify(result), status


@app.route("/dashboard/api/feedback", methods=["POST"])
@require_dashboard_user
def dashboard_feedback():
    """Store in-product feedback and optionally email it to the operator."""
    data    = request.get_json() or {}
    message = (data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "message": "message is required"}), 400
    rating  = data.get("rating")      # int 1-5 or None
    page    = (data.get("page") or "").strip()[:120] or None
    if rating is not None:
        try:
            rating = int(rating)
            if rating < 1 or rating > 5:
                rating = None
        except (TypeError, ValueError):
            rating = None
    db.create_feedback(g.tenant_id, g.user_email, message[:2000], rating, page)
    db.log_activity(g.tenant_id, "feedback", g.user_email, detail=message[:200])
    # Forward to operator email if SMTP is configured
    _feedback_email(g.tenant_id, g.user_email, message, rating, page)
    return jsonify({"success": True, "message": "Thank you for your feedback!"})


def _feedback_email(tenant_id: str, user_email: str, message: str,
                    rating: int | None, page: str | None) -> None:
    """Best-effort email forward to FEEDBACK_EMAIL env var."""
    recipient = os.getenv("FEEDBACK_EMAIL", "")
    smtp_host = os.getenv("SMTP_HOST", "")
    if not recipient or not smtp_host:
        return
    try:
        smtp_user = os.getenv("SMTP_USER", "")
        smtp_pass = os.getenv("SMTP_PASS", "")
        smtp_port = int(os.getenv("SMTP_PORT", "587"))
        body = (
            f"New feedback from AID Helpdesk\n\n"
            f"Tenant:  {tenant_id}\n"
            f"User:    {user_email}\n"
            f"Page:    {page or '—'}\n"
            f"Rating:  {rating or '—'}/5\n\n"
            f"{message}"
        )
        msg = MIMEText(body, "plain")
        msg["Subject"] = f"[AID Feedback] {user_email}"
        msg["From"]    = smtp_user or "noreply@aidhelpdesk.com"
        msg["To"]      = recipient
        with smtplib.SMTP(smtp_host, smtp_port) as s:
            s.starttls()
            if smtp_user and smtp_pass:
                s.login(smtp_user, smtp_pass)
            s.send_message(msg)
    except Exception:
        pass  # non-critical; feedback is already stored in DB


@app.route("/dashboard/api/usage")
@require_dashboard_user
def dashboard_usage():
    """Return this tenant's usage, plan, and limits for the current month."""
    plan    = db.get_tenant_plan(g.tenant_id)
    limits  = db.get_plan_limits(plan)
    usage   = db.get_usage(g.tenant_id) or {}
    history = db.get_usage_history(g.tenant_id)
    billing = db.get_billing_subscription(g.tenant_id) or {}
    return jsonify({"success": True, "data": {
        "current": usage,
        "history": history,
        "plan":    plan,
        "limits":  limits,
        "billing": {
            "status": billing.get("status", "none"),
            "current_period_end": billing.get("current_period_end"),
            "portal_available": bool(db.get_billing_customer(g.tenant_id)),
        },
    }})


@app.route("/dashboard/api/time-saved")
@require_dashboard_user
def dashboard_time_saved():
    """Compute IT time saved by AI-assisted actions for the current calendar month."""
    action_counts = db.get_action_counts_this_month(g.tenant_id)
    total_minutes = 0.0
    breakdown     = {}
    for action, count in action_counts.items():
        mins_per = TIME_SAVED_MINUTES.get(action, 0)
        saved    = mins_per * count
        total_minutes += saved
        if saved > 0:
            breakdown[action] = {"count": count, "minutes_per": mins_per, "minutes_saved": saved}

    hours = int(total_minutes // 60)
    mins  = int(total_minutes % 60)
    if hours > 0:
        display = f"{hours}h {mins}m"
    elif mins > 0:
        display = f"{mins}m"
    else:
        display = "0m"

    return jsonify({"success": True, "data": {
        "total_minutes": total_minutes,
        "hours":         hours,
        "minutes":       mins,
        "display":       display,
        "breakdown":     breakdown,
    }})


# ---------------------------------------------------------------------------
# Custom scripts CRUD
# ---------------------------------------------------------------------------

import re as _re_module

def _slugify(text: str) -> str:
    """Convert a display name to a safe slug for the AI assistant to reference."""
    s = text.lower().strip()
    s = _re_module.sub(r"[^a-z0-9]+", "_", s)
    return s.strip("_")[:40]


@app.route("/dashboard/api/scripts", methods=["GET"])
@require_dashboard_user
def dashboard_list_scripts():
    scripts = db.list_custom_scripts(g.tenant_id)
    # Never expose ps_content in the list view
    safe = [{k: v for k, v in s.items() if k != "ps_content"} for s in scripts]
    return jsonify({"success": True, "data": safe})


@app.route("/dashboard/api/scripts", methods=["POST"])
@require_dashboard_user
def dashboard_create_script():
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    cap, _, limits = _custom_scripts_limit(g.tenant_id)
    if cap == 0:
        return jsonify({
            "success": False,
            "message": "Custom scripts require an active Pro or Enterprise subscription.",
        }), 403
    if cap is not None and len(db.list_custom_scripts(g.tenant_id)) >= cap:
        return jsonify({
            "success": False,
            "limit_reached": True,
            "message": f"Custom script limit reached ({cap} on {limits['label']} plan).",
        }), 429
    data           = request.get_json() or {}
    name           = (data.get("name") or "").strip()
    description    = (data.get("description") or "").strip()
    ps_content     = (data.get("ps_content") or "").strip()
    args_desc      = (data.get("args_description") or "").strip()
    classification = data.get("classification", "write")
    if not name or not description or not ps_content:
        return jsonify({"success": False, "message": "name, description, and ps_content are required."}), 400
    if classification not in ("read", "write", "destructive"):
        classification = "write"
    slug   = _slugify(name)
    script = db.create_custom_script(
        g.tenant_id, name, slug, description, ps_content, args_desc, classification)
    db.log_activity(g.tenant_id, "script_created", g.user_email,
                    detail=f"Custom script '{name}' (slug: {slug}) created")
    return jsonify({"success": True, "data": {k: v for k, v in script.items() if k != "ps_content"}}), 201


@app.route("/dashboard/api/scripts/<script_id>", methods=["GET"])
@require_dashboard_user
def dashboard_get_script(script_id):
    script = db.get_custom_script(g.tenant_id, script_id)
    if not script:
        return jsonify({"success": False, "message": "Script not found."}), 404
    return jsonify({"success": True, "data": script})


@app.route("/dashboard/api/scripts/<script_id>", methods=["PATCH"])
@require_dashboard_user
def dashboard_update_script(script_id):
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    cap, _, _ = _custom_scripts_limit(g.tenant_id)
    if cap == 0:
        return jsonify({
            "success": False,
            "message": "Custom scripts require an active Pro or Enterprise subscription.",
        }), 403
    data    = request.get_json() or {}
    allowed = {"name", "description", "ps_content", "args_description", "classification", "enabled"}
    fields  = {k: v for k, v in data.items() if k in allowed}
    if "name" in fields:
        fields["slug"] = _slugify(fields["name"])
    if not db.update_custom_script(g.tenant_id, script_id, **fields):
        return jsonify({"success": False, "message": "Script not found."}), 404
    return jsonify({"success": True})


@app.route("/dashboard/api/scripts/<script_id>", methods=["DELETE"])
@require_dashboard_user
def dashboard_delete_script(script_id):
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    if not db.delete_custom_script(g.tenant_id, script_id):
        return jsonify({"success": False, "message": "Script not found."}), 404
    db.log_activity(g.tenant_id, "script_deleted", g.user_email,
                    detail=f"Custom script {script_id} deleted")
    return jsonify({"success": True})


@app.route("/dashboard/api/settings", methods=["GET"])
@require_dashboard_user
def dashboard_get_settings():
    """Return this tenant's AI / automation settings."""
    settings = db.get_settings(g.tenant_id)
    # Never expose smtp_pass in API response — redact it
    safe = dict(settings)
    if safe.get("smtp_pass"):
        safe["smtp_pass"] = ""   # client shows placeholder text instead
    if safe.get("graph_client_secret"):
        safe["graph_client_secret"] = ""   # never echo the secret back; client shows placeholder text
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
               "smtp_host", "smtp_port", "smtp_user", "smtp_pass", "smtp_from",
               "ai_context", "ai_name", "ai_model",
               "report_enabled", "report_frequency", "report_day",
               "report_hour", "report_recipients", "last_report_sent",
               "slack_webhook_url", "teams_webhook_url",
               "graph_tenant_id", "graph_client_id", "graph_client_secret"}
    _, limits = _tenant_plan_limits(g.tenant_id)
    if data.get("janus_auto_actions") and not limits.get("auto_actions"):
        return jsonify({
            "success": False,
            "message": "AI auto-actions require an active Pro or Enterprise subscription.",
        }), 403
    if data.get("report_enabled") and not limits.get("scheduled_reports"):
        return jsonify({
            "success": False,
            "message": "Scheduled reports require an active Pro or Enterprise subscription.",
        }), 403
    wants_integrations = (
        ("slack_webhook_url" in data and str(data.get("slack_webhook_url") or "").strip()) or
        ("teams_webhook_url" in data and str(data.get("teams_webhook_url") or "").strip())
    )
    if wants_integrations and not limits.get("integrations"):
        return jsonify({
            "success": False,
            "message": "Slack/Teams integrations require an active Pro or Enterprise subscription.",
        }), 403
    for k, v in data.items():
        if k in allowed:
            # Never overwrite smtp_pass / graph_client_secret with an empty
            # string (blank = "keep existing")
            if k in ("smtp_pass", "graph_client_secret") and v == "":
                continue
            if k == "ai_name":
                v = str(v or "").strip() or DEFAULT_AI_NAME
            if k == "ai_model" and v not in AI_MODELS:
                v = DEFAULT_AI_TIER
            current[k] = v
    db.update_settings(g.tenant_id, current)
    db.log_activity(g.tenant_id, "settings_changed", g.user_email, detail="Settings updated")
    safe = dict(current)
    for _secret in ("smtp_pass", "graph_client_secret"):
        if safe.get(_secret):
            safe[_secret] = ""   # never echo secrets back; client shows placeholder
    return jsonify({"success": True, "data": safe})


@app.route("/dashboard/api/integrations/test", methods=["POST"])
@require_dashboard_user
def dashboard_test_webhook():
    """Send a test notification to a Slack or Teams webhook."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    block = _paid_feature_block(g.tenant_id, "integrations", "Slack/Teams integrations")
    if block:
        return jsonify({"success": False, "message": block}), 403
    data     = request.get_json() or {}
    platform = data.get("platform", "slack")
    url      = data.get("webhook_url", "").strip()
    if not url:
        return jsonify({"success": False, "message": "webhook_url is required."}), 400
    try:
        _send_webhook_notification(url, f":white_check_mark: AID Helpdesk test notification from {g.tenant_name}. Your {platform.capitalize()} integration is working.", platform)
        return jsonify({"success": True})
    except Exception as e:
        return jsonify({"success": False, "message": str(e)}), 500


@app.route("/dashboard/api/memory", methods=["GET"])
@require_dashboard_user
def dashboard_list_memory():
    """Return all stored AI memories for this tenant."""
    memories = db.list_memories(g.tenant_id)
    return jsonify({"success": True, "data": memories})


@app.route("/dashboard/api/memory", methods=["POST"])
@require_dashboard_user
def dashboard_create_memory():
    """Manually add a memory (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data     = request.get_json() or {}
    key      = data.get("key", "").strip()
    value    = data.get("value", "").strip()
    category = data.get("category", "general").strip()
    if not key or not value:
        return jsonify({"success": False, "message": "key and value are required."}), 400
    mem = db.create_memory(g.tenant_id, category, key, value,
                           confidence=1.0, source="manual")
    db.log_activity(g.tenant_id, "memory_added", g.user_email,
                    detail=f"Memory added: {key}")
    return jsonify({"success": True, "data": mem}), 201


@app.route("/dashboard/api/memory/<memory_id>", methods=["PATCH"])
@require_dashboard_user
def dashboard_update_memory(memory_id):
    """Edit a memory entry (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    data = request.get_json() or {}
    ok   = db.update_memory(g.tenant_id, memory_id,
                             key=data.get("key"), value=data.get("value"),
                             category=data.get("category"), confidence=data.get("confidence"))
    if not ok:
        return jsonify({"success": False, "message": "Memory not found."}), 404
    return jsonify({"success": True})


@app.route("/dashboard/api/memory/<memory_id>", methods=["DELETE"])
@require_dashboard_user
def dashboard_delete_memory(memory_id):
    """Delete a memory entry (admin only)."""
    if g.user_role != "admin":
        return jsonify({"success": False, "message": "Admin only."}), 403
    if not db.delete_memory(g.tenant_id, memory_id):
        return jsonify({"success": False, "message": "Memory not found."}), 404
    db.log_activity(g.tenant_id, "memory_deleted", g.user_email,
                    detail=f"Memory {memory_id} deleted")
    return jsonify({"success": True})


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
# Shared AI analysis helper
# ---------------------------------------------------------------------------

def _run_janus_analysis(tenant_id, tenant_name, ticket_id, title, description,
                        requester_name, requester_email, limits):
    """
    Run AI analysis on a ticket and update it in the DB.
    Returns a dict with keys: parsed, auto_resolved (bool), error (str or None).
    Silently skips if API key is missing, plan limit hit, or the AI assistant is disabled.
    """
    import anthropic as _anthropic
    api_key  = os.getenv("ANTHROPIC_API_KEY", "")
    settings = db.get_settings(tenant_id)
    ai_name  = _get_ai_name(settings)
    ai_actor = ai_name

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

        tenant_caps  = db.get_tenant_capabilities(tenant_id) or []
        dns_enabled  = "dns" in tenant_caps
        dns_context  = ""
        if dns_enabled:
            dns_context = """

DNS LOOKUPS: This tenant's agent has DNS management connected. If the ticket sounds
DNS-shaped (a hostname isn't resolving, a device can't be reached by name, "works by IP
but not by name", a record looks wrong or missing) you may run ONE read-only DNS lookup
before finishing your analysis. To do that, respond with ONLY this JSON instead of the
final analysis format:
{"dns_lookup": {"action": "list_dns_zones|get_dns_zone|list_dns_records|get_dns_scavenging", "args": ["arg1"]}}
You will then be shown the lookup result and asked for the final analysis. Only use
read actions here (list_dns_zones, get_dns_zone, list_dns_records, get_dns_scavenging) --
never request a DNS write (add/update/remove a record) from analysis. If a DNS fix is
needed, put it in the "action"/"args" fields of the final analysis JSON as normal so it
goes through the existing confirm flow like any other action."""

        trusted_domain  = settings.get("email_domain", "")
        security_checks = settings.get("security_checks", True)
        security_context = ""
        if security_checks:
            if trusted_domain:
                nd = _normalize_domain(trusted_domain)
                # Python does the domain check; the AI only gets the binary verdict.
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
{dns_context}

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
  add_dns_record         args: [zone, name, type, value, ttl_seconds]  -- add a DNS record (recommend only; do not auto-resolve from analysis)
  update_dns_record      args: [zone, name, type, old_value, new_value] -- change a DNS record's value (recommend only; do not auto-resolve from analysis)

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
- For group operations, use the group name exactly as the user wrote it.
- DNS fixes (add_dns_record, update_dns_record) must always be recommended with can_auto_resolve set to false -- they always need a human to confirm, never set it true for those two actions."""

        resp   = client.messages.create(
            model=_ai_model(tenant_id),
            max_tokens=500,
            messages=[{"role": "user", "content": prompt}]
        )
        raw    = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

        # One optional DNS read hop: the model may ask for a read-only DNS lookup
        # before finalising its analysis. Only wired up when the tenant's agent
        # actually has the 'dns' capability, and only READ actions are allowed --
        # this mirrors the single-lookup chain used by the AI chat flow.
        try:
            precheck = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            precheck = None

        if dns_enabled and isinstance(precheck, dict) and "dns_lookup" in precheck:
            lookup      = precheck.get("dns_lookup") or {}
            lookup_action = lookup.get("action")
            lookup_args   = lookup.get("args", [])
            DNS_READ_ACTIONS = {"list_dns_zones", "get_dns_zone", "list_dns_records", "get_dns_scavenging"}

            if lookup_action in DNS_READ_ACTIONS:
                command     = db.queue_command(tenant_id, lookup_action, lookup_args)
                result_data = None
                deadline    = time.time() + 15
                while time.time() < deadline:
                    time.sleep(0.6)
                    result = db.get_command_result(command["id"], tenant_id)
                    if result:
                        result_data = result
                        break

                lookup_summary = (
                    json.dumps(result_data.get("data", {}))[:1200]
                    if result_data and result_data.get("success")
                    else "Lookup failed or the agent did not respond in time -- proceed without DNS data."
                )

                follow_up_prompt = f"""{prompt}

You already ran a DNS lookup: {lookup_action} {lookup_args}
Result: {lookup_summary}

Now give your final analysis using the exact JSON format above. Do not request another lookup."""

                resp = client.messages.create(
                    model=_ai_model(tenant_id),
                    max_tokens=500,
                    messages=[{"role": "user", "content": follow_up_prompt}]
                )
                raw = resp.content[0].text.strip().lstrip("```json").lstrip("```").rstrip("```").strip()

        parsed = json.loads(raw)

        # Code-enforced, not prompt-enforced: DNS fixes are recommendations only
        # and must go through the existing confirm flow, never auto-execute here.
        if parsed.get("action") in ("add_dns_record", "update_dns_record"):
            parsed["can_auto_resolve"] = False

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
                             f"[Threat {threat_score}/10 - {threat_title}] {analysis_text}", ai_actor)
        db.increment_usage(tenant_id, "janus_calls")

        if security_flag:
            db.log_activity(tenant_id, "security_flag", ai_actor,
                            target=requester_email or requester_name,
                            detail=f"Ticket #{ticket_id[:8]}: {security_flag}")

        # Auto-action
        auto_resolved = False
        can_auto      = parsed.get("can_auto_resolve", False)
        janus_action  = parsed.get("action")
        janus_args    = parsed.get("args", [])
        auto_actions  = settings.get("janus_auto_actions", [])

        # Python-enforced policy check — blocks DESTRUCTIVE actions regardless
        # of what the LLM said. This is code enforcing rules, not a prompt.
        policy_ok, policy_reason = action_policy.validate(
            janus_action or "",
            source="ai_auto",
            tenant_auto_actions=auto_actions,
        )

        if can_auto and janus_action and janus_args and perm_ok and policy_ok and limits.get("auto_actions", False):
            queue_args = _queue_args_with_ad_budget(tenant_id, janus_action, janus_args)
            if _is_ad_mutation(janus_action):
                limit_block = _ad_usage_limit_block(tenant_id, janus_action, janus_args)
                if limit_block:
                    db.add_ticket_action(
                        ticket_id, tenant_id, "janus_analysis",
                        f"{ai_name} could not auto-apply {janus_action}: {limit_block['message']}",
                        ai_actor
                    )
                    return {"parsed": parsed, "auto_resolved": False, "error": None}
                db.log_audit(tenant_id, ai_actor, janus_action, janus_args[0] if janus_args else "", "queued")
                _charge_ad_usage(tenant_id, janus_action, janus_args)

            db.add_ticket_action(ticket_id, tenant_id, "janus_analysis",
                                 f"{ai_name} is automatically applying fix: {janus_action} {janus_args}...", ai_actor)
            command    = db.queue_command(tenant_id, janus_action, queue_args)
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
                                     f"{ai_name} auto-resolved: {janus_action} on {janus_args[0] if janus_args else '?'} - {result_data['message']}", ai_actor)
                db.log_activity(tenant_id, "ticket_resolved", ai_actor, target=ticket_id,
                                detail=f"Auto-resolved via {janus_action}")
                auto_resolved = True
                if requester_email:
                    send_ticket_email(
                        to_email=requester_email,
                        to_name=requester_name or "",
                        ticket_title=title,
                        action_taken=f"{janus_action} on {janus_args[0] if janus_args else '?'}",
                        message=f"Your account issue has been resolved automatically by {ai_name}.",
                        tenant_settings=db.get_settings(tenant_id),
                    )
            elif result_data:
                db.add_ticket_action(ticket_id, tenant_id, "ad_action",
                                     f"{ai_name} auto-apply failed: {result_data['message']}", ai_actor)
            else:
                db.add_ticket_action(ticket_id, tenant_id, "ad_action",
                                     f"{ai_name} auto-apply timed out - agent may be offline.", ai_actor)

        elif janus_action and not policy_ok:
            # Policy blocked the auto-action -- log it clearly in the audit trail
            db.add_ticket_action(ticket_id, tenant_id, "janus_analysis",
                                 f"Auto-action blocked by policy: {policy_reason}", ai_actor)
            db.log_activity(tenant_id, "security_flag", ai_actor,
                            target=ticket_id,
                            detail=f"Policy blocked '{janus_action}': {policy_reason}")

        return {"parsed": parsed, "auto_resolved": auto_resolved, "error": None}

    except Exception as e:
        print(f"[ai] Analysis error for ticket {ticket_id}: {e}", flush=True)
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
    """Create a ticket and trigger AI auto-analysis."""
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

    # Notify Slack/Teams
    notify_integrations(
        g.tenant_id,
        f":ticket: New ticket from {requester_name or g.user_email}: *{title}*\n{description[:200]}"
    )

    # AI auto-analysis via shared helper
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
            # Notify Slack/Teams on status changes
            if new_status in ("resolved", "closed"):
                notify_integrations(
                    g.tenant_id,
                    f":white_check_mark: Ticket *{ticket.get('title', ticket_id)}* marked {new_status} by {g.user_email}"
                )
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
    """Re-run AI analysis on an existing ticket."""
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
    Queue the AI-suggested AD action for this ticket.
    Returns a command_id immediately — frontend polls /dashboard/api/result/<id>
    and calls /dashboard/api/tickets/<id>/apply-fix/complete when done.
    """
    ticket = db.get_ticket(ticket_id, g.tenant_id)
    if not ticket:
        return jsonify({"success": False, "message": "Ticket not found."}), 404
    if not ticket.get("janus_action"):
        return jsonify({"success": False, "message": "No AI suggestion available."}), 400

    data = request.get_json(silent=True) or {}
    action  = ticket["janus_action"]
    args    = json.loads(ticket.get("janus_action_args") or "[]")
    policy_ok, policy_reason = action_policy.validate(action, source="human")
    if not policy_ok:
        return jsonify({"success": False, "message": policy_reason}), 403

    if action == "run_custom_script":
        block = _paid_feature_block(g.tenant_id, "custom_scripts_limit", "Custom scripts")
        if block:
            return jsonify({"success": False, "message": block}), 403

    if action in DESTRUCTIVE_ACTIONS:
        confirm_token = str(data.get("confirm_token", "")).strip()
        if not confirm_token:
            code = _issue_confirm_token(g.tenant_id, action, args)
            return jsonify({
                "success": False,
                "requires_confirmation": True,
                "confirm_token": code,
                "action_label": f"Apply ticket fix: {action} {args}",
                "message": "This ticket fix requires confirmation.",
            }), 202
        pending = _consume_confirm_token(g.tenant_id, confirm_token)
        if not pending or pending.get("action") != action:
            return jsonify({"success": False, "message": "Invalid or expired confirmation code."}), 403
        args = pending["args"]

    queue_args = _queue_args_with_ad_budget(g.tenant_id, action, args)

    if _is_ad_mutation(action):
        limit_block = _ad_usage_limit_block(g.tenant_id, action, args)
        if limit_block:
            return jsonify({
                "success": False,
                "limit_reached": True,
                "message": limit_block["message"],
            }), 429
        db.log_audit(g.tenant_id, g.user_email, action, args[0] if args else "", "queued")
        _charge_ad_usage(g.tenant_id, action, args)

    command = db.queue_command(g.tenant_id, action, queue_args)

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
    """Return an AI health summary of recent AD stats + tickets."""
    try:
        import anthropic
        api_key  = os.getenv("ANTHROPIC_API_KEY", "")
        settings = db.get_settings(g.tenant_id)
        ai_name  = _get_ai_name(settings)
        if not api_key or not settings.get("janus_enabled", True):
            return jsonify({"success": True, "data": {"message": None}})
        plan, limits = _tenant_plan_limits(g.tenant_id)
        usage = db.get_usage(g.tenant_id) or {}
        cap = limits.get("janus_calls")
        if cap is not None and usage.get("janus_calls", 0) >= cap:
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
        db.increment_usage(g.tenant_id, "janus_calls")
        return jsonify({"success": True, "data": {"message": message}})
    except Exception as e:
        return jsonify({"success": True, "data": {"message": None}})


@app.route("/dashboard/api/audit")
@require_dashboard_user
def dashboard_audit():
    """Return recent audit log entries for this tenant."""
    log = db.get_audit_log(g.tenant_id, limit=100)
    return jsonify({"success": True, "data": log})


def _csv_safe(value) -> str:
    """
    Neutralise CSV / formula injection.

    The audit log is the product's compliance evidence trail and customers
    open it in Excel / Google Sheets. A cell whose value begins with =, +, -,
    @, tab or CR is interpreted as a formula on open — and the `target` field
    can contain attacker-influenced data (e.g. a crafted username). Prefixing
    such values with a single quote forces the cell to be treated as text
    without altering the visible content. (OWASP CSV Injection mitigation.)
    """
    s = "" if value is None else str(value)
    if s and s[0] in ("=", "+", "-", "@", "\t", "\r"):
        return "'" + s
    return s


@app.route("/dashboard/api/audit/export")
@require_dashboard_user
def dashboard_audit_export():
    """Download the full audit log as a CSV file."""
    import csv, io, re, datetime as _dt
    from flask import Response
    log = db.get_audit_log(g.tenant_id, limit=10000)
    output = io.StringIO()
    fieldnames = ["created_at", "user_email", "action", "target", "status"]
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in log:
        writer.writerow({k: _csv_safe(row.get(k, "")) for k in fieldnames})

    # Dated, filesystem-safe filename so repeated exports don't overwrite and
    # an unusual tenant name can't produce a broken/empty filename.
    slug  = re.sub(r"[^a-z0-9]+", "-", (g.tenant_name or "").lower()).strip("-") or "tenant"
    stamp = _dt.date.today().isoformat()
    filename = f"audit-log-{slug}-{stamp}.csv"
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


@app.route("/admin/feedback", methods=["GET"])
@require_admin
def admin_list_feedback():
    """Return recent feedback submissions (newest first) as JSON."""
    try:
        limit = int(request.args.get("limit", 100))
    except (TypeError, ValueError):
        limit = 100
    limit = max(1, min(limit, 1000))
    return jsonify({"success": True, "data": db.list_feedback(limit)})


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


@app.route("/admin/flush-queue", methods=["POST"])
@require_admin
def admin_flush_queue():
    """
    Cancel all pending commands for a tenant (or all tenants if no tenant_id given).
    Useful for clearing a clogged queue after connectivity issues.
    Body (optional): { "tenant_id": "..." }
    """
    data      = request.get_json() or {}
    tenant_id = data.get("tenant_id", "").strip() or None
    count     = db.flush_pending_commands(tenant_id)
    scope     = f"tenant {tenant_id}" if tenant_id else "all tenants"
    return jsonify({"success": True, "message": f"Cancelled {count} pending command(s) for {scope}."})


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


@app.route("/agent/capabilities", methods=["POST"])
@require_tenant
def agent_capabilities():
    """Agent calls this on startup to report which service modules and
    detected Windows roles it can serve (e.g. ["ad", "dns", "dhcp"]).
    Stored per-tenant so the dashboard can grey out tabs the agent can't
    back yet."""
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"success": False, "message": "JSON body required."}), 400
    caps = data.get("capabilities")
    if not isinstance(caps, list):
        return jsonify({"success": False, "message": "'capabilities' must be a list."}), 400
    if len(caps) > 20:
        return jsonify({"success": False, "message": "Too many capabilities (max 20)."}), 400
    clean = []
    for c in caps:
        if not isinstance(c, str):
            return jsonify({"success": False, "message": "Each capability must be a string."}), 400
        c = c.strip()
        if not c or len(c) > 32 or not re.match(r"^[A-Za-z0-9_]+$", c):
            return jsonify({"success": False, "message": f"Invalid capability name: {c!r}"}), 400
        clean.append(c)
    db.set_tenant_capabilities(g.tenant["id"], clean)
    return jsonify({"ok": True})


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

    command = db.get_command(command_id, g.tenant["id"])
    db.store_result(command_id, g.tenant["id"], success, message, result_data)
    extra_units = _reconcile_bulk_ad_usage(g.tenant["id"], command, success, result_data)
    msg = "Result stored."
    if extra_units:
        msg = f"Result stored. Reconciled {_usage_units_label(extra_units)} for bulk usage."
    return jsonify({"success": True, "message": msg})


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
    if action not in action_policy.ALL_ACTIONS:
        return jsonify({"success": False, "message": f"'{action}' is not a recognised action."}), 400
    ok, reason = action_policy.validate(action, source="human")
    if not ok:
        return jsonify({"success": False, "message": reason}), 403
    if action == "run_custom_script":
        block = _paid_feature_block(g.tenant["id"], "custom_scripts_limit", "Custom scripts")
        if block:
            return jsonify({"success": False, "message": block}), 403
    if action in DESTRUCTIVE_ACTIONS:
        supplied = str(data.get("confirm_token", "")).strip()
        if not supplied:
            code = _issue_confirm_token(g.tenant["id"], action, args)
            return jsonify({
                "success": False,
                "confirmation_required": True,
                "confirm_token": code,
                "message": f"'{action}' requires confirmation.",
            }), 409
        entry = _consume_confirm_token(g.tenant["id"], supplied)
        if not entry or entry.get("action") != action:
            return jsonify({"success": False, "message": "Invalid or expired confirm_token."}), 403
        args = entry["args"]
    is_mutation = _is_ad_mutation(action)
    queue_args = _queue_args_with_ad_budget(g.tenant["id"], action, args)
    if is_mutation:
        limit_block = _ad_usage_limit_block(g.tenant["id"], action, args)
        if limit_block:
            return jsonify({
                "success": False,
                "limit_reached": True,
                "message": limit_block["message"],
            }), 429
        db.log_audit(g.tenant["id"], "legacy-api", action, args[0] if args else "", "queued")
        _charge_ad_usage(g.tenant["id"], action, args)
    command = db.queue_command(g.tenant["id"], action, queue_args)
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


# ---------------------------------------------------------------------------
# Scheduled reports -- background thread, checks every 30 minutes
# Sends HTML digest emails to tenants who have reports configured.
# ---------------------------------------------------------------------------

def _build_report_html(tenant_name: str, stats: dict, locked: list, expired: list,
                       audit: list, period_label: str, ai_name: str = DEFAULT_AI_NAME) -> tuple[str, str]:
    """Build (subject, html_body) for a scheduled report."""
    total   = stats.get("total", 0)
    n_lock  = stats.get("locked", 0)
    n_exp   = stats.get("expired", 0)

    locked_rows = "".join(
        f"<tr><td>{u.get('Name','')}</td><td>{u.get('SamAccountName','')}</td></tr>"
        for u in (locked or [])[:20]
    ) or "<tr><td colspan='2' style='color:#64748b'>None</td></tr>"

    expired_rows = "".join(
        f"<tr><td>{u.get('Name','')}</td><td>{u.get('SamAccountName','')}</td></tr>"
        for u in (expired or [])[:20]
    ) or "<tr><td colspan='2' style='color:#64748b'>None</td></tr>"

    recent_actions = "".join(
        f"<tr><td>{a.get('created_at','')[:16]}</td><td>{a.get('action','')}</td>"
        f"<td>{a.get('target','')}</td><td>{a.get('user_email','')}</td></tr>"
        for a in (audit or [])[:15]
    ) or "<tr><td colspan='4' style='color:#64748b'>No recent activity</td></tr>"

    subject = f"AID Helpdesk {period_label} Report - {tenant_name}"

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:system-ui,sans-serif;max-width:620px;margin:0 auto;color:#1a1a2e;padding:20px;">
  <div style="background:#1a1830;border-radius:12px;padding:16px 24px;margin-bottom:24px;">
    <span style="color:#818cf8;font-size:20px;font-weight:700;">AID</span>
    <span style="color:#e2e8f0;font-size:20px;"> Helpdesk</span>
    <span style="color:#64748b;font-size:14px;margin-left:12px;">{period_label} Report</span>
  </div>

  <h2 style="font-size:16px;margin:0 0 16px;">Domain overview - {tenant_name}</h2>
  <div style="display:flex;gap:16px;margin-bottom:24px;">
    <div style="flex:1;background:#f8fafc;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:28px;font-weight:700;">{total}</div>
      <div style="font-size:12px;color:#64748b;">Total Users</div>
    </div>
    <div style="flex:1;background:#fef2f2;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:28px;font-weight:700;color:#ef4444;">{n_lock}</div>
      <div style="font-size:12px;color:#64748b;">Locked Out</div>
    </div>
    <div style="flex:1;background:#fffbeb;border-radius:8px;padding:12px;text-align:center;">
      <div style="font-size:28px;font-weight:700;color:#f59e0b;">{n_exp}</div>
      <div style="font-size:12px;color:#64748b;">Pwd Expired</div>
    </div>
  </div>

  <h3 style="font-size:14px;margin:0 0 8px;">Locked accounts</h3>
  <table width="100%" cellpadding="6" style="border-collapse:collapse;margin-bottom:20px;font-size:13px;">
    <tr style="background:#f1f5f9;"><th align="left">Name</th><th align="left">Username</th></tr>
    {locked_rows}
  </table>

  <h3 style="font-size:14px;margin:0 0 8px;">Expired passwords</h3>
  <table width="100%" cellpadding="6" style="border-collapse:collapse;margin-bottom:20px;font-size:13px;">
    <tr style="background:#f1f5f9;"><th align="left">Name</th><th align="left">Username</th></tr>
    {expired_rows}
  </table>

  <h3 style="font-size:14px;margin:0 0 8px;">Recent AI activity</h3>
  <table width="100%" cellpadding="6" style="border-collapse:collapse;margin-bottom:24px;font-size:12px;">
    <tr style="background:#f1f5f9;">
      <th align="left">Time</th><th align="left">Action</th>
      <th align="left">Target</th><th align="left">By</th>
    </tr>
    {recent_actions}
  </table>

  <hr style="border:none;border-top:1px solid #e2e8f0;margin:20px 0;">
  <p style="color:#64748b;font-size:11px;">
    Powered by {ai_name} - AID Helpdesk | This report was sent automatically.
  </p>
</body>
</html>"""
    return subject, html


def _should_send_report(settings: dict) -> bool:
    """Determine if a scheduled report is due for this tenant right now."""
    from datetime import datetime, timedelta
    if not settings.get("report_enabled"):
        return False
    recipients = settings.get("report_recipients", "")
    if not recipients:
        return False
    freq = settings.get("report_frequency", "weekly")
    try:
        target_hour = int(settings.get("report_hour", 8))
    except (ValueError, TypeError):
        target_hour = 8

    now = datetime.utcnow()
    # Only fire in the target hour window
    if now.hour != target_hour:
        return False

    last_sent_str = settings.get("last_report_sent", "")
    if last_sent_str:
        try:
            last_sent = datetime.fromisoformat(last_sent_str)
            # Don't fire again within the same day (prevents double-sends)
            if (now - last_sent).total_seconds() < 23 * 3600:
                return False
        except ValueError:
            pass

    if freq == "daily":
        return True
    if freq == "weekly":
        try:
            target_day = int(settings.get("report_day", 0))   # 0=Monday
        except (ValueError, TypeError):
            target_day = 0
        return now.weekday() == target_day

    return False


def _run_scheduled_reports():
    """Check all tenants and send due reports. Runs in a background thread."""
    with app.app_context():
        try:
            tenants = db.list_all_tenants()
        except Exception:
            return
        for tenant in tenants:
            try:
                tid      = tenant["id"]
                settings = db.get_settings(tid)
                _, limits = _tenant_plan_limits(tid)
                if not limits.get("scheduled_reports"):
                    continue
                if not _should_send_report(settings):
                    continue

                smtp = _resolve_smtp(settings)
                if not smtp["host"] or not smtp["user"]:
                    continue   # SMTP not configured for this tenant

                recipients = [r.strip() for r in settings.get("report_recipients", "").split(",") if r.strip()]
                if not recipients:
                    continue

                # Fetch domain data via the command queue (requires agent to be online)
                # For the report we use cached audit data + activity log directly from DB
                # (no live AD call needed — avoids requiring agent to be running at report time)
                audit    = db.get_audit_log(tid, limit=15)
                period   = "Weekly" if settings.get("report_frequency", "weekly") == "weekly" else "Daily"

                # Build placeholder stats from audit log counts (no live AD required)
                action_counts = db.get_action_counts_this_month(tid)
                stats = {
                    "total":   "—",
                    "locked":  action_counts.get("unlock_account", 0),
                    "expired": action_counts.get("reset_password", 0),
                }

                subject, html_body = _build_report_html(
                    tenant.get("name", tid), stats, [], [], audit, period, _get_ai_name(settings))

                for recipient in recipients:
                    send_email(recipient, subject, subject, html_body, settings)

                # Mark as sent
                settings["last_report_sent"] = __import__("datetime").datetime.utcnow().isoformat()
                db.update_settings(tid, settings)

            except Exception as e:
                print(f"[reports] Error for tenant {tenant.get('id')}: {e}")


def _start_report_scheduler():
    """Launch background thread that checks for due reports every 30 minutes."""
    import threading

    def _loop():
        import time as _time
        while True:
            try:
                _run_scheduled_reports()
            except Exception as e:
                print(f"[reports] Scheduler error: {e}")
            _time.sleep(30 * 60)   # check every 30 minutes

    t = threading.Thread(target=_loop, daemon=True, name="report-scheduler")
    t.start()
    print("[reports] Scheduled report checker started (30 min interval)")


# Start the report scheduler once (works under gunicorn and direct invocation)
_start_report_scheduler()


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    print("\n AD Helpdesk -- Cloud Backend + Dashboard")
    print(" ------------------------------------------")
    print(f" Listening on port {port}")
    print(f" Dashboard: http://localhost:{port}/")
    print(" Press Ctrl+C to stop\n")
    app.run(host="0.0.0.0", port=port, debug=False)
