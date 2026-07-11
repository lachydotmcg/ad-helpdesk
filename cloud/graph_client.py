"""
Microsoft Graph client for Entra ID operations using the client-credentials flow.

This module talks to Microsoft Graph as an application (not on behalf of a
signed-in user), so it needs an Entra ID app registration with application
permissions and admin consent. Every public method returns a result envelope
of the form {"success": bool, "message": str, "data": ...} and never raises
to the caller.

How to create the app registration (Azure portal steps, exact):

1. Sign in to portal.azure.com and go to "Microsoft Entra ID".
2. In the left menu choose "App registrations", then click "New registration".
3. Give it a name (for example "AID Helpdesk Entra Bridge").
4. Under "Supported account types" choose "Accounts in this organizational
   directory only (Single tenant)".
5. Leave "Redirect URI" blank. Click "Register".
6. On the app's Overview page, copy the "Application (client) ID" and the
   "Directory (tenant) ID". These are GRAPH_CLIENT_ID and GRAPH_TENANT_ID.
7. Go to "Certificates & secrets" then "Client secrets" then "New client
   secret". Give it a description and an expiry, click "Add", and copy the
   secret VALUE immediately (it is not shown again). This is
   GRAPH_CLIENT_SECRET.
8. Go to "API permissions" then "Add a permission" then "Microsoft Graph"
   then "Application permissions" (not delegated). Add:
     - User.Read.All
     - Group.ReadWrite.All
     - User.ReadWrite.All
     - Directory.Read.All
9. Click "Grant admin consent for <your organization>" and confirm. Every
   permission should show a green check under "Status" once this is done.
10. Store the tenant ID, client ID, and client secret in the AID Helpdesk
    settings page (encrypted at rest), not in environment variables, so
    each tenant can use its own app registration.

Resetting passwords needs extra care: the passwordProfile PATCH used here
requires the User.ReadWrite.All application permission, and if the target
user holds a privileged directory role, the app registration itself needs
to hold an equal or higher directory role (Graph blocks privileged writes
from apps without one). Some tenants restrict password resets further and
require the authentication methods API with
UserAuthenticationMethod.ReadWrite.All instead of the passwordProfile PATCH.
This module implements the passwordProfile PATCH as the primary path since
it works for the common case; if a tenant rejects it with a 403, the
friendly error message will say so.
"""

import time
import urllib.parse

import requests
import msal

GRAPH_BASE = "https://graph.microsoft.com/v1.0"
GRAPH_SCOPE = ["https://graph.microsoft.com/.default"]

CAPABILITY = "entra"

# Flat action-name map for later wiring into agent-style dispatch tables.
# Values are GraphClient method names (strings, not bound callables) because
# a GraphClient instance must be constructed per-tenant with that tenant's
# credentials before any of these can be called. Callers should do:
#   client = GraphClient(tenant_id, client_id, client_secret)
#   result = getattr(client, ACTIONS["list_entra_users"])(**kwargs)
ACTIONS = {
    "list_entra_users": "list_users",
    "get_entra_user": "get_user",
    "list_entra_groups": "list_groups",
    "get_entra_group_members": "get_group_members",
    "add_entra_group_member": "add_group_member",
    "remove_entra_group_member": "remove_group_member",
    "revoke_entra_sessions": "revoke_sessions",
    "reset_entra_password": "reset_password",
}

_USER_SELECT = "id,displayName,userPrincipalName,mail,accountEnabled,onPremisesSyncEnabled,userType"
_GROUP_SELECT = "id,displayName,description,mail,groupTypes,securityEnabled,mailEnabled"

_MAX_RETRY_WAIT = 10


def _safe_id(value):
    """URL-path-escape an id/UPN segment. Rejects empty or malformed values."""
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    # Reject path traversal / query injection attempts outright rather than
    # relying solely on escaping.
    if "/" in value or "?" in value or "#" in value or "\\" in value:
        # UPNs legitimately contain no '/', so treat this as hostile input
        # rather than silently escaping and forwarding it.
        return None
    return urllib.parse.quote(value, safe="@.-_")


def _envelope(success, message, data=None):
    return {"success": success, "message": message, "data": data}


