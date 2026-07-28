#!/usr/bin/env python3
"""
deploy_bridge.py -- Application deployment via Group Policy (startup-script model).

Pushes an installer (MSI or silent EXE) to every machine in an OU by creating a
GPO whose machine startup script runs the installer from a file share. See
docs/deploy-app-design.md for the full rationale. In short:

  * GPO "Software Installation" has no real PowerShell API, so we use a computer
    startup-script GPO instead. It is fully scriptable and works for EXE and MSI.
  * The installer lives on a dedicated SMB share (never SYSVOL, which would
    replicate a large payload to every DC via DFSR). The GPO holds only a ~1 KB
    script. File size is therefore bounded only by the share disk and the LAN,
    and nothing large ever flows through the AID cloud.

All public functions return: {"success": bool, "message": str, "data": ...}.
Each action splits into a pure _build_<name>_script(...) (offline-testable) and a
public wrapper that validates input BEFORE any PowerShell is built.

LIVE-DC NOTE: the startup-script GPO plumbing (registering the Scripts + PowerShell
client-side-extension GUIDs on the GPO's AD object via gPCMachineExtensionNames, and
bumping the GPO version so clients re-read it) is fiddly and has not been exercised
against a real domain controller yet. The script builders below implement the
documented approach; validate on a lab DC before shipping.
"""

import os
import re

from winrm_core import (
    _run,
    _ps_escape,
    _normalise_list,
    _make_ou_path,
    role_installed_check,
)

CAPABILITY = "deploy"

# GPOs AID creates are all prefixed so we can list/manage only our own.
GPO_PREFIX = "AID Deploy - "

# Client-side-extension GUIDs that register a GPO as carrying machine scripts:
# [ {Scripts CSE} {PowerShell scripts tool} ]
_SCRIPTS_CSE = "[{42B5FAAE-6536-11D2-AE5A-0000F87571E3}{40B6664F-4972-11D1-A7CA-0000F87571E3}]"

# ---------------------------------------------------------------------------
# Validation (Python-side, BEFORE any PowerShell is built)
# ---------------------------------------------------------------------------

# App display names become part of the GPO name; keep them conservative.
_APP_NAME_RE = re.compile(r"^[A-Za-z0-9 .\-_()]+$")
# A package file name on the share: no path separators, no traversal.
_PACKAGE_RE = re.compile(r"^[A-Za-z0-9 .\-_()]+\.(msi|exe)$", re.IGNORECASE)
# Silent-install arguments for EXE installers: a conservative allowlist so nothing
# hostile reaches the command line. Covers the common /S /silent /qn -style flags.
_SILENT_ARGS_RE = re.compile(r'^[A-Za-z0-9 /\-=._:\\"]*$')

_ALLOWED_TYPES = {"msi", "exe"}


class DeployValidationError(ValueError):
    """Raised when user-supplied input fails validation before any PS is built."""


def _deploy_share() -> str:
    """The configured software share UNC (e.g. \\\\FILESERVER\\aid-apps$).
    Injected by the agent from agent-config.json's software_deploy.share_unc."""
    share = os.getenv("AID_DEPLOY_SHARE_UNC", "").strip()
    if not share:
        raise DeployValidationError(
            "Software deployment share is not configured. Set software_deploy.share_unc "
            "in agent-config.json to a file share (not SYSVOL) that holds your installers."
        )
    if not (share.startswith("\\\\") or share.startswith("//")):
        raise DeployValidationError("The software share must be a UNC path, e.g. \\\\SERVER\\share.")
    if any(ord(c) < 32 for c in share) or "'" in share:
        raise DeployValidationError("The software share path contains characters that are not allowed.")
    return share.rstrip("\\/")


def _validate_app_name(name: str) -> str:
    if not name or not isinstance(name, str):
        raise DeployValidationError("Application name is required.")
    name = name.strip()
    if not _APP_NAME_RE.match(name):
        raise DeployValidationError(
            "Application name may only contain letters, digits, spaces, dots, hyphens, "
            "underscores, and parentheses."
        )
    if len(name) > 120:
        raise DeployValidationError("Application name is too long (max 120 characters).")
    return name


