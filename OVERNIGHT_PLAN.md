# AID Helpdesk — Overnight Expansion Plan

**Goal:** Evolve AID Helpdesk from "AD helpdesk" into a full Windows Server management system: Active Directory, DNS, DHCP, Group Policy (NPS as stretch), plus Entra ID via Microsoft Graph. Landing page gets a Higgsfield hero video (user-confirmed spend only).

**Run mode:** `/loop` with ScheduleWakeup. Each wakeup: pick the next unchecked task, implement (delegating to Sonnet subagents for well-scoped file work), verify, commit, check the box in this file, schedule next wakeup. This file is the single source of truth for progress.

---

## Ground rules for the loop

1. **Commit per completed task** with a descriptive message. Push to `main` only when the app boots cleanly (`python cloud/app.py` starts without traceback and `/health` responds). If unsure, commit locally and leave pushing for morning review.
2. **Never spend Higgsfield credits autonomously.** Phase 6 prepares prompts only; generation happens with Lachy awake and confirming.
3. **No real AD/DNS/DHCP server is available overnight.** Every new bridge function must be verifiable without a live DC:
   - pure function that *builds* a PowerShell script string, separate from the function that runs it
   - a `python -c` smoke test that imports the module and prints a generated script
4. **Graceful degradation:** every new service module must return a friendly error when the Windows role isn't installed (e.g. "DNS Server role not detected on this host") rather than a raw PowerShell traceback.
5. **Safety tiers (match existing model):**
   - Reads: auto-execute.
   - Routine writes (DNS record add, DHCP reservation): ticket/chat confirm flow, same as password resets.
   - High blast-radius (GPO link/unlink/edit, DNS zone delete, DHCP scope delete, Entra role changes): require the 6-digit human-confirm token, same as `disable_account`.
6. **No em dashes** in any user-facing copy. Ever.
6b. **Use proper inline SVG icons/logos in UI work**, not emoji or plain text labels. Lachy keeps a good reference collection in his Metis Orchestrator project (OneDrive\Documents, Metis folder) — match that quality bar for nav items, badges, and feature cards.
7. At the end of the overnight run, write the jarvis-log entry summarising what shipped.

---

## Phase 0 — Foundations (do first, everything depends on it)

- [x] **0.1 Extract shared WinRM core.** Pull `_session`, `_run`, circuit breaker, and error helpers out of `ad_bridge.py` into `winrm_core.py`. `ad_bridge.py` imports from it; behaviour unchanged. Smoke test: `python -c "import ad_bridge"` and existing agent still maps all ACTIONS.
- [x] **0.2 Action registry refactor in agent.py.** (agent side of 0.3 done too; cloud side in progress) Replace the hand-written ACTIONS dict with auto-registration: each bridge module exposes `ACTIONS = {"name": callable}` and agent.py merges them. Keeps the flat action-name namespace (`list_dns_zones`, not `dns.list_zones`) for backwards compat.
- [x] **0.3 Capability handshake.** Agent reports its available modules + detected Windows roles to the cloud on startup (`POST /agent/capabilities`). Cloud stores per-tenant capabilities; dashboard tabs grey out when the agent can't serve them. DB: add `capabilities` JSON column to tenants (or agents) table with migration handled in `init_db()` for both SQLite and Postgres.
- [x] **0.4 Dashboard tab shell.** Sidebar/tab navigation in the dashboard template: Active Directory (existing content moves here), DNS, DHCP, Group Policy, Entra ID, Tickets, Settings. Tabs beyond AD render "coming online" placeholders until their phase lands. Keep the existing purple/indigo design language.

## Phase 1 — DNS (easiest win, sets the pattern)

- [x] **1.1 `dns_bridge.py`**: list_zones, get_zone, list_records(zone, type?), add_record (A/AAAA/CNAME/MX/TXT/PTR), update_record, remove_record, get_scavenging, toggle_scavenging. Uses `DnsServer` PS module via winrm_core. Script-builder functions unit-testable offline.
- [x] **1.2 Agent wiring**: register DNS actions, capability flag `dns`.
- [x] **1.3 Cloud routes + tab UI**: zone list → record table with inline add/edit/delete (writes go through the confirm flow). Record-type badges, TTL display, search.
- [x] **1.4 AI tools**: expose DNS actions to the assistant's tool schema with descriptions + arg specs; threat-score guidance (record delete on a zone apex = high).
- [x] **1.5 Ticket auto-resolution**: "the printer hostname isn't resolving" style tickets can trigger DNS lookups in analysis.

## Phase 2 — DHCP