class GraphClient:
    """
    Microsoft Graph client using the MSAL client-credentials flow.

    One instance is created per tenant, using that tenant's stored app
    registration credentials. Token acquisition and caching (including
    refresh) is handled by msal.ConfidentialClientApplication internally;
    callers just call acquire_token_for_client() before each request and
    msal returns a cached token when still valid.
    """

    def __init__(self, tenant_id, client_id, client_secret, timeout=15):
        self.tenant_id = tenant_id
        self.client_id = client_id
        self.client_secret = client_secret
        self.timeout = timeout
        self._app = msal.ConfidentialClientApplication(
            client_id=client_id,
            client_credential=client_secret,
            authority=f"https://login.microsoftonline.com/{tenant_id}",
        )

    def _get_token(self):
        result = self._app.acquire_token_for_client(scopes=GRAPH_SCOPE)
        if not result or "access_token" not in result:
            error = (result or {}).get("error_description", "unknown error acquiring token")
            return None, error
        return result["access_token"], None

    def _friendly_error(self, status_code, body):
        code = None
        message = None
        try:
            error = (body or {}).get("error", {})
            code = error.get("code")
            message = error.get("message")
        except AttributeError:
            pass

        suffix = f" (Graph error code: {code})" if code else ""

        if status_code == 401:
            return f"Authentication failed against Microsoft Graph. The client secret may be wrong or expired.{suffix}"
        if status_code == 403:
            return (
                "Microsoft Graph denied this request (403 Forbidden). The app registration is likely "
                "missing a required application permission (e.g. User.Read.All, Group.ReadWrite.All, "
                "User.ReadWrite.All, or Directory.Read.All), or admin consent has not been granted."
                f"{suffix}"
            )
        if status_code == 404:
            return f"The requested object was not found in Microsoft Graph.{suffix}"
        if status_code == 429:
            return f"Microsoft Graph is rate-limiting this app. Try again shortly.{suffix}"
        if status_code and 500 <= status_code < 600:
            return f"Microsoft Graph had a server error ({status_code}). Try again shortly.{suffix}"
        return message or f"Microsoft Graph request failed with status {status_code}.{suffix}"

    def _request(self, method, path, params=None, json_body=None, extra_headers=None):
        token, error = self._get_token()
        if error:
            return _envelope(False, f"Could not acquire a Graph access token: {error}", None)

        url = path if path.startswith("http") else f"{GRAPH_BASE}{path}"
        headers = {"Authorization": f"Bearer {token}"}
        if extra_headers:
            headers.update(extra_headers)

        attempt = 0
        retried_429 = False
        retried_5xx = False

        while True:
            try:
                resp = requests.request(
                    method, url, headers=headers, params=params, json=json_body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                return _envelope(False, f"Network error contacting Microsoft Graph: {exc}", None)

            if resp.status_code == 429 and not retried_429:
                retried_429 = True
                wait = 1.0
                retry_after = resp.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait = min(float(retry_after), _MAX_RETRY_WAIT)
                    except ValueError:
                        wait = _MAX_RETRY_WAIT
                time.sleep(wait)
                continue

            if 500 <= resp.status_code < 600 and not retried_5xx:
                retried_5xx = True
                time.sleep(1.0)
                continue

            break

        if resp.status_code >= 400:
            try:
                body = resp.json()
            except ValueError:
                body = {}
            return _envelope(False, self._friendly_error(resp.status_code, body), None)

        if resp.status_code == 204 or not resp.content:
            return _envelope(True, "OK", None)

        try:
            return _envelope(True, "OK", resp.json())
        except ValueError:
            return _envelope(True, "OK", None)

    def _paginate(self, path, params, top):
        """GET path/params, following @odata.nextLink until `top` items collected."""
        collected = []
        token, error = self._get_token()
        if error:
            return _envelope(False, f"Could not acquire a Graph access token: {error}", None)

        url = f"{GRAPH_BASE}{path}"
        next_params = params
        while url and len(collected) < top:
            result = self._request("GET", url, params=next_params)
            if not result["success"]:
                return result
            data = result["data"] or {}
            collected.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            next_params = None  # nextLink already contains the query string
            if not url:
                break

        return _envelope(True, "OK", collected[:top])

    # ---- Users -----------------------------------------------------

    def list_users(self, search="", top=50):
        """List users, optionally filtered by a search term against displayName/userPrincipalName."""
        params = {"$select": _USER_SELECT, "$top": min(top, 999)}
        headers = None
        search = (search or "").strip()
        if search:
            escaped = search.replace('"', '\\"')
            params["$search"] = f'"displayName:{escaped}" OR "userPrincipalName:{escaped}"'
            headers = {"ConsistencyLevel": "eventual"}
            params["$count"] = "true"

        result = self._paginate_with_headers("/users", params, top, headers)
        return result

    def _paginate_with_headers(self, path, params, top, headers):
        collected = []
        url = f"{GRAPH_BASE}{path}"
        next_params = params
        while url and len(collected) < top:
            result = self._request("GET", url, params=next_params, extra_headers=headers)
            if not result["success"]:
                return result
            data = result["data"] or {}
            collected.extend(data.get("value", []))
            url = data.get("@odata.nextLink")
            next_params = None
            headers = None
            if not url:
                break
        return _envelope(True, "OK", collected[:top])

    def get_user(self, upn_or_id):
        """Get a single user by userPrincipalName or object id."""
        safe = _safe_id(upn_or_id)
        if not safe:
            return _envelope(False, "Invalid user identifier supplied.", None)
        return self._request("GET", f"/users/{safe}", params={"$select": _USER_SELECT})

    # ---- Groups ------------------------------------------------------

    def list_groups(self, search="", top=50):
        """List groups, optionally filtered by a search term against displayName."""
        params = {"$select": _GROUP_SELECT, "$top": min(top, 999)}
        headers = None
        search = (search or "").strip()
        if search:
            escaped = search.replace('"', '\\"')
            params["$search"] = f'"displayName:{escaped}"'
            headers = {"ConsistencyLevel": "eventual"}
            params["$count"] = "true"

        return self._paginate_with_headers("/groups", params, top, headers)

    def get_group_members(self, group_id, top=100):
        """List the direct members of a group."""
        safe = _safe_id(group_id)
        if not safe:
            return _envelope(False, "Invalid group identifier supplied.", None)
        params = {"$select": _USER_SELECT, "$top": min(top, 999)}
        return self._paginate(f"/groups/{safe}/members", params, top)

    def add_group_member(self, group_id, user_id):
        """Add a user to a group."""
        safe_group = _safe_id(group_id)
        safe_user = _safe_id(user_id)
        if not safe_group or not safe_user:
            return _envelope(False, "Invalid group or user identifier supplied.", None)
        body = {"@odata.id": f"https://graph.microsoft.com/v1.0/directoryObjects/{safe_user}"}
        return self._request("POST", f"/groups/{safe_group}/members/$ref", json_body=body)

    def remove_group_member(self, group_id, user_id):
        """Remove a user from a group."""
        safe_group = _safe_id(group_id)
        safe_user = _safe_id(user_id)
        if not safe_group or not safe_user:
            return _envelope(False, "Invalid group or user identifier supplied.", None)
        return self._request("DELETE", f"/groups/{safe_group}/members/{safe_user}/$ref")

    # ---- Sessions and password ----------------------------------------

    def revoke_sessions(self, user_id):
        """Revoke all refresh tokens/sign-in sessions for a user (forces re-authentication)."""
        safe = _safe_id(user_id)
        if not safe:
            return _envelope(False, "Invalid user identifier supplied.", None)
        return self._request("POST", f"/users/{safe}/revokeSignInSessions")

    def reset_password(self, user_id, new_password, force_change=True):
        """
        Reset a user's password via the passwordProfile PATCH.

        Requires the User.ReadWrite.All application permission. If the
        target user holds a privileged directory role, the app registration
        needs an equal or higher directory role assigned to it, or Graph
        will reject the request with a 403. Some tenants instead require
        the authentication methods API (UserAuthenticationMethod.ReadWrite.All)
        for password resets; this method uses the passwordProfile PATCH as
        the primary, simpler path and surfaces a friendly 403 message if a
        tenant rejects it.
        """
        safe = _safe_id(user_id)
        if not safe:
            return _envelope(False, "Invalid user identifier supplied.", None)
        if not new_password:
            return _envelope(False, "A new password is required.", None)
        body = {
            "passwordProfile": {
                "forceChangePasswordNextSignIn": bool(force_change),
                "password": new_password,
            }
        }
        return self._request("PATCH", f"/users/{safe}", json_body=body)

    # ---- Diagnostics ---------------------------------------------------

    def test_connection(self):
        """Verify credentials work by fetching the tenant's organization info."""
        result = self._request("GET", "/organization")
        if not result["success"]:
            return result
        orgs = (result["data"] or {}).get("value", [])
        if not orgs:
            return _envelope(False, "Connected to Microsoft Graph but no organization data was returned.", None)
        name = orgs[0].get("displayName", "unknown organization")
        return _envelope(True, f"Connected successfully to {name}.", {"displayName": name})
