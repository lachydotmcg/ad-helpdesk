# Change Log — AD Helpdesk

Running log of improvements made by the automated review task (`temp-aid-review`).
Most recent first.

---

## 2026-05-31 — Security/UX: escape AD- and email-derived data across all dashboard render functions (stored XSS + "O'Brien" display bug)

**Type:** Security hardening + correctness/UX fix
**File:** `cloud/templates/dashboard.html`

**The problem:** The single-page dashboard builds most of its tables and panels
by string-concatenating values straight into `innerHTML` and inline `onclick`
handlers. A large number of those values come from **untrusted or
semi-untrusted sources** — Active Directory object fields (display name, OU,
group names), inbound-email ticket data (subject/title, requester name &
email), and persisted activity-log entries (actor/target/detail). None of it
was being output-encoded, which caused two distinct classes of bug:

1. **Stored / DOM XSS.** A ticket created from an inbound email is the clearest
   vector: the sender fully controls the subject line, and `renderTickets()`
   rendered `${t.title}` raw into the table. A subject like
   `<img src=x onerror=...>` would execute in the **admin's** browser session
   the moment they opened the Tickets page. The activity feed
   (`renderActivityFeed`) and ticket thread (`a.content`, `a.user_email`) had
   the same issue with persisted data; the user tables and user-detail modal had
   it with AD-derived fields.
2. **A real, everyday display bug.** Names with an apostrophe — `O'Brien`,
   `D'Angelo`, extremely common in the target market (schools) — broke the
   generated `onclick="unlockUser('${uname}')"` handlers, so the Unlock / Reset
   / Disable buttons silently did nothing for those users. Names containing
   `<`, `>` or `"` corrupted the surrounding markup.

An `escHtml()` helper already existed but was applied only in the newest code
paths (the AI assistant memory, ticket labels), and it was **defined twice** — once
without quote-escaping (line ~3810) and once with (~4700). The later definition
won via hoisting, so the incomplete copy was dead code.

**The fix:**
- Consolidated to a **single canonical `escHtml()`** (handles `& < > "` and
  null/undefined); removed the inferior duplicate.
- Added a `jsArg()` helper for the one remaining hard case — embedding a value
  inside a single-quoted JS string that itself lives in a double-quoted HTML
  attribute (the `onclick="fn('…')"` pattern). It HTML-escapes, then doubles
  backslashes and escapes apostrophes so the value survives HTML-decoding
  without breaking out of the JS string literal.
- Applied the helpers to **every render site that emits untrusted data**:
  `renderUsers`, `loadLocked`, `loadExpired`, `openUserDetail` (meta grid,
  group pills, action buttons), `renderActivityFeed`, the ticket list
  (`renderTickets`), the ticket-detail sidebar + actions thread, and the team
  members table + assignee `<option>`s. The `mailto:` href now wraps the
  requester email in `encodeURIComponent`.

**Verification:**
- Jinja parses; the rendered inline script passes `node --check`.
- Unit-checked the helpers in Node: `escHtml("O'Brien")` → `O'Brien` (renders
  correctly), `escHtml("<script>…")` is neutralised, and a full round-trip
  (`jsArg` → place in `onclick="fn('…')"` → HTML-decode → `eval`) returns the
  **exact** original string for `O'Brien`, while a break-out payload
  (`a')+alert(1)+('b`) is rendered inert. Behaviour for ordinary names
  (no special chars) is byte-for-byte unchanged.

**Note (unchanged from prior runs):** CLAUDE.md is still stale — the AI assistant
persistent memory is fully built (db at v6, `agent_memory` table) and the
"Uncommitted Changes" section no longer reflects reality. A CLAUDE.md refresh
remains a good low-risk future task.

---

## 2026-05-31 — Fix: destructive-confirmation gate had drifted (bulk_move_users / create_ou bypassed confirmation)

**Type:** Security / correctness fix
**Files:** `cloud/app.py`, `cloud/templates/dashboard.html`

**The bug:** The 6-digit confirmation challenge for high-risk AD actions was
gated on a hand-maintained `DESTRUCTIVE_ACTIONS` set in `app.py` (used by *both*
`/dashboard/api/exec` and the public `/api/v1/actions/<action>` endpoint). That
set had drifted out of sync with `action_policy.DESTRUCTIVE`:

