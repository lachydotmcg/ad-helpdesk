# AID Helpdesk

<p align="center">
  <img src="cloud/static/aid-logo.svg" alt="AID Helpdesk" width="120"/>
</p>

<p align="center">
  <strong>Your IT Admin, powered by AI.</strong><br/>
  Manage Windows Active Directory in plain English - with a smart ticket system, AI auto-resolution, and a cloud dashboard your whole team can use.
</p>

<p align="center">
  <a href="https://web-production-01ecc.up.railway.app">Live demo</a> &nbsp;·&nbsp;
  <a href="https://web-production-01ecc.up.railway.app/signup">Get started free</a>
</p>

---

## What is AID Helpdesk?

AID Helpdesk is a multi-tenant SaaS that puts an AI layer in front of your Windows Server Active Directory. Staff submit support tickets in plain English ("I'm locked out", "I need a temp password") and **Janus** - AID's built-in AI - analyses, triages, and resolves them automatically, then logs every action for your audit trail.

No scripting. No clicking through MMC consoles. Just describe the problem and it gets handled.

---

## How it works

```
Staff member submits ticket (web or email)
    │
    ▼
Janus AI analyses the request
    │ checks requester identity, permissions, security flags
    ▼
Auto-resolve (Pro+) or queue for admin approval
    │
    ▼
AID Agent (running on your Windows Server)
    │ executes PowerShell via WinRM against Active Directory
    ▼
Result logged to audit trail + email sent back to requester
```

The **AID Agent** is a lightweight Windows process that runs on your server. It polls the cloud dashboard, executes approved AD commands locally, and posts results back. No inbound ports required - works behind NAT, firewalls, and across Tailscale.

---

## Getting started

### 1. Create an account

