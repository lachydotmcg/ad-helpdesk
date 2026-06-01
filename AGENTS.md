# AD Helpdesk — Project Brief for Codex

> **Owner:** Lachlan (18yo solo founder, bytehavencreations@gmail.com)
> **Status:** Active development, pre-launch. Everything is built except Stripe billing.
> **Deployed on:** Railway (cloud backend), customer Windows servers (agent)

---

## What This Is

**AD Helpdesk** is a multi-tenant SaaS that lets IT admins manage Active Directory from a browser dashboard, anywhere in the world — no VPN, no open inbound ports, no RDP.

A cloud backend runs on Railway. A lightweight Windows agent installs on the customer's network, polls outbound HTTPS, executes AD commands locally via WinRM/PowerShell, and posts results back. The dashboard includes **Janus**, an AI assistant (Codex Haiku) that understands natural language AD requests and can resolve helpdesk tickets automatically.

**Target customer:** Schools and SMBs with on-prem Active Directory. Pain points are password resets, account unlocks, bulk student OU moves at semester rollover.

---

## Repository Layout

```
ad-helpdesk/
├── ad_bridge.py              # All PowerShell execution — called by agent.py
├── agent.py                  # Windows agent: polls cloud, executes, posts results
├── agent-config.example.json # Template config for agent install
├── requirements.txt          # Python deps (Flask, psycopg2, anthropic, etc.)
├── Procfile                  # Railway: web=gunicorn cloud.app:app
├── nixpacks.toml             # Railway build config
├── cloud/
│   ├── app.py                # Flask backend + dashboard routes + Janus AI
│   ├── db.py                 # Database layer (SQLite dev / PostgreSQL prod)
│   ├── action_policy.py      # Hard permission enforcement (READ/WRITE/DESTRUCTIVE)
│   └── templates/
│       └── dashboard.html    # Entire single-page frontend (vanilla JS)
├── installer/
│   └── setup_wizard.py       # Windows EXE installer wizard (PyInstaller target)
└── legacy/                   # Old local-only version — ignore
```

---

## Architecture

### Cloud Backend (`cloud/app.py`)
Flask app, deployed on Railway. Handles:
- Tenant auth (session-based dashboard login, API key for agents)
- Command queue: dashboard enqueues AD commands → agent polls → agent posts results
- Janus AI chat (`/dashboard/chat`) — Codex Haiku with tool-use style prompting
- Helpdesk tickets (create, assign, resolve via AI or manual)
- Scheduled email reports (background thread, runs every 30 min)
- SMTP email intake (tickets created from inbound emails)
- Admin panel at `/admin/ui` (protected by `ADMIN_KEY` env var)

### Windows Agent (`agent.py` + `ad_bridge.py`)
- Polls `GET /agent/poll` every 0.5s, authenticated by tenant API key
- Receives `{action, args, id}` commands
- Calls the matching function in `ad_bridge.py`
- Posts result back via `POST /agent/result`
- No inbound ports needed — works behind NAT, through Tailscale

### Database (`cloud/db.py`)
- SQLite for local dev, PostgreSQL for production (Railway)
- Migrations are version-gated in `migrate_db()` (currently at v5)
- Key tables: `tenants`, `tenant_users`, `commands`, `results`, `audit_log`, `chat_sessions`, `chat_messages`, `tickets`, `ticket_actions`, `usage`, `tenant_settings`, `activity_log`, `custom_scripts`

### Permission Layer (`cloud/action_policy.py`)
**This is the security backbone. Never bypass it.**
- `READ` — zero side effects, any source
- `WRITE` — reversible mutations, Janus can auto-resolve if tenant enables it
- `DESTRUCTIVE` — always requires human confirmation, Janus auto-resolve is hard-blocked at Python layer regardless of prompt
- `validate(action, source, tenant_auto_actions)` is called before every queue operation

---

## All AD Actions

### READ (safe, no confirmation needed)
`get_user_info`, `list_users`, `list_users_in_ou`, `search_users`, `list_locked_accounts`, `list_expired_passwords`, `get_stats`, `list_ous`, `list_groups`, `search_groups`, `get_group_members`, `list_group_memberships`

### WRITE (reversible, can be auto-resolved by Janus if enabled)
`unlock_account`, `enable_account`, `reset_password`, `force_password_change`, `add_to_group`, `set_password_never_expires`, `run_custom_script`

### DESTRUCTIVE (always requires human confirmation — enforced in Python, not prompt)
`disable_account`, `remove_from_group`, `create_user`, `move_user`, `create_ou`, `bulk_move_users`

---

## Janus AI

