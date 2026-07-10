#!/usr/bin/env python3
"""
dns_bridge.py -- Windows DNS Server Automation Bridge
Connects to a Windows Server via WinRM and executes DNS operations.

All public functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded (see winrm_core.py).

Every action follows a script-builder / executor split so the PowerShell can
be unit-tested offline without a live DNS server:
    _build_<name>_script(...) -> str   # pure, no I/O
    <name>(...)                        # calls _run(_build_<name>_script(...))

Update strategy note (update_record): the DnsServer PowerShell module has no
single "rename value" cmdlet for most record types. The safe, well-documented
pattern is Get-DnsServerResourceRecord to fetch the existing record object,
clone it (.Clone()), mutate the clone's RecordData, then
Set-DnsServerResourceRecord -OldInputObject / -NewInputObject. That is what
_build_update_record_script implements below.
"""

import re

from winrm_core import (
    _run,
    _ps_escape,
    _normalise_list,
    role_installed_check,
)

CAPABILITY = "dns"

# ---------------------------------------------------------------------------
# Validation (Python-side, BEFORE any PowerShell is built)
# ---------------------------------------------------------------------------

# Conservative: letters, digits, dots, hyphens, underscores, @ (root record).
_NAME_RE = re.compile(r"^[A-Za-z0-9.\-_@]+$")

_ALLOWED_RECORD_TYPES = {"A", "AAAA", "CNAME", "MX", "TXT", "PTR"}

_MAX_RECORDS = 500


class DnsValidationError(ValueError):
    """Raised when user-supplied input fails validation before any PS is built."""


def _validate_name(value: str, label: str) -> str:
    """Validate a zone name or record name/hostname. Raises DnsValidationError."""
    if not value or not isinstance(value, str):
        raise DnsValidationError(f"{label} is required.")
    value = value.strip()
    if not _NAME_RE.match(value):
        raise DnsValidationError(
            f"{label} '{value}' contains characters that are not allowed. "
            "Only letters, digits, dots, hyphens, underscores, and '@' are permitted."
        )
    if len(value) > 253:
        raise DnsValidationError(f"{label} is too long (max 253 characters).")
    return value


def _validate_record_type(record_type: str) -> str:
    rt = (record_type or "").strip().upper()
    if rt not in _ALLOWED_RECORD_TYPES:
        raise DnsValidationError(
            f"Record type '{record_type}' is not supported. "
            f"Allowed types: {', '.join(sorted(_ALLOWED_RECORD_TYPES))}."
        )
    return rt


def _validate_ttl(ttl_seconds) -> int:
    try:
        ttl = int(ttl_seconds)
    except (TypeError, ValueError):
        raise DnsValidationError("TTL must be a whole number of seconds.")
    if not (1 <= ttl <= 2592000):
        raise DnsValidationError("TTL must be between 1 and 2592000 seconds (30 days).")
    return ttl


def _validate_value(value: str, record_type: str) -> str:
    """Loose validation for record data -- length + no control characters.
    Type-specific shaping (e.g. splitting MX preference/host) happens in the
    script builder; this just guards against garbage before it reaches PS.
    """
    if value is None or not isinstance(value, str) or not value.strip():
        raise DnsValidationError(f"A value is required for a {record_type} record.")
    v = value.strip()
    if len(v) > 512:
        raise DnsValidationError("Record value is too long (max 512 characters).")
    if any(ord(c) < 32 for c in v):
        raise DnsValidationError("Record value contains control characters, which is not allowed.")
    return v


def _parse_mx_value(value: str) -> tuple:
    """Accepts 'preference priority.mail.host' or '10 mail.host'. Returns (preference, host)."""
    parts = value.split()
    if len(parts) != 2:
        raise DnsValidationError(
            "MX value must be in the form '<preference> <mail host>', e.g. '10 mail.example.local'."
        )
    pref_raw, host = parts
    try:
        preference = int(pref_raw)
    except ValueError:
        raise DnsValidationError("MX preference must be a whole number.")
    if not (0 <= preference <= 65535):
        raise DnsValidationError("MX preference must be between 0 and 65535.")
    host = _validate_name(host, "MX mail host")
    return preference, host


# ---------------------------------------------------------------------------
# Script builders (pure -- no I/O, safe to call and print offline)
# ---------------------------------------------------------------------------

def _preamble() -> str:
    return role_installed_check("DnsServer", "DNS Server role")