def _validate_package(package: str) -> str:
    if not package or not isinstance(package, str):
        raise DeployValidationError("Package file name is required.")
    package = package.strip()
    if not _PACKAGE_RE.match(package):
        raise DeployValidationError(
            f"Package '{package}' is not valid. Provide a bare .msi or .exe file name that "
            "exists on the software share (no folders or paths)."
        )
    return package


def _validate_type(install_type: str) -> str:
    t = (install_type or "").strip().lower()
    if t not in _ALLOWED_TYPES:
        raise DeployValidationError("Install type must be 'msi' or 'exe'.")
    return t


def _validate_silent_args(silent_args: str) -> str:
    s = (silent_args or "").strip()
    if not _SILENT_ARGS_RE.match(s):
        raise DeployValidationError(
            "Silent-install arguments contain characters that are not allowed. Use only the "
            "installer's silent switches (e.g. /S, /qn, /norestart)."
        )
    if len(s) > 300:
        raise DeployValidationError("Silent-install arguments are too long (max 300 characters).")
    return s


def _validate_ou(ou_dn_or_name: str) -> str:
    if not ou_dn_or_name or not isinstance(ou_dn_or_name, str):
        raise DeployValidationError("Target OU is required.")
    ou_dn_or_name = ou_dn_or_name.strip()
    if any(ord(c) < 32 for c in ou_dn_or_name):
        raise DeployValidationError("OU identifier contains control characters, which is not allowed.")
    if len(ou_dn_or_name) > 500:
        raise DeployValidationError("OU identifier is too long (max 500 characters).")
    return _make_ou_path(ou_dn_or_name)


def _validate_gpo_name(name: str) -> str:
    """A full 'AID Deploy - <App>' GPO name, for remove/list operations."""
    if not name or not isinstance(name, str):
        raise DeployValidationError("Deployment name is required.")
    name = name.strip()
    if not name.startswith(GPO_PREFIX):
        raise DeployValidationError(f"Deployment name must start with '{GPO_PREFIX}'.")
    if not _APP_NAME_RE.match(name):
        raise DeployValidationError("Deployment name contains characters that are not allowed.")
    return name


# ---------------------------------------------------------------------------
# Script builders (pure -- no I/O, safe to call and print offline)
# ---------------------------------------------------------------------------

def _preamble() -> str:
    return role_installed_check("GroupPolicy", "Group Policy Management")


def _build_install_script(package: str, install_type: str, silent_args: str) -> str:
    """The idempotent installer that the GPO drops as a machine startup script.
    Runs on every boot, so it checks for a per-package marker first and no-ops if
    the app is already installed. Kept as its own builder so it can be inspected."""
    pkg = _ps_escape(package)
    args = _ps_escape(silent_args) if silent_args else ""
    if install_type == "msi":
        run_line = f"$p = Start-Process msiexec.exe -ArgumentList '/i \"$src\" /qn /norestart' -Wait -PassThru"
    else:
        # EXE: use the admin-provided silent switches verbatim (already allowlisted).
        run_line = f"$p = Start-Process \"$src\" -ArgumentList '{args}' -Wait -PassThru"
    return f"""$ErrorActionPreference = 'Stop'
$log = 'C:\\ProgramData\\AIDHelpdesk'
if (-not (Test-Path $log)) {{ New-Item -ItemType Directory -Path $log -Force | Out-Null }}
$marker = Join-Path $log 'deployed-{pkg}.done'
$logfile = Join-Path $log 'deploy.log'
if (Test-Path $marker) {{ return }}
$src = '{_ps_escape(_deploy_share_unsafe())}\\{pkg}'
try {{
  {run_line}
  $code = $p.ExitCode
  # MSI success = 0 or 3010 (reboot required); most EXE installers use 0.
  if ($code -eq 0 -or $code -eq 3010) {{ Set-Content -Path $marker -Value (Get-Date).ToString() }}
  Add-Content -Path $logfile -Value ((Get-Date).ToString() + '  {pkg}  exit=' + $code)
}} catch {{
  Add-Content -Path $logfile -Value ((Get-Date).ToString() + '  {pkg}  ERROR ' + $_.Exception.Message)
}}
"""


