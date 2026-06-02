# Stripe Billing Integration Plan

Reviewed on 2026-06-01. This is a planning document only; no application code
has been changed as part of this review.

Official Stripe references checked:

- Checkout Sessions API: https://docs.stripe.com/api/checkout/sessions
- Checkout Session creation: https://docs.stripe.com/api/checkout/sessions/create
- Subscription lifecycle/statuses: https://docs.stripe.com/billing/subscriptions/overview
- Subscription webhooks: https://docs.stripe.com/billing/subscriptions/webhooks
- Webhook signature verification, Python: https://docs.stripe.com/webhooks?lang=python
- Customer portal sessions: https://docs.stripe.com/api/customer_portal/sessions/create
- Products/prices and lookup keys: https://docs.stripe.com/products-prices/manage-prices

## Current Codebase Findings

Stripe is already partially wired, but it is not launch-ready.

Existing billing code:

- `cloud/app.py:987` renders `GET /billing`.
- `cloud/app.py:1069` has `POST /billing/create-checkout-session`.
- `cloud/app.py:1107` has `GET /billing/success`.
- `cloud/app.py:1115` has `POST /billing/webhook`.
- `cloud/templates/billing.html` has checkout buttons and usage display.
- `requirements.txt` includes `stripe>=9.0.0`; `cloud/requirements.txt` does not.
  Railway currently starts from the repo root, so the root requirements file is
  the deployed dependency source. Keep `cloud/requirements.txt` in sync anyway
  for local or cloud-directory deploys.

Current billing storage is too thin:

- `cloud/db.py:60` stores only `tenants.plan`.
- `cloud/db.py:1126` defines the active plan matrix in `PLAN_LIMITS`.
- There is no Stripe customer ID, subscription ID, subscription status, price ID,
  current period, cancellation state, last invoice, or webhook idempotency table.

Current Stripe gaps:

- Checkout is not admin-only today; any logged-in dashboard user can attempt a
  subscription change.
- Checkout does not persist or reuse a Stripe Customer.
- Checkout can create duplicate subscriptions for the same tenant.
- Checkout uses inline `price_data` when env price IDs are missing. That is useful
  during prototyping but should be disabled in production.
- Checkout stores metadata on the Checkout Session only; subscription metadata is
  not set, so `customer.subscription.deleted` cannot reliably map back to a tenant.
- The webhook only handles `checkout.session.completed` and
  `customer.subscription.deleted`.
- The webhook trusts event metadata instead of mapping Stripe Price IDs to app
  plan tiers.
- The webhook has no idempotency handling even though Stripe retries events.
- The webhook allows unsigned payloads when `STRIPE_WEBHOOK_SECRET` is unset.
  That should only ever happen in local development.
- There is no customer portal endpoint for card updates, invoices, cancellation,
  or plan changes.

Current feature gates:

- Existing good gates:
  - `/webhook/email/<api_key>` checks `limits["email_intake"]`.
  - Dashboard and inbound ticket creation check `limits["tickets"]`.
  - Team member creation checks `limits["team_members"]`.
  - `/api/v1/actions/<action>` uses `action_policy` plus `ad_commands` quota.
  - AI auto-resolve uses `limits["auto_actions"]` and `action_policy`.
- Gates to fix:
  - `/dashboard/api/exec` uses a stale local `write_actions` set and misses newer
    billable mutations like `run_custom_script`, `create_ou`, and
    `bulk_move_users`.
  - `_run_chat` increments `ai_calls` after LLM work; it should preflight the
    AI quota before calling Anthropic.
  - `_run_chat` chained actions are queued without rerunning the full policy,
    confirmation, and quota path.
  - `/dashboard/api/tickets/<id>/apply-fix` queues ticket fixes without policy,
    destructive confirmation, or `ad_commands` quota checks.
  - `/dashboard/api/scripts/*` has no plan gate or script count limit.
  - `/dashboard/api/settings` allows enabling auto-actions, scheduled reports,
    and integrations without server-side plan checks.
  - `/dashboard/api/integrations/test` has no plan gate.
  - `_run_scheduled_reports` sends reports without checking the tenant plan at
    send time.
  - `/dashboard/api/insights` consumes an LLM call without checking or counting
    AI quota.
  - `/dashboard/api/memory/*` is live despite memory being described as future
    work; gate it deliberately or hide it until launch.
  - Legacy `/api/command` queues commands without action policy, destructive
    confirmation, audit logging, or quota enforcement. Deprecate it or route it
    through the same execution helper as the newer APIs.
  - Legacy `/webhook/email` creates a ticket on the first tenant and bypasses the
    plan-aware `/webhook/email/<api_key>` path. Remove, disable, or gate it.

