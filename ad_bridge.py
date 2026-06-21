#!/usr/bin/env python3
"""
ad_bridge.py -- Active Directory Automation Bridge
Connects to a Windows Server via WinRM and executes AD operations.

All functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded.
"""

import os
import json
import time
import winrm
from dotenv import load_dotenv

load_dotenv()

_VM_IP      = os.getenv("AD_VM_IP")
_DOMAIN     = os.getenv("AD_DOMAIN")          # e.g. "lab" (without .local)
_ADMIN_USER = os.getenv("AD_ADMIN_USER", "Administrator")
_ADMIN_PASS = os.getenv("AD_ADMIN_PASS")

# HTTPS (port 5986) is the default. Set AD_WINRM_HTTP=1 in your env/config
# to fall back to plain HTTP — only acceptable inside a Tailscale tunnel or
# fully isolated LAN where you accept the risk of unencrypted credentials.
_USE_HTTPS  = os.getenv("AD_WINRM_HTTP", "0") != "1"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _winrm_target() -> str:
    return f"{_VM_IP}:{5986 if _USE_HTTPS else 5985}"


def _session() -> winrm.Session:
    if _USE_HTTPS:
        endpoint = f"https://{_VM_IP}:5986/wsman"
    else:
        endpoint = f"http://{_VM_IP}:5985/wsman"
    return winrm.Session(
        endpoint,
        auth=(f"{_DOMAIN}\\{_ADMIN_USER}", _ADMIN_PASS),
        transport="ntlm",
        server_cert_validation="ignore",   # accepts self-signed certs; traffic is still encrypted
        read_timeout_sec=20,               # fail a stalled read instead of hanging
        operation_timeout_sec=15,          # must be < read_timeout_sec (pywinrm rule)
    )


# ── WinRM connectivity circuit breaker ──────────────────────────────────────
# If the domain controller is unreachable, every command would otherwise hang
# for the full connection timeout. After a few consecutive connection failures
# we "open" the breaker and fail fast for a cooldown window, so a DC outage
# can't turn a queue of commands into a multi-minute pile-up. Only connection
# errors trip it — ordinary AD errors (user not found, etc.) do not.
_CB_FAIL_THRESHOLD = 3
_CB_COOLDOWN_SEC   = 60
_cb_failures   = 0
_cb_open_until = 0.0

_CONN_ERROR_HINTS = (
    "max retries", "connection", "timed out", "timeout", "refused",
    "no route", "unreachable", "failed to establish", "newconnectionerror",
)


def _is_connection_error(msg: str) -> bool:
    m = (msg or "").lower()
    return any(h in m for h in _CONN_ERROR_HINTS)


