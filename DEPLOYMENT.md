# Deploying AID Helpdesk to Railway

## One-time setup (~5 minutes)

1. Push this repo to GitHub
2. Railway → **New Project** → **Deploy from GitHub** → select your repo
3. Railway → your project → **New** → **Database** → **Add PostgreSQL**
4. Railway auto-sets `DATABASE_URL` — the app detects this and uses PostgreSQL automatically
5. Set environment variables in Railway (Settings → Variables):

| Variable | Value |
|---|---|
| `SECRET_KEY` | Any random 32+ character string |
| `ADMIN_KEY` | Your chosen admin secret for creating tenants |
| `ANTHROPIC_API_KEY` | From [console.anthropic.com](https://console.anthropic.com) |

6. Click **Deploy**

---

## Environment variables (full reference)

| Variable | Required | Purpose |
|---|---|---|
| `SECRET_KEY` | Yes | Flask session signing key |
| `ADMIN_KEY` | Yes | Protects `/admin/*` endpoints |
| `ANTHROPIC_API_KEY` | Yes | Claude Haiku for AI assistant |
| `DATABASE_URL` | Auto-set by Railway | PostgreSQL connection string |
| `PORT` | Auto-set by Railway | Server port (default 5000) |
| `SMTP_HOST` | Optional | Outbound email (reports, ticket replies, password resets) |
| `SMTP_PORT` | Optional | Usually 587 |
| `SMTP_USER` | Optional | SMTP login |
| `SMTP_PASS` | Optional | SMTP password or app password |
| `SMTP_FROM` | Optional | From address for outbound email |
| `STRIPE_PUBLIC_KEY` | Optional | Enables Stripe billing UI |

---

## After first deploy — create your tenant account

Run this in PowerShell (replace `YOUR-APP` and `YOUR_ADMIN_KEY`):

```powershell
$body = @{ name="Your Company"; email="you@company.com"; password="yourpassword" } | ConvertTo-Json
Invoke-RestMethod -Uri "https://YOUR-APP.railway.app/admin/tenants" `
  -Method POST -Body $body -ContentType "application/json" `
  -Headers @{"X-Admin-Key"="YOUR_ADMIN_KEY"}
```

Save the `api_key` from the response — you'll need it when configuring the Windows agent.

Find your API key any time: dashboard → **Settings** → **Agent Configuration**.

---

## Procfile

```
web: gunicorn cloud.app:app
```

This is already committed. Railway picks it up automatically.

---

## Email (optional)

To enable outbound email (scheduled reports, ticket resolution replies):

```
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=helpdesk@yourcompany.com
SMTP_PASS=your-app-password
SMTP_FROM=helpdesk@yourcompany.com
```

For Gmail, use an App Password (Google Account → Security → 2-Step Verification → App passwords).

---

## Database migrations

Migrations run automatically on startup via `migrate_db()` in `cloud/db.py`. The schema version is tracked in the `schema_version` table (currently **v5**). No manual steps required — just deploy and the schema is updated.
