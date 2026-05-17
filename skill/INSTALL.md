# Installing the AD Helpdesk Cowork Skill

## What this does

Once installed, you can talk to Claude in Cowork using plain English to manage
your Active Directory. No JSON, no clicking - just natural language.

Examples:
- "Unlock Jake Miller's account"
- "Reset sarah.jones's password"
- "Create a new IT user - first name Tom, last name Brady, username tom.brady"
- "Show me all locked accounts"
- "Move test.mcgee to the Teachers OU"

---

## Requirements

- AD Helpdesk installed and working (watcher.py runs successfully from your ad-bridge folder)
- Claude Cowork desktop app

---

## How it works

Claude reads SKILL.md to understand all available AD actions and the file queue format.
You write cmd.json to your bridge folder, watcher.py executes the operation against AD,
and result.json is written back. Claude reads the result and reports back in plain English.

---

## Setup

1. Copy `skill-config.example.json` to `skill-config.json` in this folder
2. Edit `skill-config.json` with your bridge directory path (where watcher.py runs)
3. Start watcher.py in your bridge folder: `python watcher.py`
4. In Cowork, just talk naturally

---

## Troubleshooting

**No response / timeout** - Make sure watcher.py is running in your ad-bridge folder.

**Access denied / WinRM errors** - Check your VM is on and Tailscale is connected.

**Wrong OU names** - Update the OU names in SKILL.md to match your AD structure.
