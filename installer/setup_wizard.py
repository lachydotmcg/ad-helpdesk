#!/usr/bin/env python3
"""
AID Helpdesk Agent - Setup Wizard & Windows Service
====================================================
This single executable serves dual purpose:

  Double-click (no args)  →  Runs the setup wizard GUI
  aid-agent-setup.exe install/start/stop/remove  →  Manages the Windows Service

Build with:
    build.bat  (see installer/build.bat)
"""

import sys
import os
import json
import time
import threading
import traceback

# ── Null stdout fix for PyInstaller --windowed mode ────────────────────────
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w")

# ── Constants ───────────────────────────────────────────────────────────────
INSTALL_DIR      = r"C:\Program Files\AID Helpdesk Agent"
CONFIG_FILE      = os.path.join(INSTALL_DIR, "agent-config.json")
SERVICE_NAME     = "AIDHelpdeskAgent"
SERVICE_DISPLAY  = "AID Helpdesk Agent"
CLOUD_URL_DEFAULT = "https://web-production-01ecc.up.railway.app"

_SERVICE_CMDS = {"install", "remove", "start", "stop", "restart", "debug", "update", "querycontrol"}
SERVICE_MODE  = len(sys.argv) > 1 and sys.argv[1].lower() in _SERVICE_CMDS


# ============================================================================
#  WINDOWS SERVICE
# ============================================================================
if SERVICE_MODE:
    import win32serviceutil
    import win32service
    import win32event
    import servicemanager
    import requests

    class AIDAgentService(win32serviceutil.ServiceFramework):
        _svc_name_         = SERVICE_NAME
        _svc_display_name_ = SERVICE_DISPLAY
        _svc_description_  = (
            "AID Helpdesk cloud agent - polls for AD commands and "
            "executes them locally via WinRM."
        )

        def __init__(self, args):
            win32serviceutil.ServiceFramework.__init__(self, args)
            self._stop_event = win32event.CreateEvent(None, 0, 0, None)
            self._running    = False

        def SvcStop(self):
            self.ReportServiceStatus(win32service.SERVICE_STOP_PENDING)
            self._running = False
            win32event.SetEvent(self._stop_event)

        def SvcDoRun(self):
            servicemanager.LogMsg(
                servicemanager.EVENTLOG_INFORMATION_TYPE,
                servicemanager.PYS_SERVICE_STARTED,
                (self._svc_name_, ""),
            )
            self._running = True
            self._run_agent()

        def _run_agent(self):
            # Config lives next to the installed exe
            cfg_path = os.path.join(os.path.dirname(sys.executable), "agent-config.json")
            if not os.path.exists(cfg_path):
                servicemanager.LogErrorMsg("AID Agent: agent-config.json not found.")
                return

            with open(cfg_path, "r") as f:
                config = json.load(f)

            # Inject WinRM credentials so ad_bridge picks them up
            os.environ["AD_VM_IP"]      = config.get("ad_vm_ip", "")
            os.environ["AD_DOMAIN"]     = config.get("ad_domain", "")
            os.environ["AD_ADMIN_USER"] = config.get("ad_admin_user", "Administrator")
            os.environ["AD_ADMIN_PASS"] = config.get("ad_admin_pass", "")

            import ad_bridge  # bundled by PyInstaller

            ACTIONS = {
                "get_user_info":              lambda a: ad_bridge.get_user_info(*a),
                "list_users":                 lambda a: ad_bridge.list_users(*a) if a else ad_bridge.list_users(),
                "search_users":               lambda a: ad_bridge.search_users(*a),
                "list_locked_accounts":       lambda a: ad_bridge.list_locked_accounts(),
                "list_expired_passwords":     lambda a: ad_bridge.list_expired_passwords(),
                "get_stats":                  lambda a: ad_bridge.get_stats(),
                "list_ous":                   lambda a: ad_bridge.list_ous(),
                "list_groups":                lambda a: ad_bridge.list_groups(),
                "search_groups":              lambda a: ad_bridge.search_groups(*a),
                "get_group_members":          lambda a: ad_bridge.get_group_members(*a),
                "list_group_memberships":     lambda a: ad_bridge.list_group_memberships(*a),
                "add_to_group":               lambda a: ad_bridge.add_to_group(*a),
                "remove_from_group":          lambda a: ad_bridge.remove_from_group(*a),
                "reset_password":             lambda a: ad_bridge.reset_password(*a),
                "unlock_account":             lambda a: ad_bridge.unlock_account(*a),
                "disable_account":            lambda a: ad_bridge.disable_account(*a),
                "enable_account":             lambda a: ad_bridge.enable_account(*a),
                "force_password_change":      lambda a: ad_bridge.force_password_change(*a),
                "set_password_never_expires": lambda a: ad_bridge.set_password_never_expires(*a),
                "create_user":                lambda a: ad_bridge.create_user(*a),
                "move_user":                  lambda a: ad_bridge.move_user(*a),
            }

            cloud_url = config["cloud_url"].rstrip("/")
            api_key   = config["tenant_api_key"]
            headers   = {"X-API-Key": api_key, "Content-Type": "application/json"}
            timeout   = config.get("timeout_seconds", 10)
            POLL_SEC  = 0.5

            while self._running:
                try:
                    r = requests.get(
                        f"{cloud_url}/agent/poll",
                        headers=headers,
                        timeout=timeout,
                    )
                    if r.status_code == 401:
                        servicemanager.LogErrorMsg("AID Agent: Invalid API key. Service stopping.")
                        break

                    command = r.json().get("command")
                    if command:
                        action = command.get("action", "")
                        args   = command.get("args", [])
                        try:
                            result = ACTIONS[action](args) if action in ACTIONS else {
                                "success": False,
                                "message": f"Unknown action: {action}",
                                "data": None,
                            }
                        except Exception:
                            result = {"success": False, "message": traceback.format_exc(), "data": None}

                        requests.post(
                            f"{cloud_url}/agent/result",
                            headers=headers,
                            json={
                                "command_id": command["id"],
                                "success":    result.get("success", False),
                                "message":    result.get("message", ""),
                                "data":       result.get("data"),
                            },
                            timeout=timeout,
                        )

                except requests.exceptions.ConnectionError:
                    time.sleep(5)
                    continue
                except Exception as e:
                    servicemanager.LogErrorMsg(f"AID Agent error: {e}")

                time.sleep(POLL_SEC)

    if __name__ == "__main__":
        win32serviceutil.HandleCommandLine(AIDAgentService)


