"""
cloud/db.py -- SQLite database layer for AD Helpdesk cloud backend.

Tables:
  tenants       -- one row per customer (name, api_key)
  tenant_users  -- dashboard login accounts per tenant (email, password_hash, role)
  commands      -- queued AD operations
  results       -- completed operation results
  audit_log     -- every write operation with who did it
"""

import sqlite3
import uuid
import json
import os
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "adhelpdesk.db")


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """Create tables if they don't exist."""
    with get_conn() as conn:
        conn.executescript("""
        CREATE TABLE IF NOT EXISTS tenants (
            id          TEXT PRIMARY KEY,
            name        TEXT NOT NULL,
            api_key     TEXT NOT NULL UNIQUE,
            created_at  TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS tenant_users (
            id            TEXT PRIMARY KEY,
            tenant_id     TEXT NOT NULL,
            email         TEXT NOT NULL,
            password_hash TEXT NOT NULL,
            role          TEXT NOT NULL DEFAULT 'admin',
            created_at    TEXT NOT NULL,
            UNIQUE(tenant_id, email),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS commands (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            action      TEXT NOT NULL,
            args        TEXT NOT NULL,
            status      TEXT NOT NULL DEFAULT 'pending',
            created_at  TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS results (
            id          TEXT PRIMARY KEY,
            command_id  TEXT NOT NULL UNIQUE,
            tenant_id   TEXT NOT NULL,
            success     INTEGER NOT NULL,
            message     TEXT,
            data        TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (command_id) REFERENCES commands(id)
        );

        CREATE TABLE IF NOT EXISTS audit_log (
            id         TEXT PRIMARY KEY,
            tenant_id  TEXT NOT NULL,
            user_email TEXT NOT NULL,
            action     TEXT NOT NULL,
            target     TEXT,
            status     TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS chat_sessions (
            id         TEXT PRIMARY KEY,
            tenant_id  TEXT NOT NULL,
            user_email TEXT NOT NULL,
            title      TEXT NOT NULL DEFAULT 'New conversation',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS chat_messages (
            id         TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            tenant_id  TEXT NOT NULL,
            role       TEXT NOT NULL,
            content    TEXT NOT NULL,
            action     TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
        );

        CREATE TABLE IF NOT EXISTS tickets (
            id                 TEXT PRIMARY KEY,
            tenant_id          TEXT NOT NULL,
            title              TEXT NOT NULL,
            description        TEXT NOT NULL,
            status             TEXT NOT NULL DEFAULT 'open',
            priority           TEXT NOT NULL DEFAULT 'medium',
            requester_name     TEXT,
            requester_email    TEXT,
            assigned_to        TEXT,
            source             TEXT NOT NULL DEFAULT 'manual',
            janus_analysis     TEXT,
            janus_action       TEXT,
            janus_action_args  TEXT,
            resolved_at        TEXT,
            created_by         TEXT NOT NULL,
            created_at         TEXT NOT NULL,
            updated_at         TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS ticket_actions (
            id         TEXT PRIMARY KEY,
            ticket_id  TEXT NOT NULL,
            tenant_id  TEXT NOT NULL,
            type       TEXT NOT NULL,
            content    TEXT NOT NULL,
            user_email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            FOREIGN KEY (ticket_id) REFERENCES tickets(id)
        );

        CREATE TABLE IF NOT EXISTS usage (
            id         TEXT PRIMARY KEY,
            tenant_id  TEXT NOT NULL,
            month      TEXT NOT NULL,
            janus_calls INTEGER NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            UNIQUE(tenant_id, month),
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS tenant_settings (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL UNIQUE,
            settings    TEXT NOT NULL DEFAULT '{}',
            updated_at  TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );

        CREATE TABLE IF NOT EXISTS activity_log (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            event_type  TEXT NOT NULL,
            actor       TEXT NOT NULL,
            target      TEXT,
            detail      TEXT,
            created_at  TEXT NOT NULL,
            FOREIGN KEY (tenant_id) REFERENCES tenants(id)
        );
        """)


# ---------------------------------------------------------------------------
# Tenant helpers
# ---------------------------------------------------------------------------

def create_tenant(name: str) -> dict:
    tenant_id = str(uuid.uuid4())
    api_key   = str(uuid.uuid4()).replace("-", "")
    now       = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tenants (id, name, api_key, created_at) VALUES (?, ?, ?, ?)",
            (tenant_id, name, api_key, now)
        )
    return {"id": tenant_id, "name": name, "api_key": api_key}


def get_tenant_by_key(api_key: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE api_key = ?", (api_key,)
        ).fetchone()
    return dict(row) if row else None


def get_tenant_by_id(tenant_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tenants WHERE id = ?", (tenant_id,)
        ).fetchone()
    return dict(row) if row else None


def list_tenants() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM tenants").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tenant user helpers (dashboard logins)
# ---------------------------------------------------------------------------