def _build_list_zones_script() -> str:
    return f"""
{_preamble()}
Get-DnsServerZone |
  Select-Object ZoneName, ZoneType, DynamicUpdate, IsAutoCreated, IsReverseLookupZone |
  ConvertTo-Json
"""


def _build_get_zone_script(zone_name: str) -> str:
    z = _ps_escape(zone_name)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-DnsServerZone -Name '{z}' |
  Select-Object ZoneName, ZoneType, DynamicUpdate, IsAutoCreated, IsReverseLookupZone,
                IsDsIntegrated, IsPaused, IsShutdown |
  ConvertTo-Json
"""


def _build_list_records_script(zone_name: str, record_type: str = "") -> str:
    z = _ps_escape(zone_name)
    rr_param = ""
    if record_type:
        rr_param = f"-RRType '{_ps_escape(record_type)}'"
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$records = Get-DnsServerResourceRecord -ZoneName '{z}' {rr_param} | Select-Object -First {_MAX_RECORDS}
$records | ForEach-Object {{
    $rt = $_.RecordType
    $rd = $_.RecordData
    $value = switch ($rt) {{
        'A'     {{ $rd.IPv4Address.ToString() }}
        'AAAA'  {{ $rd.IPv6Address.ToString() }}
        'CNAME' {{ $rd.HostNameAlias }}
        'MX'    {{ "$($rd.Preference) $($rd.MailExchange)" }}
        'TXT'   {{ $rd.DescriptiveText }}
        'PTR'   {{ $rd.PtrDomainName }}
        default {{ $rd.ToString() }}
    }}
    [PSCustomObject]@{{
        HostName   = $_.HostName
        RecordType = $rt
        TTLSeconds = $_.TimeToLive.TotalSeconds
        Value      = $value
        Timestamp  = $_.Timestamp
    }}
}} | ConvertTo-Json
"""


def _build_add_record_script(zone_name: str, name: str, record_type: str, value: str, ttl_seconds: int) -> str:
    z = _ps_escape(zone_name)
    n = _ps_escape(name)
    rt = record_type  # already validated/upper-cased by caller
    ttl_param = f"-TimeToLive ([TimeSpan]::FromSeconds({ttl_seconds}))"

    if rt == "A":
        v = _ps_escape(value)
        add_line = f"Add-DnsServerResourceRecordA -ZoneName '{z}' -Name '{n}' -IPv4Address '{v}' {ttl_param}"
    elif rt == "AAAA":
        v = _ps_escape(value)
        add_line = f"Add-DnsServerResourceRecordAAAA -ZoneName '{z}' -Name '{n}' -IPv6Address '{v}' {ttl_param}"
    elif rt == "CNAME":
        v = _ps_escape(value)
        add_line = f"Add-DnsServerResourceRecordCName -ZoneName '{z}' -Name '{n}' -HostNameAlias '{v}' {ttl_param}"
    elif rt == "MX":
        preference, host = _parse_mx_value(value)
        h = _ps_escape(host)
        add_line = (
            f"Add-DnsServerResourceRecordMX -ZoneName '{z}' -Name '{n}' "
            f"-MailExchange '{h}' -Preference {preference} {ttl_param}"
        )
    elif rt == "TXT":
        v = _ps_escape(value)
        add_line = f"Add-DnsServerResourceRecord -ZoneName '{z}' -Txt -Name '{n}' -DescriptiveText '{v}' {ttl_param}"
    elif rt == "PTR":
        v = _ps_escape(value)
        add_line = f"Add-DnsServerResourceRecordPtr -ZoneName '{z}' -Name '{n}' -PtrDomainName '{v}' {ttl_param}"
    else:
        # Should never happen -- validated in the public function.
        raise DnsValidationError(f"Unsupported record type: {rt}")

    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
{add_line}
Write-Output '{{"result":"added","zone":"{z}","name":"{n}","type":"{rt}"}}'
"""


def _build_remove_record_script(zone_name: str, name: str, record_type: str, value: str) -> str:
    z = _ps_escape(zone_name)
    n = _ps_escape(name)
    rt = record_type

    # Build a filter that matches on the RecordData for the given type so we
    # only remove the specific value, not every record with this name+type.
    match_expr = _record_match_expression(rt, value)

    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$target = Get-DnsServerResourceRecord -ZoneName '{z}' -Name '{n}' -RRType '{rt}' |
  Where-Object {{ {match_expr} }}
if (-not $target) {{
    Write-Error "No matching {rt} record found for '{n}' in zone '{z}' with the given value."
    exit 1
}}
$target | ForEach-Object {{
    Remove-DnsServerResourceRecord -ZoneName '{z}' -InputObject $_ -Force
}}
Write-Output '{{"result":"removed","zone":"{z}","name":"{n}","type":"{rt}"}}'
"""


