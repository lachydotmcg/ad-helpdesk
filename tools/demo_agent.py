#!/usr/bin/env python3
"""
demo_agent.py -- a fake AD Helpdesk agent that serves realistic synthetic data.

Speaks the same protocol as the real agent (poll -> execute -> post result) but
answers from a fabricated domain instead of a real one. Nothing here touches
WinRM, Active Directory, or any real infrastructure.

Use it to explore the dashboard, take screenshots, or demo the product without
standing up a Windows Server.

    python tools/demo_agent.py --url http://localhost:5000 --key <agent-key>

Stop with Ctrl+C. The dashboard will show the agent as online while it runs.
"""

import argparse
import random
import sys
import time
from datetime import datetime, timedelta

import requests

DOMAIN = "corp.local"

# --- the fake domain ------------------------------------------------------

_FIRST = ["Sarah", "James", "Priya", "Tom", "Aisha", "Daniel", "Grace", "Liam",
          "Mia", "Noah", "Olivia", "Ethan", "Chloe", "Lucas", "Ruby", "Henry",
          "Isla", "Jack", "Ava", "Oscar", "Zoe", "Leo", "Nina", "Felix"]
_LAST = ["Chen", "Whitfield", "Nadkarni", "Brady", "Rahman", "Okafor", "Lin",
         "Murphy", "Delgado", "Hughes", "Petrov", "Kowalski", "Bianchi",
         "Fraser", "Novak", "Osei", "Baker", "Romano", "Silva", "Tanaka"]
_OUS = ["Staff", "IT", "Finance", "Sales", "Students", "Executive"]
_TITLES = {"IT": ["Systems Administrator", "Helpdesk Technician", "Network Engineer"],
           "Finance": ["Accountant", "Payroll Officer", "Finance Manager"],
           "Sales": ["Account Executive", "Sales Manager", "BDR"],
           "Staff": ["Teacher", "Coordinator", "Administrator"],
           "Students": ["Student"],
           "Executive": ["Director", "Chief Operating Officer"]}


def _build_users(n=48):
    random.seed(7)   # stable data across restarts, so screenshots stay consistent
    users, seen = [], set()
    now = datetime.utcnow()
    for i in range(n):
        first, last = random.choice(_FIRST), random.choice(_LAST)
        sam = f"{first.lower()}.{last.lower()}"
        if sam in seen:
            continue
        seen.add(sam)
        ou = random.choice(_OUS)
        locked = i % 11 == 3
        disabled = i % 17 == 5
        expired = i % 13 == 7
        users.append({
            "Name": f"{first} {last}",
            "SamAccountName": sam,
            "GivenName": first, "Surname": last,
            "UserPrincipalName": f"{sam}@{DOMAIN}",
            "EmailAddress": f"{sam}@{DOMAIN}",
            "Enabled": not disabled,
            "LockedOut": locked,
            "PasswordExpired": expired,
            "LastLogonDate": (now - timedelta(hours=random.randint(1, 260))).isoformat(),
            "PasswordLastSet": (now - timedelta(days=random.randint(3, 200))).isoformat(),
            "Department": ou,
            "Title": random.choice(_TITLES.get(ou, ["Staff Member"])),
            "OU": ou,
            "GroupCount": random.randint(1, 6),
            "DistinguishedName": f"CN={first} {last},OU={ou},DC=corp,DC=local",
        })
    return users


USERS = _build_users()
GROUPS = [{"Name": g, "GroupCategory": "Security", "GroupScope": "Global",
           "Description": d, "MemberCount": random.randint(3, 40)}
          for g, d in [("Domain Admins", "Full domain control"),
                       ("IT-Staff", "IT department"),
                       ("Finance-ReadOnly", "Finance share, read only"),
                       ("VPN-Users", "Remote access"),
                       ("Printer-Access", "Follow-me printing"),
                       ("Sales-Team", "Sales department")]]

