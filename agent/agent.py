#!/usr/bin/env python3
"""
agent.py -- AD Helpdesk cloud agent.

Install this on the machine that has WinRM access to your AD server.
It connects OUT to the cloud backend, picks up queued commands, executes
them locally via ad_bridge.py, and posts results back.

No inbound ports needed. Works behind NAT, firewalls, and across Tailscale.

Setup:
    1. Copy agent-config.example.json to agent-config.json
    2. Fill in your cloud_url and tenant_api_key
    3. Run: python agent.py

The agent will connect to the cloud and start processing commands immediately.
"""

import os
import sys
import json
import time
import traceback
import requests
import ad_bridge

CONFIG_FILE   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "agent-config.json")
POLL_INTERVAL = 0.5  # seconds between polls


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        print(f"\n [ERROR] agent-config.json not found.")
        print(f"         Copy agent-config.example.json to agent-config.json and fill in your details.\n")
        sys.exit(1)
    with open(CONFIG_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


# Merge action registries from every available bridge module. Each module
# exposes ACTIONS (flat name -> callable taking an args list) and CAPABILITY.
_BRIDGE_MODULES = [ad_bridge]
for _name in ("dns_bridge", "dhcp_bridge", "gpo_bridge", "nps_bridge", "deploy_bridge"):
    try:
        _BRIDGE_MODULES.append(__import__(_name))
    except ImportError:
        pass

ACTIONS = {}
CAPABILITIES = []
for _mod in _BRIDGE_MODULES:
    ACTIONS.update(getattr(_mod, "ACTIONS", {}))
    _cap = getattr(_mod, "CAPABILITY", None)
    if _cap:
        CAPABILITIES.append(_cap)


def execute(command: dict) -> dict:
    action = command.get("action", "")
    args   = command.get("args", [])
    if action not in ACTIONS:
        return {"success": False, "message": f"Unknown action: {action}", "data": None}
    try:
        return ACTIONS[action](args)
    except Exception:
        return {"success": False, "message": traceback.format_exc(), "data": None}


def main():
    config    = load_config()
    cloud_url = config["cloud_url"].rstrip("/")
    api_key   = config["tenant_api_key"]
    headers   = {"X-API-Key": api_key, "Content-Type": "application/json"}
    timeout   = config.get("timeout_seconds", 10)

    # Inject WinRM credentials from config into environment so ad_bridge picks them up.
    # (When running via the Windows Service installer these are set before import;
    #  when running agent.py directly they come from agent-config.json here.)
    for key, env in [
        ("ad_vm_ip",      "AD_VM_IP"),
        ("ad_domain",     "AD_DOMAIN"),
        ("ad_admin_user", "AD_ADMIN_USER"),
        ("ad_admin_pass", "AD_ADMIN_PASS"),
    ]:
        if config.get(key):
            os.environ.setdefault(env, config[key])

    # Optional: software-deployment share for the deploy_app action (deploy_bridge).
    _deploy = config.get("software_deploy", {})
    if _deploy.get("share_unc"):
        os.environ.setdefault("AID_DEPLOY_SHARE_UNC", _deploy["share_unc"])
    if _deploy.get("share_local"):
        os.environ.setdefault("AID_DEPLOY_SHARE_LOCAL", _deploy["share_local"])

    print("\n AD Helpdesk -- Cloud Agent")
    print(" --------------------------------")
    print(f" Cloud: {cloud_url}")
    print(" Polling for commands every 0.5s")
    print(" Press Ctrl+C to stop\n")

    # Quick connectivity check
    try:
        r = requests.get(f"{cloud_url}/health", timeout=timeout)
        r.raise_for_status()
        print(f" [OK] Connected to cloud backend\n")
    except Exception as e:
        print(f" [ERROR] Cannot reach cloud backend: {e}")
        print(f"         Check cloud_url in agent-config.json and make sure the server is running.\n")
        sys.exit(1)

    # Report which service modules this agent has, so the dashboard can light
    # up the matching tabs. Older cloud backends without the endpoint just 404;
    # that's fine, the agent works exactly as before.
    try:
        requests.post(
            f"{cloud_url}/agent/capabilities",
            headers=headers,
            json={"capabilities": CAPABILITIES, "actions": sorted(ACTIONS.keys())},
            timeout=timeout,
        )
        print(f" [OK] Capabilities reported: {', '.join(CAPABILITIES)}\n")
    except Exception:
        pass

    while True:
        try:
            # Poll for a pending command
            r = requests.get(f"{cloud_url}/agent/poll", headers=headers, timeout=timeout)

            if r.status_code == 401:
                print(" [ERROR] Invalid API key - check tenant_api_key in agent-config.json")
                sys.exit(1)

            data    = r.json()
            command = data.get("command")

            if command:
                action = command.get("action", "?")
                args   = command.get("args", [])
                print(f" [{time.strftime('%H:%M:%S')}] {action}({', '.join(str(a) for a in args)})")

                result = execute(command)
                status = "OK" if result.get("success") else "FAIL"
                print(f" [{time.strftime('%H:%M:%S')}] {status}: {result.get('message','')[:80]}")

                # Post result back to cloud
                requests.post(
                    f"{cloud_url}/agent/result",
                    headers=headers,
                    json={
                        "command_id": command["id"],
                        "success":    result.get("success", False),
                        "message":    result.get("message", ""),
                        "data":       result.get("data"),
                    },
                    timeout=timeout
                )

        except KeyboardInterrupt:
            print("\n Agent stopped.")
            sys.exit(0)
        except requests.exceptions.ConnectionError:
            print(f" [{time.strftime('%H:%M:%S')}] Connection lost - retrying...")
        except Exception as e:
            print(f" [ERROR] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