def _record_match_expression(rt: str, value: str) -> str:
    """PS boolean expression (against $_) matching a resource record's data to `value`."""
    if rt == "A":
        v = _ps_escape(value)
        return f"$_.RecordData.IPv4Address.ToString() -eq '{v}'"
    if rt == "AAAA":
        v = _ps_escape(value)
        return f"$_.RecordData.IPv6Address.ToString() -eq '{v}'"
    if rt == "CNAME":
        v = _ps_escape(value)
        return f"$_.RecordData.HostNameAlias -eq '{v}'"
    if rt == "MX":
        preference, host = _parse_mx_value(value)
        h = _ps_escape(host)
        return f"($_.RecordData.MailExchange -eq '{h}') -and ($_.RecordData.Preference -eq {preference})"
    if rt == "TXT":
        v = _ps_escape(value)
        return f"$_.RecordData.DescriptiveText -eq '{v}'"
    if rt == "PTR":
        v = _ps_escape(value)
        return f"$_.RecordData.PtrDomainName -eq '{v}'"
    raise DnsValidationError(f"Unsupported record type: {rt}")


def _build_update_record_script(zone_name: str, name: str, record_type: str, old_value: str, new_value: str) -> str:
    """The DnsServer module has no in-place 'rename value' cmdlet, so this
    implements: fetch matching old record -> clone -> mutate clone's record
    data -> Set-DnsServerResourceRecord -OldInputObject/-NewInputObject.
    """
    z = _ps_escape(zone_name)
    n = _ps_escape(name)
    rt = record_type
    match_expr = _record_match_expression(rt, old_value)
    mutate_line = _record_mutate_expression(rt, new_value)

    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$old = Get-DnsServerResourceRecord -ZoneName '{z}' -Name '{n}' -RRType '{rt}' |
  Where-Object {{ {match_expr} }} |
  Select-Object -First 1
if (-not $old) {{
    Write-Error "No matching {rt} record found for '{n}' in zone '{z}' with the old value given."
    exit 1
}}
$new = $old.Clone()
{mutate_line}
Set-DnsServerResourceRecord -ZoneName '{z}' -OldInputObject $old -NewInputObject $new
Write-Output '{{"result":"updated","zone":"{z}","name":"{n}","type":"{rt}"}}'
"""


def _record_mutate_expression(rt: str, new_value: str) -> str:
    """PS statement(s) mutating $new.RecordData in place for the given type."""
    if rt == "A":
        v = _ps_escape(new_value)
        return f"$new.RecordData.IPv4Address = [System.Net.IPAddress]::Parse('{v}')"
    if rt == "AAAA":
        v = _ps_escape(new_value)
        return f"$new.RecordData.IPv6Address = [System.Net.IPAddress]::Parse('{v}')"
    if rt == "CNAME":
        v = _ps_escape(new_value)
        return f"$new.RecordData.HostNameAlias = '{v}'"
    if rt == "MX":
        preference, host = _parse_mx_value(new_value)
        h = _ps_escape(host)
        return f"$new.RecordData.MailExchange = '{h}'; $new.RecordData.Preference = {preference}"
    if rt == "TXT":
        v = _ps_escape(new_value)
        return f"$new.RecordData.DescriptiveText = '{v}'"
    if rt == "PTR":
        v = _ps_escape(new_value)
        return f"$new.RecordData.PtrDomainName = '{v}'"
    raise DnsValidationError(f"Unsupported record type: {rt}")


def _build_get_scavenging_script() -> str:
    return f"""
{_preamble()}
Get-DnsServerScavenging | Select-Object ScavengingState, ScavengingInterval, LastScavengeTime, RefreshInterval, NoRefreshInterval | ConvertTo-Json
"""


def _build_set_scavenging_script(enabled: bool) -> str:
    flag = "$true" if enabled else "$false"
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Set-DnsServerScavenging -ScavengingState {flag}
Write-Output '{{"result":"ok","scavenging_state":"{flag}"}}'
"""


# ---------------------------------------------------------------------------
# Public functions (validate -> build -> run)
# ---------------------------------------------------------------------------

