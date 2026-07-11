#!/usr/bin/env python3
"""
dhcp_bridge.py -- Windows DHCP Server Automation Bridge
Connects to a Windows Server via WinRM and executes DHCP operations.

All public functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded (see winrm_core.py).

Every action follows a script-builder / executor split so the PowerShell can
be unit-tested offline without a live DHCP server:
    _build_<name>_script(...) -> str   # pure, no I/O
    <name>(...)                        # calls _run(_build_<name>_script(...))
"""

import re

from winrm_core import (
    _run,
    _ps_escape,
    _normalise_list,
    role_installed_check,
)

CAPABILITY = "dhcp"

# ---------------------------------------------------------------------------
# Validation (Python-side, BEFORE any PowerShell is built)
# ---------------------------------------------------------------------------

_NAME_RE = re.compile(r"^[A-Za-z0-9. \-_]+$")

_MAX_LEASES = 500


class DhcpValidationError(ValueError):
    """Raised when user-supplied input fails validation before any PS is built."""


def _validate_ip(value: str, label: str) -> str:
    """Validate a dotted-quad IPv4 address. Raises DhcpValidationError."""
    if not value or not isinstance(value, str):
        raise DhcpValidationError(f"{label} is required.")
    value = value.strip()
    parts = value.split(".")
    if len(parts) != 4:
        raise DhcpValidationError(f"{label} '{value}' is not a valid IPv4 address.")
    for part in parts:
        if not part.isdigit() or len(part) > 3:
            raise DhcpValidationError(f"{label} '{value}' is not a valid IPv4 address.")
        if not (0 <= int(part) <= 255):
            raise DhcpValidationError(
                f"{label} '{value}' is not a valid IPv4 address (octet '{part}' out of range 0-255)."
            )
    return value


def _validate_mac(value: str) -> str:
    """Accepts AA-BB-CC-DD-EE-FF, AABBCCDDEEFF, or AA:BB:CC:DD:EE:FF.
    Normalises to dashed uppercase form: AA-BB-CC-DD-EE-FF."""
    if not value or not isinstance(value, str):
        raise DhcpValidationError("MAC address is required.")
    raw = value.strip().upper().replace(":", "-")
    if re.match(r"^[0-9A-F]{2}(-[0-9A-F]{2}){5}$", raw):
        return raw
    if re.match(r"^[0-9A-F]{12}$", raw):
        return "-".join(raw[i:i + 2] for i in range(0, 12, 2))
    raise DhcpValidationError(
        f"MAC address '{value}' is not valid. Use AA-BB-CC-DD-EE-FF, AABBCCDDEEFF, or AA:BB:CC:DD:EE:FF."
    )


def _validate_text(value: str, label: str, required: bool = True, max_len: int = 128) -> str:
    """Conservative allowlist: letters, digits, space, dot, hyphen, underscore."""
    if value is None or not isinstance(value, str) or not value.strip():
        if required:
            raise DhcpValidationError(f"{label} is required.")
        return ""
    v = value.strip()
    if len(v) > max_len:
        raise DhcpValidationError(f"{label} is too long (max {max_len} characters).")
    if not _NAME_RE.match(v):
        raise DhcpValidationError(
            f"{label} '{v}' contains characters that are not allowed. "
            "Only letters, digits, spaces, dots, hyphens, and underscores are permitted."
        )
    return v


# ---------------------------------------------------------------------------
# Script builders (pure -- no I/O, safe to call and print offline)
# ---------------------------------------------------------------------------

def _preamble() -> str:
    return role_installed_check("DhcpServer", "DHCP Server role")


def _build_list_scopes_script() -> str:
    return f"""
{_preamble()}
Get-DhcpServerv4Scope |
  Select-Object ScopeId, Name, State, StartRange, EndRange, SubnetMask, LeaseDuration |
  ConvertTo-Json
"""


def _build_get_scope_script(scope_id: str) -> str:
    s = _ps_escape(scope_id)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$scope = Get-DhcpServerv4Scope -ScopeId '{s}' |
  Select-Object ScopeId, Name, State, StartRange, EndRange, SubnetMask, LeaseDuration