Janus is the AI assistant embedded in the dashboard. Key behaviours:
- Uses Codex Haiku via Anthropic API
- System prompt includes: all available actions + args, tenant's Janus context (custom free-text), list of enabled custom scripts
- Outputs structured JSON: `{"response": "...", "actions": [{"action": "...", "args": [...]}]}`
- LOOKUP_ACTIONS are auto-resolved silently (no confirmation modal) — includes all READ actions + `list_users_in_ou`
- DESTRUCTIVE actions trigger a confirmation modal in the frontend (6-digit code)
- **Bulk/conditional ops:** Janus can issue `bulk_move_users` with a full rules array in a single call — no looping. Example: move 500 students based on username suffix patterns, with auto-OU creation.
- **Custom scripts:** Janus resolves a script slug → fetches `ps_content` from DB → embeds as `args[0]` before queuing to agent. The agent never deals with slugs, only PS content.
- **Auto-resolve:** Janus can auto-close tickets if tenant setting `auto_resolve` is on, restricted to actions in `auto_actions` list.

### Janus Config Page (`showPage('janus-config')`)
Three panels:
1. **Janus Behaviour** — name (future), auto-resolve toggle, enabled auto-actions, janus_context free-text
2. **Scheduled Reports** — enable/disable, frequency (daily/weekly/monthly), day, hour, recipient emails
3. **Custom Scripts** — upload `.ps1` files, set slug/name/description/args/classification, Janus can call by slug

---

## Features Completed (as of last commit + uncommitted changes)

**Dashboard:**
- [x] Stats row: Users, Locked Accounts, Tickets Open, Time Saved (purple card, this month)
- [x] Activity feed
- [x] User search, user detail modal
- [x] Group management
- [x] Ticket system (slide-over modal, ticket numbers, minimise to draggable mini-bar)
- [x] Janus AI chat with action execution
- [x] Configure Janus page (Janus Behaviour + Scheduled Reports + Custom Scripts panels)
- [x] Settings page (SMTP, agent config, auto-actions, plan info)
- [x] Onboarding flow for new tenants
- [x] Admin panel at `/admin/ui`

**Agent / Backend:**
- [x] Outbound polling agent (no inbound ports)
- [x] EXE installer wizard (PyInstaller, `installer/setup_wizard.py`)
- [x] Windows Service support
- [x] HTTPS WinRM (not NTLM plaintext)
- [x] Destructive action 6-digit confirmation tokens
- [x] Email intake (inbound SMTP → tickets)
- [x] Scheduled email reports (HTML, per tenant)
- [x] Custom PowerShell scripts (upload, Janus callable by slug)
- [x] `bulk_move_users` with wildcard pattern rules + auto-OU creation
- [x] `create_ou` (idempotent)
- [x] `list_users_in_ou`
- [x] `run_custom_script` (PS content substitution with `$ARGS[n]`)
- [x] Time saved tally (conservative estimates, credible not hyped)
- [x] Ticket mini-bar (draggable, minimise ticket while working)
- [x] Multi-tenant plan limits (Starter/Pro/Enterprise)
- [x] Threat scoring on users
- [x] Janus ticket auto-resolve

**NOT YET DONE:**
- [ ] Stripe billing (explicitly deferred — build last)
- [ ] Janus persistent memory / learning system (see Future Vision below)

---

## Uncommitted Changes (need a commit)

The following changes are staged/modified but not yet committed:

- `ad_bridge.py` — `run_custom_script`, `create_ou`, `list_users_in_ou`, `bulk_move_users`
- `agent.py` — added those 4 actions to ACTIONS dict
- `cloud/action_policy.py` — `list_users_in_ou` → READ, `run_custom_script` → WRITE, `create_ou`/`bulk_move_users` → DESTRUCTIVE, REVERSIBLE entries
- `cloud/app.py` — TIME_SAVED_MINUTES, `/dashboard/api/time-saved`, custom scripts CRUD endpoints, Janus slug resolution, scheduled reports system, janus_context/report settings, upgraded system prompt (bulk ops, conditional logic, custom scripts)
- `cloud/db.py` — `custom_scripts` table (v5 migration), full CRUD functions, `get_action_counts_this_month`, `list_all_tenants`
- `cloud/templates/dashboard.html` — time saved card, Configure Janus page, custom scripts modal, ticket minimise button, draggable mini-bar, `loadJanusConfig()`, `loadScriptsList()`, `minimiseTicket()` etc.

