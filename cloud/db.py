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
  chat_sessions  -- AI assistant conversation sessions
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
import secrets
from datetime import datetime
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.path.join(os.path.dirname(__file__), "adhelpdesk.db")

DATABASE_URL = os.getenv("DATABASE_URL", "")
_USE_PG = bool(DATABASE_URL)

if _USE_PG:
    import psycopg2
    import psycopg2.errors
    import psycopg2.extras

_STRIPE_EVENT_DUPLICATE_ERRORS = (sqlite3.IntegrityError,)
if _USE_PG:
    _STRIPE_EVENT_DUPLICATE_ERRORS = (
        sqlite3.IntegrityError,
        psycopg2.errors.UniqueViolation,
    )


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
        id             TEXT PRIMARY KEY,
        name           TEXT NOT NULL,
        api_key        TEXT NOT NULL UNIQUE,
        created_at     TEXT NOT NULL,
        plan           TEXT NOT NULL DEFAULT 'free',
        last_agent_ping TEXT
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
        ad_commands INTEGER NOT NULL DEFAULT 0,
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
    """CREATE TABLE IF NOT EXISTS password_reset_tokens (
        id         TEXT PRIMARY KEY,
        user_id    TEXT NOT NULL,
        tenant_id  TEXT NOT NULL,
        token      TEXT NOT NULL UNIQUE,
        expires_at TEXT NOT NULL,
        used       INTEGER NOT NULL DEFAULT 0,
        FOREIGN KEY (user_id)   REFERENCES tenant_users(id),
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS custom_scripts (
        id               TEXT PRIMARY KEY,
        tenant_id        TEXT NOT NULL,
        name             TEXT NOT NULL,
        slug             TEXT NOT NULL,
        description      TEXT NOT NULL,
        args_description TEXT,
        ps_content       TEXT NOT NULL,
        classification   TEXT NOT NULL DEFAULT 'write',
        enabled          INTEGER NOT NULL DEFAULT 1,
        created_at       TEXT NOT NULL,
        updated_at       TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS agent_memory (
        id          TEXT PRIMARY KEY,
        tenant_id   TEXT NOT NULL,
        category    TEXT NOT NULL DEFAULT 'general',
        key         TEXT NOT NULL,
        value       TEXT NOT NULL,
        confidence  REAL NOT NULL DEFAULT 0.8,
        source      TEXT NOT NULL DEFAULT 'auto',
        created_at  TEXT NOT NULL,
        last_used   TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS billing_customers (
        tenant_id          TEXT PRIMARY KEY,
        stripe_customer_id TEXT NOT NULL UNIQUE,
        created_at         TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS billing_subscriptions (
        tenant_id              TEXT PRIMARY KEY,
        stripe_subscription_id TEXT NOT NULL UNIQUE,
        plan                   TEXT NOT NULL DEFAULT 'free',
        status                 TEXT NOT NULL DEFAULT 'none',
        current_period_end     TEXT,
        updated_at             TEXT NOT NULL,
        FOREIGN KEY (tenant_id) REFERENCES tenants(id)
    )""",
    """CREATE TABLE IF NOT EXISTS stripe_events (
        event_id     TEXT NOT NULL UNIQUE,
        type         TEXT NOT NULL,
        received_at  TEXT NOT NULL,
        processed_at TEXT
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


def migrate_db():
    """Apply incremental schema changes for existing databases (safe to re-run)."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        # v1: add last_agent_ping + plan columns
        if _USE_PG:
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS last_agent_ping TEXT")
            cur.execute("ALTER TABLE tenants ADD COLUMN IF NOT EXISTS plan TEXT NOT NULL DEFAULT 'free'")
        else:
            cur.execute("PRAGMA table_info(tenants)")
            cols = [r["name"] for r in cur.fetchall()]
            if "last_agent_ping" not in cols:
                cur.execute("ALTER TABLE tenants ADD COLUMN last_agent_ping TEXT")
            if "plan" not in cols:
                cur.execute("ALTER TABLE tenants ADD COLUMN plan TEXT NOT NULL DEFAULT 'free'")
        # v2: ad_commands column on usage table
        if _USE_PG:
            cur.execute("ALTER TABLE usage ADD COLUMN IF NOT EXISTS ad_commands INTEGER NOT NULL DEFAULT 0")
        else:
            cur.execute("PRAGMA table_info(usage)")
            ucols = [r["name"] for r in cur.fetchall()]
            if "ad_commands" not in ucols:
                cur.execute("ALTER TABLE usage ADD COLUMN ad_commands INTEGER NOT NULL DEFAULT 0")
        # v3: password reset tokens table (idempotent — CREATE TABLE IF NOT EXISTS)
        cur.execute("""CREATE TABLE IF NOT EXISTS password_reset_tokens (
            id         TEXT PRIMARY KEY,
            user_id    TEXT NOT NULL,
            tenant_id  TEXT NOT NULL,
            token      TEXT NOT NULL UNIQUE,
            expires_at TEXT NOT NULL,
            used       INTEGER NOT NULL DEFAULT 0
        )""")
        # v4: labels column on tickets (comma-separated label strings)
        if _USE_PG:
            cur.execute("ALTER TABLE tickets ADD COLUMN IF NOT EXISTS labels TEXT")
        else:
            cur.execute("PRAGMA table_info(tickets)")
            tcols = [r["name"] for r in cur.fetchall()]
            if "labels" not in tcols:
                cur.execute("ALTER TABLE tickets ADD COLUMN labels TEXT")
        # v5: custom_scripts table (idempotent)
        cur.execute("""CREATE TABLE IF NOT EXISTS custom_scripts (
            id               TEXT PRIMARY KEY,
            tenant_id        TEXT NOT NULL,
            name             TEXT NOT NULL,
            slug             TEXT NOT NULL,
            description      TEXT NOT NULL,
            args_description TEXT,
            ps_content       TEXT NOT NULL,
            classification   TEXT NOT NULL DEFAULT 'write',
            enabled          INTEGER NOT NULL DEFAULT 1,
            created_at       TEXT NOT NULL,
            updated_at       TEXT NOT NULL
        )""")
        # v6: agent_memory table for named AI persona and organisational learning
        cur.execute("""CREATE TABLE IF NOT EXISTS agent_memory (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            category    TEXT NOT NULL DEFAULT 'general',
            key         TEXT NOT NULL,
            value       TEXT NOT NULL,
            confidence  REAL NOT NULL DEFAULT 0.8,
            source      TEXT NOT NULL DEFAULT 'auto',
            created_at  TEXT NOT NULL,
            last_used   TEXT NOT NULL
        )""")
        # v7: launch-grade Stripe billing state and webhook idempotency.
        cur.execute("""CREATE TABLE IF NOT EXISTS billing_customers (
            tenant_id          TEXT PRIMARY KEY,
            stripe_customer_id TEXT NOT NULL UNIQUE,
            created_at         TEXT NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS billing_subscriptions (
            tenant_id              TEXT PRIMARY KEY,
            stripe_subscription_id TEXT NOT NULL UNIQUE,
            plan                   TEXT NOT NULL DEFAULT 'free',
            status                 TEXT NOT NULL DEFAULT 'none',
            current_period_end     TEXT,
            updated_at             TEXT NOT NULL
        )""")
        cur.execute("""CREATE TABLE IF NOT EXISTS stripe_events (
            event_id     TEXT NOT NULL UNIQUE,
            type         TEXT NOT NULL,
            received_at  TEXT NOT NULL,
            processed_at TEXT
        )""")
        if _USE_PG:
            cur.execute("""
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = 'stripe_events'
            """)
            event_cols = {r["column_name"] for r in cur.fetchall()}
            if "event_id" not in event_cols:
                cur.execute("ALTER TABLE stripe_events ADD COLUMN event_id TEXT")
                event_cols.add("event_id")
            if "id" in event_cols:
                cur.execute("UPDATE stripe_events SET event_id = id WHERE event_id IS NULL")
        else:
            cur.execute("PRAGMA table_info(stripe_events)")
            event_cols = [r["name"] for r in cur.fetchall()]
            if "event_id" not in event_cols:
                cur.execute("ALTER TABLE stripe_events ADD COLUMN event_id TEXT")
                event_cols.append("event_id")
            if "id" in event_cols:
                cur.execute("UPDATE stripe_events SET event_id = id WHERE event_id IS NULL")
        cur.execute("CREATE UNIQUE INDEX IF NOT EXISTS stripe_events_event_id_key ON stripe_events(event_id)")
        # v8: feedback table for in-product feedback during trial
        cur.execute("""CREATE TABLE IF NOT EXISTS feedback (
            id          TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            user_email  TEXT NOT NULL,
            message     TEXT NOT NULL,
            rating      INTEGER,
            page        TEXT,
            created_at  TEXT NOT NULL
        )""")
        # v9: shared rate-limit + confirm-token state (replaces in-memory dicts
        # so limits and the destructive-action handshake survive deploys and
        # hold across multiple dynos/instances).
        cur.execute("""CREATE TABLE IF NOT EXISTS rate_hits (
            id      TEXT PRIMARY KEY,
            bucket  TEXT NOT NULL,
            hit_at  TEXT NOT NULL
        )""")
        cur.execute("CREATE INDEX IF NOT EXISTS rate_hits_bucket_idx ON rate_hits(bucket)")
        cur.execute("""CREATE TABLE IF NOT EXISTS confirm_tokens (
            code        TEXT PRIMARY KEY,
            tenant_id   TEXT NOT NULL,
            action      TEXT NOT NULL,
            args        TEXT NOT NULL,
            expires_at  TEXT NOT NULL,
            used        INTEGER NOT NULL DEFAULT 0,
            created_at  TEXT NOT NULL
        )""")
        conn.commit()
    except Exception:
        pass  # Safe to ignore — already exists
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tenant helpers
# ---------------------------------------------------------------------------

