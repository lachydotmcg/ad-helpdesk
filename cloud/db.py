"""
cloud/db.py -- Database layer for AD Helpdesk cloud backend.

Supports both SQLite (local dev) and PostgreSQL (Railway/production).
Set DATABASE_URL env var to use PostgreSQL; otherwise falls back to SQLite.

Tables:
  tenants        -- one row per customer (name, api_key)
  tenant_users   -- dashboard login accounts per tenant (email, password_hash, role)
  commands       -- queued AD operations
  results        -- completed operation results
  audit_log      -- every write operation with who did it
  chat_sessions  -- Janus AI conversation sessions
  chat_messages  -- individual messages in a session
  tickets        -- helpdesk tickets
  ticket_actions -- notes/actions on tickets
  usage          -- monthly usage counters
  tenant_settings-- per-tenant config JSON blob
  activity_log   -- chronological feed of all events
"""

import sqlite3
import uuid
import json
import os
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "adhelpdesk.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")
_USE_PG = bool(DATABASE_URL)

if _USE_PG:
    import psycopg2
    import psycopg2.extras


def _get_conn():
    """Return a database connection. PostgreSQL if DATABASE_URL is set, else SQLite."""
    if _USE_PG:
        return psycopg2.connect(DATABASE_URL)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def _cur(conn):
    """Return a dict-row cursor for the given connection."""
    if _USE_PG:
        return conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    return conn.cursor()


_PH = "%s" if _USE_PG else "?"


def _row(row):
    """Convert a db row to a plain dict, or None."""
    return dict(row) if row else None


def _rows(rows):
    """Convert a list of db rows to plain dicts."""
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------