## Stripe Catalog To Create

Use one Stripe Product per paid app tier. This makes Checkout, invoices, Customer
Portal, and webhook plan mapping show the tier explicitly instead of only
showing a generic `AID Helpdesk` product.

Products:

| App plan | Stripe product name | Metadata |
|---|---|---|
| Free | none | No Stripe subscription |
| Pro | `AID Helpdesk Pro` | `app=aid_helpdesk`, `plan=pro` |
| Enterprise | `AID Helpdesk Enterprise` | `app=aid_helpdesk`, `plan=enterprise` |

Prices:

| App plan | Stripe price nickname | Lookup key | Unit amount | Currency | Interval | Metadata |
|---|---|---|---:|---|---|---|
| Free | none | none | 0 | n/a | n/a | No Stripe subscription |
| Pro | `AID Helpdesk Pro Monthly` | `aid_helpdesk_pro_monthly_aud` | 2900 | `aud` | month | `plan=pro` |
| Enterprise | `AID Helpdesk Enterprise Monthly` | `aid_helpdesk_enterprise_monthly_aud` | 9900 | `aud` | month | `plan=enterprise` |

Launch recommendation:

- Create test-mode Products/Prices first, then repeat in live mode after webhook
  verification passes.
- Use Stripe Price IDs in the backend env vars for launch. Lookup keys are still
  useful for catalog management, but the first implementation can map env Price
  IDs directly to app tiers and store the related Product ID for audit/debug
  visibility.
- Decide GST behavior before launch. If A$29/A$99 are advertised as final prices,
  set tax behavior consistently and update Stripe Tax/marketing copy together.
- Enable Billing Smart Retries and Stripe-hosted dunning emails.
- Enable Customer Portal features:
  - payment method updates
  - invoice history
  - cancellation at period end
  - subscription updates between Pro and Enterprise

Required environment variables:

```text
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_PRICE_PRO=price_...
STRIPE_PRICE_ENTERPRISE=price_...
APP_BASE_URL=https://app.aidhelpdesk.com
```

Optional:

```text
STRIPE_PUBLIC_KEY=pk_live_...
```

`STRIPE_PUBLIC_KEY` is not required for redirect-based Checkout, but the current
template uses it as a "billing enabled" toggle. It can remain or be replaced with
a server-side `billing_enabled` boolean.

## Database Plan

Current migrations go through v6 (`agent_memory`). Add a v7 migration in
`cloud/db.py`.

Keep `tenants.plan` as the hot path for feature gates. Stripe sync should update
the richer billing table first, then update `tenants.plan` using
`db.set_tenant_plan()`.

Add `tenant_billing`:

```sql
CREATE TABLE IF NOT EXISTS tenant_billing (
    tenant_id              TEXT PRIMARY KEY,
    stripe_customer_id     TEXT UNIQUE,
    stripe_subscription_id TEXT UNIQUE,
    stripe_product_id      TEXT,
    stripe_price_id        TEXT,
    plan                   TEXT NOT NULL DEFAULT 'free',
    status                 TEXT NOT NULL DEFAULT 'none',
    cancel_at_period_end   INTEGER NOT NULL DEFAULT 0,
    current_period_start   TEXT,
    current_period_end     TEXT,
    trial_end              TEXT,
    last_invoice_id        TEXT,
    last_payment_status    TEXT,
    updated_at             TEXT NOT NULL
);
```

Add `stripe_events`:

```sql
CREATE TABLE IF NOT EXISTS stripe_events (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    received_at  TEXT NOT NULL,
    processed_at TEXT
);
```

Add DB helpers:

- `get_tenant_billing(tenant_id)`
- `upsert_tenant_billing(tenant_id, **fields)`
- `get_tenant_by_stripe_customer(customer_id)`
- `get_tenant_by_stripe_subscription(subscription_id)`
- `record_stripe_event(event_id, event_type) -> bool`
- `mark_stripe_event_processed(event_id)`
- `sync_tenant_plan_from_billing(tenant_id)`

Access-status rule:

- Provision paid features for `active` and `trialing`.
- Keep paid features for `past_due` during the current paid period/grace window.
- Revoke paid features for `canceled`, `unpaid`, `incomplete_expired`, `paused`,
  or missing subscription.
