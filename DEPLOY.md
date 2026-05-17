# Deploying AID Helpdesk to Railway

## One-time setup (~5 minutes)

1. Push this repo to GitHub
2. Go to railway.app → New Project → Deploy from GitHub → select your repo
3. Railway → your project → New → Database → Add PostgreSQL
4. Railway auto-sets `DATABASE_URL` — the app detects this automatically and uses PostgreSQL
5. Set these env vars in Railway dashboard (Settings → Variables):
   - `SECRET_KEY` = (any random 32+ char string)
   - `ADMIN_KEY` = (your chosen admin secret for creating tenants)
   - `ANTHROPIC_API_KEY` = (from console.anthropic.com)
6. Click Deploy

## After first deploy — create your tenant account

Run this in PowerShell (replace `YOUR-APP` and `YOUR_ADMIN_KEY`):

```powershell
$body = @{ name="Your Company"; email="you@company.com"; password="yourpassword" } | ConvertTo-Json
Invoke-RestMethod -Uri "https://YOUR-APP.railway.app/admin/tenants" `
  -Method POST -Body $body -ContentType "application/json" `
  -Headers @{"X-Admin-Key"="YOUR_ADMIN_KEY"}
```

Save the `api_key` from the response — you'll need it for the Windows agent.

## Find your API key anytime

Log into the dashboard → Settings → **Agent Configuration** — your API key is shown there.

## Configure the Windows agent

Edit `agent-config.json` on your AD server:

```json
{
  "cloud_url": "https://YOUR-APP.railway.app",
  "tenant_api_key": "KEY_FROM_SETTINGS_PAGE",
  "timeout_seconds": 15
}
```

Then run:
```
python agent.py
```

## Optional: Email notifications

Add these env vars in Railway (Settings → Variables) to enable automatic email replies when tickets are resolved:

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=helpdesk@yourcompany.com
SMTP_PASS=your-gmail-app-password
SMTP_FROM=helpdesk@yourcompany.com
```

For Gmail, use an App Password (Google Account → Security → 2FA → App passwords).
