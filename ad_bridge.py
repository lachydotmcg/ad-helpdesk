#!/usr/bin/env python3
"""
ad_bridge.py -- Active Directory Automation Bridge
Connects to a Windows Server 2022 VM via WinRM and executes AD operations.

All functions return: {"success": bool, "message": str, "data": dict | list | None}
Credentials are loaded from .env -- never hardcoded.
"""

import os
import json
import winrm
from dotenv import load_dotenv

load_dotenv()

_VM_IP      = os.getenv("AD_VM_IP")
_DOMAIN     = os.getenv("AD_DOMAIN")
_ADMIN_USER = os.getenv("AD_ADMIN_USER", "Administrator")
_ADMIN_PASS = os.getenv("AD_ADMIN_PASS")

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _session() -> winrm.Session:
    return winrm.Session(
        f"http://{_VM_IP}:5985/wsman",
        auth=(f"{_DOMAIN}\\{_ADMIN_USER}", _ADMIN_PASS),
        transport="ntlm",
        server_cert_validation="ignore",
    )


def _run(ps_script: str) -> dict:
    try:
        result = _session().run_ps(ps_script)
        if result.status_code != 0:
            err = result.std_err.decode("utf-8", errors="replace").strip()
            return {"success": False, "message": err, "data": None}
        raw = result.std_out.decode("utf-8", errors="replace").strip()
        data = json.loads(raw) if raw else None
        return {"success": True, "message": "OK", "data": data}
    except Exception as e:
        return {"success": False, "message": str(e), "data": None}


def _ps_escape(value: str) -> str:
    return value.replace("'", "''")


def _normalise_list(data) -> list:
    """PS returns a dict for single results, list for multiple. Always return list."""
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


# ---------------------------------------------------------------------------
# Query operations
# ---------------------------------------------------------------------------

def get_user_info(username: str) -> dict:
    u = _ps_escape(username)
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Identity '{u}' -Properties * |
  Select-Object Name, SamAccountName, UserPrincipalName, EmailAddress,
                Enabled, LockedOut, PasswordExpired, PasswordLastSet,
                LastLogonDate, DistinguishedName, MemberOf |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"User info retrieved for {username}."
    return r


def list_users(ou: str = "") -> dict:
    """
    List all AD users, optionally filtered by OU.
    ou: SearchBase DN, e.g. "OU=Staff,DC=lab,DC=local"
    """
    ou_param = f'-SearchBase "{_ps_escape(ou)}"' if ou else ""
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter * {ou_param} -Properties LockedOut, PasswordExpired, LastLogonDate, MemberOf |
  Select-Object Name, SamAccountName, Enabled, LockedOut, PasswordExpired, LastLogonDate,
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
    """
    Search users by name or SAM account name.
    """
    q = _ps_escape(query)
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter {{(Name -like '*{q}*') -or (SamAccountName -like '*{q}*')}} `
  -Properties LockedOut, PasswordExpired, LastLogonDate |
  Select-Object Name, SamAccountName, Enabled, LockedOut, PasswordExpired, LastLogonDate,
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
    """
    Return all currently locked-out AD accounts.
    """
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
    """
    Return all accounts with expired passwords.
    """
    script = """
Import-Module ActiveDirectory
Search-ADAccount -PasswordExpired -UsersOnly |
  Select-Object Name, SamAccountName, LastLogonDate,
                @{Name='OU';Expression={($_.DistinguishedName -split ',OU=')[1]}} |
  ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["data"] = _normalise_list(r["data"])
        r["message"] = f"{len(r['data'])} expired password(s)."
    return r


def get_stats() -> dict:
    """
    Return a summary of AD stats for the dashboard header.
    """
    script = """
Import-Module ActiveDirectory
$all     = (Get-ADUser -Filter *).Count
$enabled = (Get-ADUser -Filter {Enabled -eq $true}).Count
$locked  = (Search-ADAccount -LockedOut).Count
$expired = (Search-ADAccount -PasswordExpired -UsersOnly).Count
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
Import-Module ActiveDirectory
Remove-ADGroupMember -Identity '{g}' -Members '{u}' -Confirm:$false
Write-Output '{{"result":"removed","user":"{u}","group":"{g}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = f"{username} removed from group {group}."
    return r


def _normalise_list(data) -> list:
    """PS returns a dict (not a list) when there is only one result. Normalise."""
    if data is None:
        return []
    return data if isinstance(data, list) else [data]


def list_users(ou: str = "") -> dict:
    """
    List all AD users, optionally filtered by OU.
    ou: SearchBase DN, e.g. "OU=Staff,DC=lab,DC=local"
    """
    ou_param = f'-SearchBase "{_ps_escape(ou)}"' if ou else ""
    script = f"""
Import-Module ActiveDirectory
Get-ADUser -Filter * {ou_param} -Properties LockedOut, PasswordExpired, LastLogonDate, MemberOf |
  Select-Object Name, SamAccountName, Enabled, LockedOut, PasswordExpired, LastLogonDate,
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
  -Properties LockedOut, PasswordExpired, LastLogonDate |
  Select-Object Name, SamAccountName, Enabled, LockedOut, PasswordExpired, LastLogonDate,
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
    """Return all accounts with expired passwords."""
    script = """
Import-Module ActiveDirectory
Search-ADAccount -PasswordExpired |
  Where-Object { $_.Enabled -eq $true } |
  Select-Object Name, SamAccountName, LastLogonDate,
                @{Name='OU';Expression={($_.DistinguishedName -split ',OU=')[1]}} |
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
$expired = (Search-ADAccount -PasswordExpired | Where-Object {$_.Enabled -eq $true}).Count
@{total=$all; enabled=$enabled; locked=$locked; expired=$expired} | ConvertTo-Json
"""
    r = _run(script)
    if r["success"]:
        r["message"] = "Stats retrieved."
    return r


def create_user(first: str, last: str, username: str, ou: str = "") -> dict:
    f  = _ps_escape(first)
    l  = _ps_escape(last)
    u  = _ps_escape(username)
    ou_param = f"-Path '{_ps_escape(ou)}'" if ou else ""
    script = f"""
Import-Module ActiveDirectory
$secure = ConvertTo-SecureString 'ChangeMe123!' -AsPlainText -Force
New-ADUser `
  -GivenName '{f}' `
  -Surname '{l}' `
  -Name '{f} {l}' `
  -SamAccountName '{u}' `
  -UserPrincipalName '{u}@{_DOMAIN.lower()}.local' `
  -AccountPassword $secure `
  -Enabled $true `
  -ChangePasswordAtLogon $true `
  {ou_param}
Write-Output '{{"result":"created","user":"{u}","name":"{f} {l}"}}'
"""
    r = _run(script)
    if r["success"]:
        r["message"] = (
            f"User {first} {last} ({username}) created. "
            "Default password: ChangeMe123! -- user must change on first logon."
        )
    return r
