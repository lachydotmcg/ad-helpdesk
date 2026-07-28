#!/usr/bin/env python3
"""
demo_seed.py -- fill a local AD Helpdesk install with realistic demo content.

Creates tickets, activity, and audit history so the dashboard looks like a live
system (useful for screenshots and demos). Pairs with tools/demo_agent.py, which
serves the AD/DNS/DHCP/GPO/NPS data itself.

    python tools/demo_seed.py                 # seed the first tenant
    python tools/demo_seed.py --wipe          # remove demo content first

Safe by design: it only ever adds rows to the tenant you point it at, and --wipe
only clears tickets/activity/audit for that tenant.
"""

import argparse
import os
import random
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "cloud"))
import db  # noqa: E402

AI = "Assistant"


def _parse(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except Exception:
        return None

TICKETS = [
    ("I'm locked out of my account", "Tried logging in a few times this morning and now it says my account is locked. Need access for a client call at 11.",
     "high", "Priya Nadkarni", "priya.nadkarni@corp.local", "email", "resolved", "unlock_account"),
    ("Password reset please", "My password expired over the weekend and I can't get back in.",
     "medium", "Tom Brady", "tom.brady@corp.local", "email", "resolved", "reset_password"),
    ("New starter - needs account", "Grace Okafor starts Monday in Finance. Can we get her set up with the standard Finance access?",
     "medium", "Sarah Chen", "sarah.chen@corp.local", "manual", "open", None),
    ("Printer on level 2 not resolving", "printer01 doesn't work by name but 10.0.5.20 does. Started after the weekend maintenance.",
     "medium", "Daniel Hughes", "daniel.hughes@corp.local", "email", "open", None),
    ("Add me to the VPN group", "Working remotely next week and need VPN access.",
     "low", "Liam Murphy", "liam.murphy@corp.local", "email", "resolved", "add_to_group"),
    ("Wi-Fi keeps dropping in the west wing", "Several staff reporting drop-outs since yesterday. Might be DHCP running out?",
     "high", "Aisha Rahman", "aisha.rahman@corp.local", "email", "in_progress", None),
    ("Offboard James Whitfield", "James finished Friday. Please disable his account and remove group access.",
     "urgent", "Sarah Chen", "sarah.chen@corp.local", "manual", "resolved", "disable_account"),
    ("Shared drive access for Sales", "The new Sales starters can't see the Sales share.",
     "medium", "Mia Delgado", "mia.delgado@corp.local", "email", "open", None),
]

ANALYSES = {
    "unlock_account": ("Verified requester against AD. Account was locked after 5 failed sign-ins from a known device. "
                       "Low risk, routine lockout. Unlocked and confirmed the user can authenticate.", 2),
    "reset_password": ("Requester identity confirmed from the sending domain. Password expired per the 90-day policy. "
                       "Issued a temporary password and forced a change at next logon.", 2),
    "add_to_group": ("Requester verified. VPN-Users is a standard access group and the request came from the user's own "
                     "address. Added, and noted for the quarterly access review.", 3),
    "disable_account": ("Offboarding request from an authorised IT approver. High blast radius, so this required human "
                        "confirmation before running. Account disabled and group memberships removed.", 6),
}

ACTIVITY = [
    ("ad_action", AI, "priya.nadkarni", "Unlocked account after verifying the requester"),
    ("ticket_created", "priya.nadkarni@corp.local", "TCK-1042", "I'm locked out of my account"),
    ("ad_action", AI, "tom.brady", "Reset password and forced a change at next logon"),
    ("janus_action", AI, "corp.local", "Analysed 4 new tickets, auto-resolved 2"),
    ("ad_action", "sarah.chen@corp.local", "james.whitfield", "Disabled account (offboarding, confirmed)"),
    ("security_flag", AI, "unknown@gmail.com", "Flagged a password-reset request from an external domain"),
    ("settings_changed", "sarah.chen@corp.local", "settings", "Updated AI provider to Local"),
    ("team_member_added", "sarah.chen@corp.local", "daniel.hughes@corp.local", "Added as an operator"),
    ("ad_action", AI, "liam.murphy", "Added to VPN-Users"),
    ("ad_action", AI, "corp.local", "Created DNS A record printer01 -> 10.0.5.20"),
]

AUDIT = [
    (AI, "unlock_account", "priya.nadkarni", "success"),
    (AI, "reset_password", "tom.brady", "success"),
    (AI, "add_to_group", "liam.murphy -> VPN-Users", "success"),
    ("sarah.chen@corp.local", "disable_account", "james.whitfield", "success"),
    (AI, "get_user_info", "grace.okafor", "success"),
    (AI, "list_locked_accounts", "corp.local", "success"),
    ("sarah.chen@corp.local", "add_dns_record", "printer01.corp.local", "success"),
    (AI, "reset_password", "unknown@gmail.com", "blocked"),
    ("daniel.hughes@corp.local", "list_dhcp_leases", "10.0.5.0", "success"),
    (AI, "get_stats", "corp.local", "success"),
]


def _set_created(ticket_id, iso):
    """created_at is not writable through update_ticket, by design."""
    conn = db._get_conn()
    try:
        cur = db._cur(conn)
        cur.execute(f"UPDATE tickets SET created_at = {db._PH} WHERE id = {db._PH}",
                    (iso, ticket_id))
        conn.commit()
    finally:
        conn.close()


def _backdate(table, tenant_id, column="created_at", days=9):
    """Spread existing rows over the last `days` so charts and feeds look lived-in."""
    conn = db._get_conn()
    try:
        cur = db._cur(conn)
        cur.execute(f"SELECT id FROM {table} WHERE tenant_id = {db._PH} ORDER BY {column} DESC", (tenant_id,))
        ids = [db._row(r)["id"] for r in cur.fetchall()]
        now = datetime.utcnow()
        for i, rid in enumerate(ids):
            ts = (now - timedelta(days=random.random() * days,
                                  hours=random.randint(0, 20))).isoformat()
            cur.execute(f"UPDATE {table} SET {column} = {db._PH} WHERE id = {db._PH}", (ts, rid))
        conn.commit()
        return len(ids)
    finally:
        conn.close()


def wipe(tenant_id):
    conn = db._get_conn()
    try:
        cur = db._cur(conn)
        n = 0
        for t in ("tickets", "activity_log", "audit_log"):
            try:
                cur.execute(f"DELETE FROM {t} WHERE tenant_id = {db._PH}", (tenant_id,))
                n += cur.rowcount or 0
            except Exception:
                pass
        conn.commit()
        print(f"  wiped {n} demo row(s)")
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description="Seed demo content for screenshots.")
    ap.add_argument("--wipe", action="store_true", help="clear tickets/activity/audit first")
    ap.add_argument("--tenant", help="tenant id (defaults to the first one)")
    a = ap.parse_args()

    tenants = db.list_all_tenants()
    if not tenants:
        print("No tenant found. Start the server once with AID_LOCAL_MODE=1 first.")
        return 1
    t = next((x for x in tenants if x["id"] == a.tenant), tenants[0])
    tid = t["id"]
    print(f"\nSeeding demo content into '{t['name']}'\n")

    if a.wipe:
        wipe(tid)

    random.seed(11)
    made = 0
    # Actions the assistant can carry out end to end. Tickets resolved by one of
    # these are marked auto_resolved so the metrics page shows a realistic
    # deflection rate rather than a flat zero.
    AUTO_ACTIONS = {"unlock_account", "reset_password", "add_to_group"}
    # created_at is backdated HERE rather than by the generic _backdate pass, so
    # everything derived from it stays consistent. Doing it afterwards would move
    # the open date while leaving first response and resolution where they were,
    # producing tickets answered days before they were raised.
    now = datetime.utcnow()
    for i, (title, desc, prio, rname, remail, source, status, action) in enumerate(TICKETS):
        tk = db.create_ticket(tid, remail, title, desc, priority=prio,
                              requester_name=rname, requester_email=remail, source=source)
        created = now - timedelta(days=random.random() * 9, hours=random.randint(0, 20))
        fields = {
            "status":  status,
            # Recomputed from the backdated open date, not the real one.
            "due_at":  db.sla_due_at(prio, created.isoformat()),
        }
        if action:
            analysis, threat = ANALYSES.get(action, ("Reviewed and actioned.", 3))
            fields["janus_analysis"] = analysis
            fields["janus_action"] = action
        # Spread first responses over a plausible range so the mean and median
        # differ, which is the whole reason both are reported.
        fields["first_response_at"] = (created + timedelta(minutes=random.choice(
            [4, 7, 12, 25, 40, 95, 180, 240]))).isoformat()
        if status == "resolved":
            auto = action in AUTO_ACTIONS
            fields["auto_resolved"] = 1 if auto else 0
            # Auto-resolved tickets close in minutes; the rest take a human a day or two.
            delta = timedelta(minutes=random.choice([3, 6, 11])) if auto \
                    else timedelta(hours=random.choice([5, 26, 51]))
            resolved = created + delta
            fields["resolved_at"] = resolved.isoformat()
            fields["sla_breached"] = 1 if resolved.isoformat() > fields["due_at"] else 0
            if auto:
                fields["first_response_at"] = (created + timedelta(minutes=1)).isoformat()
        db.update_ticket(tk["id"], tid, **fields)
        _set_created(tk["id"], created.isoformat())
        made += 1
    print(f"  {made} tickets")

    for ev, actor, target, detail in ACTIVITY:
        db.log_activity(tid, ev, actor, target=target, detail=detail)
    print(f"  {len(ACTIVITY)} activity entries")

    for who, action, target, statusv in AUDIT:
        db.log_audit(tid, who, action, target, statusv)
    print(f"  {len(AUDIT)} audit entries")

    # Make it look like it has been running for a while. Tickets are excluded:
    # their dates were set consistently above and this pass would desynchronise
    # them from their own first-response and resolution times.
    for table in ("activity_log", "audit_log"):
        try:
            _backdate(table, tid)
        except Exception:
            pass
    try:
        db.increment_usage(tid, "janus_calls", 34)
        db.increment_usage(tid, "ad_commands", 61)
        print("  usage counters set")
    except Exception:
        pass

    print("\nDone. Now run the demo agent so AD/DNS/DHCP data appears:")
    print(f"  python tools/demo_agent.py --key {t.get('api_key','<agent-key>')}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