def update_agent_ping(tenant_id: str) -> None:
    """Record that the agent just polled. Throttled: only writes once per 10s per tenant."""
    from datetime import timedelta
    now    = datetime.utcnow().isoformat()
    cutoff = (datetime.utcnow() - timedelta(seconds=10)).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE tenants SET last_agent_ping = {_PH} "
            f"WHERE id = {_PH} AND (last_agent_ping IS NULL OR last_agent_ping < {_PH})",
            (now, tenant_id, cutoff)
        )
        conn.commit()
    finally:
        conn.close()


def get_last_agent_error(tenant_id: str, within_seconds: int = 300) -> str | None:
    """Return the message of the most recent FAILED command result within the
    window, or None. Lets the dashboard show *why* actions aren't completing
    (e.g. 'Cannot reach the domain controller...') instead of a bare timeout."""
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(seconds=within_seconds)).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT message FROM results WHERE tenant_id = {_PH} AND success = 0 "
            f"AND created_at > {_PH} ORDER BY created_at DESC LIMIT 1",
            (tenant_id, cutoff)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    msg = (row.get("message") if row else None) or None
    return msg.strip()[:300] if msg else None


# ---------------------------------------------------------------------------
# Shared rate limiting + confirmation tokens (DB-backed)
# These replace the old in-memory dicts so abuse limits and the destructive-
# action handshake are consistent across deploys and multiple dynos.
# ---------------------------------------------------------------------------

