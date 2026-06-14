# Self-hosting AID Helpdesk

You can self-host the agent, the full cloud backend, or both. The entire codebase is in this repo.

---

## Self-hosting the agent only

The agent is MIT-licensed and fully open source.

### Requirements

- Python 3.9+ on a machine with WinRM access to your AD server
- Windows Server 2019/2022 with Active Directory Domain Services
- WinRM enabled on the server

### Steps

```bash
git clone https://github.com/lachydotmcg/ad-helpdesk.git
cd ad-helpdesk
pip install -r requirements.txt
cp agent-config.example.json agent-config.json
# Edit agent-config.json — see field reference below
python agent.py
```

### agent-config.json fields

| Field | Description |
|---|---|
| `cloud_url` | URL of your cloud backend (e.g. `https://your-app.railway.app`) |
| `tenant_api_key` | Your tenant API key from the dashboard Settings page |
| `ad_vm_ip` | IP address of your Windows Server |
| `ad_domain` | NetBIOS domain name (e.g. `LAB`, not `lab.local`) |
| `ad_admin_user` | Service account username (e.g. `svc.helpdesk`) |
| `ad_admin_pass` | Service account password |
| `timeout_seconds` | WinRM command timeout (default: 15) |

> **Use the NetBIOS domain name** (e.g. `LAB`), not the FQDN (`lab.local`). NTLM auth will fail with the FQDN.

---

## Keeping the agent running without pywin32

The agent connects **outbound only** — it never listens on a port. That means it does not have to run on the AD server itself: any always-on Windows machine with WinRM (or Tailscale) reach to the AD server can host it. Because of that, you don't need a true Windows Service (and the `pywin32` package the service installer relies on) just to keep it alive. The simplest dependency-free option is **Windows Task Scheduler**, which is built into every Windows install.

### Prerequisites

- Put your WinRM credentials in a `.env` file in the repo root (the same folder as `agent.py`). `ad_bridge.py` loads it automatically via `python-dotenv`:

  ```ini
  AD_VM_IP=100.x.x.x
  AD_DOMAIN=LAB
  AD_ADMIN_USER=svc.helpdesk
  AD_ADMIN_PASS=YourPassword
  # Force plain-HTTP WinRM on port 5985 instead of HTTPS 5986.
  # Safe when the agent reaches the AD server over a Tailscale tunnel,
  # since the tunnel already encrypts the traffic end to end.
  AD_WINRM_HTTP=1
  ```

  > `AD_WINRM_HTTP` is read from the environment only (it is **not** one of the keys `agent.py` injects from `agent-config.json`), so it must live in `.env` or be set as a real environment variable. `cloud_url` and `tenant_api_key` still come from `agent-config.json`.

### Register the task

Run this once from an **elevated** Command Prompt or PowerShell. It launches `python agent.py` every time you log on. Replace the path with your actual checkout location:

```bat
schtasks /create /tn "AID Helpdesk Agent" /sc onlogon /rl highest /f ^
  /tr "cmd /c cd /d C:\Users\you\ad-helpdesk && python agent.py"
```

- **Working directory matters.** The `cd /d C:\Users\you\ad-helpdesk` is required so the agent finds `agent-config.json` and loads `.env` from the repo root. Without it, Task Scheduler starts in `C:\Windows\System32` and `.env` won't be picked up.
- If `python` isn't on the system `PATH`, use the full interpreter path, e.g. `cd /d C:\Users\you\ad-helpdesk && "C:\Python312\python.exe" agent.py`.
- `/sc onlogon` starts it at user logon; swap for `/sc onstart` if you want it to run at boot before anyone logs in (requires `/ru SYSTEM`).

### Start it now and verify

```bat
:: Start immediately without logging out
schtasks /run /tn "AID Helpdesk Agent"

:: Confirm it's registered and see Last Run Time / Last Result
schtasks /query /tn "AID Helpdesk Agent" /v /fo LIST
```

A healthy `Last Result` is `0x0` (still running) or the task showing as **Running** in `taskschd.msc`. The most reliable confirmation is on the cloud side: the dashboard **Settings → Agent** status flips to connected within a few seconds once the agent starts polling. To stop or remove it:

```bat
schtasks /end    /tn "AID Helpdesk Agent"
schtasks /delete /tn "AID Helpdesk Agent" /f
```

---

## Enable WinRM on your AD server

```powershell
Enable-PSRemoting -Force
```

Use HTTPS WinRM (port 5986) — the agent defaults to this. Restrict firewall rules to your local subnet. See [SECURITY.md](SECURITY.md) for full WinRM guidance.

---

## Create a minimal service account

The agent does **not** need Domain Admin rights. Use a dedicated account with delegated permissions only:

```powershell
# 1. Create the service account
New-ADUser -Name "Helpdesk Service" -SamAccountName "svc.helpdesk" `
  -AccountPassword (ConvertTo-SecureString "YourPassword" -AsPlainText -Force) `
  -Enabled $true -PasswordNeverExpires $true

# 2. Allow WinRM access
Add-ADGroupMember -Identity "Remote Management Users" -Members "svc.helpdesk"

# 3. Delegate OU permissions (adjust OU path to match your domain)
$ou = "OU=YourOU,DC=lab,DC=local"
dsacls $ou /G "LAB\svc.helpdesk:CA;Reset Password;user" /I:S
dsacls $ou /G "LAB\svc.helpdesk:RPWP;pwdLastSet;user" /I:S
dsacls $ou /G "LAB\svc.helpdesk:RPWP;lockoutTime;user" /I:S
dsacls $ou /G "LAB\svc.helpdesk:RPWP;userAccountControl;user" /I:S
```

> `svc.helpdesk` does **not** need to be a member of local Administrators or Domain Admins. The delegated permissions above give it exactly what it needs and nothing more.

---

## Self-hosting the full cloud backend

The cloud backend (`cloud/`) is MIT + Commons Clause licensed. Self-hosting for your own organisation is free and encouraged. Reselling a hosted instance as a subscription service to third parties requires a separate commercial licence — contact [lachyswebdev@gmail.com](mailto:lachyswebdev@gmail.com).

### Requirements

- Python 3.9+
- PostgreSQL (or SQLite for local dev/test)
- An Anthropic API key

### Steps

```bash
git clone https://github.com/lachydotmcg/ad-helpdesk.git
cd ad-helpdesk
pip install -r requirements.txt
```

**Local dev (SQLite — no DATABASE_URL needed):**

```bash
cd cloud
python app.py
```

Dashboard is at `http://localhost:5000`. The DB is created automatically on first run.

**Production (PostgreSQL):**

```bash
export DATABASE_URL=postgresql://user:pass@host/dbname
export SECRET_KEY=your-secret-key
export ADMIN_KEY=your-admin-key
export ANTHROPIC_API_KEY=sk-ant-...
python -m gunicorn cloud.app:app
```

See [DEPLOYMENT.md](DEPLOYMENT.md) for the full Railway deployment guide.

---

## Building the Windows installer

```bat
cd installer
build.bat
```

Requires Python 3.9+ and PyInstaller on Windows. Output: `installer/dist/aid-agent-setup.exe`.
