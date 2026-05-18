"""Apply runtime permissions to the DPIA Review app's service principal.

Closes the Phase 4 Theme 3 gap where the app deployed but couldn't
actually function until a deployer hand-applied four grants. Now wired
into deploy_all.sh as the ``app_perms`` step, so the deploy is genuinely
one command end-to-end.

What it does:
  1. Resolves the dpia_review_app's runtime SP via
     ``databricks apps get compliance-dpia-review`` — the SP is provisioned by
     Databricks at app create time, so this script only runs after the
     bundle deploy.
  2. Reads .persona_emails.json (written by setup_persona_users.py) to
     learn which workspace users are CCO, GC, CFO.
  3. Applies four buckets of permissions:
       a. Workspace `CAN_USE` on the SQL warehouse → the app's SP
       b. UC `SELECT, MODIFY` on compliance_pack.compliance.dpia_runs → SP
          (so the app can list runs AND issue the approval UPDATE — no
          persona user has MODIFY, only the SP does)
       c. UC `READ VOLUME` on compliance_pack.compliance.dpia_artifacts → SP
          (so the PDF download can read the artifact JSON)
       d. App-level `CAN_USE` → CCO + GC + CFO persona users
          (CMO is intentionally excluded — DPIA review isn't a marketing
          concern)

Idempotency:
  - UC GRANTs are no-ops on re-grant (UC handles dedup naturally).
  - Workspace permission API uses PATCH which adds to existing ACLs
    without removing other grantees.
  Re-running this script after any other grant change is safe.

Failure modes (each surfaces a clear error before any side effect):
  - App not yet deployed → exits 1 with "run `databricks bundle deploy`
    first"
  - .persona_emails.json missing → exits 1 pointing at setup_persona_users
  - Warehouse not discoverable → exits 1 (same logic as bootstrap_catalog)

Wired into deploy_all.sh as `app_perms`; runs after `personas` so
.persona_emails.json is guaranteed to exist.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
CATALOG = os.environ.get("COMPLIANCE_CATALOG", "compliance_pack")
APP_NAME = "compliance-dpia-review"
APPROVER_PERSONAS = ("cco", "gc")  # can click the Approve button
VIEWER_PERSONAS   = ("cfo",)        # CAN_USE on app, but Approve hidden in UI
ALLOWED_PERSONAS  = APPROVER_PERSONAS + VIEWER_PERSONAS

PERSONA_EMAILS_PATH = REPO_ROOT / "dashboards" / "personas" / ".persona_emails.json"


def _databricks(*args: str) -> str:
    r = subprocess.run(["databricks", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"databricks {' '.join(args)} failed: {r.stderr[:300]}")
    return r.stdout


def discover_warehouse() -> str:
    """Same logic as bootstrap_catalog.py — accepts STOPPED warehouses
    since serverless auto-starts on the first query."""
    if env := os.environ.get("COMPLIANCE_WAREHOUSE_ID"):
        return env
    out = _databricks("warehouses", "list", "-o", "json")
    warehouses = json.loads(out)
    if not warehouses:
        raise RuntimeError("no SQL warehouses found in this workspace.")
    running_serverless = [w for w in warehouses
                          if w.get("state") == "RUNNING" and w.get("enable_serverless_compute")]
    running_any = [w for w in warehouses if w.get("state") == "RUNNING"]
    stopped_serverless = [w for w in warehouses if w.get("enable_serverless_compute")]
    pick = (running_serverless or running_any or stopped_serverless or warehouses)[0]
    return pick["id"]


def discover_app() -> dict[str, str]:
    """Resolve the dpia-review app's name + SP info.

    Returns ``{"app_name": <name>, "sp_principal": <client_id>}``.

    - ``app_name`` is what the workspace permissions API expects on its
      path (``/api/2.0/permissions/apps/<name>``). Empirically the app's
      ``id`` UUID is rejected by the permissions endpoint with
      "App with name <UUID> does not exist", so always pass ``name``.
    - ``sp_principal`` is the SP's client_id (a UUID), which is the
      grantee identifier accepted by UC ``GRANT … TO `<id>` ``.

    Raises if the app isn't deployed yet.
    """
    try:
        raw = _databricks("apps", "get", APP_NAME, "-o", "json")
    except RuntimeError as e:
        raise RuntimeError(
            f"App '{APP_NAME}' not found. Run `databricks bundle deploy --target dev` "
            f"first so the bundle creates the app + provisions its runtime SP."
        ) from e
    info = json.loads(raw)

    # workspace permissions API uses `name`, not the UUID `id`.
    name = info.get("name")
    if not name:
        raise RuntimeError(f"Could not find 'name' on app response: {info}")

    # SP client_id is what UC GRANTs accept as a grantee. CLI version
    # variations surface different fields; try canonical ones first.
    sp_principal = (
        info.get("service_principal_client_id")
        or (info.get("service_principal") or {}).get("client_id")
        or (info.get("service_principal") or {}).get("application_id")
    )
    if not sp_principal:
        raise RuntimeError(
            f"Could not find service_principal_client_id on app response. "
            f"Fields seen: {sorted(info.keys())}. Raw response: {raw[:500]}"
        )

    return {"app_name": str(name), "sp_principal": str(sp_principal)}


def run_sql(warehouse_id: str, stmt: str) -> dict:
    payload = {"warehouse_id": warehouse_id, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_app_perms_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_app_perms_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"error": r.stderr[:400]}
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        return {"error": d.get("status", {}).get("error", {}).get("message", "")[:400]}
    return {"ok": True}


def patch_workspace_permission(resource_path: str, body: dict[str, Any]) -> dict:
    """PATCH (not PUT) so we ADD to existing ACLs rather than replace."""
    Path("/tmp/_app_perms_body.json").write_text(json.dumps(body))
    r = subprocess.run(
        ["databricks", "api", "patch", resource_path,
         "--json", "@/tmp/_app_perms_body.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"error": r.stderr[:400]}
    return {"ok": True}


def load_persona_emails() -> dict[str, str]:
    if not PERSONA_EMAILS_PATH.exists():
        raise RuntimeError(
            f"{PERSONA_EMAILS_PATH.relative_to(REPO_ROOT)} not found. "
            f"Run `python3 scripts/setup_persona_users.py` (or the full "
            f"`scripts/setup_all_personas.py`) first."
        )
    return json.loads(PERSONA_EMAILS_PATH.read_text())


# --------------------------------------------------------------------------
# Grants
# --------------------------------------------------------------------------


def grant(warehouse_id: str, label: str, stmt: str) -> bool:
    res = run_sql(warehouse_id, stmt)
    if "error" in res:
        print(f"  ✗ {label}")
        print(f"      → {res['error']}")
        return False
    print(f"  ✓ {label}")
    return True


def patch(label: str, resource_path: str, body: dict[str, Any]) -> bool:
    res = patch_workspace_permission(resource_path, body)
    if "error" in res:
        print(f"  ✗ {label}")
        print(f"      → {res['error']}")
        return False
    print(f"  ✓ {label}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dry-run", action="store_true",
                        help="print the plan; do not apply any grants")
    args = parser.parse_args()

    print(f"DPIA Review app permissions — catalog `{CATALOG}`")
    print("=" * 70)

    # Resolve app + SP
    print("Resolving app...")
    try:
        app = discover_app()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"  app_name      = {app['app_name']}")
    print(f"  sp_principal  = {app['sp_principal']}")

    # Resolve warehouse
    print("\nResolving warehouse...")
    try:
        warehouse_id = discover_warehouse()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(f"  warehouse_id  = {warehouse_id}")

    # Load persona emails
    print("\nLoading persona emails...")
    try:
        persona_emails = load_persona_emails()
    except RuntimeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    persona_grants = {p: persona_emails.get(p) for p in ALLOWED_PERSONAS}
    for p, email in persona_grants.items():
        if email:
            tag = "approver" if p in APPROVER_PERSONAS else "viewer"
            print(f"  {p:<3} ({tag:<8}) = {email}")
        else:
            print(f"  {p:<3} = MISSING — will skip")

    sp = app["sp_principal"]
    plan: list[tuple[str, str, dict | str]] = [
        ("warehouse CAN_USE → SP",
         f"/api/2.0/permissions/warehouses/{warehouse_id}",
         {"access_control_list": [
             {"service_principal_name": sp, "permission_level": "CAN_USE"}
         ]}),
        # USE CATALOG + USE SCHEMA are PREREQUISITES for SELECT and
        # MODIFY to actually be exercised — without them the SP gets
        # PERMISSION_DENIED on `compliance_pack.compliance.dpia_runs` even
        # though the row-level grant exists. UC checks them in order:
        # USE CATALOG → USE SCHEMA → table grant.
        (f"UC USE CATALOG ON CATALOG {CATALOG} → SP",
         "sql",
         f"GRANT USE CATALOG ON CATALOG {CATALOG} TO `{sp}`"),
        (f"UC USE SCHEMA ON SCHEMA {CATALOG}.compliance → SP",
         "sql",
         f"GRANT USE SCHEMA ON SCHEMA {CATALOG}.compliance TO `{sp}`"),
        (f"UC SELECT, MODIFY ON TABLE {CATALOG}.compliance.dpia_runs → SP",
         "sql",
         f"GRANT SELECT, MODIFY ON TABLE {CATALOG}.compliance.dpia_runs TO `{sp}`"),
        (f"UC READ VOLUME ON VOLUME {CATALOG}.compliance.dpia_artifacts → SP",
         "sql",
         f"GRANT READ VOLUME ON VOLUME {CATALOG}.compliance.dpia_artifacts TO `{sp}`"),
    ]
    for persona in ALLOWED_PERSONAS:
        email = persona_grants.get(persona)
        if email:
            plan.append((
                f"app CAN_USE → {persona} ({email})",
                f"/api/2.0/permissions/apps/{app['app_name']}",
                {"access_control_list": [
                    {"user_name": email, "permission_level": "CAN_USE"}
                ]},
            ))

    print(f"\nPlanned grants ({len(plan)}):")
    for label, _, _ in plan:
        print(f"  • {label}")

    if args.dry_run:
        print("\n(dry-run — no grants applied)")
        return 0

    print("\nApplying...")
    failed = 0
    for label, kind, body in plan:
        if kind == "sql":
            ok = grant(warehouse_id, label, body)  # body is the SQL string here
        else:
            ok = patch(label, kind, body)  # kind is the API path
        if not ok:
            failed += 1

    print()
    print("=" * 70)
    if failed == 0:
        print("✓ All grants applied. The DPIA Review app is now fully provisioned.")
        return 0
    print(f"✗ {failed} grant(s) failed. Re-run after fixing — script is idempotent.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