def _run(ps_script: str) -> dict:
    global _cb_failures, _cb_open_until
    now = time.monotonic()

    # Breaker open → fail fast without attempting a connection.
    if now < _cb_open_until:
        wait = int(_cb_open_until - now)
        return {
            "success": False, "data": None,
            "message": (f"Domain controller at {_winrm_target()} unreachable — skipping for "
                        f"{wait}s after repeated connection failures. Check the AD server is on "
                        f"and WinRM is listening (Test-NetConnection {_VM_IP} -Port "
                        f"{5986 if _USE_HTTPS else 5985})."),
        }

    try:
        result = _session().run_ps(ps_script)
        _cb_failures = 0   # any success closes the breaker
        if result.status_code != 0:
            err = result.std_err.decode("utf-8", errors="replace").strip()
            return {"success": False, "message": err, "data": None}
        raw = result.std_out.decode("utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else None
        return {"success": True, "message": "OK", "data": data}
    except Exception as e:
        msg = str(e)
        if _is_connection_error(msg):
            _cb_failures += 1
            if _cb_failures >= _CB_FAIL_THRESHOLD:
                _cb_open_until = now + _CB_COOLDOWN_SEC
            return {
                "success": False, "data": None,
                "message": (f"Cannot reach the domain controller at {_winrm_target()} over WinRM. "
                            f"Check the server is on and the port is open. ({msg})"),
            }
        # Non-connection failure (e.g. AD object not found) — do not trip the breaker.
        return {"success": False, "message": msg, "data": None}


def _ps_escape(value: str) -> str:
    return value.replace("'", "''")


def _normalise_list(data) -> list:
    """PowerShell returns a dict for single results, list for multiple. Always return list."""
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def _domain_dn() -> str:
    """Build the DC=... base DN from AD_DOMAIN env var.
    Handles both 'lab' (→ DC=lab,DC=local) and 'lab.local' formats.
    """
    domain = _DOMAIN.lower() if _DOMAIN else "domain.local"
    if "." not in domain:
        domain = domain + ".local"
    return ",".join(f"DC={part}" for part in domain.split("."))


def _make_ou_path(ou_name: str) -> str:
    """Convert a simple OU name to a full Distinguished Name.
    Accepts either a plain name ('Staff') or a full DN ('OU=Staff,DC=lab,DC=local').
    """
    if not ou_name:
        return ""
    ou_name = ou_name.strip()
    if "=" in ou_name:
        return ou_name           # already a full DN
    return f"OU={ou_name},{_domain_dn()}"


# ---------------------------------------------------------------------------
# Query operations
# ---------------------------------------------------------------------------

def get_user_info(username: str) -> dict:
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Get-ADUser -Identity '{u}' -Properties * |
  Select-Object Name, SamAccountName, UserPrincipalName, EmailAddress,
                Enabled, LockedOut, PasswordExpired, PasswordLastSet,
                LastLogonDate, DistinguishedName, Department, Title, MemberOf |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"User info retrieved for {username}."
    return r


def list_users(ou: str = "") -> dict:
    """List all AD users, optionally filtered by OU (plain name or full DN)."""
    ou_path  = _make_ou_path(ou)
    ou_param = f'-SearchBase "{_ps_escape(ou_path)}"' if ou_path else ""
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter * {ou_param} -Properties LockedOut, PasswordExpired, LastLogonDate, MemberOf, Department, Title |
  Select-Object Name, SamAccountName, GivenName, Surname, Enabled, LockedOut, PasswordExpired,
                LastLogonDate, Department, Title,
                @{{Name='OU';Expression={{($_.DistinguishedName -split ',OU=')[1]}}}},
                @{{Name='GroupCount';Expression={{$_.MemberOf.Count}}}} |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} user(s) found."
    return r


def search_users(query: str) -> dict:
    """Search users by name or SAM account name."""
    q = _ps_escape(query)
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter {{(Name -like '*{q}*') -or (SamAccountName -like '*{q}*')}} `
  -Properties LockedOut, PasswordExpired, LastLogonDate, Department, Title |
  Select-Object Name, SamAccountName, GivenName, Surname, Enabled, LockedOut,
                PasswordExpired, LastLogonDate, Department, Title,
                @{{Name='OU';Expression={{($_.DistinguishedName -split ',OU=')[1]}}}} |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} user(s) matched '{query}'."
    return r


def list_locked_accounts() -> dict:
    """Return all currently locked-out AD accounts."""
    script = """
Import-Module ActiveDirectory
Search-ADAccount -LockedOut |
  Select-Object Name, SamAccountName, LastLogonDate,
                @{Name='OU';Expression={($_.DistinguishedName -split ',OU=')[1]}} |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} locked account(s)."
    return r


def list_expired_passwords() -> dict:
    """Return all accounts with expired passwords or must-change-at-logon flag.
    Uses Get-ADUser + Where-Object instead of Search-ADAccount so it also
    catches accounts where ChangePasswordAtLogon=true but domain max age=0.
    """
    script = """
Import-Module ActiveDirectory
Get-ADUser -Filter {Enabled -eq $true} -Properties PasswordExpired, PasswordLastSet, LastLogonDate |
  Where-Object { $_.PasswordExpired -eq $true } |
  Select-Object Name, SamAccountName, PasswordLastSet, LastLogonDate,
                @{Name='OU';Expression={($_.DistinguishedName -split ',OU=')[1]}} |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} account(s) with expired passwords."
    return r


def get_stats() -> dict:
    """Return summary counts for the dashboard header."""
    script = """
Import-Module ActiveDirectory
$all     = (Get-ADUser -Filter *).Count
$enabled = (Get-ADUser -Filter {Enabled -eq $true}).Count
$locked  = (Search-ADAccount -LockedOut).Count
# Use Where-Object on PasswordExpired so we catch both policy-expired accounts
# AND accounts with ChangePasswordAtLogon=true (Search-ADAccount misses these
# when domain max password age is 0 / never expires)
$expired = (Get-ADUser -Filter {Enabled -eq $true} -Properties PasswordExpired |
            Where-Object {$_.PasswordExpired -eq $true}).Count
@{total=$all; enabled=$enabled; locked=$locked; expired=$expired} | ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["message"] = "Stats retrieved."
    return r


# ---------------------------------------------------------------------------
# Write operations
# ---------------------------------------------------------------------------

def reset_password(username: str, temp_password: str) -> dict:
    u = _ps_escape(username)
    p = _ps_escape(temp_password)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$secure = ConvertTo-SecureString '{p}' -AsPlainText -Force
Set-ADAccountPassword -Identity '{u}' -NewPassword $secure -Reset
Set-ADUser -Identity '{u}' -ChangePasswordAtLogon $true
Write-Output '{{"result":"password_reset","user":"{u}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"Password reset for {username}. User must change on next logon."
    return r


def unlock_account(username: str) -> dict:
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Unlock-ADAccount -Identity '{u}'
Write-Output '{{"result":"unlocked","user":"{u}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"Account unlocked for {username}."
    return r


def disable_account(username: str) -> dict:
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Disable-ADAccount -Identity '{u}'
Write-Output '{{"result":"disabled","user":"{u}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"Account disabled for {username}."
    return r


def enable_account(username: str) -> dict:
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Enable-ADAccount -Identity '{u}'
Write-Output '{{"result":"enabled","user":"{u}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"Account enabled for {username}."
    return r


def add_to_group(username: str, group: str) -> dict:
    u = _ps_escape(username)
    g = _ps_escape(group)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Add-ADGroupMember -Identity '{g}' -Members '{u}'
Write-Output '{{"result":"added","user":"{u}","group":"{g}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"{username} added to group {group}."
    return r


def remove_from_group(username: str, group: str) -> dict:
    u = _ps_escape(username)
    g = _ps_escape(group)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Remove-ADGroupMember -Identity '{g}' -Members '{u}' -Confirm:$false
Write-Output '{{"result":"removed","user":"{u}","group":"{g}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"{username} removed from group {group}."
    return r


def create_user(first: str, last: str, username: str, ou: str = "") -> dict:
    f_ = _ps_escape(first)
    l_ = _ps_escape(last)
    u  = _ps_escape(username)
    ou_path  = _make_ou_path(ou)
    ou_param = f"-Path '{_ps_escape(ou_path)}'" if ou_path else ""
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$secure = ConvertTo-SecureString 'ChangeMe123!' -AsPlainText -Force
New-ADUser `
  -GivenName '{f_}' `
  -Surname '{l_}' `
  -Name '{f_} {l_}' `
  -SamAccountName '{u}' `
  -UserPrincipalName '{u}@{_DOMAIN.lower()}.local' `
  -AccountPassword $secure `
  -Enabled $true `
  -ChangePasswordAtLogon $true `
  {ou_param}
