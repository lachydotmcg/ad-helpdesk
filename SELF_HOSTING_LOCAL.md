# Run AID Helpdesk fully locally (no cloud, no cost)

AID Helpdesk is the same codebase whether you use the hosted SaaS or run the whole
thing yourself on-prem. This guide is the **local edition**: one organisation, your
own AI, no Anthropic key, no billing, no data leaving your network.

You get the full platform - Active Directory, DNS, DHCP, Group Policy, NPS, Entra,
app deployment, tickets, and the AI assistant - running on your own hardware, with
the assistant powered by a local model via [Ollama](https://ollama.com).

## What "local" means here

```
  Your browser
       |
       v
  AID dashboard (Flask)  ----> Ollama (local LLM, e.g. llama3)   [AI_PROVIDER=ollama]
       |                        on this box or any box on your LAN
       v
  AID agent  --WinRM/HTTPS-->  Windows Server (Active Directory, DNS, DHCP, ...)
```

Nothing calls out to the internet for AI or billing. The dashboard, the database
(SQLite by default), the agent, and the model all run on infrastructure you control.

## Quickstart

### 1. Install a local model with Ollama

Download [Ollama](https://ollama.com), then pull a model:

```bash
ollama pull llama3          # solid default; qwen2.5 or mistral also work well
```

Ollama can run on the same box as the dashboard, or on a separate machine - just
point `OLLAMA_URL` at it (e.g. `http://192.168.1.50:11434`).

### 2. Configure the environment

Copy `cloud/.env.example` to `.env` and set:

```bash
AI_PROVIDER=ollama
OLLAMA_URL=http://localhost:11434
OLLAMA_MODEL=llama3

AID_LOCAL_MODE=1
AID_LOCAL_ORG=Acme IT
AID_LOCAL_ADMIN_EMAIL=admin@acme.local
AID_LOCAL_ADMIN_PASSWORD=pick-a-good-password

SECRET_KEY=change-this-to-a-long-random-string
SETTINGS_ENCRYPTION_KEY=change-this-to-a-long-random-string
```

No `ANTHROPIC_API_KEY`, no `ADMIN_KEY`, no Stripe, no `DATABASE_URL` needed. (SQLite
is used automatically when `DATABASE_URL` is unset. For multiple machines or heavier
use, point `DATABASE_URL` at your own Postgres.)

### 3. Run it

```bash
pip install -r requirements.txt
python cloud/app.py
```

On first start you'll see the provisioned login printed to the console:

```
 AID Helpdesk -- local mode
 ----------------------------
 Organisation: Acme IT
 Sign in at /login with:  admin@acme.local / pick-a-good-password
 Agent API key:           3f9c...
 AI provider:             ollama (http://localhost:11434)
```

Open `http://localhost:5000`, sign in, and change the admin password in Settings.

### 4. Connect the agent to your Windows Server

Install the AID agent on a box that can reach your domain controller over WinRM,
paste the **Agent API key** from the console output and your AD credentials, and
point its `cloud_url` at your dashboard (e.g. `http://your-box:5000`). The agent
polls outbound, so no inbound ports or VPN are required. See the main README's
agent section.

## Cloud vs local - same code, two modes

| | Cloud SaaS | Local self-host |
|---|---|---|
| AI | Anthropic (`AI_PROVIDER=anthropic`) | Ollama (`AI_PROVIDER=ollama`) |
| Tenants | Multi-tenant | Single org (`AID_LOCAL_MODE=1`) |
| Sign-up / billing | Yes (Stripe) | None |
| Database | Postgres | SQLite (or your own Postgres) |
| Cost | Per-plan | Free |
| Data location | Hosted | Entirely on your network |

Switching modes is purely configuration - there is no separate build. That keeps the
local and cloud editions from drifting apart.

## Notes

- The **intelligence tiers** (Normal / High) in Configure AI are an Anthropic concept.
  In Ollama mode the assistant always uses `OLLAMA_MODEL`; swap that env var to change
  models. (A future version may map tiers to different local models.)
- Ollama replies are only as good as the model you pull. For agentic action-planning,
  a 7B+ instruct model is the practical floor; larger models plan multi-step actions
  more reliably.
- Everything else - the AD/DNS/DHCP/GPO/NPS/deploy bridges, tickets, audit log, the
  command palette - works identically in both modes.