def list_zones() -> dict:
    """List DNS zones. Auto-created zones (e.g. TrustAnchors, reverse root
    hints) are filtered out of the default view but still carry the
    is_auto_created flag for anyone who wants them."""
    r = _run(_build_list_zones_script())
    if r["success"]:
        zones = _normalise_list(r["data"])
        out = []
        for z in zones:
            item = {
                "name": z.get("ZoneName"),
                "zone_type": z.get("ZoneType"),
                "dynamic_update": z.get("DynamicUpdate"),
                "is_auto_created": bool(z.get("IsAutoCreated")),
                "is_reverse_lookup_zone": bool(z.get("IsReverseLookupZone")),
            }
            out.append(item)
        visible = [z for z in out if not z["is_auto_created"]]
        r["data"] = visible
        r["message"] = f"{len(visible)} zone(s) found ({len(out) - len(visible)} auto-created zone(s) hidden)."
    return r


def get_zone(zone_name: str) -> dict:
    try:
        zone_name = _validate_name(zone_name, "Zone name")
    except DnsValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_get_zone_script(zone_name))
    if r["success"]:
        r["message"] = f"Zone info retrieved for {zone_name}."
    return r


def list_records(zone_name: str, record_type: str = "") -> dict:
    try:
        zone_name = _validate_name(zone_name, "Zone name")
        rt = _validate_record_type(record_type) if record_type else ""
    except DnsValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_records_script(zone_name, rt))
    if r["success"]:
        records = _normalise_list(r["data"])
        r["data"] = records
        r["message"] = f"{len(records)} record(s) found in zone '{zone_name}'" + (f" (type {rt})" if rt else "") + "."
    return r


def add_record(zone_name: str, name: str, record_type: str, value: str, ttl_seconds=3600) -> dict:
    try:
        zone_name = _validate_name(zone_name, "Zone name")
        name = _validate_name(name, "Record name")
        rt = _validate_record_type(record_type)
        value = _validate_value(value, rt)
        ttl = _validate_ttl(ttl_seconds)
        if rt == "MX":
            _parse_mx_value(value)  # validate shape early for a clean error
        script = _build_add_record_script(zone_name, name, rt, value, ttl)
    except DnsValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"{rt} record '{name}' added to zone '{zone_name}' (TTL {ttl}s)."
    return r


def update_record(zone_name: str, name: str, record_type: str, old_value: str, new_value: str) -> dict:
    """Update an existing record's value. Implemented as clone + Set-DnsServerResourceRecord
    (Get old -> clone -> mutate clone -> Set with -OldInputObject/-NewInputObject), since the
    DnsServer module has no in-place 'change value' cmdlet."""
    try:
        zone_name = _validate_name(zone_name, "Zone name")
        name = _validate_name(name, "Record name")
        rt = _validate_record_type(record_type)
        old_value = _validate_value(old_value, rt)
        new_value = _validate_value(new_value, rt)
        script = _build_update_record_script(zone_name, name, rt, old_value, new_value)
    except DnsValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"{rt} record '{name}' in zone '{zone_name}' updated."
    return r


def remove_record(zone_name: str, name: str, record_type: str, value: str) -> dict:
    try:
        zone_name = _validate_name(zone_name, "Zone name")
        name = _validate_name(name, "Record name")
        rt = _validate_record_type(record_type)
        value = _validate_value(value, rt)
        script = _build_remove_record_script(zone_name, name, rt, value)
    except DnsValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"{rt} record '{name}' removed from zone '{zone_name}'."
    return r


def get_scavenging() -> dict:
    r = _run(_build_get_scavenging_script())
    if r["success"]:
        r["message"] = "Scavenging settings retrieved."
    return r


def set_scavenging(enabled_bool) -> dict:
    enabled = str(enabled_bool).strip().lower() in ("true", "1", "yes", "on")
    r = _run(_build_set_scavenging_script(enabled))
    if r["success"]:
        r["message"] = f"Scavenging {'enabled' if enabled else 'disabled'}."
    return r


# ---------------------------------------------------------------------------
# Action registry -- merged by agent.py. Every bridge module exposes ACTIONS
# mapping a flat action name to a callable taking one list of args.
# ---------------------------------------------------------------------------

ACTIONS = {
    "list_dns_zones":      lambda a: list_zones(),
    "get_dns_zone":        lambda a: get_zone(*a),
    "list_dns_records":    lambda a: list_records(*a),
    "add_dns_record":      lambda a: add_record(*a),
    "update_dns_record":   lambda a: update_record(*a),
    "remove_dns_record":   lambda a: remove_record(*a),
    "get_dns_scavenging":  lambda a: get_scavenging(),
    "set_dns_scavenging":  lambda a: set_scavenging(*a),
}