def create_tenant_user(tenant_id: str, email: str, password: str, role: str = "admin") -> dict:
    user_id = str(uuid.uuid4())
    pw_hash = generate_password_hash(password)
    now     = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO tenant_users (id, tenant_id, email, password_hash, role, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (user_id, tenant_id, email.lower().strip(), pw_hash, role, now)
        )
    return {"id": user_id, "tenant_id": tenant_id, "email": email, "role": role}


def verify_tenant_user(email: str, password: str) -> dict | None:
    """Check email + password across all tenants. Returns user + tenant info or None."""
    with get_conn() as conn:
        row = conn.execute(
            """SELECT u.*, t.name as tenant_name, t.api_key
               FROM tenant_users u
               JOIN tenants t ON t.id = u.tenant_id
               WHERE u.email = ?""",
            (email.lower().strip(),)
        ).fetchone()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return dict(row)


def list_tenant_users(tenant_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT id, email, role, created_at FROM tenant_users WHERE tenant_id = ?",
            (tenant_id,)
        ).fetchall()
    return [dict(r) for r in rows]


def tenant_has_users(tenant_id: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT 1 FROM tenant_users WHERE tenant_id = ? LIMIT 1", (tenant_id,)
        ).fetchone()
    return row is not None


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def queue_command(tenant_id: str, action: str, args: list) -> dict:
    cmd_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO commands (id, tenant_id, action, args, status, created_at) VALUES (?, ?, ?, ?, 'pending', ?)",
            (cmd_id, tenant_id, action, json.dumps(args), now)
        )
    return {"id": cmd_id, "action": action, "args": args}


def get_pending_command(tenant_id: str) -> dict | None:
    """Return the oldest pending command for this tenant and mark it as running."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM commands WHERE tenant_id = ? AND status = 'pending' ORDER BY created_at ASC LIMIT 1",
            (tenant_id,)
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE commands SET status = 'running' WHERE id = ?", (row["id"],)
        )
    return {
        "id":     row["id"],
        "action": row["action"],
        "args":   json.loads(row["args"]),
    }


def get_command_result(command_id: str, tenant_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM results WHERE command_id = ? AND tenant_id = ?",
            (command_id, tenant_id)
        ).fetchone()
    if not row:
        return None
    return {
        "command_id": row["command_id"],
        "success":    bool(row["success"]),
        "message":    row["message"],
        "data":       json.loads(row["data"]) if row["data"] else None,
    }


# ---------------------------------------------------------------------------
# Result helpers
# ---------------------------------------------------------------------------

def store_result(command_id: str, tenant_id: str, success: bool, message: str, data) -> None:
    result_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO results (id, command_id, tenant_id, success, message, data, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (result_id, command_id, tenant_id, int(success), message, json.dumps(data), now)
        )
        conn.execute(
            "UPDATE commands SET status = 'completed' WHERE id = ?", (command_id,)
        )


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------

def log_audit(tenant_id: str, user_email: str, action: str, target: str, status: str) -> None:
    log_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO audit_log (id, tenant_id, user_email, action, target, status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (log_id, tenant_id, user_email, action, target, status, now)
        )


def get_audit_log(tenant_id: str, limit: int = 50) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Chat session helpers
# ---------------------------------------------------------------------------

def create_chat_session(tenant_id: str, user_email: str, title: str = "New conversation") -> dict:
    session_id = str(uuid.uuid4())
    now        = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, tenant_id, user_email, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            (session_id, tenant_id, user_email, title, now, now)
        )
    return {"id": session_id, "title": title, "created_at": now}


def update_chat_session_title(session_id: str, title: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title[:80], now, session_id)
        )


def touch_chat_session(session_id: str) -> None:
    now = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute("UPDATE chat_sessions SET updated_at = ? WHERE id = ?", (now, session_id))


def list_chat_sessions(tenant_id: str, limit: int = 30) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_sessions WHERE tenant_id = ? ORDER BY updated_at DESC LIMIT ?",
            (tenant_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]


def add_chat_message(session_id: str, tenant_id: str, role: str, content: str, action: str = None) -> dict:
    msg_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, tenant_id, role, content, action, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, tenant_id, role, content, action, now)
        )
    return {"id": msg_id, "role": role, "content": content, "action": action, "created_at": now}


def get_chat_messages(session_id: str, tenant_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM chat_messages WHERE session_id = ? AND tenant_id = ? ORDER BY created_at ASC",
            (session_id, tenant_id)
        ).fetchall()
    return [dict(r) for r in rows]


def get_chat_session(session_id: str, tenant_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ? AND tenant_id = ?",
            (session_id, tenant_id)
        ).fetchone()
    return dict(row) if row else None


# ---------------------------------------------------------------------------
# Ticket helpers
# ---------------------------------------------------------------------------

def create_ticket(tenant_id: str, created_by: str, title: str, description: str,
                  priority: str = "medium", requester_name: str = None,
                  requester_email: str = None, source: str = "manual") -> dict:
    ticket_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tickets
               (id, tenant_id, title, description, status, priority, requester_name,
                requester_email, source, created_by, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?)""",
            (ticket_id, tenant_id, title, description, priority,
             requester_name, requester_email, source, created_by, now, now)
        )
    return {"id": ticket_id, "title": title, "status": "open", "priority": priority, "created_at": now}


