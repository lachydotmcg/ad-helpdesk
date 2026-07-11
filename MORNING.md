# Morning summary: overnight platform expansion

All seven phases of OVERNIGHT_PLAN.md are done except the bits that need you. The app boots clean, /health is green, and the agent now merges **56 actions across 5 capabilities** (ad, dns, dhcp, gpo, nps). 14 commits pushed.

## What shipped

- **Foundations**: winrm_core.py (shared transport + circuit breaker), self-registering bridge modules, capability handshake (agent reports its roles, dashboard tabs grey out accordingly), tab shell.
- **DNS**: full bridge, tab (zones, record table, add/delete through the confirm flow), AI tools, and ticket analysis can now chain one read-only DNS lookup when a ticket smells DNS-shaped. Writes from analysis are force-blocked in code, not just prompt.
- **DHCP**: full bridge, tab with scope utilisation bars, one-click lease to reservation, exclusions gated destructive.
- **Group Policy**: bridge (reads + writes), tab with report drill-in and OU picker. All four GPO writes are token-confirm only.
- **Entra ID**: Graph client (MSAL client credentials), settings section with test-connection and an app-registration guide, user/group management tab, hybrid routing (synced users are read-only in Entra and pointed at the AD tab), unified AD/Entra lookup for the assistant with source labels.
- **NPS**: read-only bridge + display tab (summary cards, RADIUS clients, policies). Shared secrets are explicitly never returned.
- **Docs**: ARCHITECTURE.md and SECURITY.md rewritten for the module registry, handshake, safety tiers, and Entra. README reframed as a Windows Server platform.

## Needs you

1. **Landing page** (7.1 second half): your uncommitted landing.html + karen.png edits are still sitting in the working tree, untouched. Note karen.png shrank from 131 KB to 1.6 KB in your working copy; double-check that's intentional before committing. The "Full Windows Server management" landing section is still to add.
2. **Higgsfield hero** (Phase 6): skipped entirely per your note that it won't work.
3. **Secrets at rest**: DONE. Tenant secrets (Entra client secret, SMTP password) are now Fernet-encrypted at rest via cloud/secrets_crypto.py, keyed off a new SETTINGS_ENCRYPTION_KEY env var. Set that env var on Heroku/Railway before real credentials go in (any passphrase works; see DEPLOYMENT.md). Without it the app still runs but stores those two fields plaintext and logs a warning. Existing plaintext values auto-encrypt on next save.
4. **Live testing**: everything was verified offline (script builders, validation, boot, route auth). Nothing has touched a real DC yet. Suggested first live checks: agent startup capability report, list_dns_zones, list_dhcp_scopes, list_gpos, and one token-confirm GPO write against the lab VM.
5. **Entra app registration**: to light up the Entra tab you need to create the Azure app registration (the settings page now has the step-by-step guide) and paste the three values.

## Post-run QA (done after the loop closed)

- Ran a cross-consistency audit over the 8 parallel subagents' work: all 56 agent actions are correctly tiered in action_policy.py (no untiered actions, no typos), every queue_command action name resolves to a real bridge action, and all capability keys line up across the bridges, the app _require_*_capability gates, and the dashboard tab-greying. Clean.
- Fixed one real nit the audit surfaced: the disabled Entra nav tab still had an onclick (opened an empty page); dropped it to match the other disabled tabs.
- Confirmed the committed cloud/adhelpdesk.db is not tracked (in .gitignore) and is empty anyway (no tenants/secrets), so no data leak in git history.