def rate_limit_allow(bucket: str, max_calls: int, window_seconds: int) -> bool:
    """Sliding-window rate limit. Records a hit and returns True if allowed,
    or False if the bucket already holds >= max_calls within the window."""
    from datetime import timedelta
    now    = datetime.utcnow()
    cutoff = (now - timedelta(seconds=window_seconds)).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        # Drop this bucket's expired hits, then count what remains in-window.
        cur.execute(f"DELETE FROM rate_hits WHERE bucket = {_PH} AND hit_at < {_PH}",
                    (bucket, cutoff))
        cur.execute(f"SELECT COUNT(*) AS n FROM rate_hits WHERE bucket = {_PH}", (bucket,))
        n = _row(cur.fetchone())["n"]
        if n >= max_calls:
            conn.commit()
            return False
        cur.execute(f"INSERT INTO rate_hits (id, bucket, hit_at) VALUES ({_PH}, {_PH}, {_PH})",
                    (str(uuid.uuid4()), bucket, now.isoformat()))
        conn.commit()
        return True
    finally:
        conn.close()


def issue_confirm_token(tenant_id: str, action: str, args: list,
                        ttl_seconds: int = 300) -> str:
    """Create a single-use 6-digit confirmation code for a destructive action."""
    from datetime import timedelta
    now     = datetime.utcnow()
    expires = (now + timedelta(seconds=ttl_seconds)).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        # Opportunistic cleanup of expired tokens.
        cur.execute(f"DELETE FROM confirm_tokens WHERE expires_at < {_PH}", (now.isoformat(),))
        # Generate a code, retrying on the rare PK collision.
        for _ in range(5):
            code = "".join(secrets.choice("0123456789") for _ in range(6))
            try:
                cur.execute(
                    f"INSERT INTO confirm_tokens "
                    f"(code, tenant_id, action, args, expires_at, used, created_at) "
                    f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, 0, {_PH})",
                    (code, tenant_id, action, json.dumps(args), expires, now.isoformat())
                )
                conn.commit()
                return code
            except Exception:
                conn.rollback()
                continue
        raise RuntimeError("Could not allocate a confirmation code")
    finally:
        conn.close()