def _build_deploy_app_script(app_name: str, package: str, install_type: str,
                             silent_args: str, ou_dn: str) -> str:
    """Create the GPO, drop the idempotent startup script into its SYSVOL Scripts
    folder, register the script CSEs + bump the version so clients pick it up, then
    link the GPO to the target OU."""
    gpo_name = _ps_escape(GPO_PREFIX + app_name)
    ou = _ps_escape(ou_dn)
    install_script = _build_install_script(package, install_type, silent_args)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'

# 1. Create (or reuse) the deployment GPO
$existing = Get-GPO -Name '{gpo_name}' -ErrorAction SilentlyContinue
if ($existing) {{ $gpo = $existing }} else {{ $gpo = New-GPO -Name '{gpo_name}' }}
$guid = '{{' + $gpo.Id.ToString() + '}}'
$domain = $gpo.DomainName

# 2. Write the idempotent install script into the GPO's machine startup folder
$scriptRoot = "\\\\$domain\\SYSVOL\\$domain\\Policies\\$guid\\Machine\\Scripts"
$startupDir = Join-Path $scriptRoot 'Startup'
New-Item -ItemType Directory -Path $startupDir -Force | Out-Null
$installBody = @'
{install_script}
'@
Set-Content -Path (Join-Path $startupDir 'aid-install.ps1') -Value $installBody -Encoding UTF8

# 3. Register the PowerShell startup script in psscripts.ini (must be Unicode)
$ini = "[Startup]`r`n0CmdLine=aid-install.ps1`r`n0Parameters=`r`n"
Set-Content -Path (Join-Path $scriptRoot 'psscripts.ini') -Value $ini -Encoding Unicode

# 4. Register the Scripts client-side extensions on the GPO's AD object and bump
#    the machine version so clients re-process the policy.
$domainDN = (Get-ADDomain).DistinguishedName
$gpoDN = "CN=$guid,CN=Policies,CN=System,$domainDN"
Set-ADObject -Identity $gpoDN -Replace @{{ 'gPCMachineExtensionNames' = '{_SCRIPTS_CSE}' }}
$obj = Get-ADObject -Identity $gpoDN -Properties versionNumber
$newVer = [int]$obj.versionNumber + 1
Set-ADObject -Identity $gpoDN -Replace @{{ 'versionNumber' = $newVer }}
$gptIni = "\\\\$domain\\SYSVOL\\$domain\\Policies\\$guid\\gpt.ini"
Set-Content -Path $gptIni -Value ("[General]`r`nVersion=" + $newVer) -Encoding ASCII

# 5. Link the GPO to the target OU
$link = Get-GPInheritance -Target '{ou}' | Select-Object -ExpandProperty GpoLinks | Where-Object {{ $_.GpoId -eq $gpo.Id }}
if (-not $link) {{ New-GPLink -Guid $gpo.Id -Target '{ou}' -LinkEnabled Yes | Out-Null }}

[pscustomobject]@{{ name = $gpo.DisplayName; id = $gpo.Id.ToString(); ou = '{ou}' }} | ConvertTo-Json
"""


def _build_list_packages_script(share_unc: str) -> str:
    s = _ps_escape(share_unc)
    return f"""
$ErrorActionPreference = 'Stop'
if (-not (Test-Path '{s}')) {{ Write-Error 'Software share not reachable: {s}'; exit 1 }}
Get-ChildItem -Path '{s}' -File -Include *.msi,*.exe -Recurse:$false -ErrorAction SilentlyContinue |
  Select-Object Name,
                @{{N='SizeMB';E={{[math]::Round($_.Length/1MB,1)}}}},
                @{{N='Type';E={{$_.Extension.TrimStart('.').ToLower()}}}},
                @{{N='Modified';E={{$_.LastWriteTime}}}} |
  Sort-Object Name |
  ConvertTo-Json
