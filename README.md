# AD Helpdesk

A lightweight, self-hosted Active Directory automation bridge. Manage users, reset passwords, unlock accounts, and handle group membership — all from Python or a browser. No expensive enterprise tooling required.

Built for IT labs, small businesses, and anyone running Windows Server AD who wants simple automation without the overhead.

---

## What It Does

| Operation | CLI | API | Dashboard |
|-----------|-----|-----|-----------|
| Get user info | ✅ | ✅ | ✅ |
| Reset password | ✅ | ✅ | ✅ |
| Unlock account | ✅ | ✅ | ✅ |
| Enable / disable | ✅ | ✅ | ✅ |
| Add / remove group | ✅ | ✅ | ✅ |
| Create user | ✅ | ✅ | ✅ |

Every operation is logged with a timestamp to `ps-scripts/audit.log`.

---

## Architecture

```
Your Machine (Mac / Linux / Windows)
    │
    │  WinRM over HTTP (port 5985)
    │  NTLM authentication
    ▼
Windows Server 2022 VM
    ├─ Active Directory Domain Services
    ├─ WinRM / PowerShell Remoting enabled
    └─ ps-scripts/*.ps1  (executed remotely)
```

Works across networks via **Tailscale** — no VPN or port forwarding required.

---

## Prerequisites

### Your machine
- Python 3.9+
- Network access to the VM (same LAN, or Tailscale)

### Windows Server VM
- Windows Server 2019 / 2022
- Active Directory Domain Services installed and promoted
- WinRM enabled (run as Administrator on the VM):

```powershell
Enable-PSRemoting -Force
```

- Service account in **Remote Management Users** and local **Administrators**:

```powershell
# Create a dedicated service account (recommended)
New-ADUser -Name "Helpdesk Service" -SamAccountName "svc.helpdesk" `
  -AccountPassword (ConvertTo-SecureString "YourPassword" -AsPlainText -Force) `
  -Enabled $true

# Add to required groups
Add-ADGroupMember -Identity "Remote Management Users" -Members "svc.helpdesk"
net localgroup Administrators "LAB\svc.helpdesk" /add
```

> **Note:** `Add-LocalGroupMember` does not work reliably on Domain Controllers. Use `net localgroup` instead.

---

## Setup

### 1. Clone the repo

```bash
git clone https://github.com/yourusername/ad-helpdesk.git
cd ad-helpdesk
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

### 3. Configure

```bash
cp .env.example .env
```

Edit `.env`:

```
AD_VM_IP=100.x.x.x       # VM's IP (Tailscale IP recommended)
AD_DOMAIN=LAB             # NetBIOS name, NOT lab.local
AD_ADMIN_USER=svc.helpdesk
AD_ADMIN_PASS=yourpassword
```

> **AD_DOMAIN must be the NetBIOS name** (e.g. `LAB`), not the FQDN (`lab.local`). NTLM auth will fail otherwise.

### 4. Test the connection

```bash
python test_connection.py
```

You should see a table of your AD users. Common errors and fixes:

| Error | Fix |
|-------|-----|
| `credentials were rejected` | Check AD_DOMAIN is the NETBIOS name, not FQDN |
| `Access is denied` | Add service account to Remote Management Users and run `net localgroup Administrators "LAB\svc.helpdesk" /add` |
| `Connection refused` | Run `Enable-PSRemoting -Force` on the VM; check firewall allows port 5985 |

---

## Usage

### CLI

```bash
python cli.py get_user_info sarah.chen
python cli.py reset_password sarah.chen TempPass123!
python cli.py unlock_account john.smith
python cli.py add_to_group sarah.chen "Help Desk"
python cli.py remove_from_group sarah.chen "Help Desk"
python cli.py disable_account sarah.chen
python cli.py enable_account sarah.chen
python cli.py create_user Sarah Chen sarah.chen "OU=Staff,DC=lab,DC=local"
```

### Python API

```python
from ad_bridge import reset_password, get_user_info, unlock_account

result = reset_password("sarah.chen", "TempPass123!")
# {"success": True, "message": "Password reset for sarah.chen...", "data": {...}}

result = get_user_info("sarah.chen")
print(result["data"]["Enabled"])  # True
```

### Web API (api_server.py)

Start the server:

```bash
python api_server.py
```

Endpoints:

```
GET  /health
GET  /user/<username>
POST /user/<username>/reset_password    { "temp_password": "..." }
POST /user/<username>/unlock
POST /user/<username>/enable
POST /user/<username>/disable
POST /user/<username>/groups/<group>
DELETE /user/<username>/groups/<group>
POST /user                              { "first": "", "last": "", "username": "", "ou": "" }
```

All endpoints accept an `X-API-Key` header matching `API_KEY` in your `.env`.

### AI / Automation bridge (watcher.py)

Allows external tools to trigger AD operations via the filesystem — no direct WinRM access needed from the calling tool:

```bash
python watcher.py
```

Write a `cmd.json` to the project folder:

```json
{"id": "001", "action": "get_user_info", "args": ["sarah.chen"]}
```

Read the result from `result.json`:

```json
{"id": "001", "success": true, "message": "...", "data": {...}}
```

Supported actions: `get_user_info`, `reset_password`, `unlock_account`, `disable_account`, `enable_account`, `add_to_group`, `remove_from_group`, `create_user`

---

## Audit Log

Every operation appended to `ps-scripts/audit.log`:

```
2024-01-15 09:32:11 | Reset-Password | User=sarah.chen | Status=SUCCESS | Password reset, ChangePasswordAtLogon=True
2024-01-15 09:45:02 | Unlock-Account | User=john.smith | Status=SUCCESS | Account unlocked
2024-01-15 10:01:44 | Create-User    | User=new.user   | Status=FAILED  | User already exists
```

---

## Security

- Credentials live only in `.env` — never hardcoded, never committed
- Use a **dedicated service account** (`svc.helpdesk`) rather than domain Administrator
- WinRM is HTTP-only — acceptable within a Tailscale tunnel (encrypted end-to-end), not suitable for open internet exposure
- Never expose port 5985 to the internet
- `API_KEY` header protects the web API from unauthorised access

---

## Roadmap

- [x] v0.1 — Core bridge (WinRM, CLI, PowerShell scripts, audit log, AI file queue)
- [x] v0.2 — Web dashboard (Flask UI, REST API, live user panel, search, stats)
- [x] v0.3 — Cowork skill for natural language AD management
- [ ] v1.0 — Docker image, HTTPS, role-based access, demo mode

---

## Contributing

PRs welcome. Please open an issue first for major changes.

---

## License

MIT
"# ad-helpdesk" 
