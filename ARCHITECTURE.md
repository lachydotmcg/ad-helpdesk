# AID Helpdesk — Architecture

## System overview

```
  Staff Browser
       │  HTTPS
       ▼
  ┌──────────────────────────────────┐
  │   AID Cloud Dashboard            │  cloud/app.py — Railway / any VPS
  │   Flask + PostgreSQL             │
  └──────────────┬─────────────────--┘
                 │
                 │  outbound HTTPS polling (GET /agent/poll every 0.5s)
                 │  ◀── no inbound ports · no VPN · works behind NAT
                 ▼
  ┌──────────────────────────────────┐
  │   AID Agent                      │  agent.py — Windows Service
  └──────────────┬───────────────────┘
                 │
                 │  WinRM over HTTPS (TLS · port 5986 · local network only)
                 ▼
  ┌──────────────────────────────────┐
  │   Windows Server                 │
  │   Active Directory               │  ← never touches the internet
  └──────────────────────────────────┘
```

---

## Components

### Cloud backend (`cloud/app.py`)

Flask application deployed on Railway. Responsibilities:

- **Tenant auth** — session-based dashboard login, API key auth for agents
- **Command queue** — dashboard enqueues AD commands → agent polls → agent posts results
- **AI assistant** (`/dashboard/chat`) — Claude Haiku with structured JSON output
- **Helpdesk tickets** — create, assign, AI triage, resolve
- **Scheduled reports** — background thread runs every 30 min, sends HTML email reports per tenant
- **Email intake** — inbound SMTP webhook → tickets (`/webhook/email/<api_key>`)
- **Admin panel** — `/admin/ui`, protected by `ADMIN_KEY`

### Windows agent (`agent.py` + `ad_bridge.py`)

- Polls `GET /agent/poll` every 0.5s, authenticated by tenant API key
- Receives `{action, args, id}` command structs
- Calls the matching function in `ad_bridge.py` (all WinRM/PowerShell execution lives here)
- Posts result to `POST /agent/result`
- No inbound ports — pure outbound polling

### Permission layer (`cloud/action_policy.py`)

Every command passes through `action_policy.validate(action, source, tenant_auto_actions)` before being queued. This is the security boundary — not the AI prompt.

| Tier | Policy |
|---|---|
| READ | Zero side effects — always allowed |
| WRITE | Reversible — allowed if tenant has auto-actions enabled for that action |
| DESTRUCTIVE | Always requires human confirmation — AI auto-resolve is blocked at Python layer |

### Database (`cloud/db.py`)

SQLite for local dev, PostgreSQL for production. The `_USE_PG` flag and `_PH` placeholder (`%s` vs `?`) abstract the difference.

Migrations are version-gated in `migrate_db()` (currently v5). Schema version is tracked in the `schema_version` table.

---

## Database schema

| Table | Purpose |
|---|---|
| `tenants` | Tenant records, API keys, plan |
| `tenant_users` | Dashboard users per tenant |
| `tenant_settings` | Per-tenant config (SMTP, AI settings, report schedule, AI persona name) |
| `commands` | Queued AD commands |
| `results` | Command results posted by agent |
| `audit_log` | Immutable action log |
| `tickets` | Helpdesk tickets |
| `ticket_actions` | Actions taken on tickets |
| `chat_sessions` / `chat_messages` | AI chat history |
| `usage` | Monthly usage counters (AI calls, AD commands) |
| `activity_log` | Dashboard activity feed |
| `custom_scripts` | Uploaded PowerShell scripts callable by AI |

---

## Action flow

```
1. User types in dashboard ("unlock tom.brady")
        │
        ▼
2. POST /dashboard/chat  →  AI assistant (Claude Haiku)
   Returns: {"response": "...", "actions": [{"action": "unlock_account", "args": ["tom.brady"]}]}
        │
        ▼
3. action_policy.validate("unlock_account", source="janus", tenant_auto_actions=[...])
   WRITE tier: passes if tenant has unlock_account in auto_actions list
        │
        ▼
4. Command inserted into `commands` table  (status=pending)
        │
        ▼
5. Agent polls GET /agent/poll  →  receives command
        │
        ▼
6. ad_bridge.unlock_account("tom.brady")  →  PowerShell via WinRM
        │
        ▼
7. Agent POST /agent/result  →  result stored, audit log entry written
        │
        ▼
8. Dashboard polls for result, displays to user
```

---

## AI assistant

- **Model:** Claude Haiku via Anthropic API
- **Input:** system prompt containing all available actions + args, tenant's AI context (org-specific facts the AI has learned), list of enabled custom scripts
- **Output:** `{"response": "...", "actions": [{"action": "...", "args": [...]}]}`
- **READ actions:** auto-resolved silently (no confirmation modal)
- **DESTRUCTIVE actions:** trigger a 6-digit confirmation modal — queued only after correct token submission
- **Custom scripts:** AI calls a slug → `app.py` fetches `ps_content` from DB and embeds it as `args[0]` before queuing. The agent receives PS content directly, never a slug.
- **Bulk ops:** `bulk_move_users` takes a full rules array in one call — the AI never loops `move_user` for batch operations

---

## Repository layout

```
ad-helpdesk/
├── agent.py                  # Windows agent: polls cloud, executes, posts results
├── ad_bridge.py              # All PowerShell/WinRM execution
├── agent-config.example.json # Template config for agent install
├── requirements.txt
├── Procfile                  # Railway: web=gunicorn cloud.app:app
├── nixpacks.toml             # Railway build config
├── cloud/
│   ├── app.py                # Flask backend, routes, AI assistant
│   ├── db.py                 # Database layer (SQLite dev / PostgreSQL prod)
│   ├── action_policy.py      # Hard permission enforcement (READ/WRITE/DESTRUCTIVE)
│   └── templates/
│       └── dashboard.html    # Single-page frontend (vanilla JS)
├── installer/
│   └── setup_wizard.py       # Windows EXE installer wizard
└── legacy/                   # v0.1–v0.4 local-only files — not active
```

---

## Plan limits

| Plan | AI calls/mo | AD actions/mo | Tickets | Team members |
|---|---|---|---|---|
| Free | 10 | 5 | 20 | 1 |
| Pro | 500 | 200 | Unlimited | 5 |
| Enterprise | 2,000 | 1,000 | Unlimited | Unlimited |

Limits are enforced server-side before each operation. The agent is never involved in limit checks.
