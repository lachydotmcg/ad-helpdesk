#!/usr/bin/env python3
"""
build-releases.py -- package AD Helpdesk into its two release artifacts.

AD Helpdesk ships as two separate downloads, because they run on different
machines and have completely different dependencies:

    aid-server-<version>.zip   the dashboard, database, and AI assistant.
                               Runs anywhere Python does (Linux, VM, NAS, Docker).
                               Needs Flask + friends, never touches WinRM.

    aid-agent-<version>.zip    the WinRM bridge that actually talks to Windows.
                               Runs on a domain-joined Windows box.
                               Needs pywinrm, never needs Flask/Anthropic/Postgres.

Usage:
    python build-releases.py               # build both into dist/
    python build-releases.py --server      # server only
    python build-releases.py --agent       # agent only
    python build-releases.py --version 1.3.0
"""

import argparse
import os
import shutil
import sys
import zipfile

ROOT = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(ROOT, "dist")
DEFAULT_VERSION = "1.3.0"

# --- what goes in each artifact -------------------------------------------

SERVER_FILES = [
    "README.md",
    "LICENSE",
    "SELF_HOSTING.md",
    "SECURITY.md",
    "ARCHITECTURE.md",
    "DEPLOYMENT.md",
    "requirements.txt",
]
SERVER_DIRS = ["cloud"]

# The agent lives in agent/, but its release is flattened to the zip root so a
# user can run `pip install -r requirements.txt` and `python agent.py` straight
# out of the extracted folder.
AGENT_FILES = [
    "agent/agent.py",
    "agent/winrm_core.py",
    "agent/ad_bridge.py",
    "agent/dns_bridge.py",
    "agent/dhcp_bridge.py",
    "agent/gpo_bridge.py",
    "agent/nps_bridge.py",
    "agent/deploy_bridge.py",
    "agent/agent-config.example.json",
    "agent/requirements.txt",
    "LICENSE",
]
AGENT_DIRS = ["skill"]

# Never ship these, whatever directory they turn up in.
EXCLUDE_NAMES = {"__pycache__", ".pytest_cache", "node_modules", ".git"}
EXCLUDE_SUFFIXES = (".pyc", ".pyo", ".db", ".log")
# Secrets / local state that must never end up in a release.
EXCLUDE_FILES = {"agent-config.json", ".env", "adhelpdesk.db"}


def _skip(path: str) -> bool:
    name = os.path.basename(path)
    if name in EXCLUDE_FILES or name in EXCLUDE_NAMES:
        return True
    if name.endswith(EXCLUDE_SUFFIXES):
        return True
    return any(part in EXCLUDE_NAMES for part in path.split(os.sep))


def _add_file(zf: zipfile.ZipFile, src: str, arc_root: str, rel: str, flatten: bool = False) -> int:
    """Add one file. `flatten` drops the source directory so agent/agent.py
    lands at the zip root as agent.py."""
    if not os.path.exists(src) or _skip(src):
        return 0
    arcname = os.path.basename(rel) if flatten else rel
    zf.write(src, os.path.join(arc_root, arcname))
    return 1


def _add_dir(zf: zipfile.ZipFile, root: str, arc_root: str, dirname: str) -> int:
    n = 0
    base = os.path.join(root, dirname)
    if not os.path.isdir(base):
        return 0
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in EXCLUDE_NAMES]
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            if _skip(full):
                continue
            rel = os.path.relpath(full, root)
            zf.write(full, os.path.join(arc_root, rel))
            n += 1
    return n


def build(kind: str, version: str) -> str:
    files, dirs = (SERVER_FILES, SERVER_DIRS) if kind == "server" else (AGENT_FILES, AGENT_DIRS)
    name = f"aid-{kind}-{version}"
    os.makedirs(DIST, exist_ok=True)
    out = os.path.join(DIST, name + ".zip")
    count = 0
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            count += _add_file(zf, os.path.join(ROOT, f), name, f, flatten=(kind == "agent"))
        for d in dirs:
            count += _add_dir(zf, ROOT, name, d)
        # A short, artifact-specific getting-started note.
        zf.writestr(os.path.join(name, "START-HERE.txt"), _readme_for(kind, version))
    size = os.path.getsize(out) / 1024
    print(f"  {os.path.basename(out):<32} {count:>4} files  {size:>8.1f} KB")
    return out


def _readme_for(kind: str, version: str) -> str:
    if kind == "server":
        return f"""AD Helpdesk SERVER {version}
=================================

The dashboard, database, and AI assistant. Run this anywhere Python runs.

  1. pip install -r requirements.txt
  2. Set your AI provider and local mode, e.g.:
        AI_PROVIDER=ollama
        AID_LOCAL_MODE=1
  3. python cloud/app.py
  4. Open http://localhost:5000 -- the admin login and the AGENT KEY are
     printed to the console on first start.

Then install the AGENT (separate download) on a Windows box that can reach
your domain controller, and give it that agent key plus this server's address.

Full guide: SELF_HOSTING.md
"""
    return f"""AD Helpdesk AGENT {version}
================================

Runs on a domain-joined Windows box and executes AD / DNS / DHCP / Group Policy
/ NPS commands over WinRM. It polls the server outbound, so no inbound ports,
VPN, or firewall changes are needed.

  1. pip install -r requirements.txt
  2. copy agent-config.example.json agent-config.json
  3. Fill in:
        cloud_url        your server's address, e.g. http://192.168.1.20:5000
        tenant_api_key   the AGENT KEY shown in Settings on the server
        ad_vm_ip / ad_domain / ad_admin_user / ad_admin_pass
  4. python agent.py

The dashboard will show "Agent: Online" once it connects.

NOTE: agent-config.json holds credentials. Keep it readable only by the service
account that runs the agent, and never commit it to source control.
"""


def main():
    ap = argparse.ArgumentParser(description="Build AD Helpdesk release artifacts.")
    ap.add_argument("--server", action="store_true", help="build the server artifact only")
    ap.add_argument("--agent", action="store_true", help="build the agent artifact only")
    ap.add_argument("--version", default=os.getenv("AID_VERSION", DEFAULT_VERSION))
    ap.add_argument("--clean", action="store_true", help="empty dist/ first")
    a = ap.parse_args()

    if a.clean and os.path.isdir(DIST):
        shutil.rmtree(DIST)

    both = not (a.server or a.agent)
    print(f"\nBuilding AD Helpdesk {a.version} -> dist/\n")
    if a.server or both:
        build("server", a.version)
    if a.agent or both:
        build("agent", a.version)
    print("\nDone. Attach these to a GitHub release.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