DNS_RECORDS = [
    {"name": "@", "type": "A", "value": "10.0.1.10", "ttl": 3600},
    {"name": "dc01", "type": "A", "value": "10.0.1.10", "ttl": 3600},
    {"name": "fileserver", "type": "A", "value": "10.0.1.22", "ttl": 3600},
    {"name": "printer01", "type": "A", "value": "10.0.5.20", "ttl": 3600},
    {"name": "intranet", "type": "CNAME", "value": "fileserver.corp.local", "ttl": 3600},
    {"name": "@", "type": "MX", "value": "10 mail.corp.local", "ttl": 3600},
]


def _r(ok=True, msg="OK", data=None):
    return {"success": ok, "message": msg, "data": data}


def handle(action, args):
    """Return a plausible result for any action the dashboard or AI may send."""
    locked = [u for u in USERS if u["LockedOut"]]
    expired = [u for u in USERS if u["PasswordExpired"]]

    if action == "get_stats":
        return _r(data={"total": len(USERS),
                        "enabled": sum(1 for u in USERS if u["Enabled"]),
                        "locked": len(locked), "expired": len(expired),
                        "disabled": sum(1 for u in USERS if not u["Enabled"]),
                        "groups": len(GROUPS), "ous": len(_OUS)})
    if action == "list_users":
        return _r(msg=f"{len(USERS)} user(s) found.", data=USERS)
    if action == "list_locked_accounts":
        return _r(msg=f"{len(locked)} locked account(s).", data=locked)
    if action == "list_expired_passwords":
        return _r(msg=f"{len(expired)} expired password(s).", data=expired)
    if action in ("search_users", "list_users_in_ou"):
        q = (args[0] if args else "").lower()
        hits = [u for u in USERS if q in u["Name"].lower() or q in u["SamAccountName"].lower()
                or q in u["OU"].lower()] or USERS[:8]
        return _r(msg=f"{len(hits)} match(es).", data=hits)
    if action == "get_user_info":
        sam = (args[0] if args else "").lower()
        u = next((x for x in USERS if x["SamAccountName"] == sam), USERS[0])
        return _r(msg=f"User info for {u['SamAccountName']}.", data=u)
    if action == "list_ous":
        return _r(data=[{"Name": o, "DistinguishedName": f"OU={o},DC=corp,DC=local"} for o in _OUS])
    if action in ("list_groups", "search_groups"):
        return _r(msg=f"{len(GROUPS)} group(s).", data=GROUPS)
    if action == "get_group_members":
        return _r(data=[{"Name": u["Name"], "SamAccountName": u["SamAccountName"]} for u in USERS[:9]])
    if action == "list_group_memberships":
        return _r(data=[{"Name": g["Name"]} for g in GROUPS[:3]])

    # DNS
    if action == "list_dns_zones":
        return _r(msg="2 zone(s) found.", data=[
            {"ZoneName": DOMAIN, "ZoneType": "Primary", "DynamicUpdate": "Secure",
             "IsReverseLookupZone": False, "is_auto_created": False},
            {"ZoneName": "1.0.10.in-addr.arpa", "ZoneType": "Primary", "DynamicUpdate": "Secure",
             "IsReverseLookupZone": True, "is_auto_created": False}])
    if action == "list_dns_records":
        return _r(msg=f"{len(DNS_RECORDS)} record(s).", data=DNS_RECORDS)
    if action == "get_dns_scavenging":
        return _r(data={"ScavengingState": True, "RefreshInterval": "7.00:00:00"})

    # DHCP
    if action in ("list_dhcp_scopes", "get_dhcp_scope_stats"):
        return _r(msg="3 scope(s).", data=[
            {"ScopeId": "10.0.1.0", "Name": "Servers", "State": "Active",
             "StartRange": "10.0.1.50", "EndRange": "10.0.1.200",
             "SubnetMask": "255.255.255.0", "PercentageInUse": 42.0, "Free": 87, "InUse": 63},
            {"ScopeId": "10.0.5.0", "Name": "Staff Wi-Fi", "State": "Active",
             "StartRange": "10.0.5.20", "EndRange": "10.0.5.250",
             "SubnetMask": "255.255.255.0", "PercentageInUse": 94.0, "Free": 14, "InUse": 217},
            {"ScopeId": "10.0.9.0", "Name": "Guest", "State": "Active",
             "StartRange": "10.0.9.10", "EndRange": "10.0.9.100",
             "SubnetMask": "255.255.255.0", "PercentageInUse": 61.0, "Free": 35, "InUse": 56}])
    if action == "list_dhcp_leases":
        now = datetime.utcnow()
        return _r(data=[{"IPAddress": f"10.0.5.{40+i}",
                         "ClientId": f"A4-83-E7-{i:02X}-1{i%10}-9B",
                         "HostName": f"{USERS[i]['SamAccountName'].split('.')[0]}-laptop",
                         "AddressState": "Active",
                         "LeaseExpiryTime": (now + timedelta(hours=6+i)).isoformat()}
                        for i in range(12)])
    if action == "list_dhcp_reservations":
        return _r(data=[{"IPAddress": "10.0.5.20", "ClientId": "B8-27-EB-4C-11-02",
                         "Name": "printer01", "Description": "Follow-me printer"}])
    if action == "list_dhcp_exclusions":
        return _r(data=[{"StartRange": "10.0.5.1", "EndRange": "10.0.5.19"}])

    # Group Policy
    if action == "list_gpos":
        return _r(msg="5 GPO(s).", data=[
            {"name": "Default Domain Policy", "id": "31b2f340-016d-11d2-945f-00c04fb984f9",
             "status": "AllSettingsEnabled", "created": "2023-02-11T09:14:00", "modified": "2026-05-02T11:03:00"},
            {"name": "Password Policy - Strict", "id": "a1c3e5f7-1111-4a2b-9c8d-2e4f6a8b0c1d",
             "status": "AllSettingsEnabled", "created": "2024-06-01T10:00:00", "modified": "2026-06-18T15:41:00"},
            {"name": "USB Lockdown", "id": "b2d4f6a8-2222-4b3c-8d9e-3f5a7b9c1d2e",
             "status": "ComputerSettingsDisabled", "created": "2024-09-12T14:22:00", "modified": "2026-04-30T08:12:00"},
            {"name": "Finance Drive Mappings", "id": "c3e5a7b9-3333-4c4d-9eaf-4a6b8c0d2e3f",
             "status": "AllSettingsEnabled", "created": "2025-01-20T16:30:00", "modified": "2026-07-01T09:55:00"},
            {"name": "Wi-Fi Certificate Deployment", "id": "d4f6b8c0-4444-4d5e-afba-5b7c9d1e3f4a",
             "status": "UserSettingsDisabled", "created": "2025-03-05T11:11:00", "modified": "2026-06-22T13:27:00"}])
    if action == "get_gpo_report":
        return _r(data={"raw_available": True, "name": "Password Policy - Strict",
                        "computer": {"enabled": True, "extensions": [
                            {"name": "Security Settings", "setting_count": 14},
                            {"name": "Registry", "setting_count": 6}]},
                        "user": {"enabled": False, "extensions": []}})
    if action in ("list_gpo_links", "get_gpo_inheritance"):
        return _r(data=[{"DisplayName": "Password Policy - Strict", "Enabled": True,
                         "Enforced": True, "Order": 1},
                        {"DisplayName": "USB Lockdown", "Enabled": True, "Enforced": False, "Order": 2}])

    # NPS
    if action == "get_nps_summary":
        return _r(data={"radius_clients": 4, "network_policies": 6, "connection_request_policies": 2})
    if action == "list_nps_radius_clients":
        return _r(data=[{"Name": "WiFi-Controller", "Address": "10.0.1.5", "VendorName": "RADIUS Standard", "Enabled": True},
                        {"Name": "VPN-Gateway", "Address": "10.0.1.8", "VendorName": "RADIUS Standard", "Enabled": True},
                        {"Name": "Switch-Core-01", "Address": "10.0.1.2", "VendorName": "Cisco", "Enabled": True},
                        {"Name": "Switch-Edge-04", "Address": "10.0.1.4", "VendorName": "Cisco", "Enabled": False}])
    if action in ("list_nps_network_policies", "list_nps_connection_policies"):
        return _r(data=[{"PolicyName": "Staff Wi-Fi Access", "Enabled": True, "ProcessingOrder": 1},
                        {"PolicyName": "Guest Wi-Fi (restricted)", "Enabled": True, "ProcessingOrder": 2},
                        {"PolicyName": "VPN - IT only", "Enabled": True, "ProcessingOrder": 3}])

    # Deployment
    if action == "list_deploy_packages":
        return _r(data=[{"Name": "7z2201-x64.msi", "SizeMB": 1.8, "Type": "msi", "Modified": "2026-05-14T10:22:00"},
                        {"Name": "AdobeReader.msi", "SizeMB": 212.4, "Type": "msi", "Modified": "2026-06-02T14:51:00"},
                        {"Name": "Zoom-Installer.exe", "SizeMB": 78.1, "Type": "exe", "Modified": "2026-07-08T09:05:00"}])
    if action == "list_deployments":
        return _r(data=[{"name": "AID Deploy - 7-Zip", "id": "e5a7c9d1-5555-4e6f-b0cb-6c8d0e2f4a5b",
                         "status": "AllSettingsEnabled", "modified": "2026-07-10T11:00:00",
                         "links": ["corp.local/Workstations"]}])

    # Writes: acknowledge plausibly.
    friendly = {
        "unlock_account": "Account unlocked.",
        "reset_password": "Password reset. Temporary password sent to the user.",
        "enable_account": "Account enabled.",
        "disable_account": "Account disabled.",
        "force_password_change": "User must change password at next logon.",
        "add_to_group": "User added to the group.",
        "remove_from_group": "User removed from the group.",
        "add_dns_record": "DNS record created.",
        "remove_dns_record": "DNS record removed.",
        "add_dhcp_reservation": "Reservation created.",
        "deploy_app": "Deployment GPO created and linked.",
    }
    return _r(msg=friendly.get(action, f"{action} completed."), data=None)


