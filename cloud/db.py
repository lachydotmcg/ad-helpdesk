"""
cloud/db.py -- SQLite database layer for AD Helpdesk cloud backend.

Tables:
  tenants  -- one row per customer (name, api_key)
  commands -- queued AD operations
  results  -- completed operation results
"""

import sqlite3
import uuid
import os
from datetime import datetime

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


def list_tenants() -> list:
    with get_conn() as conn:
        rows = conn.execute("SELECT id, name, created_at FROM tenants").fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Command helpers
# ---------------------------------------------------------------------------

def queue_command(tenant_id: str, action: str, args: list) -> dict:
    import json
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
    import json
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
    import json
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
    import json
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
