#!/usr/bin/env python3
"""
setup_wizard.py -- AD Helpdesk first-time setup wizard.

Run this once to configure your connection to the cloud backend.
Creates agent-config.json and optionally sets the agent to start on boot.

Run:
    python setup_wizard.py
"""

import os
import sys
import json
import winreg
import subprocess
from pathlib import Path

BASE_DIR    = Path(__file__).parent
CONFIG_FILE = BASE_DIR / "agent-config.json"
APP_NAME    = "ADHelpdeskAgent"

BANNER = r"""
    _    ____    _   _      _       _____           _
   / \  |  _ \  | | | | ___| |_ __ |  __ \___  ___| | __
  / _ \ | | | | | |_| |/ _ \ | '_ \| |  | / _ \/ __| |/ /
 / ___ \| |_| | |  _  |  __/ | |_) | |__| |  __/\__ \   <
/_/   \_\____/  |_| |_|\___|_| .__/|_____/ \___||___/_|\_\\
                              |_|
"""


def hr():
    print(" " + "-" * 50)


def ask(prompt: str, default: str = "") -> str:
    if default:
        result = input(f"  {prompt} [{default}]: ").strip()
        return result if result else default
    else:
        while True:
            result = input(f"  {prompt}: ").strip()
            if result:
                return result
            print("  This field is required.")


def set_boot(enabled: bool):
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    if enabled:
        tray = BASE_DIR / "tray.py"
        cmd  = f'"{sys.executable}" "{tray.resolve()}"'
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
    else:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
    winreg.CloseKey(key)


def test_connection(cloud_url: str, api_key: str) -> bool:
    try:
        import requests
        r = requests.get(f"{cloud_url.rstrip('/')}/health", timeout=8)
        return r.status_code == 200
    except Exception as e:
        print(f"\n  [!] Could not reach {cloud_url}")
        print(f"      {e}")
        return False


def main():
    print(BANNER)
    print("  Welcome to AD Helpdesk setup.\n")
    print("  This wizard will configure your agent to connect to the")
    print("  AD Helpdesk cloud backend.\n")

    # Check for existing config
    if CONFIG_FILE.exists():
        print("  An existing agent-config.json was found.")
        overwrite = input("  Overwrite it? [y/N]: ").strip().lower()
        if overwrite != "y":
            print("\n  Setup cancelled. Your existing config was not changed.\n")
            sys.exit(0)

    hr()
    print("\n  STEP 1 -- Cloud backend URL\n")
    print("  This is the URL where cloud/app.py is running.")
    print("  e.g. https://your-app.railway.app or http://localhost:5000\n")
    cloud_url = ask("Cloud URL")

    hr()
    print("\n  STEP 2 -- Tenant API key\n")
    print("  Your unique key from the cloud backend.")
    print("  Get one by running:")
    print(f"    curl -X POST {cloud_url}/admin/tenants \\")
    print( "      -H 'X-Admin-Key: your-admin-key' \\")
    print( "      -H 'Content-Type: application/json' \\")
    print( "      -d '{\"name\": \"Your Organisation\"}'")
    print()
    api_key = ask("Tenant API key")

    hr()
    print("\n  STEP 3 -- Testing connection...\n")
    ok = test_connection(cloud_url, api_key)
    if ok:
        print("  [OK] Cloud backend is reachable!\n")
    else:
        print("\n  [!] Could not verify connection.")
        proceed = input("  Continue anyway? [y/N]: ").strip().lower()
        if proceed != "y":
            print("\n  Setup cancelled.\n")
            sys.exit(1)

    # Write config
    config = {
        "cloud_url":       cloud_url.rstrip("/"),
        "tenant_api_key":  api_key,
        "timeout_seconds": 10
    }
    CONFIG_FILE.write_text(json.dumps(config, indent=2))
    print(f"  Config saved to agent-config.json\n")

    hr()
    print("\n  STEP 4 -- Start on boot?\n")
    print("  The tray app can start automatically when Windows starts.")
    print("  This means your agent is always running in the background.\n")
    boot = input("  Start AD Helpdesk on boot? [Y/n]: ").strip().lower()
    if boot != "n":
        set_boot(True)
        print("  [OK] Added to Windows startup.\n")
    else:
        set_boot(False)
        print("  Skipped. You can enable this later from the tray icon.\n")

    hr()
    print()
    print("  Setup complete!")
    print()
    print("  To start the agent:")
    print("    python tray.py")
    print()
    print("  Or run without the tray:")
    print("    python agent.py")
    print()

    launch = input("  Launch the tray app now? [Y/n]: ").strip().lower()
    if launch != "n":
        tray = BASE_DIR / "tray.py"
        subprocess.Popen([sys.executable, str(tray)],
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
        print("\n  AD Helpdesk is running in your system tray.\n")
    else:
        print("\n  Done. Run 'python tray.py' whenever you're ready.\n")


if __name__ == "__main__":
    main()