def consume_confirm_token(tenant_id: str, code: str) -> dict | None:
    """Atomically claim a confirmation token. Returns {action, args, tenant_id}
    if valid (correct tenant, unexpired, unused) or None. Single-use is
    guaranteed by the UPDATE ... WHERE used = 0 row-count check."""
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE confirm_tokens SET used = 1 "
            f"WHERE code = {_PH} AND tenant_id = {_PH} AND used = 0 AND expires_at > {_PH}",
            (code, tenant_id, now)
        )
        claimed = cur.rowcount
        row = None
        if claimed:
            cur.execute(f"SELECT action, args FROM confirm_tokens WHERE code = {_PH}", (code,))
            row = _row(cur.fetchone())
        conn.commit()
    finally:
        conn.close()
    if not claimed or not row:
        return None
    return {"tenant_id": tenant_id, "action": row["action"], "args": json.loads(row["args"])}


def get_agent_status(tenant_id: str) -> dict:
    """Return whether the agent is online (pinged within last 35s) plus the
    most recent failure message, if any, so the UI can explain problems."""
    from datetime import timedelta
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(f"SELECT last_agent_ping FROM tenants WHERE id = {_PH}", (tenant_id,))
        row = _row(cur.fetchone())
    finally:
        conn.close()
    last_error = get_last_agent_error(tenant_id)
    if not row or not row.get("last_agent_ping"):
        return {"online": False, "last_ping": None, "last_error": last_error}
    last   = row["last_agent_ping"]
    cutoff = (datetime.utcnow() - timedelta(seconds=35)).isoformat()
    return {"online": last > cutoff, "last_ping": last, "last_error": last_error}


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


def list_all_tenants() -> list:
    """Return all tenants (for scheduled background tasks)."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute("SELECT * FROM tenants ORDER BY name")
        return _rows(cur.fetchall())
    finally:
        conn.close()


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
        cur.execute("SELECT id, name, api_key, created_at, plan FROM tenants ORDER BY created_at DESC")
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


def delete_tenant_user(tenant_id: str, user_id: str) -> bool:
    """Remove a dashboard user. Returns True if a row was deleted."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"DELETE FROM tenant_users WHERE id = {_PH} AND tenant_id = {_PH}",
            (user_id, tenant_id)
        )
        deleted = cur.rowcount > 0
        conn.commit()
        return deleted
    finally:
        conn.close()


def update_user_password(tenant_id: str, email: str, new_password: str) -> None:
    """Update the password for a dashboard user."""
    pw_hash = generate_password_hash(new_password)
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE tenant_users SET password_hash = {_PH} WHERE tenant_id = {_PH} AND email = {_PH}",
            (pw_hash, tenant_id, email.lower().strip())
        )
        conn.commit()
    finally:
        conn.close()


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