Write-Output '{{"result":"created","user":"{u}","name":"{f_} {l_}","ou":"{_ps_escape(ou_path)}"}}'
"""
    r = _run(script)
    if r["success"]:
        ou_note = f" in OU: {ou_path}" if ou_path else ""
        r["message"] = (
            f"User {first} {last} ({username}) created{ou_note}. "
            "Default password: ChangeMe123! — user must change on first logon."
        )
    return r


def list_groups() -> dict:
    """List all AD security and distribution groups."""
    script = """
Import-Module ActiveDirectory
Get-ADGroup -Filter * -Properties Name, GroupCategory, GroupScope |
  Select-Object Name, SamAccountName, GroupCategory, GroupScope |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} group(s) found."
    return r


def search_groups(query: str) -> dict:
    """Search AD groups by partial name."""
    q = _ps_escape(query)
    script = f"""
Import-Module ActiveDirectory
Get-ADGroup -Filter {{Name -like '*{q}*'}} -Properties Name, GroupCategory, GroupScope |
  Select-Object Name, SamAccountName, GroupCategory, GroupScope |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} group(s) matching '{query}'."
    return r


def get_group_members(group: str) -> dict:
    """List all members of an AD group."""
    g = _ps_escape(group)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Get-ADGroupMember -Identity '{g}' |
  Select-Object Name, SamAccountName, objectClass |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} member(s) in '{group}'."
    return r


def list_group_memberships(username: str) -> dict:
    """List all AD groups a specific user belongs to."""
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$user = Get-ADUser -Identity '{u}' -Properties MemberOf
if ($user.MemberOf) {{
    $user.MemberOf | ForEach-Object {{
        try {{
            $g = Get-ADGroup -Identity $_
            [PSCustomObject]@{{Name=$g.Name; SamAccountName=$g.SamAccountName; GroupScope=$g.GroupScope}}
        }} catch {{ }}
    }} | Sort-Object Name | ConvertTo-Json
}} else {{
    Write-Output '[]'
}}
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{username} belongs to {len(r['data'])} group(s)."
    return r


def force_password_change(username: str) -> dict:
    """Force a user to change their password at next logon."""
    u = _ps_escape(username)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Set-ADUser -Identity '{u}' -ChangePasswordAtLogon $true
Write-Output '{{"result":"ok","user":"{u}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"{username} will be prompted to change password on next logon."
    return r


def set_password_never_expires(username: str, never_expires: str = "true") -> dict:
    """Toggle the 'password never expires' flag. Pass 'true' or 'false'."""
    u    = _ps_escape(username)
    flag = "$true" if str(never_expires).lower() in ("true", "1", "yes") else "$false"
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
Set-ADUser -Identity '{u}' -PasswordNeverExpires {flag}
Write-Output '{{"result":"ok","user":"{u}","flag":"{flag}"}}'
"""
    r = _run(script)
    if r["success"]:
        state = "never expires" if flag == "$true" else "expires per domain policy"
        r["message"] = f"Password for {username} set to: {state}."
    return r