def main():
    ap = argparse.ArgumentParser(description="Fake AD Helpdesk agent serving demo data.")
    ap.add_argument("--url", default="http://localhost:5000", help="server address")
    ap.add_argument("--key", required=True, help="agent key from Settings")
    a = ap.parse_args()

    base = a.url.rstrip("/")
    headers = {"X-API-Key": a.key, "Content-Type": "application/json"}

    print(f"\n Demo agent -> {base}")
    print(f" Serving a synthetic '{DOMAIN}' domain ({len(USERS)} users). No real AD is touched.")
    try:
        requests.get(f"{base}/health", timeout=8).raise_for_status()
    except Exception as e:
        print(f" [ERROR] Cannot reach the server: {e}\n")
        return 1
    try:
        requests.post(f"{base}/agent/capabilities", headers=headers, timeout=8,
                      json={"capabilities": ["ad", "dns", "dhcp", "gpo", "nps", "deploy"],
                            "actions": []})
        print(" [OK] Reported capabilities: ad, dns, dhcp, gpo, nps, deploy")
    except Exception:
        pass
    print(" Polling. Press Ctrl+C to stop.\n")

    while True:
        try:
            r = requests.get(f"{base}/agent/poll", headers=headers, timeout=10)
            if r.status_code == 401:
                print(" [ERROR] Bad agent key.\n")
                return 1
            cmd = (r.json() or {}).get("command")
            if cmd:
                action, args = cmd.get("action", "?"), cmd.get("args", [])
                res = handle(action, args)
                print(f" [{time.strftime('%H:%M:%S')}] {action}({', '.join(map(str, args))[:48]})")
                requests.post(f"{base}/agent/result", headers=headers, timeout=10,
                              json={"command_id": cmd["id"], "success": res["success"],
                                    "message": res["message"], "data": res["data"]})
            else:
                time.sleep(0.5)
        except KeyboardInterrupt:
            print("\n Demo agent stopped.\n")
            return 0
        except requests.exceptions.RequestException:
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
