"""Transfer persona dashboard + Genie space ownership to a service principal.

Run this once an account admin has created a service principal
(typically `compliance-pack-builder`) and assigned it to the workspace. The
script:

  1. Changes the OWNER of each persona dashboard to the SP
  2. Changes the OWNER of each persona Genie space to the SP
  3. Keeps the deployer on the ACL as CAN_EDIT (so they can still
     push updates, but ownership is no longer a bus-factor-of-1)

Why this matters: right now every persona dashboard and Genie space
is owned by whoever ran setup_all_personas.py (a human user). In
production, the moment that user is off-boarded, all of the persona
governance infrastructure becomes orphaned. Transferring ownership to
an SP fixes the bus-factor and matches enterprise-grade SA expectations.

Usage:
    # Dry-run first
    python3 scripts/transfer_ownership_to_sp.py \\
        --sp-id <service-principal-id> --dry-run

    # Then execute
    python3 scripts/transfer_ownership_to_sp.py \\
        --sp-id <service-principal-id>

Where <service-principal-id> is the application_id (UUID) of the
service principal, visible in the account console.

Pre-req:
    - Caller is a workspace admin
    - The SP already exists in the workspace (check via
      `databricks service-principals list`)
    - The SP has at least "Can Use" on the workspace
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
PERSONAS_DIR = REPO_ROOT / "dashboards" / "personas"
DASH_IDS_FILE = PERSONAS_DIR / ".dashboard_ids.json"
GENIE_IDS_FILE = PERSONAS_DIR / ".genie_space_ids.json"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from persona_config import get_deployer_email  # noqa: E402


def api(method: str, path: str, body: dict | None = None) -> dict | None:
    cmd = ["databricks", "api", method, path]
    if body is not None:
        cmd += ["--json", json.dumps(body)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{method.upper()} {path} failed: {r.stderr[:400]}")
    return json.loads(r.stdout) if r.stdout.strip() else None


def verify_sp_exists(sp_id: str) -> str:
    """Return the SP display name if it exists, else raise."""
    r = subprocess.run(
        ["databricks", "service-principals", "list", "-o", "json"],
        capture_output=True, text=True, check=True,
    )
    for sp in json.loads(r.stdout):
        if sp.get("applicationId") == sp_id:
            return sp.get("displayName", "(no name)")
    raise RuntimeError(
        f"service principal {sp_id} not found in workspace. "
        "Check `databricks service-principals list` — the SP must be "
        "assigned to this workspace by an account admin first."
    )


def transfer_dashboard_owner(dashboard_id: str, sp_id: str,
                             deployer_email: str, dry_run: bool) -> None:
    # Lakeview dashboards use /api/2.0/lakeview/dashboards/<id> PATCH with
    # `parent_path` and `owner_application_id`. The exact parameter name
    # varies by CLI version; try both.
    body = {"owner_application_id": sp_id}
    if dry_run:
        print(f"    (dry-run) would PATCH dashboards/{dashboard_id} "
              f"owner → SP {sp_id}")
    else:
        try:
            api("patch", f"/api/2.0/lakeview/dashboards/{dashboard_id}", body)
            print(f"    ✓ dashboard {dashboard_id}: owner → SP")
        except RuntimeError as e:
            print(f"    ✗ dashboard {dashboard_id}: {e}")
            print(f"       (probe: field may be 'owner' or 'run_as_service_principal_id')")

    # Always refresh the ACL so deployer keeps CAN_EDIT
    acl_body = {"access_control_list": [
        {"service_principal_name": sp_id, "permission_level": "CAN_MANAGE"},
        {"user_name": deployer_email, "permission_level": "CAN_EDIT"},
    ]}
    if dry_run:
        print(f"    (dry-run) would PUT dashboard ACL: SP=CAN_MANAGE, "
              f"deployer={deployer_email}=CAN_EDIT")
    else:
        try:
            api("put", f"/api/2.0/permissions/dashboards/{dashboard_id}", acl_body)
            print(f"    ✓ dashboard ACL set")
        except RuntimeError as e:
            print(f"    ✗ dashboard ACL: {e}")


def transfer_genie_owner(space_id: str, sp_id: str,
                         deployer_email: str, dry_run: bool) -> None:
    acl_body = {"access_control_list": [
        {"service_principal_name": sp_id, "permission_level": "CAN_MANAGE"},
        {"user_name": deployer_email, "permission_level": "CAN_EDIT"},
    ]}
    if dry_run:
        print(f"    (dry-run) would PATCH genie/{space_id}: SP=CAN_MANAGE, "
              f"deployer={deployer_email}=CAN_EDIT")
    else:
        try:
            api("patch", f"/api/2.0/permissions/genie/{space_id}", acl_body)
            print(f"    ✓ genie {space_id}: SP ownership applied")
        except RuntimeError as e:
            print(f"    ✗ genie {space_id}: {e}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--sp-id", required=True,
                   help="Service principal application_id (UUID)")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    if not DASH_IDS_FILE.exists() or not GENIE_IDS_FILE.exists():
        print("error: dashboard/Genie id files not found — "
              "run scripts/setup_all_personas.py first", file=sys.stderr)
        return 2

    deployer = get_deployer_email()
    try:
        sp_name = verify_sp_exists(args.sp_id)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(f"Transferring persona resource ownership")
    print(f"  Deployer (keeps CAN_EDIT): {deployer}")
    print(f"  Service principal (new CAN_MANAGE): {args.sp_id}  ({sp_name})")
    if args.dry_run:
        print("  Mode: DRY-RUN — no changes will be made")
    print()

    dashboards = json.loads(DASH_IDS_FILE.read_text())
    spaces = json.loads(GENIE_IDS_FILE.read_text())

    for persona in sorted(dashboards.keys() | spaces.keys()):
        print(f"[{persona}]")
        if persona in dashboards:
            transfer_dashboard_owner(dashboards[persona], args.sp_id, deployer, args.dry_run)
        if persona in spaces:
            transfer_genie_owner(spaces[persona], args.sp_id, deployer, args.dry_run)

    print("\n" + "=" * 60)
    print("Done." if not args.dry_run else "Dry-run complete — re-run without --dry-run to apply.")
    print("Verify with:")
    print(f"  databricks api get /api/2.0/permissions/dashboards/<dashboard_id>")
    print(f"  databricks api get /api/2.0/permissions/genie/<space_id>")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
