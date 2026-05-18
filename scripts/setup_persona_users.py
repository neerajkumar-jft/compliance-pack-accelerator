"""Create 4 persona demo users in the current Databricks workspace.

Each persona gets a plus-addressed email derived from the *deployer's*
own email (whoever is running this script, as returned by `databricks
current-user me`). For example, if the deployer is
`joe.smith@acme.com`, the persona users become:

    joe.smith+compliance-cco@acme.com
    joe.smith+compliance-gc@acme.com
    joe.smith+compliance-cmo@acme.com
    joe.smith+compliance-cfo@acme.com

All four password-reset emails then route to `joe.smith@acme.com`,
letting one person control all four persona logins without needing
real mailboxes for each.

Idempotent: re-running is a no-op if the users already exist. Writes
the final persona → email mapping to
`dashboards/personas/.persona_emails.json`, which downstream scripts
(`apply_persona_uc_grants.py`, `apply_persona_workspace_acls.py`) read.

Usage:
    python scripts/setup_persona_users.py
    python scripts/setup_persona_users.py --override-email other@domain
    python scripts/setup_persona_users.py --dry-run

Prerequisites:
    - Databricks CLI configured for the target workspace
    - Caller must be a workspace admin (needed to create users)
    - Caller's mail provider must support plus-addressing (Gmail,
      Microsoft 365, and most corporate mail do; some don't).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
EMAILS_FILE = REPO_ROOT / "dashboards" / "personas" / ".persona_emails.json"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_config import get_deployer_email  # noqa: E402

PERSONAS = {
    "cco": ("CCO", "Chief Compliance Officer"),
    "gc":  ("GC",  "General Counsel"),
    "cmo": ("CMO", "Chief Marketing Officer"),
    "cfo": ("CFO", "Chief Financial Officer"),
}


def persona_email(base_email: str, persona: str) -> str:
    local, domain = base_email.split("@", 1)
    return f"{local}+compliance-{persona}@{domain}"


def list_existing_users() -> dict[str, dict]:
    """userName → user object, for every existing workspace user."""
    r = subprocess.run(
        ["databricks", "users", "list", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    return {u["userName"]: u for u in json.loads(r.stdout)}


def create_user(email: str, short: str, title: str) -> str:
    """Create a single workspace user. Returns the new user's id.

    Default entitlements Databricks assigns via SCIM:
        Admin access          = Off  (good)
        Workspace access      = On   (persona is a consumer; set Off in UI if desired)
        Databricks SQL access = On   (needed for dashboard queries)
        Consumer access       = Off  (REQUIRED to be On — toggle in UI after create)
    """
    payload = {
        "userName": email,
        "displayName": f"Compliance Pack POC — {short} Persona ({title})",
        "emails": [{"value": email, "primary": True, "type": "work"}],
        "active": True,
    }
    payload_path = Path(f"/tmp/_persona_user_{short.lower()}.json")
    payload_path.write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "users", "create", "--json", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"create user {email} failed: {r.stderr[:400]}")
    return json.loads(r.stdout)["id"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--override-email",
        help="Use this email as the plus-addressing base instead of current-user. "
             "Useful if your mail provider doesn't support plus-addressing and you "
             "have a separate inbox you want all four to route to.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would be created without making changes",
    )
    args = parser.parse_args()

    base_email = args.override_email or get_deployer_email()
    if "@" not in base_email:
        print(f"error: base email {base_email!r} is not a valid email", file=sys.stderr)
        return 1
    print(f"deployer email: {base_email}")

    emails = {p: persona_email(base_email, p) for p in PERSONAS}
    print("\nPersona → plus-addressed email:")
    for p, email in emails.items():
        print(f"  {p:3s} → {email}")

    if args.dry_run:
        print("\n(dry-run: no users created)")
        return 0

    existing = list_existing_users()
    print()
    for persona, email in emails.items():
        short, title = PERSONAS[persona]
        if email in existing:
            uid = existing[email]["id"]
            print(f"[{persona}] already exists id={uid}")
            continue
        uid = create_user(email, short, title)
        print(f"[{persona}] created id={uid}")

    EMAILS_FILE.parent.mkdir(parents=True, exist_ok=True)
    EMAILS_FILE.write_text(json.dumps(emails, indent=2))
    print(f"\nWrote {EMAILS_FILE}")

    print("\n" + "─" * 72)
    print("NEXT STEPS (manual, one-time per user):")
    print("─" * 72)
    print("1. In the workspace admin UI, open each new user and toggle:")
    print("   - Consumer access: ON   (required for dashboards + Genie)")
    print("   - Workspace access: OFF (optional; cleaner persona semantics)")
    print()
    print("2. Set passwords via the 'Forgot password' flow. In an incognito")
    print("   window, go to the workspace login page, enter each persona")
    print("   email, click 'Forgot password'. The reset link routes to")
    print(f"   {base_email} via plus-addressing.")
    print()
    print("3. Run downstream scripts (in order):")
    print("     python scripts/apply_persona_uc_grants.py")
    print("     python scripts/apply_persona_workspace_acls.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
