#!/usr/bin/env python3
"""
slack_bot.py -- employee self-service over Slack.

Lets staff ask the assistant for help in Slack instead of opening a ticket:

    karen: I'm locked out
    bot:   You're unlocked, Karen. Try signing in now.

WHY SOCKET MODE
---------------
A self-hosted install usually sits behind a firewall with no inbound internet
access, so Slack's normal Events API (Slack POSTs to your public URL) will not
reach it. Socket Mode instead opens an OUTBOUND WebSocket from us to Slack, so
there is no public URL, no inbound port, and no reverse proxy. That is the same
reasoning as the agent, which polls outbound so the domain controller never has
to expose itself.

Microsoft Teams has no true Socket Mode equivalent, so Teams is not covered here.

SECURITY MODEL
--------------
An end user in Slack is NOT an admin, and is treated accordingly:

  * Identity is resolved SERVER-SIDE from the Slack profile email, never from
    anything the user typed. Saying "I am the CEO" changes nothing.
  * If that email does not match exactly one enabled AD account, we refuse to act.
  * Actions are then restricted by app._self_service_check: a short allowlist,
    and the target must be the requester's own account. Unlocking a colleague is
    an admin action, not self-service.
  * Every interaction is written to the activity log with the resolved identity.

Setup (per tenant, in Settings):
  1. Create a Slack app, enable Socket Mode.
  2. Bot token scopes: app_mentions:read, chat:write, im:history, im:read,
     im:write, users:read, users:read.email
  3. Subscribe to bot events: message.im, app_mention
  4. Paste the Bot token (xoxb-) and App-level token (xapp-) into Settings.
"""

import logging
import threading
import time

log = logging.getLogger(__name__)

# Slack SDK is optional: the server must run fine without it installed.
try:
    from slack_sdk import WebClient
    from slack_sdk.socket_mode import SocketModeClient
    from slack_sdk.socket_mode.response import SocketModeResponse
    _SLACK_AVAILABLE = True
except Exception:  # pragma: no cover - only when the extra is not installed
    WebClient = None
    SocketModeClient = None
    SocketModeResponse = None
    _SLACK_AVAILABLE = False


def slack_available() -> bool:
    return _SLACK_AVAILABLE


def unavailable_message() -> str:
    return ("Slack support needs the 'slack_sdk' package. Install it with "
            "'pip install slack_sdk' and restart the server.")


# ---------------------------------------------------------------------------
# Identity resolution
# ---------------------------------------------------------------------------

def resolve_ad_identity(email: str, ad_users: list) -> tuple[str | None, str]:
    """Map a Slack profile email to exactly one enabled AD account.

    Returns (sAMAccountName, reason). A None sam means we must not act, and the
    reason explains why in language safe to show the user.

    Deliberately strict: an ambiguous or disabled match refuses rather than
    guesses, because guessing here means acting on the wrong person's account.
    """
    e = (email or "").strip().lower()
    if not e:
        return None, ("I could not read an email address from your Slack profile, "
                      "so I cannot confirm who you are. Please raise a ticket instead.")
    matches = []
    for u in ad_users or []:
        for field in ("EmailAddress", "UserPrincipalName", "mail"):
            v = str(u.get(field) or "").strip().lower()
            if v and v == e:
                matches.append(u)
                break
    if not matches:
        return None, ("I could not find an account matching your Slack email, so I "
                      "cannot make changes. An IT admin can help from here.")
    if len(matches) > 1:
        return None, ("Your email matches more than one account, so I have not made "
                      "any changes. An IT admin will need to sort this one out.")
    user = matches[0]
    if user.get("Enabled") is False:
        return None, ("Your account is disabled, so I cannot action this. Please "
                      "contact IT directly.")
    sam = str(user.get("SamAccountName") or "").strip()
    if not sam:
        return None, "I could not confirm your account name, so I have not made any changes."
    return sam, ""


# ---------------------------------------------------------------------------
# The listener
# ---------------------------------------------------------------------------