**Suggested commit message:**
```
feat: custom scripts, bulk conditional AD ops, Configure Janus page, time saved tally, scheduled reports, ticket mini-bar

- Custom PS scripts: upload via UI, callable by Janus via slug, classification per script
- bulk_move_users: wildcard pattern rules, auto-create missing OUs, single batch PS call
- create_ou (idempotent), list_users_in_ou, run_custom_script in ad_bridge + agent
- action_policy: new actions classified (list_users_in_ou=READ, run_custom_script=WRITE, create_ou/bulk_move_users=DESTRUCTIVE)
- TIME_SAVED_MINUTES: conservative per-action estimates, /dashboard/api/time-saved endpoint
- Scheduled HTML email reports: background thread, per-tenant frequency/recipients settings
- Configure Janus page: Behaviour panel, Scheduled Reports panel, Custom Scripts panel
- Janus system prompt: bulk/conditional op rules, custom scripts context, janus_context injection
- Ticket mini-bar: minimise to draggable bar, expand/close from bar
- db v5 migration: custom_scripts table, get_action_counts_this_month, list_all_tenants
```

---

## Future Vision — Janus Memory / Persona System

**This was discussed but not built yet. High priority next feature after Stripe.**

The idea: move Janus from a stateless-per-request AI to a persistent agent that learns about the organisation over time.

### Concept
Inspired by patterns in memory-enabled agent architectures (e.g. Hermes-agent from Nous Research). The "learning" is done via retrieved context, not model fine-tuning — functionally equivalent from the user's perspective.

### Three Layers
1. **Base persona** — tenant gives their AI a name ("Max"), tone, personality. Stored in tenant_settings.
2. **Domain memory** — accumulated org-specific facts written after interactions. Examples:
   - "Username suffix 08 = Year 8, 09 = Year 9"
   - "Bulk moves happen at semester rollover (Jan, June)"
   - "Finance OU has stricter group policy — confirm before moves"
   - "Admin prefers dry-run summary before destructive ops"
3. **User profiles** — if helpdesk ticket submitters are tracked, the AI builds context on recurring requesters

### Technical Design
- New `agent_memory` table: `id, tenant_id, key, value, confidence, created_at, last_used`
- After each meaningful interaction, a "reflection pass" (small LLM call) extracts facts and writes them
- Before each Janus response, relevant memories are retrieved (keyword match at small scale, vector similarity at large scale) and injected into the system prompt
- Tenants can view/edit/delete memories from the Configure Janus page
- Memory write is triggered on: bulk operations, new patterns in requests, explicit corrections ("no, that's wrong — Finance OU is...")

### Commercial Value
- Switching cost goes from near-zero to "I'd lose years of institutional memory"
- "Your organisation's IT brain that gets smarter the longer you use it" — completely different value prop from generic AI tools
- Pairs naturally with the naming feature: admins feel ownership over "their" AI

---

## Environment Variables

| Variable | Purpose |
|---|---|
| `ADMIN_KEY` | Protects `/admin/*` endpoints |
| `SECRET_KEY` | Flask session secret |
| `PORT` | Port (default 5000) |
| `ANTHROPIC_API_KEY` | Codex Haiku for Janus |
| `DATABASE_URL` | PostgreSQL URL (omit for SQLite dev) |
| `SMTP_HOST/PORT/USER/PASS` | For sending reports and notifications |

Agent-side (in `agent-config.json`):
- `cloud_url`, `tenant_api_key`, `ad_vm_ip`, `ad_domain`, `ad_admin_user`, `ad_admin_pass`

---

## Key Design Decisions / Don't Break These

1. **Action policy is Python-enforced, not prompt-enforced.** The LLM output is a request. `action_policy.validate()` is the real gate. Never remove that call.
2. **Agent is outbound-only.** No inbound ports, no webhooks. This is a selling point. Don't change the polling architecture.
3. **Slug resolution happens in the cloud, not the agent.** When Janus calls `run_custom_script` with a slug, `app.py` fetches the PS content and embeds it as `args[0]` before queuing. The agent receives PS content directly.
4. **Bulk ops use a single PowerShell call.** `bulk_move_users` takes all rules as a JSON array, builds a single PS script. Janus should never loop `move_user` for batch operations.
5. **PostgreSQL in production, SQLite in dev.** `db.py` handles both via `_USE_PG`. The `_PH` placeholder (`%s` vs `?`) and `_cur()` abstractions handle the difference.
6. **Migrations are version-gated.** `migrate_db()` checks schema version before applying. Currently at v5. Always increment when adding tables/columns.

---

## Running Locally

```bash
# Cloud backend
cd cloud
python app.py

# Agent (separate terminal, needs agent-config.json filled in)
cd ..
python agent.py
```

The dashboard is at `http://localhost:5000`. First run creates the SQLite DB automatically.
