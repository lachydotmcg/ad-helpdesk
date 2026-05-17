# AD Helpdesk Skill

## Purpose
This skill enables Claude to manage Active Directory using natural language.
When a user asks you to perform any AD operation, follow the instructions below exactly.

---

## Step 1 - Find the bridge directory

The bridge directory is where cmd.json and result.json are exchanged with watcher.py.
Check for a config file first:

1. Read `skill-config.json` from the same folder as this SKILL.md
2. Use the `bridge_dir` value from that file
3. If the file does not exist, default to: `C:\Users\<ask user for their username>\ad-helpdesk`

The user typically has this in their home folder. If unsure, ask once: "What is the path to your ad-helpdesk folder?"

---

## Step 2 - Parse the user's intent

Map natural language to one of these actions:

| What the user says | Action | Args |
|---|---|---|
| "get info on X", "look up X", "what's X's account" | `get_user_info` | [username] |
| "list all users", "show me all accounts" | `list_users` | [] |
| "search for X", "find users named X" | `search_users` | [query] |
| "who's locked out", "show locked accounts" | `list_locked_accounts` | [] |
| "who has expired passwords" | `list_expired_passwords` | [] |
| "reset X's password to Y", "set X's password" | `reset_password` | [username, newpassword] |
| "unlock X", "X is locked out" | `unlock_account` | [username] |
| "disable X's account", "suspend X" | `disable_account` | [username] |
| "enable X's account", "re-enable X" | `enable_account` | [username] |
| "add X to group Y" | `add_to_group` | [username, groupname] |
| "remove X from group Y" | `remove_from_group` | [username, groupname] |
| "create user X Y with username Z in OU" | `create_user` | [first, last, username, ou_dn] |
| "move X to OU Y" | `move_user` | [username, ou_dn] |
| "dashboard stats", "how many users" | `get_stats` | [] |

**Username resolution:** If the user gives a full name (e.g. "Jake Miller"), look up the
SAM account name first using `search_users` before running write operations.

**Password policy:** If the user asks to reset a password but doesn't specify one,
generate a secure temporary password in the format `TempXXXX!` where XXXX is 4 random digits,
and tell the user what it was set to.

**OU names for lab.local:**
- IT staff: `OU=IT,OU=Staff,DC=lab,DC=local`
- Management: `OU=Management,OU=Staff,DC=lab,DC=local`
- Teachers: `OU=Teachers,OU=Staff,DC=lab,DC=local`
- Administration: `OU=Administration,OU=Staff,DC=lab,DC=local`
- Students: `OU=Students,DC=lab,DC=local`
- Leave blank for default Users container

---

## Step 3 - Write cmd.json

Generate a unique ID (4 random hex chars is fine). Write this JSON to `<bridge_dir>\cmd.json`:

```json
{
  "id": "<random-id>",
  "action": "<action>",
  "args": [<comma-separated args as strings>]
}
```

**IMPORTANT:** Use the Write tool to create this file. Do not use bash echo (special
characters in passwords will break it). The file must be valid JSON.

Example for unlocking an account:
```json
{
  "id": "a3f9",
  "action": "unlock_account",
  "args": ["jake.miller"]
}
```

Example for moving a user:
```json
{
  "id": "d4e5",
  "action": "move_user",
  "args": ["test.mcgee", "OU=Teachers,OU=Staff,DC=lab,DC=local"]
}
```

---

## Step 4 - Poll for result.json

After writing cmd.json, poll for `<bridge_dir>\result.json` using the Read tool.
- Check every 1 second for up to 15 seconds
- watcher.py deletes cmd.json and writes result.json when done
- The result has this shape:

```json
{
  "id": "<same-id>",
  "success": true or false,
  "message": "Human readable result",
  "data": null or object or array
}
```

Verify the `id` field matches your request (protects against stale results).

If no result.json appears within 15 seconds:
- Tell the user: "watcher.py may not be running. Start it with: `python watcher.py` in your ad-bridge folder."

---

## Step 5 - Report back

- If `success: true` - report the message and any relevant data in plain English
- If `success: false` - show the error message and suggest a fix if you can identify one
- For `list_users` and `search_users` - format results as a clean table
- For `get_user_info` - show name, status, last logon, groups in a readable summary
- Always overwrite result.json after reading so stale results don't interfere

---

## Write operations - always confirm first

Before running `reset_password`, `disable_account`, `create_user`, `move_user`, or `remove_from_group`,
confirm with the user unless they already phrased it as a clear instruction.

---

## Watcher must be running

If you get a timeout or file access error, remind the user:
```
python watcher.py
```
This must be running in a terminal in the ad-bridge folder for any operations to work.
