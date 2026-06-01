#!/usr/bin/env python3
"""
tray.py -- AD Helpdesk system tray manager.

Manages three modes from the system tray:
  1. Dashboard   -- local web UI (app.py on port 8888)
  2. Claude mode -- watcher.py for natural language via Cowork
  3. Cloud       -- coming in v0.5 (SaaS hosted dashboard)

Run:
    python tray.py

First time? Run setup_wizard.py to configure your cloud connection.
"""

import os
import sys
import json
import time
import socket
import winreg
import threading
import subprocess
import webbrowser
from pathlib import Path

try:
    import pystray
    from PIL import Image, ImageDraw, ImageFont
except ImportError:
    print("\n Missing dependencies. Run:")
    print("     pip install pystray pillow\n")
    sys.exit(1)

BASE_DIR       = Path(__file__).parent
CONFIG_FILE    = BASE_DIR / "agent-config.json"
LOG_FILE       = BASE_DIR / "agent.log"
WATCHER_LOG    = BASE_DIR / "watcher.log"
DASHBOARD_LOG  = BASE_DIR / "dashboard.log"
AGENT_FILE     = BASE_DIR / "agent.py"
DASHBOARD_FILE = BASE_DIR / "app.py"

# watcher.py lives in ad-bridge folder
WATCHER_FILE = BASE_DIR.parent / "ad-bridge" / "watcher.py"
if not WATCHER_FILE.exists():
    WATCHER_FILE = BASE_DIR / "watcher.py"

APP_NAME      = "ADHelpdeskAgent"
APP_DISPLAY   = "AD Helpdesk"
DASHBOARD_URL = "http://localhost:8888"

_status_lock    = threading.Lock()
_status         = "Connecting..."
_agent_proc     = None
_watcher_proc   = None
_dashboard_proc = None
_icon           = None


# ---------------------------------------------------------------------------
# Icon
# ---------------------------------------------------------------------------

def make_icon(state: str = "connecting") -> Image.Image:
    size = 64
    img  = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.rounded_rectangle([2, 2, 62, 62], radius=14,
                           fill=(22, 27, 44), outline=(79, 82, 190), width=2)
    try:
        font = ImageFont.truetype("arialbd.ttf", 22)
    except Exception:
        font = ImageFont.load_default()
    text = "AD"
    bbox = draw.textbbox((0, 0), text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - tw) // 2 - bbox[0], (size - th) // 2 - bbox[1] - 4),
              text, fill=(200, 202, 255), font=font)
    dot = {"connected": (52, 211, 153), "disconnected": (239, 68, 68),
           "connecting": (251, 191, 36)}.get(state, (251, 191, 36))
    draw.ellipse([44, 44, 60, 60], fill=dot, outline=(22, 27, 44), width=2)
    return img


# ---------------------------------------------------------------------------
# Port check
# ---------------------------------------------------------------------------

def port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


# ---------------------------------------------------------------------------
# Cloud agent
# ---------------------------------------------------------------------------

def start_agent():
    global _agent_proc
    with open(LOG_FILE, "a", encoding="utf-8") as log:
        _agent_proc = subprocess.Popen(
            [sys.executable, str(AGENT_FILE)],
            stdout=log, stderr=log, cwd=str(BASE_DIR)
        )


def monitor_agent(icon):
    global _status, _agent_proc
    start_agent()
    last_size = 0

    while True:
        time.sleep(2)

        if _agent_proc and _agent_proc.poll() is not None:
            with _status_lock:
                _status = "Disconnected - restarting..."
            icon.icon  = make_icon("disconnected")
            icon.title = f"{APP_DISPLAY} - Disconnected"
            time.sleep(3)
            start_agent()
            continue

        try:
            if LOG_FILE.exists():
                sz = LOG_FILE.stat().st_size
                if sz != last_size:
                    last_size = sz
                    text   = LOG_FILE.read_text(encoding="utf-8", errors="replace")
                    recent = "\n".join(text.strip().splitlines()[-20:]).lower()
                    if "connected to cloud" in recent or "] ok:" in recent or "] running:" in recent:
                        state, label = "connected", "Connected"
                    elif "error" in recent or "cannot reach" in recent:
                        state, label = "disconnected", "Connection error"
                    else:
                        state, label = "connecting", "Connecting..."
                    with _status_lock:
                        _status = label
                    icon.icon  = make_icon(state)
                    icon.title = f"{APP_DISPLAY} - {label}"
                elif _agent_proc and _agent_proc.poll() is None and last_size > 0:
                    with _status_lock:
                        if _status == "Connecting...":
                            _status = "Connected"
                    icon.icon  = make_icon("connected")
                    icon.title = f"{APP_DISPLAY} - Connected"
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Dashboard mode
# ---------------------------------------------------------------------------

def is_dashboard_running() -> bool:
    return port_open(8888)


def open_dashboard(icon, item):
    if is_dashboard_running():
        webbrowser.open(DASHBOARD_URL)
        return

    # Not running - offer to start it
    if DASHBOARD_FILE.exists():
        icon.notify(
            "Dashboard is not running. Starting it now...\n"
            "It will open in your browser in a few seconds.",
            APP_DISPLAY
        )
        global _dashboard_proc
        with open(DASHBOARD_LOG, "w", encoding="utf-8") as log:
            _dashboard_proc = subprocess.Popen(
                [sys.executable, str(DASHBOARD_FILE)],
                stdout=log, stderr=log, cwd=str(BASE_DIR)
            )
        time.sleep(3)
        webbrowser.open(DASHBOARD_URL)
    else:
        icon.notify(
            "Could not find app.py.\n"
            "Make sure you are running tray.py from your ad-helpdesk folder.\n"
            "Or start it manually: python app.py",
            APP_DISPLAY
        )


