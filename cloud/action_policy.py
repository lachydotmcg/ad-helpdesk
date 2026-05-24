"""
action_policy.py -- Hard enforcement boundaries for all Janus / AD actions.

This is the authoritative permission layer. The LLM output is treated as a
*request*, not a command. Python code here independently validates every action
before it is queued, regardless of what the AI said in its response.

Janus cannot bypass these rules by rephrasing, chaining, or any other means.
If an action is not in ALL_ACTIONS it will never be queued. If it is classified
as DESTRUCTIVE it will never be auto-resolved -- a human must always approve it.

Classification
--------------
READ        Read-only AD queries. Zero side effects. Any source may use these.
WRITE       Reversible mutations (unlock, enable, password reset, etc.).
            Can be Janus auto-resolved when the tenant has that setting on.
DESTRUCTIVE High-impact mutations that are hard to reverse or affect access
            significantly. ALWAYS require human confirmation. Janus auto-resolve
            is blocked for these at the Python layer -- no prompt can override it.
"""

from __future__ import annotations

# ── Action classification ────────────────────────────────────────────────────

READ: frozenset[str] = frozenset({
    "get_user_info",
    "list_users",
    "list_users_in_ou",
    "search_users",
    "list_locked_accounts",
    "list_expired_passwords",
    "get_stats",
    "list_ous",
    "list_groups",
    "search_groups",
    "get_group_members",
    "list_group_memberships",
})

WRITE: frozenset[str] = frozenset({
    "unlock_account",
    "enable_account",
    "reset_password",
    "force_password_change",
    "add_to_group",
    "set_password_never_expires",
    "run_custom_script",   # classification can be overridden per-script at queue time
})

# These can never be auto-resolved by Janus. A human must always confirm them.
DESTRUCTIVE: frozenset[str] = frozenset({
    "disable_account",
    "remove_from_group",
    "create_user",
    "move_user",
    "create_ou",
    "bulk_move_users",
})

ALL_ACTIONS: frozenset[str] = READ | WRITE | DESTRUCTIVE

# Auto-resolution is only permitted for READ + WRITE actions
AUTO_ALLOWED: frozenset[str] = READ | WRITE

# Human-facing label for each class
CLASS_LABEL: dict[str, str] = {
    **{a: "read"        for a in READ},
    **{a: "write"       for a in WRITE},
    **{a: "destructive" for a in DESTRUCTIVE},
}

# Whether each write/destructive action is reversible
REVERSIBLE: dict[str, bool] = {
    "unlock_account":             True,
    "enable_account":             True,
    "reset_password":             True,   # old password can't be recovered but account still works
    "force_password_change":      True,
    "add_to_group":               True,
    "set_password_never_expires": True,
    "disable_account":            True,   # reversible but impactful
    "remove_from_group":          True,
    "create_user":                False,  # can be deleted, but user creation is not trivially undone
    "move_user":                  True,
    "create_ou":                  False,  # can be deleted, but OU creation may have downstream effects
    "bulk_move_users":            True,   # users can be moved back, but tedious at scale
}


# ── Validation ───────────────────────────────────────────────────────────────

class PolicyViolation(Exception):
    """Raised when an action is blocked by policy."""


def validate(
    action: str,
    source: str = "human",
    tenant_auto_actions: list[str] | None = None,
) -> tuple[bool, str]:
    """
    Validate whether an action may proceed from the given source.

    Parameters
    ----------
    action : str
        The action name (e.g. "disable_account").
    source : str
        One of:
          "human"      -- Admin clicked something in the dashboard.
          "janus_chat" -- Janus decided to run this from the AI chat.
          "janus_auto" -- Janus is trying to auto-resolve a ticket.
    tenant_auto_actions : list[str] | None
        List of action names the tenant has enabled for auto-resolution.

    Returns
    -------
    (allowed: bool, reason: str)
        allowed=True means the action may proceed.
        allowed=False means it must be blocked -- reason explains why.
    """
    # Hard wall: action must be in the known list
    if action not in ALL_ACTIONS:
        return False, (
            f"'{action}' is not a recognised action. "
            f"Janus operates from a fixed list of {len(ALL_ACTIONS)} operations "
            f"and cannot execute arbitrary commands."
        )

    # Janus auto-resolve hard wall: DESTRUCTIVE actions are always blocked
    if source == "janus_auto":
        if action in DESTRUCTIVE:
            return False, (
                f"'{action}' is classified as destructive and can never be "
                f"auto-resolved. A human admin must review and confirm it."
            )
        # Check tenant has actually enabled this auto-action
        if tenant_auto_actions is not None and action not in tenant_auto_actions:
            return False, (
                f"Auto-resolution of '{action}' is not enabled for this tenant."
            )

    # Janus chat: DESTRUCTIVE actions go to human confirmation flow in the
    # frontend -- the chat endpoint still queues them, but the frontend shows
    # the confirmation modal. No extra block needed here; just log intent.

    return True, "ok"


def classify(action: str) -> str:
    """Return 'read', 'write', 'destructive', or 'unknown'."""
    return CLASS_LABEL.get(action, "unknown")


def is_destructive(action: str) -> bool:
    return action in DESTRUCTIVE


def is_write(action: str) -> bool:
    return action in WRITE


def is_read(action: str) -> bool:
    return action in READ
