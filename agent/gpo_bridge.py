#!/usr/bin/env python3
"""
gpo_bridge.py -- Windows Group Policy Automation Bridge
Connects to a Windows Server via WinRM and executes Group Policy operations.

All public functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded (see winrm_core.py).

Every action follows a script-builder / executor split so the PowerShell can
be unit-tested offline without a live domain controller:
    _build_<name>_script(...) -> str   # pure, no I/O
    <name>(...)                        # calls _run(_build_<name>_script(...))

Overnight scope note: reads (3.1) plus link/unlink/status/enforcement writes
(3.2) only. No setting-level GPO editing -- that is a future project. All
write actions here are destined for the human-confirm token tier (enforced
cloud-side); this module only provides clean, validated implementations.

GPOReport parsing note: Get-GPOReport -ReportType Xml returns a large,
schema-rich XML document. We parse it in Python with xml.etree.ElementTree
(defusedxml is not available in this environment) rather than in PowerShell,
since ElementTree's XPath support makes pulling a compact summary much
simpler than doing it in PS. The XML originates from our own domain
controller (trusted source), so we accept ElementTree's lack of hardening
against maliciously crafted XML (entity expansion, etc.) as an acceptable
risk here. The parser is defensive: any failure returns a friendly fallback
instead of a traceback.
"""

import re
import xml.etree.ElementTree as ET

from winrm_core import (
    _run,
    _ps_escape,
    _normalise_list,
    _make_ou_path,
    role_installed_check,
)

CAPABILITY = "gpo"

# ---------------------------------------------------------------------------
# Validation (Python-side, BEFORE any PowerShell is built)
# ---------------------------------------------------------------------------

# GPO display names commonly contain spaces, e.g. "Default Domain Policy".
_GPO_NAME_RE = re.compile(r"^[A-Za-z0-9 .\-_()]+$")

_GUID_RE = re.compile(
    r"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$"
)

_ALLOWED_GPO_STATUSES = {
    "AllSettingsEnabled",
    "AllSettingsDisabled",
    "ComputerSettingsDisabled",
    "UserSettingsDisabled",
}


class GpoValidationError(ValueError):
    """Raised when user-supplied input fails validation before any PS is built."""


def _validate_gpo_identifier(value: str) -> tuple:
    """Validate a GPO identifier (display name or GUID). Returns (kind, value)
    where kind is 'guid' or 'name'."""
    if not value or not isinstance(value, str):
        raise GpoValidationError("GPO name or GUID is required.")
    value = value.strip()
    if not value:
        raise GpoValidationError("GPO name or GUID is required.")
    if _GUID_RE.match(value):
        return "guid", value
    if not _GPO_NAME_RE.match(value):
        raise GpoValidationError(
            f"GPO name '{value}' contains characters that are not allowed. "
            "Only letters, digits, spaces, dots, hyphens, underscores, and parentheses are permitted."
        )
    if len(value) > 255:
        raise GpoValidationError("GPO name is too long (max 255 characters).")
    return "name", value


def _validate_ou(ou_dn_or_name: str) -> str:
    """Accepts either a plain OU name or a full DN. Reuses winrm_core's
    _make_ou_path so behaviour matches ad_bridge's OU handling exactly."""
    if not ou_dn_or_name or not isinstance(ou_dn_or_name, str):
        raise GpoValidationError("OU name or distinguished name is required.")
    ou_dn_or_name = ou_dn_or_name.strip()
    if not ou_dn_or_name:
        raise GpoValidationError("OU name or distinguished name is required.")
    if any(ord(c) < 32 for c in ou_dn_or_name):
        raise GpoValidationError("OU identifier contains control characters, which is not allowed.")
    if len(ou_dn_or_name) > 500:
        raise GpoValidationError("OU identifier is too long (max 500 characters).")
    return _make_ou_path(ou_dn_or_name)


def _validate_gpo_status(status: str) -> str:
    s = (status or "").strip()
    if s not in _ALLOWED_GPO_STATUSES:
        raise GpoValidationError(
            f"GPO status '{status}' is not supported. "
            f"Allowed values: {', '.join(sorted(_ALLOWED_GPO_STATUSES))}."
        )
    return s