Sign up free at [web-production-01ecc.up.railway.app/signup](https://web-production-01ecc.up.railway.app/signup). No credit card needed.

### 2. Copy your API key

Go to **Settings** in your dashboard and copy your tenant API key.

### 3. Run the agent on your Windows Server

Download `aid-agent-setup.exe` from your dashboard and double-click it. The setup wizard walks you through three screens:

1. **Cloud** - paste your API key; wizard verifies connectivity
2. **AD Credentials** - enter your AD server IP, domain name, and service account
3. **Install** - wizard copies the agent to `C:\Program Files\AID Helpdesk Agent\`, writes `agent-config.json`, and registers + starts the Windows Service automatically

That's it. Your dashboard shows **Agent: Online** and you're ready to receive tickets.

> **Build the installer yourself:** See `installer/build.bat` in this repo. Requires Python 3.9+ and PyInstaller on Windows.

---

## Features

### Janus AI ticketing
Staff submit tickets in plain English. Janus reads the request, checks the requester's identity against your AD domain, assigns a threat score (1-10), flags anything suspicious, and either resolves it automatically or surfaces it for admin review with a full analysis and recommended action.

### Janus AI chat
Talk to Janus directly in plain English to manage your AD. Janus chains lookups automatically - if it needs to search for a group before adding a user, it does both in one step without asking you to repeat yourself.

### Auto-actions (Pro+)
Janus can unlock accounts, reset passwords, enable/disable users, and more - hands-free, without waiting for an admin to click approve. Every action is logged.

### Email ticket intake (Pro+)
Point a Mailgun, SendGrid, or Postmark webhook at your dashboard and tickets flow in directly from email. Janus analyses them and sends the resolution back to the requester automatically.

### Team management
Invite helpdesk staff to your dashboard. Each team member gets their own login. Assign, comment on, and close tickets collaboratively.

### Activity feed & audit log
Every AD action - who requested it, what Janus decided, what was executed - is timestamped and searchable. Export to CSV any time.

---

## Janus AI Skills

Janus understands plain English requests and maps them to the following AD operations:

**Users**
- Look up a user's details, groups, and account state
- Search users by name or username (partial match)
- Create a new AD account with optional OU placement
- Move a user to a different Organisational Unit
- List all users in the domain

**Account state**
- Unlock a locked-out account
- Enable or disable an account
- Reset a password (generates a secure temp password)
- Force password change at next logon
- Toggle password never expires

**Groups**
- List all groups or search by name
- Add or remove a user from a group
- List all members of a group
- List all groups a user belongs to

**Organisational Units**
- List all OUs in the domain

**Reporting**
- Show all currently locked accounts
- Show all accounts with expired passwords
- Get domain summary stats (total users, locked, expired)

Janus chains lookups automatically. For example: "add sarah to the IT Support group" will search for the exact group name first, then add her - all in one step.

---

## AD operations reference

| Operation | Natural language | Auto-resolvable |
|---|---|---|
| Unlock account | "I'm locked out" | Pro+ |
| Reset password | "I need a temp password" | Pro+ |
| Enable / disable account | "Re-enable sarah's account" | Pro+ |
| Get user info | "Look up jake.miller" | - |
| Search users | "Find all users named Smith" | - |
| List locked accounts | "Who's locked out?" | - |
| List expired passwords | "Who has an expired password?" | - |
| Create user | "New IT user for Tom Brady" | - |
| Move OU | "Move test.mcgee to Teachers" | - |
| Add / remove group | "Add mike to IT-Admins" | - |
| Bulk operations | "Reset all expired passwords in Students OU" | - |

---

## Plans

| | Free | Pro | Enterprise |
|---|---|---|---|
| Janus AI scans / month | 10 | 500 | 2,000 |
| AD actions / month | 5 | 200 | 1,000 |
| Tickets | Up to 20 | Unlimited | Unlimited |
| Team members | 1 | Up to 5 | Unlimited |
| Email ticket intake | - | ✓ | ✓ |
| Janus auto-actions | - | ✓ | ✓ |
| Scheduled reports | - | ✓ | ✓ |
| Support | Community | Email | Priority |
| Price | A$0 | A$29/month | A$99/month |

---

## Self-hosting the agent

The AD agent is open source. Clone the repo and run it directly if you prefer not to use the installer:

```bash
git clone https://github.com/lachydotmcg/ad-helpdesk.git
cd ad-helpdesk
pip install -r requirements.txt
cp agent-config.example.json agent-config.json
# fill in cloud_url, tenant_api_key, ad_vm_ip, ad_domain, ad_admin_user, ad_admin_pass
python agent.py
```

**Requirements:**
- Python 3.9+ on a machine with WinRM access to your AD server
- Windows Server 2019/2022 with Active Directory Domain Services
- WinRM enabled on the server:

```powershell
Enable-PSRemoting -Force
```

- A service account in Remote Management Users and local Administrators:

```powershell
New-ADUser -Name "Helpdesk Service" -SamAccountName "svc.helpdesk" `
  -AccountPassword (ConvertTo-SecureString "YourPassword" -AsPlainText -Force) `
  -Enabled $true
Add-ADGroupMember -Identity "Remote Management Users" -Members "svc.helpdesk"
net localgroup Administrators "LAB\svc.helpdesk" /add
```

> **Tip:** Use the NetBIOS domain name (e.g. `LAB`), not the FQDN (`lab.local`). NTLM auth will fail with the FQDN.

---

## Self-hosting the cloud backend

The full cloud backend (`cloud/`) is included in this repo. You can deploy your own instance to Railway, Render, or any VPS:

```bash
cd cloud
pip install -r requirements.txt
# Set env vars: SECRET_KEY, DATABASE_URL (PostgreSQL), ANTHROPIC_API_KEY
python app.py
```

See `cloud/.env.example` for all environment variables.

> **License note:** Self-hosting for your own organisation is fine and encouraged. Reselling a hosted instance of the `cloud/` backend as a subscription service to third parties requires a separate commercial licence - see [Licence](#licence) below.

---

## Architecture

```
web-production-01ecc.up.railway.app  (cloud/app.py - Railway / any VPS)
    │  HTTPS polling
    ▼
agent.py  (runs on customer's Windows machine or server)
    │  WinRM / PowerShell Remoting
    ▼
Windows Server 2022 + Active Directory
```

Cowork / natural language mode (legacy local bridge):

```
Claude / Cowork  ->  watcher.py  ->  WinRM  ->  Active Directory
```

---

## Roadmap

- [x] v0.1 - Core WinRM bridge, CLI, PowerShell scripts, audit log
- [x] v0.2 - Local web dashboard, REST API, live user panel
- [x] v0.3 - Cowork skill for natural language AD management
- [x] v0.4 - Cloud agent, multi-tenant backend, system tray, setup wizard
- [x] v0.5 - Hosted SaaS dashboard, multi-user auth, per-tenant isolation
- [x] v0.6 - Janus AI chat + ticket analysis (Claude Haiku)
- [x] v0.7 - Smart ticketing system with AI auto-resolution, auto-actions
- [x] v0.8 - Email ticket intake, enterprise tier, usage-based plans
- [x] v0.9 - Threat scores, search chaining, ticket views, onboarding panel
- [ ] v1.0 - Windows Service installer (.exe), Stripe billing, scheduled reports
- [ ] v1.1 - Usage top-ups, mobile-friendly dashboard, Slack/Teams integration

---

## Security

- Credentials live only in environment variables - never hardcoded, never committed
- The agent uses a dedicated service account (`svc.helpdesk`) rather than domain Administrator
- WinRM runs over HTTP - acceptable within a Tailscale tunnel (encrypted end-to-end); do not expose port 5985 to the open internet
- All write operations are logged with timestamp, requester identity, and Janus confidence score
- Janus flags requests where the requester email domain does not match your configured trusted domain

---

## Contributing

PRs welcome on the agent, PowerShell scripts, and skill. Please open an issue first for major changes. The `cloud/` backend is source-available - bug fixes and improvements are welcome, but forks intended as competing hosted services are not.

---

## Licence

**Agent, bridge, CLI, PowerShell scripts, Cowork skill** (`agent.py`, `ad_bridge.py`, `cli.py`, `ps-scripts/`, `skill/`, `watcher.py`): [MIT](https://opensource.org/licenses/MIT)

**Cloud SaaS backend** (`cloud/`): MIT + [Commons Clause](https://commonsclause.com/)

The Commons Clause means: you may use, modify, and self-host the cloud backend freely, but you may not sell it - i.e. offer a hosted version of AID Helpdesk as a subscription service to others - without a separate commercial agreement. Contact [lachyswebdev@gmail.com](mailto:lachyswebdev@gmail.com) if you want to discuss licensing.