def get_command(command_id: str, tenant_id: str) -> dict | None:
    """Return a queued/running/completed command for this tenant."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM commands WHERE id = {_PH} AND tenant_id = {_PH}",
            (command_id, tenant_id)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    if not row:
        return None
    try:
        args = json.loads(row.get("args") or "[]")
    except Exception:
        args = []
    return {
        "id":         row["id"],
        "tenant_id":  row["tenant_id"],
        "action":     row["action"],
        "args":       args,
        "status":     row["status"],
        "created_at": row["created_at"],
    }


# How long a queued command stays valid. Anything older is expired rather than
# executed: the requester (dashboard long-poll / API) has already given up by
# then, so running it late is pointless and, for writes, surprising. This also
# stops a backlog from an agent outage turning into a multi-minute pile-up — the
# agent always picks up the freshest still-relevant command, never stale ones.
COMMAND_TTL_SECONDS = 45

def get_pending_command(tenant_id: str) -> dict | None:
    """Return the oldest *still-fresh* pending command and mark it running.

    Before selecting, any pending command older than COMMAND_TTL_SECONDS is
    marked 'expired' so the agent never churns through a stale backlog.
    """
    from datetime import timedelta
    cutoff = (datetime.utcnow() - timedelta(seconds=COMMAND_TTL_SECONDS)).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        # Expire stale pending commands in one shot (self-healing backlog).
        cur.execute(
            f"UPDATE commands SET status = 'expired' "
            f"WHERE tenant_id = {_PH} AND status = 'pending' AND created_at < {_PH}",
            (tenant_id, cutoff)
        )
        # Take the oldest command that's still within its TTL.
        cur.execute(
            f"SELECT * FROM commands WHERE tenant_id = {_PH} AND status = 'pending' "
            f"ORDER BY created_at ASC LIMIT 1",
            (tenant_id,)
        )
        row = _row(cur.fetchone())
        if not row:
            conn.commit()   # persist the expirations even when nothing is pending
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


# ---------------------------------------------------------------------------
# Custom scripts helpers
# ---------------------------------------------------------------------------

def list_custom_scripts(tenant_id: str) -> list:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM custom_scripts WHERE tenant_id = {_PH} ORDER BY name",
            (tenant_id,)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def get_custom_script(tenant_id: str, script_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM custom_scripts WHERE id = {_PH} AND tenant_id = {_PH}",
            (script_id, tenant_id)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def get_custom_script_by_slug(tenant_id: str, slug: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM custom_scripts WHERE slug = {_PH} AND tenant_id = {_PH} AND enabled = 1",
            (slug, tenant_id)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def create_custom_script(tenant_id: str, name: str, slug: str, description: str,
                         ps_content: str, args_description: str = "",
                         classification: str = "write") -> dict:
    script_id = str(uuid.uuid4())
    now       = datetime.utcnow().isoformat()
    conn      = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO custom_scripts (id, tenant_id, name, slug, description, "
            f"args_description, ps_content, classification, enabled, created_at, updated_at) "
            f"VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},1,{_PH},{_PH})",
            (script_id, tenant_id, name, slug, description,
             args_description, ps_content, classification, now, now)
        )
        conn.commit()
        return {"id": script_id, "tenant_id": tenant_id, "name": name, "slug": slug,
                "description": description, "args_description": args_description,
                "ps_content": ps_content, "classification": classification,
                "enabled": 1, "created_at": now, "updated_at": now}
    finally:
        conn.close()


def update_custom_script(tenant_id: str, script_id: str, **fields) -> bool:
    allowed = {"name", "slug", "description", "args_description",
               "ps_content", "classification", "enabled"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    now = datetime.utcnow().isoformat()
    updates["updated_at"] = now
    conn = _get_conn()
    try:
        cur = _cur(conn)
        set_clause = ", ".join(f"{k} = {_PH}" for k in updates)
        values     = list(updates.values()) + [script_id, tenant_id]
        cur.execute(
            f"UPDATE custom_scripts SET {set_clause} WHERE id = {_PH} AND tenant_id = {_PH}",
            values
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_custom_script(tenant_id: str, script_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"DELETE FROM custom_scripts WHERE id = {_PH} AND tenant_id = {_PH}",
            (script_id, tenant_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_action_counts_this_month(tenant_id: str) -> dict:
    """Return {action: count} for all logged audit actions in the current calendar month."""
    from datetime import datetime
    start = datetime.utcnow().replace(day=1, hour=0, minute=0, second=0, microsecond=0).isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT action, COUNT(*) FROM audit_log WHERE tenant_id = {_PH} AND created_at >= {_PH} GROUP BY action",
            (tenant_id, start)
        )
        return {row[0]: row[1] for row in cur.fetchall()}
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
               "janus_action", "janus_action_args", "resolved_at", "labels"}
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

def increment_usage(tenant_id: str, metric: str = "janus_calls", amount: int = 1) -> None:
    """Increment a usage counter for the current month."""
    if metric not in {"janus_calls", "ad_commands"}:
        raise ValueError(f"Unknown usage metric: {metric}")
    try:
        amount = int(amount)
    except (TypeError, ValueError):
        amount = 1
    if amount <= 0:
        return
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
            f"UPDATE usage SET {metric} = {metric} + {_PH}, updated_at = {_PH} "
            f"WHERE tenant_id = {_PH} AND month = {_PH}",
            (amount, now, tenant_id, month)
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
    return row or {"tenant_id": tenant_id, "month": month, "janus_calls": 0, "ad_commands": 0}


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
    "ai_name":            "Assistant",
    "ai_context":         "",
    "security_checks":    True,
    "email_domain":       "",
    "roles":              [],
    "custom_statuses":    [],
    "custom_priorities":  [],
    "ticket_labels":      [],
    "slack_webhook_url":  "",
    "teams_webhook_url":  "",
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
        merged = {**_SETTINGS_DEFAULTS, **saved}
        if "ai_context" not in saved and saved.get("janus_context"):
            merged["ai_context"] = saved.get("janus_context")
        return merged
    except Exception:
        return dict(_SETTINGS_DEFAULTS)


def update_settings(tenant_id: str, settings: dict) -> None:
    """Upsert tenant settings."""
    settings["ai_name"] = str(settings.get("ai_name") or "").strip() or _SETTINGS_DEFAULTS["ai_name"]
    settings.pop("janus_name", None)
    if "ai_context" in settings:
        settings.pop("janus_context", None)
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


def flush_pending_commands(tenant_id: str | None = None) -> int:
    """Cancel all pending/running commands. Returns the number of rows affected."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        if tenant_id:
            cur.execute(
                f"UPDATE commands SET status = 'cancelled' "
                f"WHERE status IN ('pending', 'running') AND tenant_id = {_PH}",
                (tenant_id,)
            )
        else:
            cur.execute(
                "UPDATE commands SET status = 'cancelled' "
                "WHERE status IN ('pending', 'running')"
            )
        count = cur.rowcount
        conn.commit()
        return count
    finally:
        conn.close()