class SlackBot:
    """One Socket Mode connection per tenant."""

    def __init__(self, tenant_id, bot_token, app_token, on_message):
        """on_message(tenant_id, slack_email, text) -> reply string."""
        self.tenant_id = tenant_id
        self.bot_token = bot_token
        self.app_token = app_token
        self.on_message = on_message
        self._client = None
        self._web = None
        self._seen = set()          # event ids we've already handled
        self._stop = False

    # -- helpers ---------------------------------------------------------
    def _email_for(self, slack_user_id):
        """Look up the Slack profile email. Server-side, never user-supplied."""
        try:
            info = self._web.users_info(user=slack_user_id)
            return ((info.get("user") or {}).get("profile") or {}).get("email", "")
        except Exception as e:
            log.warning("slack: could not read profile for %s: %s", slack_user_id, e)
            return ""

    def handle_event(self, req):
        """Process one Socket Mode request. Split out so it can be unit tested
        with a synthetic event and no live Slack connection."""
        if req.type != "events_api":
            return None
        payload = req.payload or {}
        event = payload.get("event") or {}
        etype = event.get("type")
        if etype not in ("message", "app_mention"):
            return None
        # Ignore our own messages and edits/joins, or we will loop forever.
        if event.get("bot_id") or event.get("subtype"):
            return None
        # Slack retries; de-duplicate so one question is not actioned twice.
        eid = payload.get("event_id")
        if eid:
            if eid in self._seen:
                return None
            self._seen.add(eid)
            if len(self._seen) > 500:
                self._seen = set(list(self._seen)[-200:])

        text = (event.get("text") or "").strip()
        user_id = event.get("user")
        channel = event.get("channel")
        if not text or not user_id:
            return None

        email = self._email_for(user_id)
        try:
            reply = self.on_message(self.tenant_id, email, text)
        except Exception as e:
            log.exception("slack: handler failed")
            reply = "Something went wrong on my end, so I have not made any changes."
        if reply and channel and self._web:
            try:
                self._web.chat_postMessage(channel=channel, text=reply,
                                           thread_ts=event.get("thread_ts"))
            except Exception as e:
                log.warning("slack: could not post reply: %s", e)
        return reply

    # -- lifecycle -------------------------------------------------------
    def _on_request(self, client, req):
        # Ack immediately; Slack times out in 3s and a slow model would retry.
        try:
            client.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))
        except Exception:
            pass
        self.handle_event(req)

    def start(self):
        if not _SLACK_AVAILABLE:
            raise RuntimeError(unavailable_message())
        self._web = WebClient(token=self.bot_token)
        self._client = SocketModeClient(app_token=self.app_token, web_client=self._web)
        self._client.socket_mode_request_listeners.append(self._on_request)
        self._client.connect()
        log.info("slack: connected for tenant %s", self.tenant_id)

    def stop(self):
        self._stop = True
        try:
            if self._client:
                self._client.close()
        except Exception:
            pass


_bots = {}      # tenant_id -> SlackBot
_lock = threading.Lock()


def start_for_tenant(tenant_id, bot_token, app_token, on_message):
    """Start (or restart) the listener for one tenant. Safe to call repeatedly."""
    if not (bot_token and app_token):
        return False, "Both a bot token (xoxb-) and an app token (xapp-) are required."
    if not _SLACK_AVAILABLE:
        return False, unavailable_message()
    with _lock:
        stop_for_tenant(tenant_id)
        bot = SlackBot(tenant_id, bot_token, app_token, on_message)
        try:
            bot.start()
        except Exception as e:
            return False, f"Could not connect to Slack: {str(e)[:120]}"
        _bots[tenant_id] = bot
    return True, "Connected to Slack."


def stop_for_tenant(tenant_id):
    bot = _bots.pop(tenant_id, None)
    if bot:
        bot.stop()
        return True
    return False


def is_running(tenant_id) -> bool:
    return tenant_id in _bots
