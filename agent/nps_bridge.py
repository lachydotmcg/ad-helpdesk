#!/usr/bin/env python3
"""
nps_bridge.py -- Windows Network Policy Server (NPS) Read-Only Bridge
Connects to a Windows Server via WinRM and reads NPS (RADIUS) configuration.

All public functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded (see winrm_core.py).

Every action follows a script-builder / executor split so the PowerShell can
be unit-tested offline without a live NPS server:
    _build_<name>_script(...) -> str   # pure, no I/O
    <name>(...)                        # calls _run(_build_<name>_script(...))

Overnight scope note (5.1, stretch): READ-ONLY. This module never issues a
New-/Set-/Remove-Nps* cmdlet. It exists purely to surface RADIUS clients,
network policies, and connection request policies for a display-only tab.
Any future write support (adding a RADIUS client, editing a policy) is a
separate, deliberate project -- not something to bolt on here.

SharedSecret note: Get-NpsRadiusClient exposes a SharedSecret property (the
RADIUS shared secret, effectively a credential). We NEVER select, return, or
log that field anywhere in this module -- see the explicit exclusion comment
in _build_list_radius_clients_script below.

Windows Server version note: the NPS PowerShell module's cmdlet surface has
drifted across Server releases (e.g. some builds lack Get-NpsConnectionRequestPolicy
or use different property names). role_installed_check() plus
$ErrorActionPreference = 'Stop' catches a missing module outright. For cmdlets
that may be missing even when the module is present, each script checks
Get-Command first and Write-Error's a clear, friendly line instead of letting
PowerShell throw a raw "not recognized" error.
"""

from winrm_core import (
    _run,
    _normalise_list,
    role_installed_check,
)

CAPABILITY = "nps"

# ---------------------------------------------------------------------------
# Script builders (pure -- no I/O, safe to call and print offline)
# ---------------------------------------------------------------------------

def _preamble() -> str:
    return role_installed_check("NPS", "Network Policy Server role")


def _cmdlet_guard(cmdlet: str, friendly: str) -> str:
    """Fragment that checks a specific NPS cmdlet exists before calling it.
    Some Windows Server builds ship a trimmed-down NPS module missing certain
    cmdlets -- this turns a raw 'not recognized' error into a friendly one."""
    return f"""
if (-not (Get-Command {cmdlet} -ErrorAction SilentlyContinue)) {{
  Write-Error 'This server does not expose {cmdlet} ({friendly} unavailable on this Windows Server version).'
  exit 1
}}
"""


def _build_list_radius_clients_script() -> str:
    # NOTE: SharedSecret is intentionally EXCLUDED from Select-Object below.
    # It is the RADIUS shared secret (a credential) and must never be read,
    # returned to the cloud, or displayed in the UI. Do not add it here.
    return f"""
{_preamble()}
{_cmdlet_guard('Get-NpsRadiusClient', 'RADIUS client listing')}
$ErrorActionPreference = 'Stop'
Get-NpsRadiusClient |
  Select-Object Name, Address, VendorName, AuthAttributeRequired, Enabled |
  ConvertTo-Json
"""


def _build_list_network_policies_script() -> str:
    return f"""
{_preamble()}
{_cmdlet_guard('Get-NpsNetworkPolicy', 'network policy listing')}
$ErrorActionPreference = 'Stop'
Get-NpsNetworkPolicy |
  Select-Object Name, Enabled, ProcessingOrder,
                @{{N='ConditionText';E={{ if ($_.PSObject.Properties['ConditionText']) {{ $_.ConditionText }} else {{ $null }} }}}} |
  ConvertTo-Json
"""


def _build_list_connection_request_policies_script() -> str:
    return f"""
{_preamble()}
{_cmdlet_guard('Get-NpsConnectionRequestPolicy', 'connection request policy listing')}
$ErrorActionPreference = 'Stop'
Get-NpsConnectionRequestPolicy |
  Select-Object Name, Enabled, ProcessingOrder,
                @{{N='ConditionText';E={{ if ($_.PSObject.Properties['ConditionText']) {{ $_.ConditionText }} else {{ $null }} }}}} |
  ConvertTo-Json
"""


def _build_get_nps_config_summary_script() -> str:
    # Single round-trip: counts only, for a dashboard overview card. Each
    # section is individually guarded so a missing cmdlet on one section
    # doesn't blank out the whole summary -- it just reports 0 with a note.
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'