$stats = Get-DhcpServerv4ScopeStatistics -ScopeId '{s}' |
  Select-Object Free, InUse, PercentageInUse
[PSCustomObject]@{{
    Scope      = $scope
    Statistics = $stats
}} | ConvertTo-Json -Depth 4
"""


def _build_get_scope_stats_script() -> str:
    return f"""
{_preamble()}
Get-DhcpServerv4ScopeStatistics |
  Select-Object ScopeId, Free, InUse, PercentageInUse |
  ConvertTo-Json
"""


def _build_list_leases_script(scope_id: str) -> str:
    s = _ps_escape(scope_id)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-DhcpServerv4Lease -ScopeId '{s}' | Select-Object -First {_MAX_LEASES} |
  Select-Object IPAddress, ClientId, HostName, AddressState, LeaseExpiryTime |
  ConvertTo-Json
"""


def _build_list_reservations_script(scope_id: str) -> str:
    s = _ps_escape(scope_id)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-DhcpServerv4Reservation -ScopeId '{s}' |
  Select-Object IPAddress, ClientId, Name, Description |
  ConvertTo-Json
"""


def _build_add_reservation_script(scope_id: str, ip_address: str, mac: str, name: str, description: str) -> str:
    s = _ps_escape(scope_id)
    ip = _ps_escape(ip_address)
    m = _ps_escape(mac)
    n = _ps_escape(name)
    d = _ps_escape(description)
    desc_param = f"-Description '{d}'" if description else ""
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Add-DhcpServerv4Reservation -ScopeId '{s}' -IPAddress '{ip}' -ClientId '{m}' -Name '{n}' {desc_param}
Write-Output '{{"result":"added","scope_id":"{s}","ip_address":"{ip}"}}'
"""


def _build_remove_reservation_script(scope_id: str, ip_address: str) -> str:
    s = _ps_escape(scope_id)
    ip = _ps_escape(ip_address)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Remove-DhcpServerv4Reservation -ScopeId '{s}' -IPAddress '{ip}'
Write-Output '{{"result":"removed","scope_id":"{s}","ip_address":"{ip}"}}'
"""


def _build_add_exclusion_range_script(scope_id: str, start_ip: str, end_ip: str) -> str:
    s = _ps_escape(scope_id)
    a = _ps_escape(start_ip)
    b = _ps_escape(end_ip)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Add-DhcpServerv4ExclusionRange -ScopeId '{s}' -StartRange '{a}' -EndRange '{b}'
Write-Output '{{"result":"added","scope_id":"{s}","start":"{a}","end":"{b}"}}'
"""


def _build_remove_exclusion_range_script(scope_id: str, start_ip: str, end_ip: str) -> str:
    s = _ps_escape(scope_id)
    a = _ps_escape(start_ip)
    b = _ps_escape(end_ip)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Remove-DhcpServerv4ExclusionRange -ScopeId '{s}' -StartRange '{a}' -EndRange '{b}'
Write-Output '{{"result":"removed","scope_id":"{s}","start":"{a}","end":"{b}"}}'
"""


def _build_list_exclusions_script(scope_id: str) -> str:
    s = _ps_escape(scope_id)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-DhcpServerv4ExclusionRange -ScopeId '{s}' |
  Select-Object ScopeId, StartRange, EndRange |
  ConvertTo-Json
"""


# ---------------------------------------------------------------------------
# Public functions (validate -> build -> run)
# ---------------------------------------------------------------------------

def list_scopes() -> dict:
    r = _run(_build_list_scopes_script())
    if r["success"]:
        scopes = _normalise_list(r["data"])
        out = []
        for sc in scopes:
            out.append({
                "scope_id": sc.get("ScopeId"),
                "name": sc.get("Name"),
                "state": sc.get("State"),
                "start_range": sc.get("StartRange"),
                "end_range": sc.get("EndRange"),
                "subnet_mask": sc.get("SubnetMask"),
                "lease_duration": sc.get("LeaseDuration"),
            })
        r["data"] = out
        r["message"] = f"{len(out)} scope(s) found."
    return r