def _validate_enforced(enforced_bool) -> bool:
    if isinstance(enforced_bool, bool):
        return enforced_bool
    s = str(enforced_bool).strip().lower()
    if s in ("true", "1", "yes", "on"):
        return True
    if s in ("false", "0", "no", "off"):
        return False
    raise GpoValidationError("Enforced must be a boolean (true/false).")


# ---------------------------------------------------------------------------
# Script builders (pure -- no I/O, safe to call and print offline)
# ---------------------------------------------------------------------------

def _preamble() -> str:
    return role_installed_check("GroupPolicy", "Group Policy Management")


def _gpo_param(kind: str, value: str) -> str:
    """Returns the -Guid or -Name PowerShell parameter fragment for Get-GPO/-GPLink calls."""
    if kind == "guid":
        return f"-Guid '{_ps_escape(value)}'"
    return f"-Name '{_ps_escape(value)}'"


def _build_list_gpos_script() -> str:
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-GPO -All |
  Select-Object DisplayName, @{{N='Id';E={{$_.Id.ToString()}}}}, GpoStatus, CreationTime, ModificationTime |
  ConvertTo-Json
"""


def _build_get_gpo_script(kind: str, value: str) -> str:
    p = _gpo_param(kind, value)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-GPO {p} |
  Select-Object DisplayName, @{{N='Id';E={{$_.Id.ToString()}}}}, GpoStatus, Description,
                CreationTime, ModificationTime, Owner, DomainName, WmiFilter |
  ConvertTo-Json
"""


def _build_get_gpo_report_script(kind: str, value: str) -> str:
    p = _gpo_param(kind, value)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Get-GPOReport {p} -ReportType Xml
"""


def _build_list_gpo_links_script(ou_dn: str) -> str:
    o = _ps_escape(ou_dn)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$inheritance = Get-GPInheritance -Target '{o}'
$inheritance.GpoLinks |
  Select-Object DisplayName, @{{N='GpoId';E={{$_.GpoId.ToString()}}}}, Order, Enabled, Enforced, Target |
  ConvertTo-Json
"""


def _build_get_gpo_inheritance_script(ou_dn: str) -> str:
    o = _ps_escape(ou_dn)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$inheritance = Get-GPInheritance -Target '{o}'
[PSCustomObject]@{{
    Target             = $inheritance.Target
    GpoInheritanceBlocked = $inheritance.GpoInheritanceBlocked
    InheritedGpoLinks  = $inheritance.InheritedGpoLinks |
        Select-Object DisplayName, @{{N='GpoId';E={{$_.GpoId.ToString()}}}}, Order, Enabled, Enforced, Target
    GpoLinks           = $inheritance.GpoLinks |
        Select-Object DisplayName, @{{N='GpoId';E={{$_.GpoId.ToString()}}}}, Order, Enabled, Enforced, Target
}} | ConvertTo-Json -Depth 6
"""


def _build_link_gpo_script(kind: str, value: str, ou_dn: str) -> str:
    p = _gpo_param(kind, value)
    o = _ps_escape(ou_dn)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
New-GPLink {p} -Target '{o}'
Write-Output '{{"result":"linked","ou":"{o}"}}'
"""


def _build_unlink_gpo_script(kind: str, value: str, ou_dn: str) -> str:
    p = _gpo_param(kind, value)
    o = _ps_escape(ou_dn)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Remove-GPLink {p} -Target '{o}'
Write-Output '{{"result":"unlinked","ou":"{o}"}}'
"""


def _build_set_gpo_status_script(kind: str, value: str, status: str) -> str:
    p = _gpo_param(kind, value)
    s = _ps_escape(status)
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$gpo = Get-GPO {p}
$gpo.GpoStatus = '{s}'
Write-Output '{{"result":"status_set","status":"{s}"}}'
"""


def _build_set_link_enforced_script(kind: str, value: str, ou_dn: str, enforced: bool) -> str:
    p = _gpo_param(kind, value)
    o = _ps_escape(ou_dn)
    flag = "Yes" if enforced else "No"
    return f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