$clientCount = 0
$clientsAvailable = $true
if (Get-Command Get-NpsRadiusClient -ErrorAction SilentlyContinue) {{
  $clientCount = @(Get-NpsRadiusClient).Count
}} else {{
  $clientsAvailable = $false
}}

$networkPolicyCount = 0
$networkPoliciesAvailable = $true
if (Get-Command Get-NpsNetworkPolicy -ErrorAction SilentlyContinue) {{
  $networkPolicyCount = @(Get-NpsNetworkPolicy).Count
}} else {{
  $networkPoliciesAvailable = $false
}}

$crpCount = 0
$crpAvailable = $true
if (Get-Command Get-NpsConnectionRequestPolicy -ErrorAction SilentlyContinue) {{
  $crpCount = @(Get-NpsConnectionRequestPolicy).Count
}} else {{
  $crpAvailable = $false
}}

[PSCustomObject]@{{
    RadiusClientCount            = $clientCount
    RadiusClientsAvailable       = $clientsAvailable
    NetworkPolicyCount           = $networkPolicyCount
    NetworkPoliciesAvailable     = $networkPoliciesAvailable
    ConnectionRequestPolicyCount = $crpCount
    ConnectionRequestPoliciesAvailable = $crpAvailable
}} | ConvertTo-Json
"""


# ---------------------------------------------------------------------------
# Public functions (build -> run). No validation section needed -- every
# function here is a zero-argument read, there is no user-supplied input to
# sanitise before building the script.
# ---------------------------------------------------------------------------

def list_radius_clients() -> dict:
    r = _run(_build_list_radius_clients_script())
    if r["success"]:
        clients = _normalise_list(r["data"])
        out = []
        for c in clients:
            out.append({
                "name": c.get("Name"),
                "address": c.get("Address"),
                "vendor_name": c.get("VendorName"),
                "auth_attribute_required": c.get("AuthAttributeRequired"),
                "enabled": bool(c.get("Enabled")),
                # SharedSecret is deliberately never read from PowerShell output
                # (excluded at the Select-Object stage above), so there is
                # nothing to strip here -- it never crosses the wire.
            })
        r["data"] = out
        r["message"] = f"{len(out)} RADIUS client(s) found."
    return r


def list_network_policies() -> dict:
    r = _run(_build_list_network_policies_script())
    if r["success"]:
        policies = _normalise_list(r["data"])
        out = []
        for p in policies:
            out.append({
                "name": p.get("Name"),
                "enabled": bool(p.get("Enabled")),
                "processing_order": p.get("ProcessingOrder"),
                "condition_text": p.get("ConditionText"),
            })
        r["data"] = out
        r["message"] = f"{len(out)} network policy(ies) found."
    return r


def list_connection_request_policies() -> dict:
    r = _run(_build_list_connection_request_policies_script())
    if r["success"]:
        policies = _normalise_list(r["data"])
        out = []
        for p in policies:
            out.append({
                "name": p.get("Name"),
                "enabled": bool(p.get("Enabled")),
                "processing_order": p.get("ProcessingOrder"),
                "condition_text": p.get("ConditionText"),
            })
        r["data"] = out
        r["message"] = f"{len(out)} connection request policy(ies) found."
    return r


def get_nps_config_summary() -> dict:
    r = _run(_build_get_nps_config_summary_script())
    if r["success"]:
        d = r["data"] or {}
        summary = {
            "radius_client_count": d.get("RadiusClientCount", 0),
            "radius_clients_available": bool(d.get("RadiusClientsAvailable", True)),
            "network_policy_count": d.get("NetworkPolicyCount", 0),
            "network_policies_available": bool(d.get("NetworkPoliciesAvailable", True)),
            "connection_request_policy_count": d.get("ConnectionRequestPolicyCount", 0),
            "connection_request_policies_available": bool(d.get("ConnectionRequestPoliciesAvailable", True)),
        }
        r["data"] = summary
        r["message"] = "NPS configuration summary retrieved."
    return r


# ---------------------------------------------------------------------------
# Action registry -- merged by agent.py. Every bridge module exposes ACTIONS
# mapping a flat action name to a callable taking one list of args.
# ---------------------------------------------------------------------------

ACTIONS = {
    "list_nps_radius_clients":     lambda a: list_radius_clients(),
    "list_nps_network_policies":   lambda a: list_network_policies(),
    "list_nps_connection_policies": lambda a: list_connection_request_policies(),
    "get_nps_summary":             lambda a: get_nps_config_summary(),
}