- For `incomplete`, do not upgrade the tenant until payment completes.

Manual/admin plans:

- Keep existing admin plan endpoints for support and manual Enterprise deals.
- If manual overrides are expected, add `plan_source` or `manual_override_until`
  to `tenant_billing` so Stripe webhooks do not accidentally downgrade a comped
  tenant.

## Endpoint Plan

### Keep and update `GET /billing`

Current route: `cloud/app.py:987`.

Update it to read both:

- app plan/limits from `db.get_tenant_plan()` and `db.get_plan_limits()`
- billing state from `tenant_billing`

Show:

- current plan
- subscription status
- renewal date or cancellation date
- usage against limits
- checkout upgrade buttons when eligible
- customer portal button when `stripe_customer_id` exists
- admin-only controls for subscription management

Fix copy inconsistencies during implementation:

- `cloud/templates/signup.html` says GBP29/month; pricing elsewhere is A$29.
- `cloud/templates/billing.html` shows A$29 for any non-free plan, including
  Enterprise.
- Billing copy says Enterprise has scheduled reports, while `PLAN_LIMITS` has
  scheduled reports enabled for Pro and Enterprise.

### Replace `POST /billing/create-checkout-session`

Current route: `cloud/app.py:1069`.

Required behavior:

- Require dashboard session.
- Require `g.user_role == "admin"`.
- Accept `{ "plan": "pro" | "enterprise" }`.
- Reject `free`.
- Resolve `plan` to `STRIPE_PRICE_PRO` or `STRIPE_PRICE_ENTERPRISE`.
- Return 503 if the requested Price ID is missing. Do not use inline `price_data`
  in production.
- Find or create a Stripe Customer:
  - reuse `tenant_billing.stripe_customer_id` when present
  - otherwise create `stripe.Customer` with tenant name, admin email, and
    metadata `{tenant_id}`
  - persist the customer ID before creating Checkout
- If the tenant already has an active/trialing subscription, return a Customer
  Portal URL instead of creating a second subscription.
- Create a Checkout Session with:
  - `mode="subscription"`
  - `customer=<stripe_customer_id>`
  - `client_reference_id=<tenant_id>`
  - `line_items=[{"price": price_id, "quantity": 1}]`
  - `metadata={"tenant_id": tenant_id, "plan": plan}`
  - `subscription_data={"metadata": {"tenant_id": tenant_id, "plan": plan}}`
  - `allow_promotion_codes=True`
  - `success_url=f"{APP_BASE_URL}/billing/success?session_id={CHECKOUT_SESSION_ID}"`
  - `cancel_url=f"{APP_BASE_URL}/billing?cancelled=1"`
  - optional `automatic_tax={"enabled": True}` after Stripe Tax is configured

The webhook remains the source of truth. The success redirect must not upgrade
the tenant directly.

### Keep `GET /billing/success`

Current route: `cloud/app.py:1107`.

Keep this as a redirect/display route. It may retrieve the Checkout Session for
read-only display, but it must not mutate `tenants.plan`.

### Replace `POST /billing/webhook`

Current route: `cloud/app.py:1115`.

This route becomes the authoritative billing sync.

Security requirements:

- Use `request.get_data()` as the raw payload.
- Verify `Stripe-Signature` with `STRIPE_WEBHOOK_SECRET`.
- Reject unsigned events outside local development.
- Store event IDs in `stripe_events` and return 2xx for already-processed
  duplicate events.

Plan mapping must use Product/Price IDs from server config, not metadata alone.
For launch, map the configured monthly Price IDs directly:

```python
PRICE_TO_PLAN = {
    os.getenv("STRIPE_PRICE_PRO"): "pro",
    os.getenv("STRIPE_PRICE_ENTERPRISE"): "enterprise",
}
```

When syncing a subscription, also persist `subscription.items.data[0].price.product`
as `stripe_product_id` so future annual prices can map by product metadata or a
`PRODUCT_TO_PLAN` table without losing billing history.

Handle these events:

- `checkout.session.completed`
  - persist `customer` and `subscription`
  - retrieve the Subscription from Stripe, expanded enough to read
    `items.data.price`
  - map Price ID to plan
  - store status and period dates
  - set `tenants.plan` only if status is `active` or `trialing`
- `customer.subscription.created`
  - run the same subscription sync helper
- `customer.subscription.updated`
  - update plan if the Price changed
  - update status, cancellation flag, and current period end
  - keep paid features until period end when `cancel_at_period_end=True`
