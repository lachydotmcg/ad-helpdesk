# Competitive gaps: ServiceNow and Moveworks

An honest audit of what the established AI-helpdesk players do that AD Helpdesk
does not, what we do that they cannot, and which gaps are actually worth closing.

Researched July 2026. Verified against our own schema rather than assumed: every
"missing" item below returned zero hits in `cloud/app.py` / `cloud/db.py`.

---

## Framing

AI-driven auto-resolution of AD tickets is **not** a new category. ServiceNow
reports its autonomous agents handle 90%+ of targeted L1 volume (password resets,
account unlocks, VPN, access requests), and Moveworks (acquired by ServiceNow) has
had deep AD/Entra/Okta integration for years.

So the interesting question is not "are we first" (we are not), it is **where are
we genuinely differentiated, and where are we simply missing table stakes**.

---

## What they have that we do not

### Moveworks

| Gap | Severity | Notes |
|---|---|---|
| **Two-way Slack / Teams assistant** | **High** | Their whole deflection model: employees ask a bot in Slack/Teams and never open a ticket. Ours is outbound notification only (`_send_webhook_notification`, incoming webhooks, one-way). Staff cannot talk to it. |
| **Knowledge base + enterprise search (RAG)** | **High** | They index SharePoint, Confluence, wikis, and answer from documentation when no action fits. We have no knowledge storage at all: if a question is not an AD action, we have no answer. |
| **Integration breadth** | Medium | Okta, Workday, Jira, Zendesk, Salesforce, Jamf, DocuSign. We are Windows Server + Entra. |
| **Agent Studio (low-code agent builder)** | Medium | Non-developers extend it. Extending us means writing a Python bridge module. |
| **Pre-built agent marketplace** | Low | Nice-to-have, not a blocker. |

### ServiceNow (full ITSM)

| Gap | Severity | Verdict |
|---|---|---|
| **SLA management** (targets, breach tracking, auto-escalation) | **High** | Build. Real operational gap. |
| **Ticket attachments** | **High** | Build. "Here is a screenshot" is table stakes; conspicuous in any demo. |
| **Knowledge articles** | **High** | Build, ties to the RAG gap above. |
| **Reporting / analytics** (MTTR, first-contact resolution, CSAT) | Medium | Partial build. We have usage counters and time-saved, not service metrics. |
| **Due dates, escalation rules** | Medium | Cheap against the existing schema. |
| **Approval workflows** | Medium | We have a 6-digit confirm token, not a routed multi-party approval. |
| **CMDB** (configuration items, dependency mapping) | Low | **Skip.** Enterprise ITSM, not an AD helpdesk. |
| **Change management** (standard/normal/emergency, CAB) | Low | **Skip.** |
| **Problem management** (many incidents to one root cause) | Low | **Skip.** |
| **Service catalog** (request items, fulfilment) | Low | **Skip.** |

Skipping the bottom four is a deliberate positioning choice, not an oversight.
Chasing full ITSM means competing with ServiceNow on their ground with none of
their surface area. The point of this product is depth in Windows Server plus
sovereignty, not process breadth.

---

## What we have that they do not

Worth stating plainly, because it is the actual moat:

- **They do not manage Windows Server infrastructure.** Both do *identity* actions:
  password reset, unlock, group membership. Neither manages **DNS zones, DHCP
  scopes, Group Policy, NPS, or application deployment**. Our 62 actions across six
  roles is depth they do not have. They orchestrate *around* infrastructure; we
  operate *on* it.
- **Self-hosted with a local model.** Both are SaaS. Staff identities and ticket
  contents leave the building to make their auto-resolution work. Ours does not.
- **Security by construction.** A hard-coded Python allowlist decides what runs, not
  prompt instructions. Destructive actions are never auto-resolved regardless of
  what the model proposes.
- **Free and unmetered when self-hosted.** No per-seat, per-resolution pricing.

---

## Priority order

Ranked by (value to a real deal) / (effort), not by how impressive it sounds.

1. ~~**Two-way Slack assistant**~~ - **built.** Socket Mode listener, server-side
   identity resolution, self-service action allowlist, Settings UI. Teams is still
   open and needs a different transport (see the note below). The live Socket Mode
   handshake is untested against a real Slack app.
2. ~~**Ticket attachments**~~ - **built.** Extension allowlist, size cap, traversal
   guard, tenant-scoped.
3. ~~**SLA basics**~~ - **built.** Priority-based targets, due dates, breach flag,
   escalation to the activity log, first-response timestamps.
4. ~~**Knowledge base**~~ - **built**, retrieval included. Not RAG in the vector
   sense, and that was a deliberate call rather than a shortcut: Anthropic has no
   embeddings API, so a Cloud tenant cannot embed with the provider they already
   pay for, and the local alternative (sentence-transformers, so torch) is two
   gigabytes of dependency on a domain controller. BM25 covers every tenant with
   no new dependencies. `kb.search()` is the seam if an embedding re-rank is
   worth adding later for tenants whose provider supports it.
5. **Service metrics** - MTTR, first-contact resolution, CSAT. Now the top
   remaining item, and cheaper than it was: SLA work already added
   `first_response_at` and `resolved_at`, so first-contact resolution and MTTR
   are mostly a query rather than a schema change.

---

## Architectural note for #1

A self-hosted server usually sits **behind a firewall with no inbound internet
access**, so the normal Slack Events API (Slack POSTs to your public URL) does not
work for most of our installs.

**Slack Socket Mode** is the answer: the app opens an *outbound* WebSocket to Slack
and receives events over it. No public URL, no inbound ports, no reverse proxy.
That is exactly the same philosophy as our agent, which polls outbound so the
domain controller never exposes itself.

Microsoft Teams has no true Socket Mode equivalent; a Teams bot needs a reachable
HTTPS endpoint. So Teams support realistically means either an internet-reachable
install or a relay, and should be treated as a separate, later piece of work rather
than assumed to come free with Slack.

### Identity is the security question

If a Slack user says "reset my password", we must know who that is in AD. The
mapping is Slack profile email to AD `EmailAddress`/UPN, and it has to be resolved
**server-side from the Slack event**, never from anything the user types, or anyone
could impersonate anyone.

An end user in Slack must also have **strictly less** authority than an admin in
the dashboard: self-service actions on their own account only. Unlocking a
*colleague* is an admin action, not a self-service one.
