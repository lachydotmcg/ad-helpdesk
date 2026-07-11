# AID Helpdesk  - Architecture

## System overview

```
  Staff Browser
       │  HTTPS
       ▼
  ┌──────────────────────────────────┐
  │   AID Cloud Dashboard            │  cloud/app.py  - Railway / any VPS
  │   Flask + PostgreSQL             │
  └──────────────┬─────────────────--┘
                 │
                 ├── outbound HTTPS polling (GET /agent/poll every 0.5s)
                 │   ◀── no inbound ports · no VPN · works behind NAT
                 │
                 └── Microsoft Graph (client-credentials, per-tenant app registration)
                 │
     ┌───────────┴────────────────────────────┐
     ▼                                          ▼
  ┌──────────────────────────────────┐   ┌──────────────────────────────────┐
  │   AID Agent                      │   │   Microsoft Entra ID             │
  │   agent.py  - Windows Service     │   │   (cloud-to-cloud, no agent hop) │
  └──────────────┬───────────────────┘   └──────────────────────────────────┘
                 │
                 │  WinRM over HTTPS (TLS · port 5986 · local network only)
                 ▼
  ┌──────────────────────────────────┐
  │   Windows Server                 │
  │   AD · DNS · DHCP · GPO          │  ← never touches the internet
  └──────────────────────────────────┘
```

The agent path (WinRM) and the Entra path (Graph) are independent. WinRM commands always route through the agent's outbound poll loop and touch a specific Windows Server over the local network. Graph calls go straight from the cloud backend to Microsoft's cloud, tenant to tenant, with no agent involved  - there is no on-prem hop to reach Entra ID.

---

## Components

### Cloud backend (`cloud/app.py`)

Flask application deployed on Railway. Responsibilities:

- **Tenant auth**  - session-based dashboard login, API key auth for agents
- **Command queue**  - dashboard enqueues AD commands → agent polls → agent posts results
- **AI assistant** (`/dashboard/chat`)  - Claude Haiku with structured JSON output
- **Helpdesk tickets**  - create, assign, AI triage, resolve
- **Scheduled reports**  - background thread runs every 30 min, sends HTML email reports per tenant
- **Email intake**  - inbound SMTP webhook → tickets (`/webhook/email/<api_key>`)
- **Admin panel**  - `/admin/ui`, protected by `ADMIN_KEY`

### Windows agent (`agent.py` + bridge modules)

- Polls `GET /agent/poll` every 0.5s, authenticated by tenant API key
- Receives `{action, args, id}` command structs
- Looks the action up in its merged `ACTIONS` registry (see "Bridge module registry" below) and calls the matching function
- Posts result to `POST /agent/result`
- POSTs `{capabilities, actions}` to `POST /agent/capabilities` once at startup (see "Capability handshake" below)
- No inbound ports  - pure outbound polling

### WinRM transport (`winrm_core.py`)

Every bridge module shares one WinRM transport layer instead of opening its own session:

- **Session setup**  - builds the `winrm.Session` (HTTPS by default, port 5986), reading credentials from the environment
- **Connectivity circuit breaker**  - after 3 consecutive connection failures (not ordinary AD/PowerShell errors  - only transport-level failures like timeouts or refused connections) the breaker opens for a 60-second cooldown and fails fast, so a DC outage cannot turn a queue of commands into a multi-minute pile-up
- **`_run(ps_script)`**  - the common execute-and-parse helper; every bridge module's script builders end in a call to this, so the result envelope (`{"success", "message", "data"}`) is identical across AD, DNS, DHCP, and GPO
- **`role_installed_check(role_module, friendly)`**  - a PowerShell preamble each bridge prepends to its scripts. It checks `Get-Module -ListAvailable` for the relevant role module (`DnsServer`, `DhcpServer`, `GroupPolicy`, ...) and exits with a friendly error instead of a raw PowerShell exception if the role isn't installed on the target host. This is the graceful-degradation path  - a tenant without a DHCP server configured gets a clear "DHCP Server role not detected" message, not a stack trace

`ad_bridge.py` was refactored to import its transport from `winrm_core.py` rather than owning it, so the breaker and credentials are shared across every module rather than duplicated per-role.

### Bridge module registry

Each Windows Server role is its own bridge module (`ad_bridge.py`, `dns_bridge.py`, `dhcp_bridge.py`, `gpo_bridge.py`, ...). A bridge module is the unit of extension referenced in the vision: every future role (file shares, print services, IIS, ...) slots in the same way. A module exposes exactly two things:

- **`ACTIONS`**  - a flat dict mapping action name to a callable that takes an args list, e.g. `{"list_dns_zones": lambda a: list_zones(), ...}`
- **`CAPABILITY`**  - a short string identifying the role, e.g. `"dns"`, `"dhcp"`, `"gpo"`

`agent.py` imports each known bridge module (`ad_bridge` unconditionally, then `dns_bridge`, `dhcp_bridge`, `gpo_bridge`, `nps_bridge` best-effort via `try/except ImportError`), and merges their `ACTIONS` dicts into one flat registry and their `CAPABILITY` strings into a `CAPABILITIES` list. As of tonight this registry carries roughly 52 actions across the AD, DNS, DHCP, and GPO bridges. `execute()` is a single dispatch point: look the action up in the merged registry, call it, catch and wrap any exception into the standard result envelope.

To add a new Windows Server role as a module:

1. Write `<role>_bridge.py` with script-builder functions that call `winrm_core._run(...)`, each preceded by `winrm_core.role_installed_check(...)` for the relevant PowerShell module
2. Add input validation functions for every user-supplied value (see SECURITY.md  - validate before building any PowerShell)
3. Export `ACTIONS` and `CAPABILITY` at module level
4. Add the module name to the best-effort import list in `agent.py`
5. Add the new actions to the correct tier in `cloud/action_policy.py` (READ / WRITE / DESTRUCTIVE)
6. Add a dashboard tab gated on the new capability string (see "Capability handshake" below)