def create_feedback(tenant_id: str, user_email: str, message: str,
                    rating: int | None = None, page: str | None = None) -> dict:
    """Store a feedback submission."""
    fb_id = str(uuid.uuid4())
    now   = datetime.utcnow().isoformat()
    conn  = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO feedback (id, tenant_id, user_email, message, rating, page, created_at) "
            f"VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})",
            (fb_id, tenant_id, user_email, message, rating, page, now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"id": fb_id}


def list_feedback(limit: int = 100) -> list:
    """Return recent feedback submissions across all tenants, newest first."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM feedback ORDER BY created_at DESC LIMIT {_PH}",
            (limit,),
        )
        return _rows(cur.fetchall())
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


# ---------------------------------------------------------------------------
# Billing helpers
# ---------------------------------------------------------------------------

def get_billing_customer(tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM billing_customers WHERE tenant_id = {_PH}",
            (tenant_id,)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def get_tenant_id_by_stripe_customer(stripe_customer_id: str) -> str | None:
    if not stripe_customer_id:
        return None
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT tenant_id FROM billing_customers WHERE stripe_customer_id = {_PH}",
            (stripe_customer_id,)
        )
        row = _row(cur.fetchone())
        return row["tenant_id"] if row else None
    finally:
        conn.close()


def upsert_billing_customer(tenant_id: str, stripe_customer_id: str) -> dict:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""INSERT INTO billing_customers (tenant_id, stripe_customer_id, created_at)
               VALUES ({_PH}, {_PH}, {_PH})
               ON CONFLICT(tenant_id) DO UPDATE
               SET stripe_customer_id = excluded.stripe_customer_id""",
            (tenant_id, stripe_customer_id, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "tenant_id": tenant_id,
        "stripe_customer_id": stripe_customer_id,
        "created_at": now,
    }


def get_billing_subscription(tenant_id: str) -> dict | None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM billing_subscriptions WHERE tenant_id = {_PH}",
            (tenant_id,)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def get_tenant_id_by_stripe_subscription(stripe_subscription_id: str) -> str | None:
    if not stripe_subscription_id:
        return None
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT tenant_id FROM billing_subscriptions WHERE stripe_subscription_id = {_PH}",
            (stripe_subscription_id,)
        )
        row = _row(cur.fetchone())
        return row["tenant_id"] if row else None
    finally:
        conn.close()