def get_ticket(ticket_id: str, tenant_id: str) -> dict | None:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM tickets WHERE id = ? AND tenant_id = ?", (ticket_id, tenant_id)
        ).fetchone()
    return dict(row) if row else None


def list_tickets(tenant_id: str, status: str = None, limit: int = 100) -> list:
    with get_conn() as conn:
        if status:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE tenant_id = ? AND status = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, status, limit)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM tickets WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
                (tenant_id, limit)
            ).fetchall()
    return [dict(r) for r in rows]


def update_ticket(ticket_id: str, tenant_id: str, **fields) -> None:
    now     = datetime.utcnow().isoformat()
    allowed = {"status", "priority", "assigned_to", "janus_analysis",
               "janus_action", "janus_action_args", "resolved_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["updated_at"] = now
    if "status" in updates and updates["status"] == "resolved":
        updates["resolved_at"] = now
    cols = ", ".join(f"{k} = ?" for k in updates)
    vals = list(updates.values()) + [ticket_id, tenant_id]
    with get_conn() as conn:
        conn.execute(f"UPDATE tickets SET {cols} WHERE id = ? AND tenant_id = ?", vals)


def add_ticket_action(ticket_id: str, tenant_id: str, action_type: str,
                      content: str, user_email: str) -> dict:
    action_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO ticket_actions (id, ticket_id, tenant_id, type, content, user_email, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (action_id, ticket_id, tenant_id, action_type, content, user_email, now)
        )
        conn.execute("UPDATE tickets SET updated_at = ? WHERE id = ?", (now, ticket_id))
    return {"id": action_id, "type": action_type, "content": content, "created_at": now}


def get_ticket_actions(ticket_id: str, tenant_id: str) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM ticket_actions WHERE ticket_id = ? AND tenant_id = ? ORDER BY created_at ASC",
            (ticket_id, tenant_id)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

def increment_usage(tenant_id: str, metric: str = "janus_calls") -> None:
    """Increment a usage counter for the current month. Used for billing."""
    month = datetime.utcnow().strftime("%Y-%m")
    now   = datetime.utcnow().isoformat()
    uid   = str(uuid.uuid4())
    with get_conn() as conn:
        # Upsert: create row if not exists, then increment
        conn.execute(
            "INSERT INTO usage (id, tenant_id, month, janus_calls, updated_at) VALUES (?, ?, ?, 0, ?) ON CONFLICT(tenant_id, month) DO NOTHING",
            (uid, tenant_id, month, now)
        )
        conn.execute(
            f"UPDATE usage SET {metric} = {metric} + 1, updated_at = ? WHERE tenant_id = ? AND month = ?",
            (now, tenant_id, month)
        )


def get_usage(tenant_id: str, month: str = None) -> dict | None:
    if not month:
        month = datetime.utcnow().strftime("%Y-%m")
    with get_conn() as conn:
        row = conn.execute(
            "SELECT * FROM usage WHERE tenant_id = ? AND month = ?", (tenant_id, month)
        ).fetchone()
    return dict(row) if row else {"tenant_id": tenant_id, "month": month, "janus_calls": 0}


def get_usage_history(tenant_id: str, months: int = 6) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM usage WHERE tenant_id = ? ORDER BY month DESC LIMIT ?",
            (tenant_id, months)
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Tenant settings
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "janus_enabled":      True,
    "janus_scan_emails":  True,
    "janus_auto_actions": [],       # list of action names Janus can apply automatically
    "security_checks":    True,
    "email_domain":       "",       # e.g. "company.com" — trusted domain for security checks
    "roles":              [],       # list of requester role definitions
}


def get_settings(tenant_id: str) -> dict:
    """Return tenant settings merged with defaults."""
    with get_conn() as conn:
        row = conn.execute(
            "SELECT settings FROM tenant_settings WHERE tenant_id = ?", (tenant_id,)
        ).fetchone()
    if not row:
        return dict(_SETTINGS_DEFAULTS)
    try:
        saved = json.loads(row["settings"])
        return {**_SETTINGS_DEFAULTS, **saved}
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


def update_settings(tenant_id: str, settings: dict) -> None:
    """Upsert tenant settings."""
    now = datetime.utcnow().isoformat()
    uid = str(uuid.uuid4())
    with get_conn() as conn:
        conn.execute(
            """INSERT INTO tenant_settings (id, tenant_id, settings, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(tenant_id) DO UPDATE
               SET settings = excluded.settings, updated_at = excluded.updated_at""",
            (uid, tenant_id, json.dumps(settings), now)
        )


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def log_activity(tenant_id: str, event_type: str, actor: str,
                 target: str = None, detail: str = None) -> None:
    log_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO activity_log (id, tenant_id, event_type, actor, target, detail, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (log_id, tenant_id, event_type, actor, target, detail, now)
        )


def get_activity_feed(tenant_id: str, limit: int = 100) -> list:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM activity_log WHERE tenant_id = ? ORDER BY created_at DESC LIMIT ?",
            (tenant_id, limit)
        ).fetchall()
    return [dict(r) for r in rows]