No other file needs to change  - the agent, the action dispatch, and the capability report all pick the module up automatically.

### Capability handshake

The dashboard needs to know which roles a given tenant's agent can actually serve before it shows tabs for them  - an agent running on a box with no DHCP Server role installed shouldn't show a DHCP tab. The handshake:

1. On startup, `agent.py` computes `CAPABILITIES` (the list of `CAPABILITY` strings from every bridge module it successfully imported) and `ACTIONS` (the merged action registry)
2. It POSTs `{"capabilities": CAPABILITIES, "actions": sorted(ACTIONS.keys())}` to `POST /agent/capabilities`, authenticated by the tenant API key
3. `cloud/app.py` validates the payload (list of strings, each matching `^[A-Za-z0-9_]+$`, max 20 entries, max 32 chars each) and stores it via `db.set_tenant_capabilities()`
4. `cloud/db.py` persists the capability list as a JSON-encoded string in the `tenants.capabilities` column (migration v11)
5. The dashboard reads the tenant's stored capabilities and greys out any tab whose capability string isn't present

Older agents that predate this endpoint simply get a 404 on the POST and keep working exactly as before  - the handshake degrades gracefully in both directions (missing role on the agent side, missing endpoint on the cloud side).

### Permission layer (`cloud/action_policy.py`)

Every command passes through `action_policy.validate(action, source, tenant_auto_actions)` before being queued. This is the security boundary  - not the AI prompt.

| Tier | Policy |
|---|---|
| READ | Zero side effects  - always allowed |
| WRITE | Reversible  - allowed if tenant has auto-actions enabled for that action |
| DESTRUCTIVE | Always requires human confirmation  - AI auto-resolve is blocked at Python layer |

### Entra ID bridge (`cloud/graph_client.py`)

Unlike the Windows Server roles above, Entra ID is a cloud-native directory  - there is no on-prem WinRM hop, so this bridge talks straight to Microsoft Graph from the cloud backend using the client-credentials (application) OAuth flow, via `msal`. Each tenant supplies its own Entra app registration (client ID, client secret, tenant ID), stored per-tenant rather than in a shared environment variable, so one AID Helpdesk deployment can serve many separate Entra tenants without any credential overlap.

`GraphClient` exposes methods for user lookup, group membership, session revocation, and password reset, and every method returns the same `{"success", "message", "data"}` envelope used everywhere else in the codebase, so the AI assistant and the dashboard don't need to special-case Entra results. Because this path skips the agent entirely, it does not go through `winrm_core.py`, the bridge module `ACTIONS`/`CAPABILITY` registry, or the WinRM circuit breaker  - it is wired up alongside those, not through them.

### Database (`cloud/db.py`)

SQLite for local dev, PostgreSQL for production. The `_USE_PG` flag and `_PH` placeholder (`%s` vs `?`) abstract the difference.

Migrations are version-gated in `migrate_db()` (currently v11). Schema version is tracked in the `schema_version` table. v11 adds the `tenants.capabilities` column used by the capability handshake above.

---

## Database schema

| Table | Purpose |
|---|---|
| `tenants` | Tenant records, API keys, plan, reported capabilities |
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
3. action_policy.validate("unlock_account", source="ai_auto", tenant_auto_actions=[...])
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
- **DESTRUCTIVE actions:** trigger a 6-digit confirmation modal  - queued only after correct token submission
- **Custom scripts:** AI calls a slug → `app.py` fetches `ps_content` from DB and embeds it as `args[0]` before queuing. The agent receives PS content directly, never a slug.
- **Bulk ops:** `bulk_move_users` takes a full rules array in one call  - the AI never loops `move_user` for batch operations

---

## Repository layout

```
ad-helpdesk/
├── agent.py                  # Windows agent: polls cloud, merges bridge ACTIONS, executes, posts results
├── winrm_core.py              # Shared WinRM transport, circuit breaker, role_installed_check
├── ad_bridge.py               # AD bridge module (ACTIONS + CAPABILITY = "ad")
├── dns_bridge.py               # DNS bridge module (ACTIONS + CAPABILITY = "dns")
├── dhcp_bridge.py              # DHCP bridge module (ACTIONS + CAPABILITY = "dhcp")
├── gpo_bridge.py                # GPO bridge module (ACTIONS + CAPABILITY = "gpo")
├── agent-config.example.json # Template config for agent install
├── requirements.txt
├── Procfile                  # Railway: web=gunicorn cloud.app:app
├── nixpacks.toml             # Railway build config
├── cloud/
│   ├── app.py                # Flask backend, routes, AI assistant
│   ├── db.py                 # Database layer (SQLite dev / PostgreSQL prod)
│   ├── action_policy.py      # Hard permission enforcement (READ/WRITE/DESTRUCTIVE)
│   ├── graph_client.py        # Entra ID bridge  - Microsoft Graph, client-credentials flow
│   └── templates/
│       └── dashboard.html    # Single-page frontend (vanilla JS)
├── installer/
│   └── setup_wizard.py       # Windows EXE installer wizard
└── legacy/                   # v0.1–v0.4 local-only files  - not active
```

---

## Plan limits

| Plan | AI calls/mo | AD actions/mo | Tickets | Team members |
|---|---|---|---|---|
| Free | 10 | 5 | 20 | 1 |
| Pro | 500 | 200 | Unlimited | 5 |
| Enterprise | 2,000 | 1,000 | Unlimited | Unlimited |

Limits are enforced server-side before each operation. The agent is never involved in limit checks.