Set-GPLink {p} -Target '{o}' -Enforced {flag}
Write-Output '{{"result":"enforced_set","ou":"{o}","enforced":"{flag}"}}'
"""


# ---------------------------------------------------------------------------
# GPOReport XML parsing (pure Python, defensive -- never raises)
# ---------------------------------------------------------------------------

def _strip_ns(tag: str) -> str:
    """Strip an XML namespace prefix like '{uri}Local' -> 'Local'."""
    if "}" in tag:
        return tag.split("}", 1)[1]
    return tag


def _find_local(elem, local_name):
    """Namespace-agnostic find of the first direct/descendant child matching a local tag name."""
    for child in elem.iter():
        if _strip_ns(child.tag) == local_name:
            return child
    return None


def _findall_local(elem, local_name):
    return [child for child in elem.iter() if _strip_ns(child.tag) == local_name]


def _summarise_config_section(section_elem):
    """Summarise a <Computer> or <User> GPO report section: enabled flag +
    extension names with a rough setting count each. Defensive throughout."""
    if section_elem is None:
        return {"enabled": None, "extensions": []}

    enabled = None
    enabled_elem = _find_local(section_elem, "Enabled")
    if enabled_elem is not None and enabled_elem.text is not None:
        enabled = enabled_elem.text.strip().lower() in ("true", "1")

    extensions = []
    ext_data = _find_local(section_elem, "ExtensionData")
    ext_elems = _findall_local(ext_data, "Extension") if ext_data is not None else []
    for ext in ext_elems:
        # The extension's friendly name is usually on a Name attribute.
        name = ext.attrib.get("Name") or ext.attrib.get("{http://www.w3.org/2001/XMLSchema-instance}type") or "Unknown"
        # Rough setting count: number of immediate child elements under this extension.
        try:
            count = len(list(ext))
        except Exception:
            count = 0
        extensions.append({"name": name, "setting_count": count})

    return {"enabled": enabled, "extensions": extensions}


def _parse_gpo_report_xml(xml_text: str) -> dict:
    """Parse a Get-GPOReport -ReportType Xml document into a compact summary.
    Never raises: any failure returns a friendly fallback dict."""
    if not xml_text or not isinstance(xml_text, str) or not xml_text.strip():
        return {"raw_available": False, "summary": "Report could not be parsed"}

    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return {"raw_available": False, "summary": "Report could not be parsed"}

    try:
        name_elem = _find_local(root, "Name")
        name = name_elem.text.strip() if (name_elem is not None and name_elem.text) else None

        computer_elem = _find_local(root, "Computer")
        user_elem = _find_local(root, "User")

        summary = {
            "raw_available": True,
            "name": name,
            "computer": _summarise_config_section(computer_elem),
            "user": _summarise_config_section(user_elem),
        }
        return summary
    except Exception:
        # Defensive catch-all: never let a report-parsing edge case surface a traceback.
        return {"raw_available": False, "summary": "Report could not be parsed"}


# ---------------------------------------------------------------------------
# Public functions (validate -> build -> run)
# ---------------------------------------------------------------------------

def list_gpos() -> dict:
    r = _run(_build_list_gpos_script())
    if r["success"]:
        gpos = _normalise_list(r["data"])
        out = []
        for g in gpos:
            out.append({
                "name": g.get("DisplayName"),
                "id": g.get("Id"),
                "status": g.get("GpoStatus"),
                "created": g.get("CreationTime"),
                "modified": g.get("ModificationTime"),
            })
        r["data"] = out
        r["message"] = f"{len(out)} GPO(s) found."
    return r


def get_gpo(name_or_guid: str) -> dict:
    try:
        kind, value = _validate_gpo_identifier(name_or_guid)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_get_gpo_script(kind, value))
    if r["success"]:
        r["message"] = f"GPO info retrieved for '{value}'."
    return r


def get_gpo_report(name_or_guid: str) -> dict:
    try:
        kind, value = _validate_gpo_identifier(name_or_guid)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    # Get-GPOReport returns raw XML text, not JSON -- run it via the generic
    # PS runner but bypass _run's ConvertTo-Json/json.loads expectation by
    # wrapping the XML in a JSON string on the PowerShell side instead.
    script = f"""
{_preamble()}
$ErrorActionPreference = 'Stop'
$xml = Get-GPOReport {_gpo_param(kind, value)} -ReportType Xml
[PSCustomObject]@{{ Xml = $xml }} | ConvertTo-Json
"""
    r = _run(script)
    if not r["success"]:
        return r
    xml_text = None
    if isinstance(r["data"], dict):
        xml_text = r["data"].get("Xml")
    summary = _parse_gpo_report_xml(xml_text)
    r["data"] = summary
    r["message"] = f"GPO report retrieved for '{value}'." if summary.get("raw_available") else \
        f"GPO report retrieved for '{value}' but could not be parsed into a summary."
    return r


def list_gpo_links(ou_dn_or_name: str) -> dict:
    try:
        ou_dn = _validate_ou(ou_dn_or_name)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_list_gpo_links_script(ou_dn))
    if r["success"]:
        links = _normalise_list(r["data"])
        out = []
        for l in links:
            out.append({
                "name": l.get("DisplayName"),
                "gpo_id": l.get("GpoId"),
                "order": l.get("Order"),
                "enabled": bool(l.get("Enabled")),
                "enforced": bool(l.get("Enforced")),
                "target": l.get("Target"),
            })
        r["data"] = out
        r["message"] = f"{len(out)} GPO link(s) found on '{ou_dn}'."
    return r


def get_gpo_inheritance(ou_dn_or_name: str) -> dict:
    try:
        ou_dn = _validate_ou(ou_dn_or_name)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(_build_get_gpo_inheritance_script(ou_dn))
    if r["success"]:
        r["message"] = f"GPO inheritance retrieved for '{ou_dn}'."
    return r


def link_gpo(gpo_name: str, ou_dn: str) -> dict:
    try:
        kind, value = _validate_gpo_identifier(gpo_name)
        ou = _validate_ou(ou_dn)
        script = _build_link_gpo_script(kind, value, ou)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"GPO '{value}' linked to '{ou}'."
    return r


def unlink_gpo(gpo_name: str, ou_dn: str) -> dict:
    try:
        kind, value = _validate_gpo_identifier(gpo_name)
        ou = _validate_ou(ou_dn)
        script = _build_unlink_gpo_script(kind, value, ou)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"GPO '{value}' unlinked from '{ou}'."
    return r


def set_gpo_status(gpo_name: str, status: str) -> dict:
    try:
        kind, value = _validate_gpo_identifier(gpo_name)
        s = _validate_gpo_status(status)
        script = _build_set_gpo_status_script(kind, value, s)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"GPO '{value}' status set to {s}."
    return r


def set_link_enforced(gpo_name: str, ou_dn: str, enforced_bool) -> dict:
    try:
        kind, value = _validate_gpo_identifier(gpo_name)
        ou = _validate_ou(ou_dn)
        enforced = _validate_enforced(enforced_bool)
        script = _build_set_link_enforced_script(kind, value, ou, enforced)
    except GpoValidationError as e:
        return {"success": False, "message": str(e), "data": None}
    r = _run(script)
    if r["success"]:
        r["message"] = f"GPO '{value}' link on '{ou}' enforcement set to {enforced}."
    return r


# ---------------------------------------------------------------------------
# Action registry -- merged by agent.py. Every bridge module exposes ACTIONS
# mapping a flat action name to a callable taking one list of args.
# ---------------------------------------------------------------------------

ACTIONS = {
    "list_gpos":              lambda a: list_gpos(),
    "get_gpo":                lambda a: get_gpo(*a),
    "get_gpo_report":         lambda a: get_gpo_report(*a),
    "list_gpo_links":         lambda a: list_gpo_links(*a),
    "get_gpo_inheritance":    lambda a: get_gpo_inheritance(*a),
    "link_gpo":                lambda a: link_gpo(*a),
    "unlink_gpo":              lambda a: unlink_gpo(*a),
    "set_gpo_status":          lambda a: set_gpo_status(*a),
    "set_gpo_link_enforced":   lambda a: set_link_enforced(*a),
}
