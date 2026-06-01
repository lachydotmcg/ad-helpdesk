# Legacy

These files are from v0.1-v0.4 of AID Helpdesk when it ran as a local tool rather than a cloud SaaS.

They are kept for reference but are not part of the current architecture.

| File | Superseded by |
|---|---|
| `app.py` | `cloud/app.py` |
| `watcher.py` | `agent.py` + `cloud/app.py` |
| `setup_wizard.py` | `installer/setup_wizard.py` |
| `tray.py` | Windows Service (installed by `installer/`) |