_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS tenants (
        id          TEXT PRIMARY KEY,
        name        TEXT NOT NULL,
        api_key     TEXT NOT NULL UNIQUE,
        created_at  TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS tenant_users (
        id            TEXT PRIMARY KEY,
        tenant_id     TEXT NOT NULL,
        email         TEXT NOT NULL,
        password_hash TEXT NOT NULL,
        role          TEXT NOT NULL DEFAULT 'admin',
        created_at    TEXT NOT NULL,
        UNIQUE(tenant_id, email),
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS commands (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        action      TEXT NOT NULL,
        args        TEXT NOT NULL,
        status      TEXT NOT NULL DEFAULT 'pending',
        created_at  TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS results (
        id          TEXT PRIMARY KEY,
        command_id  TEXT NOT NULL UNIQUE,
        tenant_id   TEXT NOT NULL,
        success     INTEGER NOT NULL,
        message     TEXT,
        data        TEXT,
        created_at  TEXT NOT NULL,
        FOREIGN KEY (command_id) REFERENCES commands(id)
    )""",
    """CREATE TABLE IF NOT EXISTS audit_log (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        user_email TEXT NOT NULL,
        action     TEXT NOT NULL,
        target     TEXT,
        status     TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS chat_sessions (
        id         TEXT PRIMARY KEY,
        tenant_id  TEXT NOT NULL,
        user_email TEXT NOT NULL,
        title      TEXT NOT NULL DEFAULT 'New conversation',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS chat_messages (
        id         TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        tenant_id  TEXT NOT NULL,
        role       TEXT NOT NULL,
        content    TEXT NOT NULL,
        action     TEXT,
        created_at TEXT NOT NULL,
        FOREIGN KEY (session_id) REFERENCES chat_sessions(id)
    )""",
    """CREATE TABLE IF NOT EXISTS tickets (
        id                TEXT PRIMARY KEY,
        tenant_id         TEXT NOT NULL,
        title             TEXT NOT NULL,
        description       TEXT NOT NULL,
        status            TEXT NOT NULL DEFAULT 'open',
        priority          TEXT NOT NULL DEFAULT 'medium',
        requester_name    TEXT,
        requester_email   TEXT,
        assigned_to       TEXT,
        source            TEXT NOT NULL DEFAULT 'manual',
        janus_analysis    TEXT,
        janus_action      TEXT,
        janus_action_args TEXT,
        resolved_at       TEXT,
        created_by        TEXT NOT NULL,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS ticket_actions (
        id         TEXT PRIMARY KEY,
        ticket_id  TEXT NOT NULL,
        tenant_id  TEXT NOT NULL,
        type       TEXT NOT NULL,
        content    TEXT NOT NULL,
        user_email TEXT NOT NULL,
        created_at TEXT NOT NULL,
        FOREIGN KEY (ticket_id) REFERENCES tickets(id)
    )""",
    """CREATE TABLE IF NOT EXISTS usage (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        month       TEXT NOT NULL,
        janus_calls INTEGER NOT NULL DEFAULT 0,
        updated_at  TEXT NOT NULL,
        UNIQUE(tenant_id, month),
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS tenant_settings (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL UNIQUE,
        settings    TEXT NOT NULL DEFAULT '{}',
        updated_at  TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS activity_log (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        event_type  TEXT NOT NULL,
        actor       TEXT NOT NULL,
        target      TEXT,
        detail      TEXT,
        created_at  TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
]


def init_db():
    """Create tables if they don't exist."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        for stmt in _SCHEMA:
            cur.execute(stmt)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tenant helpers
# ---------------------------------------------------------------------------

def create_tenant(name: str) -> dict:
    tenant_id = str(uuid.uuid4())
    api_key   = str(uuid.uuid4()).replace("-", "")
    now       = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO tenants (id, name, api_key, created_at) VALUES ({_PH}, {_PH}, {_PH}, {_PH})",
            (tenant_id, name, api_key, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": tenant_id, "name": name, "api_key": api_key}


def get_tenant_by_key(api_key: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(f"SELECT * FROM tenants WHERE api_key = {_PH}", (api_key,))
        return _row(cur.fetchone())
    finally:
        conn.close()


def get_tenant_by_id(tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(f"SELECT * FROM tenants WHERE id = {_PH}", (tenant_id,))
        return _row(cur.fetchone())
    finally:
        conn.close()


def list_tenants() -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute("SELECT id, name, api_key, created_at FROM tenants")
        return _rows(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tenant user helpers (dashboard logins)
# ---------------------------------------------------------------------------

def create_tenant_user(tenant_id: str, email: str, password: str, role: str = "admin") -> dict:
    user_id = str(uuid.uuid4())
    pw_hash = generate_password_hash(password)
    now     = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO tenant_users (id, tenant_id, email, password_hash, role, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (user_id, tenant_id, email.lower().strip(), pw_hash, role, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": user_id, "tenant_id": tenant_id, "email": email, "role": role}


def verify_tenant_user(email: str, password: str) -> dict | None:
    """Check email + password across all tenants. Returns user + tenant info or None."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""SELECT u.*, t.name as tenant_name, t.api_key
               FROM tenant_users u
               JOIN tenants t ON t.id = u.tenant_id
               WHERE u.email = {_PH}""",
            (email.lower().strip(),)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    if not row:
        return None
    if not check_password_hash(row["password_hash"], password):
        return None
    return row


def list_tenant_users(tenant_id: str) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT id, email, role, created_at FROM tenant_users WHERE tenant_id = {_PH}",
            (tenant_id,)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def tenant_has_users(tenant_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT 1 FROM tenant_users WHERE tenant_id = {_PH} LIMIT 1", (tenant_id,)
        )
        return cur.fetchone() is not None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def queue_command(tenant_id: str, action: str, args: list) -> dict:
    cmd_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO commands (id, tenant_id, action, args, status, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, 'pending', {_PH})",
            (cmd_id, tenant_id, action, json.dumps(args), now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": cmd_id, "action": action, "args": args}


def get_pending_command(tenant_id: str) -> dict | None:
    """Return the oldest pending command for this tenant and mark it as running."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM commands WHERE tenant_id = {_PH} AND status = 'pending' "
            f"ORDER BY created_at ASC LIMIT 1",
            (tenant_id,)
        )
        row = _row(cur.fetchone())
        if not row:
            return None
        cur.execute(
            f"UPDATE commands SET status = 'running' WHERE id = {_PH}", (row["id"],)
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "id":     row["id"],
        "action": row["action"],
        "args":   json.loads(row["args"]),
    }


def get_command_result(command_id: str, tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM results WHERE command_id = {_PH} AND tenant_id = {_PH}",
            (command_id, tenant_id)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
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
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO results (id, command_id, tenant_id, success, message, data, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (result_id, command_id, tenant_id, int(success), message, json.dumps(data), now)
        )
        cur.execute(
            f"UPDATE commands SET status = 'completed' WHERE id = {_PH}", (command_id,)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Audit log helpers
# ---------------------------------------------------------------------------

def log_audit(tenant_id: str, user_email: str, action: str, target: str, status: str) -> None:
    log_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO audit_log (id, tenant_id, user_email, action, target, status, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (log_id, tenant_id, user_email, action, target, status, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_audit_log(tenant_id: str, limit: int = 50) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM audit_log WHERE tenant_id = {_PH} ORDER BY created_at DESC LIMIT {_PH}",
            (tenant_id, limit)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Chat session helpers
# ---------------------------------------------------------------------------

def create_chat_session(tenant_id: str, user_email: str, title: str = "New conversation") -> dict:
    session_id = str(uuid.uuid4())
    now        = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO chat_sessions (id, tenant_id, user_email, title, created_at, updated_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (session_id, tenant_id, user_email, title, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": session_id, "title": title, "created_at": now}


def update_chat_session_title(session_id: str, title: str) -> None:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE chat_sessions SET title = {_PH}, updated_at = {_PH} WHERE id = {_PH}",
            (title[:80], now, session_id)
        )
        conn.commit()
    finally:
        conn.close()


def touch_chat_session(session_id: str) -> None:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE chat_sessions SET updated_at = {_PH} WHERE id = {_PH}", (now, session_id)
        )
        conn.commit()
    finally:
        conn.close()


def list_chat_sessions(tenant_id: str, limit: int = 30) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM chat_sessions WHERE tenant_id = {_PH} ORDER BY updated_at DESC LIMIT {_PH}",
            (tenant_id, limit)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def add_chat_message(session_id: str, tenant_id: str, role: str, content: str, action: str = None) -> dict:
    msg_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO chat_messages (id, session_id, tenant_id, role, content, action, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (msg_id, session_id, tenant_id, role, content, action, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": msg_id, "role": role, "content": content, "action": action, "created_at": now}


def get_chat_messages(session_id: str, tenant_id: str) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM chat_messages WHERE session_id = {_PH} AND tenant_id = {_PH} ORDER BY created_at ASC",
            (session_id, tenant_id)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def get_chat_session(session_id: str, tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM chat_sessions WHERE id = {_PH} AND tenant_id = {_PH}",
            (session_id, tenant_id)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Ticket helpers
# ---------------------------------------------------------------------------

def create_ticket(tenant_id: str, created_by: str, title: str, description: str,
                  priority: str = "medium", requester_name: str = None,
                  requester_email: str = None, source: str = "manual") -> dict:
    ticket_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""INSERT INTO tickets
               (id, tenant_id, title, description, status, priority, requester_name,
                requester_email, source, created_by, created_at, updated_at)
               VALUES ({_PH}, {_PH}, {_PH}, {_PH}, 'open', {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})""",
            (ticket_id, tenant_id, title, description, priority,
             requester_name, requester_email, source, created_by, now, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": ticket_id, "title": title, "status": "open", "priority": priority, "created_at": now}


def get_ticket(ticket_id: str, tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM tickets WHERE id = {_PH} AND tenant_id = {_PH}", (ticket_id, tenant_id)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def list_tickets(tenant_id: str, status: str = None, limit: int = 100) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        if status:
            cur.execute(
                f"SELECT * FROM tickets WHERE tenant_id = {_PH} AND status = {_PH} "
                f"ORDER BY created_at DESC LIMIT {_PH}",
                (tenant_id, status, limit)
            )
        else:
            cur.execute(
                f"SELECT * FROM tickets WHERE tenant_id = {_PH} ORDER BY created_at DESC LIMIT {_PH}",
                (tenant_id, limit)
            )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def update_ticket(ticket_id: str, tenant_id: str, **fields) -> None:
    now     = datetime.utcnow().isoformat()
    allowed = {"status", "priority", "assigned_to", "janus_analysis",
               "janus_action", "janus_action_args", "resolved_at"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    updates["updated_at"] = now
    if "status" in updates and updates["status"] == "resolved":
        updates["resolved_at"] = now
    cols = ", ".join(f"{k} = {_PH}" for k in updates)
    vals = list(updates.values()) + [ticket_id, tenant_id]
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(f"UPDATE tickets SET {cols} WHERE id = {_PH} AND tenant_id = {_PH}", vals)
        conn.commit()
    finally:
        conn.close()


def add_ticket_action(ticket_id: str, tenant_id: str, action_type: str,
                      content: str, user_email: str) -> dict:
    action_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO ticket_actions (id, ticket_id, tenant_id, type, content, user_email, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (action_id, ticket_id, tenant_id, action_type, content, user_email, now)
        )
        cur.execute(f"UPDATE tickets SET updated_at = {_PH} WHERE id = {_PH}", (now, ticket_id))
        conn.commit()
    finally:
        conn.close()
    return {"id": action_id, "type": action_type, "content": content, "created_at": now}


def get_ticket_actions(ticket_id: str, tenant_id: str) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM ticket_actions WHERE ticket_id = {_PH} AND tenant_id = {_PH} ORDER BY created_at ASC",
            (ticket_id, tenant_id)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Usage tracking
# ---------------------------------------------------------------------------

def increment_usage(tenant_id: str, metric: str = "janus_calls") -> None:
    """Increment a usage counter for the current month."""
    month = datetime.utcnow().strftime("%Y-%m")
    now   = datetime.utcnow().isoformat()
    uid   = str(uuid.uuid4())
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO usage (id, tenant_id, month, janus_calls, updated_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, 0, {_PH}) ON CONFLICT(tenant_id, month) DO NOTHING",
            (uid, tenant_id, month, now)
        )
        cur.execute(
            f"UPDATE usage SET {metric} = {metric} + 1, updated_at = {_PH} "
            f"WHERE tenant_id = {_PH} AND month = {_PH}",
            (now, tenant_id, month)
        )
        conn.commit()
    finally:
        conn.close()


def get_usage(tenant_id: str, month: str = None) -> dict | None:
    if not month:
        month = datetime.utcnow().strftime("%Y-%m")
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM usage WHERE tenant_id = {_PH} AND month = {_PH}", (tenant_id, month)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    return row or {"tenant_id": tenant_id, "month": month, "janus_calls": 0}


def get_usage_history(tenant_id: str, months: int = 6) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM usage WHERE tenant_id = {_PH} ORDER BY month DESC LIMIT {_PH}",
            (tenant_id, months)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tenant settings
# ---------------------------------------------------------------------------

_SETTINGS_DEFAULTS = {
    "janus_enabled":      True,
    "janus_scan_emails":  True,
    "janus_auto_actions": [],
    "security_checks":    True,
    "email_domain":       "",
    "roles":              [],
}


def get_settings(tenant_id: str) -> dict:
    """Return tenant settings merged with defaults."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT settings FROM tenant_settings WHERE tenant_id = {_PH}", (tenant_id,)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
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
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""INSERT INTO tenant_settings (id, tenant_id, settings, updated_at)
               VALUES ({_PH}, {_PH}, {_PH}, {_PH})
               ON CONFLICT(tenant_id) DO UPDATE
               SET settings = excluded.settings, updated_at = excluded.updated_at""",
            (uid, tenant_id, json.dumps(settings), now)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Activity log
# ---------------------------------------------------------------------------

def log_activity(tenant_id: str, event_type: str, actor: str,
                 target: str = None, detail: str = None) -> None:
    log_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO activity_log (id, tenant_id, event_type, actor, target, detail, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (log_id, tenant_id, event_type, actor, target, detail, now)
        )
        conn.commit()
    finally:
        conn.close()


def get_first_tenant_id() -> str | None:
    """Return the id of the first tenant created. Used by email webhook routing."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute("SELECT id FROM tenants ORDER BY created_at ASC LIMIT 1")
        row = _row(cur.fetchone())
        return row["id"] if row else None
    finally:
        conn.close()


def get_activity_feed(tenant_id: str, limit: int = 100) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM activity_log WHERE tenant_id = {_PH} ORDER BY created_at DESC LIMIT {_PH}",
            (tenant_id, limit)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()
