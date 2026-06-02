# AID Helpdesk

<p align="center">
  <img src="cloud/static/aid-logo.svg" alt="AID Helpdesk" width="120"/>
</p>

<p align="center">
  <strong>Your IT Admin, powered by AI.</strong><br/>
  Manage Windows Active Directory in plain English, with a smart ticket system,<br/>
  your own named AI assistant, and a cloud dashboard your whole team can use.
</p>

<p align="center">
  <a href="https://web-production-01ecc.up.railway.app">Live demo</a> &nbsp;·&nbsp;
  <a href="https://web-production-01ecc.up.railway.app/signup">Get started free</a>
</p>

<p align="center">
  <img alt="License" src="https://img.shields.io/badge/license-MIT%20%2B%20Commons%20Clause-blue"/>
  <img alt="Python" src="https://img.shields.io/badge/python-3.9%2B-blue"/>
</p>

---

## What is AID Helpdesk?

AID Helpdesk is a multi-tenant SaaS that puts an AI layer in front of your Windows Server Active Directory. Staff submit support tickets in plain English ("I'm locked out", "I need a temp password") and **your AI assistant** (which you name, and which gets smarter about your organisation over time) analyses, triages, and resolves them automatically, then logs every action for your audit trail.

No scripting. No clicking through MMC consoles. Just describe the problem and it gets handled.

---

## How it works

```
  Staff Browser
       │  HTTPS
       ▼
  ┌──────────────────────────────────┐
  │   AID Cloud Dashboard            │  ← Railway / any VPS
  │   web-production-01ecc.up.       │
  │   railway.app                    │
  └──────────────┬───────────────────┘
                 │
                 │  outbound HTTPS polling
                 │  ◀── no inbound ports · no VPN · no firewall rules
                 ▼
  ┌──────────────────────────────────┐
  │   AID Agent                      │  ← Windows Service on your server
  │   (agent.py)                     │
  └──────────────┬───────────────────┘
                 │
                 │  WinRM over HTTPS (TLS · port 5986 · local network only)
                 ▼
  ┌──────────────────────────────────┐
  │   Windows Server                 │
  │   Active Directory               │  ← never touches the internet
  └──────────────────────────────────┘
```

The **AID Agent** is a lightweight Windows Service that polls *outbound*; your Active Directory never exposes itself to the internet, no firewall rules are needed, and it works behind NAT, Tailscale, or any network topology.

---

## Quickstart

### 1. Create an account