def upsert_billing_subscription(tenant_id: str, stripe_subscription_id: str,
                                plan: str, status: str,
                                current_period_end: str | None = None) -> dict:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""INSERT INTO billing_subscriptions
               (tenant_id, stripe_subscription_id, plan, status, current_period_end, updated_at)
               VALUES ({_PH}, {_PH}, {_PH}, {_PH}, {_PH}, {_PH})
               ON CONFLICT(tenant_id) DO UPDATE
               SET stripe_subscription_id = excluded.stripe_subscription_id,
                   plan = excluded.plan,
                   status = excluded.status,
                   current_period_end = excluded.current_period_end,
                   updated_at = excluded.updated_at""",
            (tenant_id, stripe_subscription_id, plan, status, current_period_end, now)
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "tenant_id": tenant_id,
        "stripe_subscription_id": stripe_subscription_id,
        "plan": plan,
        "status": status,
        "current_period_end": current_period_end,
        "updated_at": now,
    }


def has_active_billing_subscription(tenant_id: str) -> bool:
    sub = get_billing_subscription(tenant_id)
    return bool(sub and sub.get("status") == "active")


def record_stripe_event(event_id: str, event_type: str) -> bool:
    """
    Return True when this event should be processed.
    Duplicate event IDs return False while the original delivery is either
    processing or already processed.
    """
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        try:
            cur.execute(
                f"INSERT INTO stripe_events (event_id, type, received_at) VALUES ({_PH}, {_PH}, {_PH})",
                (event_id, event_type, now)
            )
            conn.commit()
            return True
        except _STRIPE_EVENT_DUPLICATE_ERRORS:
            conn.rollback()
            return False
    finally:
        conn.close()


def release_unprocessed_stripe_event(event_id: str) -> None:
    """
    Allow Stripe to retry an event that failed before it was marked processed.
    Already-processed rows are kept so completed webhooks remain idempotent.
    """
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"DELETE FROM stripe_events WHERE event_id = {_PH} AND processed_at IS NULL",
            (event_id,)
        )
        conn.commit()
    finally:
        conn.close()


def mark_stripe_event_processed(event_id: str) -> None:
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE stripe_events SET processed_at = {_PH} WHERE event_id = {_PH}",
            (now, event_id)
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Plan limits
# ---------------------------------------------------------------------------

PLAN_LIMITS = {
    # ── Free trial ─────────────────────────────────────────────────────────
    # All features unlocked so testers get a fair picture of the product.
    # Quotas are generous but finite — enough for real testing, not abuse.
    "free": {
        "janus_calls":          20,    # AI scans / month
        "ad_commands":          50,    # AD action units / month; bulk moves scale by rule/user blocks
        "team_members":         None,  # unlimited during trial
        "tickets":              None,  # unlimited during trial
        "email_intake":         True,
        "auto_actions":         True,
        "scheduled_reports":    True,
        "custom_scripts_limit": 3,
        "integrations":         True,
        "label":                "Free Trial",
        "price":                "A$0",
        "price_monthly":        0,
    },
    # ── Paid tiers (code kept, not yet exposed in UI) ─────────────────────
    "pro": {
        "janus_calls":          500,
        "ad_commands":          200,
        "team_members":         5,
        "tickets":              None,
        "email_intake":         True,
        "auto_actions":         True,
        "scheduled_reports":    True,
        "custom_scripts_limit": 10,
        "integrations":         True,
        "label":                "Pro",
        "price":                "A$29/month",
        "price_monthly":        29,
    },
    "enterprise": {
        "janus_calls":          2000,
        "ad_commands":          1000,
        "team_members":         None,
        "tickets":              None,
        "email_intake":         True,
        "auto_actions":         True,
        "scheduled_reports":    True,
        "custom_scripts_limit": None,
        "integrations":         True,
        "label":                "Enterprise",
        "price":                "A$99/month",
        "price_monthly":        99,
    },
}


def get_plan_limits(plan: str) -> dict:
    return PLAN_LIMITS.get(plan, PLAN_LIMITS["free"])


def get_tenant_plan(tenant_id: str) -> str:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"""SELECT t.plan, bs.plan AS billing_plan, bs.status AS billing_status
               FROM tenants t
               LEFT JOIN billing_subscriptions bs ON bs.tenant_id = t.id
               WHERE t.id = {_PH}""",
            (tenant_id,)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    plan = (row or {}).get("plan", "free")
    billing_plan = (row or {}).get("billing_plan")
    if (row or {}).get("billing_status") == "active" and billing_plan in ("pro", "enterprise"):
        return billing_plan
    if plan in ("pro", "enterprise"):
        return "free"
    return plan


def set_tenant_plan(tenant_id: str, plan: str) -> None:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(f"UPDATE tenants SET plan = {_PH} WHERE id = {_PH}", (plan, tenant_id))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Password reset tokens
# ---------------------------------------------------------------------------

def create_password_reset_token(user_id: str, tenant_id: str) -> str:
    """Create a time-limited password reset token. Returns the token string."""
    import secrets as _sec
    from datetime import timedelta
    token      = _sec.token_urlsafe(32)
    expires_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    row_id     = str(uuid.uuid4())
    conn = _get_conn()
    try:
        cur = _cur(conn)
        # Invalidate any existing unused tokens for this user
        cur.execute(
            f"UPDATE password_reset_tokens SET used = 1 WHERE user_id = {_PH} AND used = 0",
            (user_id,)
        )
        cur.execute(
            f"INSERT INTO password_reset_tokens (id, user_id, tenant_id, token, expires_at, used) "
            f"VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},0)",
            (row_id, user_id, tenant_id, token, expires_at)
        )
        conn.commit()
        return token
    finally:
        conn.close()


def consume_password_reset_token(token: str) -> dict | None:
    """Validate and consume a reset token. Returns {user_id, tenant_id} or None if invalid/expired."""
    now = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM password_reset_tokens WHERE token = {_PH} AND used = 0 AND expires_at > {_PH}",
            (token, now)
        )
        row = _row(cur.fetchone())
        if not row:
            return None
        cur.execute(
            f"UPDATE password_reset_tokens SET used = 1 WHERE id = {_PH}",
            (row["id"],)
        )
        conn.commit()
        return {"user_id": row["user_id"], "tenant_id": row["tenant_id"]}
    finally:
        conn.close()


def update_user_password_by_id(user_id: str, new_password: str) -> None:
    """Update the password for a tenant_user by their user id."""
    pw_hash = generate_password_hash(new_password)
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE tenant_users SET password_hash = {_PH} WHERE id = {_PH}",
            (pw_hash, user_id)
        )
        conn.commit()
    finally:
        conn.close()


def get_tenant_user_by_email(tenant_id: str, email: str) -> dict | None:
    """Look up a tenant_user by email (case-insensitive). Returns dict or None."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM tenant_users WHERE tenant_id = {_PH} AND LOWER(email) = LOWER({_PH})",
            (tenant_id, email)
        )
        return _row(cur.fetchone())
    finally:
        conn.close()


