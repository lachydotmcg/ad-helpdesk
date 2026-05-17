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


ACTIONS = {
    "get_user_info":          lambda a: ad_bridge.get_user_info(*a),
    "list_users":             lambda a: ad_bridge.list_users(*a) if a else ad_bridge.list_users(),
    "search_users":           lambda a: ad_bridge.search_users(*a),
    "list_locked_accounts":   lambda a: ad_bridge.list_locked_accounts(),
    "list_expired_passwords": lambda a: ad_bridge.list_expired_passwords(),
    "get_stats":              lambda a: ad_bridge.get_stats(),
    "reset_password":         lambda a: ad_bridge.reset_password(*a),
    "unlock_account":         lambda a: ad_bridge.unlock_account(*a),
    "disable_account":        lambda a: ad_bridge.disable_account(*a),
    "enable_account":         lambda a: ad_bridge.enable_account(*a),
    "add_to_group":           lambda a: ad_bridge.add_to_group(*a),
    "remove_from_group":      lambda a: ad_bridge.remove_from_group(*a),
    "create_user":            lambda a: ad_bridge.create_user(*a),
    "move_user":              lambda a: ad_bridge.move_user(*a),
}


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