def get_scope(scope_id: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_get_scope_script(scope_id))
    if r["success"]:
        r["message"] = f"Scope info retrieved for {scope_id}."
    return r


def get_scope_stats() -> dict:
    r = _run(_build_get_scope_stats_script())
    if r["success"]:
        stats = _normalise_list(r["data"])
        r["data"] = stats
        r["message"] = f"Utilisation stats retrieved for {len(stats)} scope(s)."
    return r


def list_leases(scope_id: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_leases_script(scope_id))
    if r["success"]:
        leases = _normalise_list(r["data"])
        r["data"] = leases
        r["message"] = f"{len(leases)} lease(s) found in scope '{scope_id}'."
    return r


def list_reservations(scope_id: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_reservations_script(scope_id))
    if r["success"]:
        reservations = _normalise_list(r["data"])
        r["data"] = reservations
        r["message"] = f"{len(reservations)} reservation(s) found in scope '{scope_id}'."
    return r


def add_reservation(scope_id: str, ip_address: str, mac: str, name: str, description: str = "") -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
        ip_address = _validate_ip(ip_address, "IP address")
        mac = _validate_mac(mac)
        name = _validate_text(name, "Reservation name", required=True)
        description = _validate_text(description, "Description", required=False, max_len=256)
        script = _build_add_reservation_script(scope_id, ip_address, mac, name, description)
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"Reservation for {ip_address} ({mac}) added to scope '{scope_id}'."
    return r


def remove_reservation(scope_id: str, ip_address: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
        ip_address = _validate_ip(ip_address, "IP address")
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_remove_reservation_script(scope_id, ip_address))
    if r["success"]:
        r["message"] = f"Reservation for {ip_address} removed from scope '{scope_id}'."
    return r


def add_exclusion_range(scope_id: str, start_ip: str, end_ip: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
        start_ip = _validate_ip(start_ip, "Start IP")
        end_ip = _validate_ip(end_ip, "End IP")
        script = _build_add_exclusion_range_script(scope_id, start_ip, end_ip)
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"Exclusion range {start_ip}-{end_ip} added to scope '{scope_id}'."
    return r


def remove_exclusion_range(scope_id: str, start_ip: str, end_ip: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
        start_ip = _validate_ip(start_ip, "Start IP")
        end_ip = _validate_ip(end_ip, "End IP")
        script = _build_remove_exclusion_range_script(scope_id, start_ip, end_ip)
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"Exclusion range {start_ip}-{end_ip} removed from scope '{scope_id}'."
    return r


def list_exclusions(scope_id: str) -> dict:
    try:
        scope_id = _validate_ip(scope_id, "Scope ID")
    except DhcpValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_exclusions_script(scope_id))
    if r["success"]:
        exclusions = _normalise_list(r["data"])
        r["data"] = exclusions
        r["message"] = f"{len(exclusions)} exclusion range(s) found in scope '{scope_id}'."
    return r


# ---------------------------------------------------------------------------
# Action registry -- merged by agent.py. Every bridge module exposes ACTIONS
# mapping a flat action name to a callable taking one list of args.
# ---------------------------------------------------------------------------

ACTIONS = {
    "list_dhcp_scopes":        lambda a: list_scopes(),
    "get_dhcp_scope":          lambda a: get_scope(*a),
    "get_dhcp_scope_stats":    lambda a: get_scope_stats(),
    "list_dhcp_leases":        lambda a: list_leases(*a),
    "list_dhcp_reservations":  lambda a: list_reservations(*a),
    "add_dhcp_reservation":    lambda a: add_reservation(*a),
    "remove_dhcp_reservation": lambda a: remove_reservation(*a),
    "add_dhcp_exclusion":      lambda a: add_exclusion_range(*a),
    "remove_dhcp_exclusion":   lambda a: remove_exclusion_range(*a),
    "list_dhcp_exclusions":    lambda a: list_exclusions(*a),
}