"""


def _build_list_deployments_script() -> str:
    prefix = _ps_escape(GPO_PREFIX)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-GPO -All | Where-Object {{ $_.DisplayName -like '{prefix}*' }} | ForEach-Object {{
  $links = @()
  try {{
    $rep = [xml](Get-GPOReport -Guid $_.Id -ReportType Xml)
    $links = @($rep.GPO.LinksTo | ForEach-Object {{ $_.SOMPath }})
  }} catch {{}}
  [pscustomobject]@{{
    name = $_.DisplayName
    id = $_.Id.ToString()
    status = $_.GpoStatus.ToString()
    modified = $_.ModificationTime
    links = $links
  }}
}} | ConvertTo-Json -Depth 4
"""


def _build_remove_deployment_script(gpo_name: str) -> str:
    n = _ps_escape(gpo_name)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$gpo = Get-GPO -Name '{n}'
# Unlink from every OU it is linked to, then delete the GPO. This removes the
# policy but does NOT uninstall the app from machines that already have it.
try {{
  $rep = [xml](Get-GPOReport -Guid $gpo.Id -ReportType Xml)
  foreach ($som in @($rep.GPO.LinksTo)) {{
    if ($som.SOMPath) {{ Remove-GPLink -Guid $gpo.Id -Target $som.SOMPath -ErrorAction SilentlyContinue | Out-Null }}
  }}
}} catch {{}}
Remove-GPO -Guid $gpo.Id -ErrorAction Stop | Out-Null
[pscustomobject]@{{ removed = '{n}' }} | ConvertTo-Json
"""


def _deploy_share_unsafe() -> str:
    """Share UNC for embedding in the install script. Falls back to a placeholder
    if unset so _build_install_script stays printable offline; the public
    deploy_app path validates the share via _deploy_share() before building."""
    return os.getenv("AID_DEPLOY_SHARE_UNC", "\\\\FILESERVER\\aid-apps$").rstrip("\\/")


# ---------------------------------------------------------------------------
# Public functions (validate -> build -> run)
# ---------------------------------------------------------------------------

def list_deploy_packages() -> dict:
    try:
        share = _deploy_share()
    except DeployValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_packages_script(share))
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} installer(s) available on the software share."
    return r


def deploy_app(app_name: str, package: str, target_ou: str,
               install_type: str = "msi", silent_args: str = "") -> dict:
    try:
        share = _deploy_share()  # ensures configured before we build anything
        name = _validate_app_name(app_name)
        pkg = _validate_package(package)
        itype = _validate_type(install_type)
        args = _validate_silent_args(silent_args)
        if itype == "exe" and not args:
            raise DeployValidationError(
                "EXE installs need silent-install arguments (e.g. /S or /qn) so the rollout "
                "runs unattended."
            )
        ou = _validate_ou(target_ou)
        script = _build_deploy_app_script(name, pkg, itype, args, ou)
    except DeployValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = (f"Deployment '{GPO_PREFIX}{name}' created and linked to '{ou}'. "
                        f"Machines will install {pkg} at next boot.")
    return r


def list_deployments() -> dict:
    r = _run(_build_list_deployments_script())
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} AID deployment(s) found."
    return r


def remove_deployment(gpo_name: str) -> dict:
    try:
        name = _validate_gpo_name(gpo_name)
        script = _build_remove_deployment_script(name)
    except DeployValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = (f"Deployment '{name}' unlinked and deleted. Machines that already "
                        f"installed the app keep it; this only stops future installs.")
    return r


# ---------------------------------------------------------------------------
# Action registry -- merged by agent.py.
# ---------------------------------------------------------------------------

ACTIONS = {
    "list_deploy_packages": lambda a: list_deploy_packages(),
    "deploy_app":           lambda a: deploy_app(*a),
    "list_deployments":     lambda a: list_deployments(),
    "remove_deployment":    lambda a: remove_deployment(*a),
}
