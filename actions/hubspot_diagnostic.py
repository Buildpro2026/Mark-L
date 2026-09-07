"""One command that answers "is JARVIS on the right HubSpot, and can Lee log in?"

Written to be run where the token actually lives (the Render service or the
Cron), because that is the only place the question can be answered. It
prints an account identifier and user emails; it never prints the token.

It deliberately reports two separate verdicts. A private-app token can
prove which portal it reads and whether that portal is the expected one. It
CANNOT prove anything about human sign-in — HubSpot exposes no API for
login methods, 2FA state, or super-admin identity — so this says so rather
than leaving silence to be read as success.
"""
from __future__ import annotations

from typing import Any


def run(expected_login_email: str | None = None) -> dict[str, Any]:
    from actions import hubspot_integration as hs
    from core.headless import config

    # Set up front so the note is present on every exit path, including the
    # early return below — a blank note reads as "nothing to say", which is
    # the opposite of the point.
    report: dict[str, Any] = {
        "api": {}, "portal": {}, "users": {},
        "human_login": {
            "verifiable_via_api": False,
            "note": ("Login method, 2FA state and super-admin identity are account "
                     "settings; no private-app token can read them. Confirm in the "
                     "HubSpot UI."),
        },
    }

    # ── API ──
    report["api"]["configured"] = hs.is_configured()
    if not hs.is_configured():
        report["api"]["authenticated"] = False
        report["api"]["detail"] = "HUBSPOT_TOKEN is not set on this process."
        return report

    portal = hs.verify_expected_portal()
    report["api"]["authenticated"] = bool(portal.get("verified"))
    report["api"]["detail"] = portal.get("detail")

    # ── PORTAL ──
    report["portal"] = {
        "portal_id": portal.get("portal_id"),
        "ui_domain": portal.get("ui_domain"),
        "account_type": portal.get("account_type"),
        "expected_pinned": bool(config.HUBSPOT_EXPECTED_PORTAL_ID),
        "match": portal.get("portal_match"),
    }

    # ── USERS / OWNERS ──
    owners = hs.get_owners()
    report["users"]["retrievable"] = bool(owners.get("ok"))
    if owners.get("ok"):
        report["users"]["count"] = owners.get("count")
        report["users"]["emails"] = [o.get("email") for o in owners["owners"] if o.get("email")]
        report["users"]["archived"] = [o.get("email") for o in owners["owners"] if o.get("archived")]
    else:
        report["users"]["detail"] = owners.get("detail") or owners.get("state")

    # ── HUMAN LOGIN ──
    # The one login fact a token CAN establish: whether an address exists as
    # an owner in this portal. HubSpot requires the login address to be an
    # active user of the specific account, so an address absent here cannot
    # sign in no matter how correct the company domain is.
    if expected_login_email:
        owner = hs.find_owner_by_email(expected_login_email)
        report["human_login"]["checked_email"] = expected_login_email
        if owner is None:
            report["human_login"]["is_owner_in_this_portal"] = False
            report["human_login"]["conclusion"] = (
                "This address is NOT an owner in the connected portal. It is either not a "
                "user of this account, or it belongs to a different portal."
            )
        else:
            report["human_login"]["is_owner_in_this_portal"] = True
            report["human_login"]["archived"] = owner.get("archived")
            report["human_login"]["conclusion"] = (
                "Address exists as an ARCHIVED owner — deactivated users cannot log in."
                if owner.get("archived") else
                "Address exists as an active owner. A failing login is therefore an "
                "authentication problem (method/2FA/password), not a missing user."
            )
    return report


def print_report(expected_login_email: str | None = None) -> None:
    report = run(expected_login_email)
    api, portal, users, login = (report["api"], report["portal"],
                                 report["users"], report["human_login"])

    print("HUBSPOT DIAGNOSTIC")
    print(f"  API configured      : {api.get('configured')}")
    print(f"  API authenticated   : {api.get('authenticated')}")
    if api.get("detail"):
        print(f"  detail              : {api['detail']}")
    if portal:
        print(f"  portal id           : {portal.get('portal_id')}")
        print(f"  portal ui domain    : {portal.get('ui_domain')}")
        print(f"  expected pinned     : {portal.get('expected_pinned')}")
        print(f"  portal match        : {portal.get('match')}")
    if users:
        print(f"  users retrievable   : {users.get('retrievable')}  count={users.get('count')}")
        for email in (users.get("emails") or [])[:25]:
            print(f"    - {email}")
        if users.get("archived"):
            print(f"  archived (cannot log in): {users['archived']}")
    print(f"  human login via API : NOT VERIFIABLE — {login.get('note')}")
    if login.get("checked_email"):
        print(f"  checked address     : {login['checked_email']}")
        print(f"  is owner here       : {login.get('is_owner_in_this_portal')}")
        print(f"  conclusion          : {login.get('conclusion')}")


if __name__ == "__main__":
    import sys
    print_report(sys.argv[1] if len(sys.argv) > 1 else None)