def get_tenant_by_user_email(email: str) -> dict | None:
    """Find the tenant record for a user by their email (across all tenants). Returns dict or None."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT t.* FROM tenants t "
            f"JOIN tenant_users u ON u.tenant_id = t.id "
            f"WHERE LOWER(u.email) = LOWER({_PH}) LIMIT 1",
            (email,)
        )
        return _row(cur.fetchone())
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


# ---------------------------------------------------------------------------
# Agent memory (named AI persona + organisational learning)
# ---------------------------------------------------------------------------

def list_memories(tenant_id: str, limit: int = 100) -> list:
    """Return all memories for this tenant, ordered by most recently used."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM agent_memory WHERE tenant_id = {_PH} ORDER BY last_used DESC LIMIT {_PH}",
            (tenant_id, limit)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def get_memories_for_context(tenant_id: str, limit: int = 20) -> list:
    """Return the most recently used memories for injection into the AI system prompt."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT * FROM agent_memory WHERE tenant_id = {_PH} "
            f"ORDER BY last_used DESC LIMIT {_PH}",
            (tenant_id, limit)
        )
        return _rows(cur.fetchall())
    finally:
        conn.close()


def create_memory(tenant_id: str, category: str, key: str, value: str,
                  confidence: float = 0.8, source: str = "auto") -> dict:
    mem_id = str(uuid.uuid4())
    now    = datetime.utcnow().isoformat()
    conn   = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"INSERT INTO agent_memory (id, tenant_id, category, key, value, confidence, source, created_at, last_used) "
            f"VALUES ({_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH},{_PH})",
            (mem_id, tenant_id, category, key, value, confidence, source, now, now)
        )
        conn.commit()
        return {"id": mem_id, "tenant_id": tenant_id, "category": category,
                "key": key, "value": value, "confidence": confidence,
                "source": source, "created_at": now, "last_used": now}
    finally:
        conn.close()


def update_memory(tenant_id: str, memory_id: str, **fields) -> bool:
    allowed = {"category", "key", "value", "confidence"}
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return False
    now = datetime.utcnow().isoformat()
    updates["last_used"] = now
    conn = _get_conn()
    try:
        cur = _cur(conn)
        set_clause = ", ".join(f"{k} = {_PH}" for k in updates)
        values     = list(updates.values()) + [memory_id, tenant_id]
        cur.execute(
            f"UPDATE agent_memory SET {set_clause} WHERE id = {_PH} AND tenant_id = {_PH}",
            values
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def delete_memory(tenant_id: str, memory_id: str) -> bool:
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"DELETE FROM agent_memory WHERE id = {_PH} AND tenant_id = {_PH}",
            (memory_id, tenant_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def touch_memory(tenant_id: str, memory_id: str) -> None:
    """Update last_used timestamp to signal this memory was accessed."""
    now  = datetime.utcnow().isoformat()
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"UPDATE agent_memory SET last_used = {_PH} WHERE id = {_PH} AND tenant_id = {_PH}",
            (now, memory_id, tenant_id)
        )
        conn.commit()
    finally:
        conn.close()


def upsert_memory(tenant_id: str, category: str, key: str, value: str,
                  confidence: float = 0.8, source: str = "auto") -> dict:
    """Create a memory or update its value if one with the same key already exists."""
    conn = _get_conn()
    try:
        cur = _cur(conn)
        cur.execute(
            f"SELECT id FROM agent_memory WHERE tenant_id = {_PH} AND key = {_PH}",
            (tenant_id, key)
        )
        row = _row(cur.fetchone())
    finally:
        conn.close()
    if row:
        update_memory(tenant_id, row["id"], value=value, confidence=confidence)
        return {"id": row["id"], "updated": True}
    return create_memory(tenant_id, category, key, value, confidence, source)
