# Design sketch: `deploy_app` (push applications via Group Policy)

Status: design only, not implemented. This is the plan for letting an admin roll an
application out to an OU from AID Helpdesk.

## The core problem, and the key decision

There are two hard realities that shape the whole design:

1. **The MSI must never travel through the cloud web tier.** Heroku dynos are
   ephemeral with small disks and a hard 30-second request timeout. A 200 MB
   installer uploaded through Flask would time out and there is nowhere to keep it.
   So the application bytes have to move agent-side, on the customer's own network.

2. **"GPO Software Installation" has no real PowerShell API.** There is `New-GPO`,
   `New-GPLink`, `Set-GPRegistryValue`, etc., but there is *no* cmdlet to add an MSI
   to a GPO's Software Installation node. Doing it "properly" means driving the GPMC
   COM object model (IGPMSoftwareInstallation) or hand-writing the GPO's `.aas`
   files, which is brittle and MSI-only.

### Decision: deploy via a computer **startup-script GPO**, not Software Installation

Instead of the Software Installation node, `deploy_app` creates a GPO whose
**machine startup script** runs the installer silently from a file share. Why:

- Fully scriptable end to end (create GPO, write the script, link it) with no COM.
- Works for **MSI and EXE** (any silent installer), not just MSI.
- The agent controls the logic, so we can make it idempotent and log results.

Tradeoff vs. real Software Installation: startup scripts run on **every boot**, so
the script must check "is this already installed?" first (Software Installation
tracks state for you). We handle that with a version check in the script. We also
lose GPO-native install reporting, so the script writes its own result log that the
agent can read back.

## File hosting (this is where the "file size limit" question lives)

- MSIs/EXEs live on a **dedicated SMB file share**, NOT in SYSVOL. SYSVOL replicates
  to every DC via DFSR, so a large payload there multiplies across DCs and blows
  DFSR staging quotas. A plain file server share has none of that.
- Share + NTFS permissions grant **Domain Computers: Read** (startup scripts run as
  SYSTEM = the machine account, so the *computer* needs read, not the user).
- The GPO itself only holds a ~1 KB startup script. **File size is therefore bounded
  only by the share's disk and the LAN** - no replication amplification, and nothing
  flows through AID's cloud.

Per-tenant config (added to `agent-config.json`):

```json
"software_deploy": {
  "share_unc":   "\\\\FILESERVER\\aid-apps$",
  "share_local": "D:\\aid-apps",
  "enabled": true
}
```

## How an MSI gets onto the share (MVP -> later)

- **MVP:** the admin drops the installer into the share (or a watched local folder on
  the agent host). AID lists what's there and deploys it. Zero bytes through cloud.
- **Later:** admin gives a URL; the agent fetches it to the share (`stage_package`
  below). Still nothing large through the cloud - the agent pulls it directly.

## Bridge actions (new `deploy_bridge.py`, `CAPABILITY = "deploy"`)

Following the existing bridge pattern (pure `_build_*_script`, Python-side validation
first, `role_installed_check("GroupPolicy", ...)`, friendly errors):

| Action | Tier | What it does |
|---|---|---|
| `list_deploy_packages` | READ | List installers on the share: name, size, and, for MSIs, ProductName/ProductVersion/ProductCode via `msiexec`/`Get-MsiInfo`. |
| `stage_package(url, dest_name)` | DESTRUCTIVE | Agent downloads `url` to the share and sets Domain Computers read ACL. (Later phase.) |
| `deploy_app(package, target_ou, install_type, silent_args)` | DESTRUCTIVE | Create the GPO, write the startup script, link to the OU. |
| `list_deployments()` | READ | List GPOs AID created (name prefix `AID Deploy -`) and their links. |
| `remove_deployment(name)` | DESTRUCTIVE | Unlink + delete an AID deployment GPO (does not uninstall the app; note that in the UI). |

`install_type` is `msi` or `exe`. For `exe`, `silent_args` is validated against a
conservative allowlist (letters, digits, `/ - = . \ : " space`) so nothing hostile
reaches the command line.

### What `deploy_app` actually runs

1. Validate: package exists on the share, `target_ou` resolves (reuse
   `winrm_core._make_ou_path`), install type + args are clean.
2. `New-GPO -Name "AID Deploy - <AppName>"`.
3. Write a PowerShell **startup script** into the GPO's SYSVOL Machine\Scripts\Startup
   folder, register it in `scripts.ini`/`psscripts.ini`, and bump the GPO version in
   `gpt.ini` (the script is tiny, so SYSVOL is fine here). The script:
   - checks the uninstall registry / `Get-Package` for the ProductCode or a marker;
   - if absent, runs `msiexec /i \\share\app.msi /qn /norestart` (or the EXE + silent
     switches);
   - writes a line to `C:\ProgramData\AIDHelpdesk\deploy-<app>.log` with the exit code.
4. `New-GPLink` to `target_ou`.
5. Return the GPO id + name.

## Safety and confirmation

- `deploy_app`, `stage_package`, `remove_deployment` are **DESTRUCTIVE** - they push
  software to every machine in an OU, so they require the existing 6-digit
  human-confirm token, exactly like `disable_account` and the GPO writes. The AI
  assistant may *recommend* a deployment but must never auto-resolve one.
- Reads (`list_deploy_packages`, `list_deployments`) are auto-execute.
- Every action is written to the audit log with the package, OU, and requester.

## Cloud + UI surface

- A **"Deploy App"** section on the Group Policy page: pick a package from
  `list_deploy_packages`, pick the target OU, choose Computer (startup) install, then
  the standard confirm-token modal. A "Deployments" list shows what AID has pushed,
  with unlink/remove.
- Routes mirror the other services: `GET /api/deploy/packages`, `GET /api/deploy/list`,
  `POST /api/deploy` (token-gated), `POST /api/deploy/remove` (token-gated), gated on
  the `deploy` capability.
- The cloud backend only ever sends *instructions* (package name, OU, args) to the
  agent queue. It never touches the installer bytes.

## Honest positioning

GPO-based deployment is a solid fit for "push this MSI/EXE silently to an OU," and the
startup-script approach makes it scriptable and EXE-capable. But it is boot-time,
has no native reporting, and needs the idempotency check. If application deployment
becomes a headline feature with per-device reporting, bandwidth control, and win32
packaging, the right tool is **Intune** (cloud) or **PDQ Deploy** (on-prem), and AID
should integrate with those rather than stretch GPO to cover them. Position this
feature as "quick silent rollout to an OU," and be upfront about the constraints.

## Rough build order

1. `deploy_bridge.py`: `list_deploy_packages` + `deploy_app` (MSI startup-script path),
   offline-testable script builders. Agent auto-registers it (`deploy` capability).
2. `action_policy.py` tiers + AI tool schema entries (reads free, writes token-gated).
3. Cloud routes + Group Policy "Deploy App" UI + confirm flow.
4. EXE support + `stage_package` (agent-fetch-from-URL) + `remove_deployment`.
5. Docs: SECURITY.md (new tier), a short SELF_HOSTING note on creating the share.