- `customer.subscription.deleted`
  - set billing status to `canceled`
  - set `tenants.plan='free'`, unless a manual override is active
- `invoice.paid`
  - refresh subscription and current period
  - restore paid plan when subscription is active
- `invoice.payment_failed`
  - set billing status/payment status to `past_due`
  - keep paid plan through the configured grace/current period
- `invoice.payment_action_required`
  - record status and surface "payment action required" on `/billing`
- Optional but useful:
  - `invoice.finalization_failed`
  - `customer.subscription.trial_will_end` if Stripe trials are later used

### Add `POST /billing/create-portal-session`

Purpose: let customers manage payment methods, invoices, cancellation, and plan
changes through Stripe-hosted Customer Portal.

Rules:

- Require dashboard session.
- Require `g.user_role == "admin"`.
- Require `tenant_billing.stripe_customer_id`.
- Create `stripe.billing_portal.Session` with:
  - `customer=<stripe_customer_id>`
  - `return_url=f"{APP_BASE_URL}/billing"`
- Return `{ "success": true, "url": session.url }`.

### Add `GET /billing/status` or extend `/dashboard/api/usage`

Return:

- `plan`
- `limits`
- `usage`
- `billing.status`
- `billing.current_period_end`
- `billing.cancel_at_period_end`
- `billing.portal_available`

This keeps the dashboard usage bar and billing page consistent without duplicating
subscription display logic in multiple templates.

## Plan Gating

Use `db.PLAN_LIMITS` as the single product matrix. Templates can mirror it for
UX, but every paid feature must also have a server-side route gate.

Recommended launch matrix:

| Feature | Free | Pro | Enterprise |
|---|---:|---:|---:|
| AI calls per month | 10 | 500 | 2000 |
| AD write/destructive actions per month | 5 | 200 | 1000 |
| Read-only AD lookups | unlimited | unlimited | unlimited |
| Tickets | 20 | unlimited | unlimited |
| Team members | 1 | 5 | unlimited |
| Email ticket intake | no | yes | yes |
| AI ticket auto-actions | no | yes | yes |
| Scheduled reports | no | yes | yes |
| Custom scripts | no | 10 scripts | unlimited |
| Slack/Teams notifications | no | yes | yes |
| AI memory/persona | no | no for launch | later/Enterprise |
| Support | community | email | priority/SLA |

Changes needed in `PLAN_LIMITS`:

- Add `custom_scripts_limit`.
- Add `integrations`.
- Add `memory`.
- Decide whether scheduled reports are Pro+ or Enterprise-only, then make
  `PLAN_LIMITS`, billing copy, landing copy, and settings UI match.

### Gate Every Execution Path

Create small helper functions in `cloud/app.py` for consistency:

- `tenant_limits(tenant_id)`
- `check_ai_quota(tenant_id)`
- `check_ad_command_quota(tenant_id)`
- `is_billable_ad_action(action)` using
  `action_policy.is_write(action) or action_policy.is_destructive(action)`
- `require_plan_feature(tenant_id, feature_key)`
- `queue_ad_action_checked(...)` that centralizes:
  - `action_policy.validate`
  - destructive confirmation challenge
  - AD command quota
  - audit log
  - activity log
  - usage increment
  - command queue insert

Then route the following through those helpers:

- `/dashboard/api/exec`
- `/api/v1/actions/<action>`
- `/dashboard/api/tickets/<id>/apply-fix`
- AI chat primary action path
- AI chat chained action path
- AI ticket auto-resolve path
- Legacy `/api/command`, if it remains enabled

### Gate Non-AD Features

Add or tighten these server-side gates:

- `/webhook/email/<api_key>`: already gated; keep it.
- Legacy `/webhook/email`: remove, disable, or add tenant routing plus
  `email_intake` and ticket-cap checks.
- `/dashboard/api/tickets`: keep ticket-cap gate.
- `/dashboard/api/users`: keep team-member gate.
- `/dashboard/api/scripts/*`: require Pro+, enforce `custom_scripts_limit`, and
  block execution of disabled scripts.
- `/dashboard/api/settings`: block enabling:
  - `ai_auto_actions` when `limits["auto_actions"]` is false
  - `report_enabled` when `limits["scheduled_reports"]` is false
  - Slack/Teams URLs when `limits["integrations"]` is false
- `_run_scheduled_reports`: check `limits["scheduled_reports"]` immediately
  before sending.