def list_ous() -> dict:
    """List all Organisational Units in the domain."""
    script = """
Import-Module ActiveDirectory
Get-ADOrganizationalUnit -Filter * -Properties Name, DistinguishedName |
  Select-Object Name, DistinguishedName |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} OU(s) found."
    return r


def move_user(username: str, ou_name: str) -> dict:
    """Move a user to a different OU. ou_name can be a plain name or full DN."""
    u       = _ps_escape(username)
    ou_path = _make_ou_path(ou_name)
    ou_esc  = _ps_escape(ou_path)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$user = Get-ADUser -Identity '{u}'
Move-ADObject -Identity $user.DistinguishedName -TargetPath '{ou_esc}'
Write-Output '{{"result":"moved","user":"{u}","ou":"{ou_esc}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"{username} moved to {ou_path}."
    return r


# ---------------------------------------------------------------------------
# Custom script execution
# ---------------------------------------------------------------------------

def run_custom_script(ps_content: str, *user_args) -> dict:
    """Execute a tenant-uploaded PowerShell script.

    The script may reference positional arguments as $ARGS[0], $ARGS[1], etc.
    These are substituted with the escaped values of user_args before execution.
    The script runs in the same WinRM session as all other AD operations.
    """
    script = ps_content
    for i, arg in enumerate(user_args):
        script = script.replace(f"$ARGS[{i}]", _ps_escape(str(arg)))
    return _run(script)


# ---------------------------------------------------------------------------
# OU management
# ---------------------------------------------------------------------------

def create_ou(ou_name: str, parent_ou: str = "") -> dict:
    """Create a new Organisational Unit. parent_ou can be a plain name or full DN.
    If parent_ou is omitted, creates at the domain root.
    Idempotent: if the OU already exists it returns success without error.
    """
    n = _ps_escape(ou_name)
    parent_path = _make_ou_path(parent_ou) if parent_ou else _domain_dn()
    p = _ps_escape(parent_path)
    script = f"""
$ErrorActionPreference = 'Stop'
Import-Module ActiveDirectory
$existing = Get-ADOrganizationalUnit -Filter {{Name -eq '{n}'}} `
            -SearchBase '{p}' -SearchScope OneLevel -ErrorAction SilentlyContinue
if ($existing) {{
    Write-Output '{{"result":"already_exists","ou":"{n}","path":"{p}"}}'
}} else {{
    New-ADOrganizationalUnit -Name '{n}' -Path '{p}'
    Write-Output '{{"result":"created","ou":"{n}","path":"{p}"}}'
}}
"""
    r = _run(script)
    if r["success"]:
        data = r.get("data") or {}
        if isinstance(data, dict) and data.get("result") == "already_exists":
            r["message"] = f"OU '{ou_name}' already exists under {parent_path}."
        else:
            r["message"] = f"OU '{ou_name}' created under {parent_path}."
    return r


def list_users_in_ou(ou_name: str) -> dict:
    """List all users in a specific OU (searches subtree). ou_name can be plain name or full DN."""
    ou_path = _make_ou_path(ou_name)
    ou_esc  = _ps_escape(ou_path)
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter * -SearchBase '{ou_esc}' -SearchScope Subtree `
  -Properties LockedOut, PasswordExpired, LastLogonDate, Department, Title |
  Select-Object Name, SamAccountName, GivenName, Surname, Enabled, LockedOut,
                PasswordExpired, LastLogonDate, Department, Title,
                @{{Name='OU';Expression={{($_.DistinguishedName -split ',OU=')[1]}}}} |
  Sort-Object Name |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"]    = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} user(s) in OU '{ou_name}'."
    return r