- `bulk_move_users` and `create_ou` are classified DESTRUCTIVE by the policy
  layer, but were **missing** from `DESTRUCTIVE_ACTIONS` — so they could be
  queued with **no confirmation at all** via the dashboard and the public API.
  `bulk_move_users` can relocate hundreds of accounts in one call, so this was
  the single most dangerous action skipping the gate.
- The frontend carried its own duplicate `DESTRUCTIVE_ACTIONS` Set (dead code —
  the exec path is fully server-driven), also drifted.

This directly contradicted CLAUDE.md design decision #1 ("action_policy is the
authoritative gate") because confirmation was driven by a separate, divergent
copy rather than the policy layer.

**The fix:**
- `DESTRUCTIVE_ACTIONS` is now **derived** from `action_policy.DESTRUCTIVE`
  (`= action_policy.DESTRUCTIVE | {reset_password, set_password_never_expires}`),
  so any future destructive action is automatically confirmation-gated and the
  two sets can never drift again. The two reversible-but-sensitive WRITE actions
  the founder deliberately also gates are preserved as an explicit extra set.
- Added confirmation `action_label`s for `create_ou` and `bulk_move_users` in
  `/dashboard/api/exec` so the modal shows a meaningful description.
- Corrected the frontend reference Set and annotated that the server is the
  authoritative gate.

**Verification:** `app.py` imports cleanly; `DESTRUCTIVE_ACTIONS` resolves to a
frozenset containing `bulk_move_users` + `create_ou` (plus the original six);
behaviour for `reset_password`/`set_password_never_expires` is unchanged. No
change to read/write fast-path actions.

**Note:** CLAUDE.md is now stale in two places — the AI persistent-memory /
persona feature is in fact fully built (db v6 `agent_memory` table, reflection
write pass, context retrieval into the AI prompt, CRUD endpoints, and a full
Configure-AI UI), and migrations are at v6 not v5. Worth a CLAUDE.md refresh
on a future run.

---

## 2026-05-31 — Run could not proceed: tool-result delivery stalled (no code changes)

**Type:** Operational note

**What happened:** This scheduled `temp-aid-review` run was unable to do
review/implementation work because the harness stopped delivering tool results.
The first few calls returned normally (and one later batch flushed all at once),
but after that every `Read`/`Grep`/`Bash` call returned no content for the rest
of the run, including trivial `echo` probes. File *mutations* still applied
server-side (an earlier `Edit` was confirmed in the one flush), but without
reliable read-back I can't safely explore or verify changes to this
security-sensitive codebase (AD command execution + the Python `action_policy`
gate). Per the task's own guidance, the correct output here is a report, not
unverified blind edits.

**Confirmed context gathered before the stall:**
- `cloud/action_policy.py` read in full — classification + `validate()` look
  correct; `ai_auto` hard-blocks DESTRUCTIVE, `ai_chat` defers DESTRUCTIVE
  to the frontend confirmation modal (relies on the queue endpoint enforcing the
  6-digit token server-side — worth re-verifying in `app.py` next run).
- `git status`: `cloud/app.py` and `cloud/templates/dashboard.html` still carry
  uncommitted modifications (+162 lines). Untracked: `AGENTS.md`, `hero.html`,
  `legacy/`, `log.md`.
- Recent commits added public API v1 **direct action** endpoints with plan-quota
  + destructive-confirmation enforcement (`2b106a0`, `175e9a4`, `9a7d336`).

**Prioritized backlog for the next healthy run** (focus: quality + UX):
1. **Verify server-side destructive-token enforcement** on both the dashboard
   queue path and the new `/api/v1` direct-action endpoint — confirm the
   frontend modal is not the only gate (defense in depth for the security story).
2. **Review the uncommitted `app.py` / `dashboard.html` diff** for correctness
   before it's committed (time-saved endpoint, custom-scripts CRUD, scheduled
   reports thread, AI custom-script slug resolution).
3. **AI persistent memory / persona** (`agent_memory` table) — the flagged
   high-value next feature in `CLAUDE.md`; could be scaffolded behind the
   existing Configure AI page.
4. **Rate-limiting / abuse protection** on public `/api/v1` endpoints.

---
