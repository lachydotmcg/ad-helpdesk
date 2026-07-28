#!/usr/bin/env python3
"""
winrm_core.py -- Shared WinRM transport for all Windows Server bridge modules.

Owns the session setup, the connectivity circuit breaker, and the common
PowerShell-over-WinRM execution helper (_run). Service bridges (ad_bridge,
dns_bridge, dhcp_bridge, gpo_bridge, ...) import from here so they all share
one breaker and one set of credentials.

Credentials are loaded from .env / environment -- never hardcoded.
All _run results follow: {"success": bool, "message": str, "data": ...}
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


def role_installed_check(role_module: str, friendly: str) -> str:
    """PowerShell preamble that fails fast with a friendly message when a
    Windows role/feature module (DnsServer, DhcpServer, GroupPolicy, ...) is
    not present on the target host."""
    return f"""
if (-not (Get-Module -ListAvailable -Name {role_module})) {{
  Write-Error '{friendly} not detected on this host ({role_module} PowerShell module missing).'
  exit 1
}}
Import-Module {role_module}
"""