def bulk_move_users(source_ou: str, rules_json: str, create_missing_ous: str = "true",
                    max_users: str = "") -> dict:
    """Move users in source_ou to target OUs based on SamAccountName pattern rules.

    rules_json : JSON array of {"pattern": "*08", "target_ou": "Y12"} objects.
                 Pattern uses PowerShell -like wildcards (* = any chars).
    create_missing_ous : "true" (default) auto-creates target OUs that don't exist.
    max_users : optional internal quota guard from the cloud. If supplied, the
                script counts matched users before moving anything and blocks
                the whole batch if it would exceed this number.

    Returns counts moved per rule and any per-user errors.
    """
    import json as _json
    try:
        rules = _json.loads(rules_json)
    except Exception:
        return {"success": False, "message": "Invalid rules_json — must be a JSON array.", "data": None}

    if not rules:
        return {"success": False, "message": "No rules provided.", "data": None}

    source_path = _make_ou_path(source_ou)
    src = _ps_escape(source_path)
    do_create   = str(create_missing_ous).lower() in ("true", "1", "yes")
    max_users_raw = str(max_users).strip()
    try:
        max_users_int = int(max_users_raw) if max_users_raw else -1
    except ValueError:
        max_users_int = -1

    # Build PowerShell rules array
    ps_rule_lines = []
    for rule in rules:
        pattern    = _ps_escape(str(rule.get("pattern", "")))
        target_raw = str(rule.get("target_ou", ""))
        target_dn  = _make_ou_path(target_raw) if target_raw else ""
        target_esc = _ps_escape(target_dn)
        ps_rule_lines.append(f"    @{{Pattern='{pattern}'; TargetOU='{target_esc}'}}")

    ps_rules_str   = ",\n".join(ps_rule_lines)

    # Optional OU creation block — injected per rule if create_missing_ous is true
    create_ou_block = """
        # Auto-create target OU if missing
        $ouLeaf   = ($rule.TargetOU -split ',')[0] -replace '^OU=',''
        $ouParent = $rule.TargetOU -replace '^OU=[^,]+,',''
        $exists   = Get-ADOrganizationalUnit -Filter {Name -eq $ouLeaf} `
                    -SearchBase $ouParent -SearchScope OneLevel -ErrorAction SilentlyContinue
        if (-not $exists) {
            New-ADOrganizationalUnit -Name $ouLeaf -Path $ouParent -ErrorAction SilentlyContinue
        }
""" if do_create else ""

    script = f"""
$ErrorActionPreference = 'Continue'
Import-Module ActiveDirectory

$rules = @(
{ps_rules_str}
)

$maxUsers = {max_users_int}
$users   = Get-ADUser -Filter * -SearchBase '{src}' -SearchScope Subtree
$moved   = @{{}}
$errList = @()

if ($maxUsers -ge 0) {{
    $planned = 0
    foreach ($rule in $rules) {{
        $planned += @($users | Where-Object {{ $_.SamAccountName -like $rule.Pattern }}).Count
    }}
    if ($planned -gt $maxUsers) {{
        throw "Bulk move blocked by monthly quota: $planned matched user(s), but this tenant has quota for $maxUsers user(s) in this batch."
    }}
}}

foreach ($rule in $rules) {{
{create_ou_block}
    $matched = $users | Where-Object {{ $_.SamAccountName -like $rule.Pattern }}
    $count   = 0
    foreach ($u in $matched) {{
        try {{
            Move-ADObject -Identity $u.DistinguishedName -TargetPath $rule.TargetOU
            $count++
        }} catch {{
            $errList += "$($u.SamAccountName): $_"
        }}
    }}
    $moved[$rule.Pattern] = $count
}}

$total = 0
foreach ($v in $moved.Values) {{ $total += $v }}

@{{
    moved  = $moved
    errors = $errList
    total  = $total
}} | ConvertTo-Json -Depth 4
"""
    r = _run(script)
    if r["success"] and r.get("data"):
        data  = r["data"]
        total = data.get("total", 0)
        errs  = data.get("errors", [])
        err_note = f" ({len(errs)} error(s) — check data.errors for details)" if errs else ""
        r["message"] = f"Bulk move complete: {total} user(s) moved across {len(rules)} rule(s){err_note}."
    return r