def dashboard_label(item):
    if is_dashboard_running():
        return "Open Dashboard  (running)"
    return "Open Dashboard  (click to start)"


# ---------------------------------------------------------------------------
# Claude / Cowork mode
# ---------------------------------------------------------------------------

def is_watcher_running() -> bool:
    return _watcher_proc is not None and _watcher_proc.poll() is None


def start_claude_mode(icon, item):
    global _watcher_proc

    if is_watcher_running():
        icon.notify("Claude mode is already running.\nOpen Cowork and start talking.", APP_DISPLAY)
        return

    if not WATCHER_FILE.exists():
        icon.notify(
            "watcher.py not found.\n"
            f"Expected at: {WATCHER_FILE}\n"
            "Check that your ad-bridge folder is in the right place.",
            APP_DISPLAY
        )
        return

    with open(WATCHER_LOG, "w", encoding="utf-8") as log:
        _watcher_proc = subprocess.Popen(
            [sys.executable, str(WATCHER_FILE)],
            stdout=log, stderr=log, cwd=str(WATCHER_FILE.parent)
        )

    icon.notify(
        "Claude mode is now active!\n"
        "Open Cowork and talk naturally to manage your AD.\n"
        "e.g. 'Unlock jake.miller' or 'Show me all locked accounts'",
        APP_DISPLAY
    )


def stop_claude_mode(icon, item):
    global _watcher_proc
    if is_watcher_running():
        _watcher_proc.terminate()
        _watcher_proc = None
        icon.notify("Claude mode stopped.", APP_DISPLAY)
    else:
        icon.notify("Claude mode is not currently running.", APP_DISPLAY)


def claude_label(item):
    return "Claude mode: Active" if is_watcher_running() else "Claude mode: Inactive"


# ---------------------------------------------------------------------------
# Boot / logs / exit
# ---------------------------------------------------------------------------

def is_boot_enabled() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                             r"Software\Microsoft\Windows\CurrentVersion\Run",
                             0, winreg.KEY_READ)
        winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        return True
    except FileNotFoundError:
        return False


def toggle_boot(icon, item):
    enabled = not is_boot_enabled()
    key = winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                         r"Software\Microsoft\Windows\CurrentVersion\Run",
                         0, winreg.KEY_SET_VALUE)
    if enabled:
        cmd = f'"{sys.executable}" "{Path(__file__).resolve()}"'
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
        icon.notify("AD Helpdesk will now start automatically on boot.", APP_DISPLAY)
    else:
        try:
            winreg.DeleteValue(key, APP_NAME)
        except FileNotFoundError:
            pass
        icon.notify("Removed from startup.", APP_DISPLAY)
    winreg.CloseKey(key)


def open_logs(icon, item):
    if LOG_FILE.exists():
        os.startfile(str(LOG_FILE))
    else:
        icon.notify("No log file yet. The agent may not have started.", APP_DISPLAY)


def get_status_label(item):
    with _status_lock:
        return f"Status: {_status}"


def exit_app(icon, item):
    for proc in [_agent_proc, _watcher_proc, _dashboard_proc]:
        if proc:
            try:
                proc.terminate()
            except Exception:
                pass
    icon.stop()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    global _icon

    if not CONFIG_FILE.exists():
        print("\n agent-config.json not found.")
        answer = input(" Run setup wizard now? [Y/n]: ").strip().lower()
        if answer != "n":
            subprocess.run([sys.executable, str(BASE_DIR / "setup_wizard.py")])
        sys.exit(0)

    if LOG_FILE.exists():
        try:
            LOG_FILE.unlink()
        except Exception:
            pass

    icon = pystray.Icon(
        APP_NAME,
        make_icon("connecting"),
        f"{APP_DISPLAY} - Connecting...",
        menu=pystray.Menu(

            # Header / status
            pystray.MenuItem(APP_DISPLAY, None, enabled=False),
            pystray.MenuItem(get_status_label, None, enabled=False),
            pystray.Menu.SEPARATOR,

            # Mode 1 - Local dashboard
            pystray.MenuItem(dashboard_label, open_dashboard),

            # Mode 2 - Claude / Cowork
            pystray.MenuItem(claude_label, None, enabled=False),
            pystray.MenuItem("  Start Claude mode", start_claude_mode),
            pystray.MenuItem("  Stop Claude mode",  stop_claude_mode),

            # Mode 3 - Cloud (coming soon)
            pystray.MenuItem("Cloud Dashboard  (coming in v0.5)", None, enabled=False),

            pystray.Menu.SEPARATOR,

            # Settings
            pystray.MenuItem("View Agent Logs", open_logs),
            pystray.MenuItem("Start on Boot", toggle_boot,
                             checked=lambda item: is_boot_enabled()),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", exit_app),
        )
    )

    _icon = icon
    threading.Thread(target=monitor_agent, args=(icon,), daemon=True).start()
    icon.run()


if __name__ == "__main__":
    main()
