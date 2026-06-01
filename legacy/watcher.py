#!/usr/bin/env python3
"""
watcher.py -- File-based command queue for AD Helpdesk.

Run this on your PC while Cowork is open:
    python watcher.py

Watches for cmd.json, executes the AD operation, writes result.json.
"""

import os
import sys
import json
import time
import traceback
import ad_bridge

BRIDGE_DIR    = os.path.dirname(os.path.abspath(__file__))
CMD_FILE      = os.path.join(BRIDGE_DIR, "cmd.json")
RESULT_FILE   = os.path.join(BRIDGE_DIR, "result.json")
POLL_INTERVAL = 0.5

ACTIONS = {
    # Queries
    "get_user_info":        lambda a: ad_bridge.get_user_info(*a),
    "list_users":           lambda a: ad_bridge.list_users(*a) if a else ad_bridge.list_users(),
    "search_users":         lambda a: ad_bridge.search_users(*a),
    "list_locked_accounts": lambda a: ad_bridge.list_locked_accounts(),
    "list_expired_passwords": lambda a: ad_bridge.list_expired_passwords(),
    "get_stats":            lambda a: ad_bridge.get_stats(),
    # Write operations
    "reset_password":       lambda a: ad_bridge.reset_password(*a),
    "unlock_account":       lambda a: ad_bridge.unlock_account(*a),
    "disable_account":      lambda a: ad_bridge.disable_account(*a),
    "enable_account":       lambda a: ad_bridge.enable_account(*a),
    "add_to_group":         lambda a: ad_bridge.add_to_group(*a),
    "remove_from_group":    lambda a: ad_bridge.remove_from_group(*a),
    "create_user":          lambda a: ad_bridge.create_user(*a),
}


def process(cmd: dict) -> dict:
    action = cmd.get("action", "")
    args   = cmd.get("args", [])
    cmd_id = cmd.get("id", "")

    if action not in ACTIONS:
        return {"id": cmd_id, "success": False, "message": f"Unknown action: {action}", "data": None}
    try:
        result = ACTIONS[action](args)
        result["id"] = cmd_id
        return result
    except Exception as e:
        return {"id": cmd_id, "success": False, "message": traceback.format_exc(), "data": None}


def main():
    print("\n AD Helpdesk -- Watcher")
    print(" -----------------------------------")
    print(f" Watching: {BRIDGE_DIR}")
    print(" Polling every 0.5s for cmd.json")
    print(" Press Ctrl+C to stop\n")

    for f in [CMD_FILE, RESULT_FILE]:
        if os.path.exists(f):
            os.remove(f)
            print(f" [startup] Cleared stale {os.path.basename(f)}")

    while True:
        try:
            if os.path.exists(CMD_FILE):
                with open(CMD_FILE, "r", encoding="utf-8") as f:
                    raw = f.read()
                os.remove(CMD_FILE)

                try:
                    cmd = json.loads(raw)
                except json.JSONDecodeError as e:
                    result = {"id": "", "success": False, "message": f"Invalid JSON: {e}", "data": None}
                else:
                    action = cmd.get("action", "?")
                    args   = cmd.get("args", [])
                    print(f" [{time.strftime('%H:%M:%S')}] {action}({', '.join(str(a) for a in args)})")
                    result = process(cmd)
                    print(f" [{time.strftime('%H:%M:%S')}] {'OK' if result.get('success') else 'FAIL'}: {result.get('message','')[:80]}")

                with open(RESULT_FILE, "w", encoding="utf-8") as f:
                    json.dump(result, f, indent=2, default=str)

        except KeyboardInterrupt:
            print("\n Watcher stopped.")
            sys.exit(0)
        except Exception as e:
            print(f" [ERROR] {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