- [x] **2.1 `dhcp_bridge.py`**: list_scopes, get_scope, list_leases(scope), list_reservations, add_reservation, remove_reservation, add_exclusion_range, remove_exclusion_range, scope stats (utilisation %). `DhcpServer` PS module.
- [x] **2.2 Agent wiring + capability flag.**
- [x] **2.3 Cloud tab**: scope cards with utilisation bars, lease table (IP, MAC, hostname, expiry), reservation management. Convert-lease-to-reservation one-click (confirm flow).
- [x] **2.4 AI tools + threat scoring** (scope/exclusion changes = high; reading leases = low).

## Phase 3 — Group Policy (read-first, writes gated hard)

- [x] **3.1 `gpo_bridge.py` reads**: list_gpos (name, status, links, modified), get_gpo_report (XML→parsed summary of settings), list_links(ou), get_gpo_inheritance(ou). `GroupPolicy` PS module.
- [x] **3.2 Writes (token-confirm only)**: link_gpo(gpo, ou), unlink_gpo, enable_gpo/disable_gpo, set_link_enforced. NO setting-level editing overnight; that's a future project.
- [x] **3.3 Cloud tab**: GPO list with link map (which OUs), status pills, drill-in report view rendered from the parsed XML.
- [x] **3.4 AI tools**: reads freely; every write action description explicitly instructs the model to route through human confirmation.

## Phase 4 — Entra ID via Microsoft Graph

- [x] **4.1 `cloud/graph_client.py`**: MSAL client-credentials flow. Config vars `GRAPH_TENANT_ID`, `GRAPH_CLIENT_ID`, `GRAPH_CLIENT_SECRET` per tenant (stored encrypted in DB alongside other tenant settings, not global env). Token cache in memory.
- [x] **4.2 Core operations**: list users, get user, list groups, group membership add/remove, revoke sessions, reset password (note: requires `UserAuthenticationMethod.ReadWrite.All` and the app needs Password Administrator role assignment; document this in the settings UI).
- [x] **4.3 Hybrid awareness**: flag `onPremisesSyncEnabled` users; when a user is AD-synced, route mutations to the on-prem agent instead of Graph and say so in the UI.
- [x] **4.4 Entra tab + settings page** for entering app registration details, with a step-by-step "create the app registration" guide (screenshots optional, copy exact portal steps).
- [x] **4.5 AI tools**: unified user lookup that searches both AD and Entra and labels the source.

## Phase 5 — NPS (stretch, only if everything above is green)

- [x] **5.1 Read-only**: list network policies, connection request policies, RADIUS clients (`Get-NpsRadiusClient` etc. or `netsh nps export` parse). Display-only tab. No writes overnight. Done incl. display tab.

## Phase 6 — Motion graphics (prep only, no spend)

- [ ] **6.1 Storyboard** a 5s hero loop: the four-point AID star drifting over a dark indigo field, pulsing (the Gemini contract-bloom), particles condensing into a tick. Written as a shot list with timings.
- [ ] **6.2 Exact Higgsfield prompt** (kling3_0_turbo, 5s) + negative prompt + reference image plan (use `cloud/static/Assistant.png` via media_upload). Save as `docs/higgsfield-hero.md`.
- [ ] **6.3 Landing integration stub**: `<video>` hero slot behind a feature flag, poster frame fallback to the current CSS animation so nothing breaks if the video never ships.
- [ ] **6.4 Morning step (with Lachy):** get_cost preflight, confirm ~7.5 credit spend, generate, download, compress (H.264 + webm), drop into `/static/`, flip the flag.

## Phase 7 — Polish + wrap-up

- [ ] **7.1 Landing page**: add a "Full Windows Server management" section with the new tabs (DNS/DHCP/GPO/Entra) as feature cards. Update README feature table + roadmap checkboxes. README part done; landing.html has unrelated uncommitted user edits, so that half is deferred to morning.
- [x] **7.2 Docs**: ARCHITECTURE.md gains the module registry + capability handshake; SECURITY.md gains the new safety-tier table.
- [ ] **7.3 Full boot verification**, push everything green, write jarvis-log, leave a MORNING.md summarising: what shipped, what's half-done, what needs Lachy (Higgsfield spend, Entra app registration, live-DC testing checklist).

---

## Answered by Lachy (2026-06-26)

1. **Vision:** AID Helpdesk is a *central management platform for Windows Servers*, AI-native. NPS-read-only stays the overnight stretch; file shares, print services, IIS, and anything else Windows Server are fair game for future phases. The capability handshake (0.3) is the extension point: every future role slots in as another module + tab.
2. **Entra credentials:** per-tenant in the settings UI. Confirmed.
3. **Tabs:** visible but greyed out when the agent lacks the capability. Confirmed.