# ============================================================================
#  GUI WIZARD
# ============================================================================
else:
    import tkinter as tk
    from tkinter import ttk, messagebox
    import requests
    import shutil
    import subprocess
    import socket
    import webbrowser

    # ── Colour palette ───────────────────────────────────────────────────────
    BG        = "#0f1117"
    CARD      = "#1a1b26"
    CARD2     = "#12131f"
    ACCENT    = "#a78bfa"
    ACCENT_DK = "#6d28d9"
    SUCCESS   = "#34d399"
    SUCCESS_DK= "#065f46"
    SUCCESS_BG= "#0d1f17"
    DANGER    = "#f87171"
    TEXT      = "#f0f0f0"
    MUTED     = "#6b7280"
    BORDER    = "#2a2b3d"

    FONT_TITLE = ("Segoe UI", 22, "bold")
    FONT_HEAD  = ("Segoe UI", 13, "bold")
    FONT_BODY  = ("Segoe UI", 10)
    FONT_SMALL = ("Segoe UI", 9)
    FONT_MONO  = ("Consolas", 10)

    # ── Step indicator strip ─────────────────────────────────────────────────
    STEPS = ["Welcome", "Cloud", "AD Credentials", "Install", "Done"]

    class AIDSetup(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title("AID Helpdesk Agent Setup")
            self.geometry("640x560")
            self.resizable(False, False)
            self.configure(bg=BG)
            self._center()

            # ── Vars ──────────────────────────────────────────────────────────
            self.cloud_url = tk.StringVar(value=CLOUD_URL_DEFAULT)
            self.api_key   = tk.StringVar()
            self.ad_ip     = tk.StringVar()
            self.ad_domain = tk.StringVar()
            self.ad_user   = tk.StringVar(value="svc.helpdesk")
            self.ad_pass   = tk.StringVar()

            # Step indicator at top
            self._step_bar = self._build_step_bar()
            self._step_bar.pack(fill="x")

            self._container = tk.Frame(self, bg=BG)
            self._container.pack(fill="both", expand=True)

            self._pages = [
                self._page_welcome,
                self._page_cloud,
                self._page_ad,
                self._page_installing,
                self._page_done,
            ]
            self._current = None
            self._show(0)

        def _center(self):
            self.update_idletasks()
            w, h = 640, 560
            x = (self.winfo_screenwidth()  - w) // 2
            y = (self.winfo_screenheight() - h) // 2
            self.geometry(f"{w}x{h}+{x}+{y}")

        # ── Step indicator ────────────────────────────────────────────────────

        def _build_step_bar(self):
            bar = tk.Frame(self, bg=CARD2, height=36)
            bar.pack_propagate(False)
            self._step_labels = []
            inner = tk.Frame(bar, bg=CARD2)
            inner.place(relx=0.5, rely=0.5, anchor="center")
            for i, step in enumerate(STEPS):
                if i > 0:
                    tk.Label(inner, text="--", bg=CARD2, fg=BORDER,
                             font=("Segoe UI", 8)).pack(side="left", padx=2)
                lbl = tk.Label(inner, text=step, bg=CARD2, fg=MUTED,
                               font=FONT_SMALL)
                lbl.pack(side="left")
                self._step_labels.append(lbl)
            return bar

        def _update_steps(self, idx):
            for i, lbl in enumerate(self._step_labels):
                if i < idx:
                    lbl.config(fg=SUCCESS, font=(FONT_SMALL[0], FONT_SMALL[1]))
                elif i == idx:
                    lbl.config(fg=ACCENT, font=(FONT_SMALL[0], FONT_SMALL[1], "bold"))
                else:
                    lbl.config(fg=MUTED, font=FONT_SMALL)

        # ── Navigation ────────────────────────────────────────────────────────

        def _show(self, idx):
            if self._current:
                self._current.destroy()
            self._page_idx = idx
            self._update_steps(idx)
            self._current = self._pages[idx]()
            self._current.pack(fill="both", expand=True)

        # ── Page: Welcome ─────────────────────────────────────────────────────

        def _page_welcome(self):
            f = tk.Frame(self._container, bg=BG)

            # Hero
            hero = tk.Frame(f, bg=CARD, height=200)
            hero.pack(fill="x")
            hero.pack_propagate(False)

            c = tk.Canvas(hero, width=72, height=72, bg=CARD, highlightthickness=0)
            c.create_oval(4, 4, 68, 68, fill=ACCENT_DK, outline="")
            c.create_text(36, 38, text="AI", fill="#fff", font=("Segoe UI", 20, "bold"))
            c.place(relx=0.5, y=62, anchor="center")

            tk.Label(hero, text="AID Helpdesk Agent", bg=CARD, fg=TEXT,
                     font=FONT_TITLE).place(relx=0.5, y=132, anchor="center")
            tk.Label(hero, text="Windows Agent Setup", bg=CARD, fg=MUTED,
                     font=FONT_BODY).place(relx=0.5, y=162, anchor="center")

            # Body
            body = tk.Frame(f, bg=BG, padx=64, pady=28)
            body.pack(fill="both", expand=True)

            desc = (
                "This wizard will connect your Windows Server to the AID Helpdesk "
                "cloud dashboard and install the agent as an auto-starting Windows Service.\n\n"
                "You'll need:\n"
                "  - Your AID tenant API key  (Settings in your dashboard)\n"
                "  - Your AD server IP and a service account with WinRM access\n\n"
                "Installation takes under a minute."
            )
            tk.Label(body, text=desc, bg=BG, fg=TEXT, font=FONT_BODY,
                     justify="left", wraplength=510).pack(anchor="w")

            btn_row = tk.Frame(body, bg=BG)
            btn_row.pack(fill="x", pady=(24, 0))
            self._btn(btn_row, "Get Started  →", ACCENT_DK, "#fff",
                      lambda: self._show(1)).pack(side="right")

            return f

        # ── Page: Cloud connection ─────────────────────────────────────────────

        def _page_cloud(self):
            f = tk.Frame(self._container, bg=BG)
            self._hdr(f, "Cloud connection",
                      "Enter your tenant API key from the Settings page of your dashboard.")

            body = tk.Frame(f, bg=BG, padx=64, pady=20)
            body.pack(fill="both", expand=True)
            body.columnconfigure(0, weight=1)

            self._field(body, "Dashboard URL", self.cloud_url, 0)
            self._field(body, "Tenant API Key", self.api_key, 1, show="*",
                        hint="Paste the key from Settings - it starts with  aid_")

            self.cloud_status = tk.Label(body, text="", bg=BG, fg=MUTED,
                                         font=FONT_SMALL)
            self.cloud_status.grid(row=5, column=0, sticky="w", pady=(6, 0))

            btn_row = tk.Frame(body, bg=BG)
            btn_row.grid(row=6, column=0, sticky="ew", pady=(20, 0))

            self._btn(btn_row, "Test connection", BORDER, ACCENT,
                      self._test_cloud).pack(side="left")
            nav = tk.Frame(btn_row, bg=BG)
            nav.pack(side="right")
            self._btn(nav, "← Back", BORDER, MUTED,
                      lambda: self._show(0)).pack(side="left", padx=(0, 8))
            self._btn(nav, "Next  →", ACCENT_DK, "#fff",
                      self._next_cloud).pack(side="left")

            return f

        # ── Page: AD credentials ──────────────────────────────────────────────

        def _page_ad(self):
            f = tk.Frame(self._container, bg=BG)
            self._hdr(f, "Active Directory connection",
                      "The agent connects to your AD server via WinRM. Use a dedicated "
                      "service account with Remote Management Users membership.")

            body = tk.Frame(f, bg=BG, padx=64, pady=16)
            body.pack(fill="both", expand=True)
            body.columnconfigure(0, weight=1)

            self._field(body, "AD Server IP",      self.ad_ip,     0,
                        hint="IP address of the machine running Active Directory")
            self._field(body, "NetBIOS Domain",    self.ad_domain, 1,
                        hint='Short name only - e.g.  LAB  (not lab.local)')
            self._field(body, "Service Account",   self.ad_user,   2)
            self._field(body, "Account Password",  self.ad_pass,   3, show="*")

            self.ad_status = tk.Label(body, text="", bg=BG, fg=MUTED, font=FONT_SMALL)
            self.ad_status.grid(row=9, column=0, sticky="w", pady=(6, 0))

            btn_row = tk.Frame(body, bg=BG)
            btn_row.grid(row=10, column=0, sticky="ew", pady=(16, 0))

            self._btn(btn_row, "Test WinRM", BORDER, ACCENT,
                      self._test_ad).pack(side="left")
            nav = tk.Frame(btn_row, bg=BG)
            nav.pack(side="right")
            self._btn(nav, "← Back", BORDER, MUTED,
                      lambda: self._show(1)).pack(side="left", padx=(0, 8))
            self._btn(nav, "Install  →", ACCENT_DK, "#fff",
                      self._start_install).pack(side="left")

            return f

        # ── Page: Installing ──────────────────────────────────────────────────

        def _page_installing(self):
            f = tk.Frame(self._container, bg=BG)
            self._hdr(f, "Installing...",
                      "Setting up the AID Helpdesk Agent service on this machine.")

            body = tk.Frame(f, bg=BG, padx=64, pady=28)
            body.pack(fill="both", expand=True)

            self.install_lbl = tk.Label(body, text="Preparing...", bg=BG, fg=MUTED,
                                        font=FONT_BODY)
            self.install_lbl.pack(anchor="w")

            style = ttk.Style()
            style.theme_use("default")
            style.configure("AID.Horizontal.TProgressbar",
                            troughcolor=CARD2, background=ACCENT,
                            borderwidth=0, lightcolor=ACCENT, darkcolor=ACCENT)
            self.progress = ttk.Progressbar(body, style="AID.Horizontal.TProgressbar",
                                            length=512, mode="determinate")
            self.progress.pack(fill="x", pady=(8, 14))

            self.log = tk.Text(body, bg=CARD2, fg=TEXT, font=FONT_MONO,
                               height=10, bd=0, relief="flat", state="disabled",
                               insertbackground=ACCENT, padx=12, pady=8)
            self.log.pack(fill="x")

            return f

        # ── Page: Done ────────────────────────────────────────────────────────

        def _page_done(self):
            f = tk.Frame(self._container, bg=BG)

            banner = tk.Frame(f, bg=SUCCESS_BG, height=200)
            banner.pack(fill="x")
            banner.pack_propagate(False)

            c = tk.Canvas(banner, width=72, height=72, bg=SUCCESS_BG, highlightthickness=0)
            c.create_oval(4, 4, 68, 68, fill=SUCCESS_DK, outline="")
            c.create_text(36, 38, text="✓", fill=SUCCESS, font=("Segoe UI", 28, "bold"))
            c.place(relx=0.5, y=64, anchor="center")

            tk.Label(banner, text="Agent Installed", bg=SUCCESS_BG, fg=SUCCESS,
                     font=FONT_TITLE).place(relx=0.5, y=136, anchor="center")
            tk.Label(banner, text="Your dashboard now shows: Agent Online",
                     bg=SUCCESS_BG, fg=MUTED,
                     font=FONT_BODY).place(relx=0.5, y=166, anchor="center")

            body = tk.Frame(f, bg=BG, padx=64, pady=28)
            body.pack(fill="both", expand=True)

            for label, value in [
                ("Service",      f"{SERVICE_NAME}  (Running, auto-start)"),
                ("Installed to", INSTALL_DIR),
                ("Connected to", self.cloud_url.get()),
            ]:
                row = tk.Frame(body, bg=BG)
                row.pack(fill="x", pady=3)
                tk.Label(row, text=label, bg=BG, fg=MUTED, font=FONT_SMALL,
                         width=14, anchor="w").pack(side="left")
                tk.Label(row, text=value, bg=BG, fg=TEXT, font=FONT_SMALL,
                         anchor="w").pack(side="left")

            btn_row = tk.Frame(body, bg=BG)
            btn_row.pack(fill="x", pady=(24, 0))
            self._btn(btn_row, "Open Dashboard", ACCENT_DK, "#fff",
                      lambda: webbrowser.open(self.cloud_url.get())).pack(side="left")
            self._btn(btn_row, "Close", BORDER, MUTED,
                      self.destroy).pack(side="right")

            return f

        # ── Helpers: widgets ──────────────────────────────────────────────────

        def _hdr(self, parent, title, subtitle):
            hdr = tk.Frame(parent, bg=CARD, padx=64, pady=18)
            hdr.pack(fill="x")
            tk.Label(hdr, text=title,    bg=CARD, fg=TEXT,  font=FONT_HEAD).pack(anchor="w")
            tk.Label(hdr, text=subtitle, bg=CARD, fg=MUTED, font=FONT_SMALL,
                     wraplength=512, justify="left").pack(anchor="w", pady=(2, 0))

        def _field(self, parent, label, var, row_base, show="", hint=""):
            tk.Label(parent, text=label, bg=BG, fg=MUTED,
                     font=FONT_SMALL).grid(row=row_base * 2,     column=0,
                                           sticky="w", pady=(10, 2))
            e = tk.Entry(parent, textvariable=var, bg=CARD2, fg=TEXT,
                         insertbackground=ACCENT, relief="flat", font=FONT_MONO,
                         show=show, bd=0)
            e.grid(row=row_base * 2 + 1, column=0, sticky="ew", ipady=8, ipadx=10)
            if hint:
                tk.Label(parent, text=hint, bg=BG, fg=MUTED,
                         font=("Segoe UI", 8)).grid(row=row_base * 2 + 2, column=0,
                                                     sticky="w", pady=(2, 0))
            return e

        def _btn(self, parent, text, bg, fg, cmd):
            return tk.Button(parent, text=text, bg=bg, fg=fg, font=FONT_BODY,
                             command=cmd, relief="flat", cursor="hand2",
                             padx=16, pady=8, bd=0,
                             activebackground=bg, activeforeground=fg)

        def _status(self, widget, msg, color=MUTED):
            widget.config(text=msg, fg=color)
            self.update_idletasks()

        # ── Helpers: connection tests ─────────────────────────────────────────

        def _test_cloud(self):
            self._status(self.cloud_status, "Testing...", MUTED)
            def run():
                url = self.cloud_url.get().rstrip("/")
                key = self.api_key.get().strip()
                if not key:
                    self._status(self.cloud_status, "Enter your API key first.", DANGER)
                    return
                try:
                    requests.get(f"{url}/health", timeout=8).raise_for_status()
                    r2 = requests.get(f"{url}/agent/poll",
                                      headers={"X-API-Key": key}, timeout=8)
                    if r2.status_code == 401:
                        self._status(self.cloud_status, "Invalid API key.", DANGER)
                    else:
                        self._status(self.cloud_status, "Connected successfully.", SUCCESS)
                except Exception as e:
                    self._status(self.cloud_status, f"Error: {e}", DANGER)
            threading.Thread(target=run, daemon=True).start()

        def _test_ad(self):
            self._status(self.ad_status, "Checking WinRM...", MUTED)
            def run():
                ip = self.ad_ip.get().strip()
                if not ip:
                    self._status(self.ad_status, "Enter the server IP first.", DANGER)
                    return
                try:
                    sock = socket.create_connection((ip, 5985), timeout=6)
                    sock.close()
                    self._status(self.ad_status, f"WinRM port reachable at {ip}:5985.", SUCCESS)
                except Exception as e:
                    self._status(self.ad_status, f"Cannot reach {ip}:5985 - {e}", DANGER)
            threading.Thread(target=run, daemon=True).start()

        # ── Navigation guards ─────────────────────────────────────────────────

        def _next_cloud(self):
            if not self.api_key.get().strip():
                messagebox.showwarning("API Key Required",
                                       "Please enter your tenant API key.")
                return
            self._show(2)

        def _start_install(self):
            missing = [f for f, v in [
                ("AD Server IP",     self.ad_ip.get()),
                ("NetBIOS Domain",   self.ad_domain.get()),
                ("Account Password", self.ad_pass.get()),
            ] if not v.strip()]
            if missing:
                messagebox.showwarning("Missing Fields",
                                       "Please fill in: " + ", ".join(missing))
                return
            self._show(3)
            threading.Thread(target=self._do_install, daemon=True).start()

        # ── Installation logic ────────────────────────────────────────────────

        def _log(self, msg):
            self.log.config(state="normal")
            self.log.insert("end", f"  {msg}\n")
            self.log.see("end")
            self.log.config(state="disabled")
            self.update_idletasks()

        def _prog(self, val, msg=""):
            self.progress["value"] = val
            if msg:
                self.install_lbl.config(text=msg, fg=TEXT)
            self.update_idletasks()

        def _do_install(self):
            try:
                # 1. Create install directory
                self._prog(8, "Creating installation directory...")
                os.makedirs(INSTALL_DIR, exist_ok=True)
                self._log(f"Directory: {INSTALL_DIR}")

                # 2. Copy this exe to install dir
                self._prog(20, "Copying agent files...")
                exe_src = sys.executable
                exe_dst = os.path.join(INSTALL_DIR, "aid-agent-setup.exe")
                if os.path.normcase(exe_src) != os.path.normcase(exe_dst):
                    shutil.copy2(exe_src, exe_dst)
                    self._log(f"Copied: aid-agent-setup.exe")

                # 3. Write config file
                self._prog(38, "Writing configuration...")
                config = {
                    "cloud_url":       self.cloud_url.get().rstrip("/"),
                    "tenant_api_key":  self.api_key.get().strip(),
                    "ad_vm_ip":        self.ad_ip.get().strip(),
                    "ad_domain":       self.ad_domain.get().strip(),
                    "ad_admin_user":   self.ad_user.get().strip(),
                    "ad_admin_pass":   self.ad_pass.get(),
                    "timeout_seconds": 10,
                }
                with open(CONFIG_FILE, "w") as cf:
                    json.dump(config, cf, indent=2)
                self._log("Written: agent-config.json")

                # 4. Register Windows Service
                self._prog(58, "Registering Windows Service...")
                result = subprocess.run(
                    [exe_dst, "install"],
                    capture_output=True, text=True, timeout=30,
                )
                out = (result.stdout + result.stderr).strip()
                self._log(f"Service install: {out or 'OK'}")

                # 5. Configure auto-start
                self._prog(70, "Configuring auto-start...")
                subprocess.run(
                    ["sc", "config", SERVICE_NAME, "start=", "auto"],
                    capture_output=True, timeout=15,
                )
                self._log("Auto-start on boot: enabled")

                # 6. Start the service
                self._prog(85, "Starting service...")
                result = subprocess.run(
                    ["net", "start", SERVICE_NAME],
                    capture_output=True, text=True, timeout=30,
                )
                out = (result.stdout + result.stderr).strip()
                self._log(f"Service start: {out or 'OK'}")

                # 7. Done
                self._prog(100, "Done!")
                self._log("Installation complete.")
                time.sleep(0.8)
                self.after(400, lambda: self._show(4))

            except Exception:
                tb = traceback.format_exc()
                self._prog(0, "Installation failed.")
                self._log(f"ERROR:\n{tb}")

    if __name__ == "__main__":
        AIDSetup().mainloop()