- `notify_integrations` and `/dashboard/api/integrations/test`: check
  `limits["integrations"]`.
- `/dashboard/api/insights`: check and increment `ai_calls`, or expose only
  on paid plans with a separate limit.
- `/dashboard/api/memory/*`: disable for launch or gate behind a deliberate plan
  feature flag.

### Trial vs Free Plan Decision

The UI says "14-day free trial", but the app also has a permanent `free` plan
with small quotas. Pick one before launch:

Option A, simplest launch:

- Keep the Free plan as a real freemium tier.
- The 14-day messaging becomes "14-day Pro trial" only if code grants Pro-like
  trial limits.

Option B, stricter SaaS trial:

- Add a `trial` plan or `trial_ends_at` billing state.
- New tenants get Pro-level features until trial end.
- After trial end, either downgrade to Free limits or block write/AI features
  until subscription.

Recommended for launch: Option A unless there is time to implement trial expiry
cleanly. Do not claim "trial ends, subscribe to continue" while leaving a
permanent free plan with no explicit behavior.

## Implementation Sequence

1. Create the Pro and Enterprise Stripe Products and test-mode monthly Prices.
2. Configure Stripe Customer Portal in test mode.
3. Add Railway/test env vars for Stripe secret key, webhook secret, and Price IDs.
4. Add `stripe>=9.0.0` to `cloud/requirements.txt` for consistency; the current
   Railway config uses the root `requirements.txt`, which already includes it.
5. Add v7 billing migration and DB helpers.
6. Replace Checkout Session creation with customer reuse and no inline production
   prices.
7. Add Customer Portal endpoint.
8. Replace webhook with idempotent subscription sync.
9. Add billing status API or extend `/dashboard/api/usage`.
10. Centralize execution/usage gating and patch every route listed above.
11. Update `billing.html`, dashboard settings, signup, and landing copy to match
    the final plan matrix.
12. Test all webhook flows locally with Stripe CLI.
13. Repeat catalog/env setup in live mode.
14. Run one production smoke test subscription before launch.

## Manual Verification Checklist

Local/test mode:

```bash
stripe listen --forward-to localhost:5000/billing/webhook
```

Then verify:

1. New tenant starts on Free/trial state as intended.
2. Pro checkout creates a Checkout Session.
3. Successful payment creates/persists Stripe customer and subscription IDs.
4. Webhook changes `tenants.plan` from `free` to `pro`.
5. Pro-only features work server-side.
6. Free-only limits still block server-side after manual downgrade.
7. Customer Portal opens for active customer.
8. Portal cancellation at period end keeps paid access until period end.
9. `customer.subscription.deleted` downgrades to Free.
10. `invoice.payment_failed` marks billing status without immediately breaking
    access during the grace/current period.
11. Duplicate webhook event IDs do not double-process.
12. `/dashboard/api/exec`, `/api/v1/actions`, ticket apply-fix, AI chat, and
    legacy command paths all enforce the same quota and policy rules.

Production:

1. Create live Pro and Enterprise Products/Prices.
2. Configure live webhook endpoint:
   - `https://<app-domain>/billing/webhook`
3. Subscribe to:
   - `checkout.session.completed`
   - `customer.subscription.created`
   - `customer.subscription.updated`
   - `customer.subscription.deleted`
   - `invoice.paid`
   - `invoice.payment_failed`
   - `invoice.payment_action_required`
4. Set Railway live env vars.
5. Run a real low-value/internal subscription or live coupon smoke test.
6. Confirm plan sync, usage gates, and Customer Portal.

## Launch Risks To Resolve

- Billing state is currently just `tenants.plan`; add `tenant_billing`.
- Current webhook is not idempotent.
- Current webhook can only downgrade reliably if subscription metadata contains
  tenant ID, but Checkout does not set `subscription_data.metadata`.
- Current checkout can create duplicate subscriptions.
- Current checkout falls back to inline prices.
- Current checkout is not restricted to tenant admins.
- Current dashboard AD action quota gate is stale.
- Ticket apply-fix and legacy command routes bypass billing/action gates.
- Scheduled reports and integrations can be configured without paid-plan gates.
- Memory endpoints are live even though memory is a future feature.
- `cloud/requirements.txt` lacks `stripe`; root `requirements.txt` has it and is
  what the current Railway config uses.
- Signup/dashboard/billing copy has currency and Enterprise-price mismatches.
- GST/tax behavior must be decided before publishing final launch pricing.