Sign up free at [web-production-01ecc.up.railway.app/signup](https://web-production-01ecc.up.railway.app/signup). No credit card needed.

### 2. Copy your API key

Go to **Settings** in your dashboard and copy your tenant API key.

### 3. Install the agent on your Windows Server

Download `aid-agent-setup.exe` from your dashboard and run it. The setup wizard walks you through three screens:

1. **Cloud**: paste your API key; wizard verifies connectivity
2. **AD Credentials**: enter your AD server IP, domain name, and service account
3. **Install**: wizard copies the agent to `C:\Program Files\AID Helpdesk Agent\`, writes `agent-config.json`, and registers + starts the Windows Service

Your dashboard shows **Agent: Online** and you're ready to receive tickets.

> **Build the installer yourself:** `installer/build.bat`; requires Python 3.9+ and PyInstaller on Windows.

---

## Features

### Your own named AI assistant
Give your AI a name that fits your organisation: "Max", "Alex", or whatever your team will actually use. It's not a generic chatbot; it's your organisation's AI, with its own name, its own understanding of your environment, and its own growing memory of how your AD is structured.

### AI that learns your environment
Your AI assistant builds institutional knowledge over time: username patterns, OU structure, team naming conventions, recurring requests. The longer you use AID Helpdesk, the more it understands about your specific environment without you having to explain it every time. It's the IT brain that never forgets.

### Smart ticketing
Staff submit tickets in plain English. Your AI reads the request, checks the requester's identity against your AD domain, assigns a threat score (1-10), flags anything suspicious, and either resolves it automatically or surfaces it for admin review with a full analysis and recommended action.

### AI chat
Talk to your AI directly in plain English to manage your AD. It chains lookups automatically; if it needs to find a group before adding a user, it does both in one step without asking you to repeat yourself.

### Auto-actions (Pro+)
Unlock accounts, reset passwords, enable accounts, hands-free, without waiting for an admin to click approve. Every action is logged.

### Email ticket intake (Pro+)
Point a Mailgun, SendGrid, or Postmark webhook at your dashboard and tickets flow in from email. Your AI sends the resolution back to the requester automatically.

### Scheduled reports (Pro+)
Automated HTML email reports on a schedule you set: daily, weekly, or monthly.

### Full audit trail
Every action (who requested it, what the AI decided, what was executed) is timestamped, searchable, and exportable to CSV.

---

## Audit log

Every AD action produces a structured, immutable log entry:

```
2025-11-03 08:42:17 UTC  requester=sarah.jones@school.edu  action=unlock_account   target=CN=Tom Brady,OU=Staff,DC=lab,DC=local       approval=ai-auto (confidence 0.96)       executor=svc.helpdesk@lab.local
2025-11-03 08:59:04 UTC  requester=admin@school.edu        action=reset_password   target=CN=Jake Miller,OU=Students,DC=lab,DC=local   approval=ai-auto (confidence 0.91)       executor=svc.helpdesk@lab.local
2025-11-03 09:14:38 UTC  requester=admin@school.edu        action=disable_account  target=CN=Ex Teacher,OU=Staff,DC=lab,DC=local       approval=human-confirmed (token 482016)  executor=svc.helpdesk@lab.local
```

Destructive actions like `disable_account` always require a human-confirmed 6-digit token; no exceptions, regardless of what the AI suggests.

---

## Plans

| | Free | Pro | Enterprise |
|---|---|---|---|
| AI scans / month | 10 | 500 | 2,000 |
| AD actions / month | 5 | 200 | 1,000 |
| Tickets | Up to 20 | Unlimited | Unlimited |
| Team members | 1 | Up to 5 | Unlimited |
| Email ticket intake | — | ✓ | ✓ |
| AI auto-actions | — | ✓ | ✓ |
| Scheduled reports | — | ✓ | ✓ |
| Support | Community | Email | Priority |
| Price | A$0 | A$29/month | A$99/month |

**MSP licence:** Managing multiple client environments under one agreement? Contact [lachyswebdev@gmail.com](mailto:lachyswebdev@gmail.com).

---

## Documentation

| | |
|---|---|
| [SECURITY.md](SECURITY.md) | AI safety model, trust architecture, WinRM security, tenant isolation, audit logging |
| [ARCHITECTURE.md](ARCHITECTURE.md) | Full system design, polling model, action flow, DB schema |
| [DEPLOYMENT.md](DEPLOYMENT.md) | Deploy to Railway, environment variables, PostgreSQL |
| [SELF_HOSTING.md](SELF_HOSTING.md) | Run the agent or full backend yourself |

---

## Roadmap

- [x] v0.1-v0.4: WinRM bridge, local dashboard, cloud agent, multi-tenant backend
- [x] v0.5-v0.9: Hosted SaaS, AI assistant, ticketing, threat scores, email intake, search chaining
- [x] v1.0: Windows Service installer (.exe), scheduled reports, custom PS scripts, bulk AD ops
- [ ] v1.1: Named AI persona, AI memory / organisational learning, Slack/Teams integration

---

## Contributing

PRs welcome on the agent, PowerShell scripts, and skill. Please open an issue first for major changes. The `cloud/` backend is source-available; bug fixes and improvements are welcome, but forks intended as competing hosted services are not.

---

## Licence

**Agent + bridge** (`agent.py`, `ad_bridge.py`, scripts): [MIT](https://opensource.org/licenses/MIT)

**Cloud SaaS backend** (`cloud/`): MIT + [Commons Clause](https://commonsclause.com/); self-host freely, do not resell as a hosted service without a commercial agreement. Contact [lachyswebdev@gmail.com](mailto:lachyswebdev@gmail.com).
