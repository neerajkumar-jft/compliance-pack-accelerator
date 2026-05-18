"""Approve a DPIA draft.

The DPIA Auto-Generator (Agent 1 in the compliance POC) writes every run
as ``status='draft'``. This CLI is the human-review checkpoint: it flips
a draft row in ``compliance.dpia_runs`` to ``status='approved'``, stamping
the reviewer's email and a server-side timestamp, so the row plus the
artifact JSON together become regulator-grade evidence.

Usage::

    python3 scripts/approve_dpia.py --list-drafts
    python3 scripts/approve_dpia.py --run-id <12-char-hex> --reviewer <email>
    python3 scripts/approve_dpia.py --run-id <id> --reviewer <email> --notes "Q1 2026 sign-off"
    python3 scripts/approve_dpia.py --run-id <id> --reviewer <email> --no-preview

Idempotency
-----------
- Re-running on an already-approved row: prints the existing reviewer +
  timestamp and exits 0 without modifying anything.
- Run_id not in the table: exits 1 with a clear "not found" message.
- Status='superseded': exits 1; superseded means a fresher DPIA replaced
  this one, and approving a superseded run isn't supported via the CLI
  (would require a deliberate manual UPDATE first).

Trust model
-----------
The ``--reviewer`` email is self-asserted on the command line — there's
nothing in the CLI that verifies the person typing the command is the
person whose email they entered. For the POC this is acceptable; the
audit row only proves that *someone with workspace SQL access* ran the
UPDATE. The Phase 4 Databricks App replaces this CLI with a UI that
captures the authenticated Databricks user identity automatically and
removes the trust assumption.

Discovery + auth
----------------
Same patterns as ``scripts/bootstrap_catalog.py``:
- Warehouse picked via ``COMPLIANCE_WAREHOUSE_ID`` env or ``databricks warehouses
  list``. Stopped serverless warehouses are accepted (they auto-start).
- Catalog read from ``COMPLIANCE_CATALOG`` (default ``compliance_pack``).
- Auth via the active Databricks CLI profile.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

CATALOG = os.environ.get("COMPLIANCE_CATALOG", "compliance_pack")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
PREVIEW_CHARS = 1500


def _databricks(*args: str) -> str:
    """Run a databricks CLI command and return stdout. Raises on non-zero."""
    r = subprocess.run(["databricks", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"databricks {' '.join(args)} failed: {r.stderr[:300]}")
    return r.stdout


def discover_warehouse() -> str:
    """Pick a serverless SQL warehouse — running or stopped.

    Mirrors ``scripts/bootstrap_catalog.py::discover_warehouse``. Stopped
    serverless warehouses are fine because they auto-start when the first
    SQL statement hits them; a generous ``wait_timeout`` rides out the
    cold-start.
    """
    if env := os.environ.get("COMPLIANCE_WAREHOUSE_ID"):
        return env
    out = _databricks("warehouses", "list", "-o", "json")
    warehouses = json.loads(out)
    if not warehouses:
        raise RuntimeError(
            "no SQL warehouses found in this workspace. Create one first "
            "(Compute → SQL Warehouses → Create); 'Serverless Starter "
            "Warehouse' is fine."
        )
    running_serverless = [w for w in warehouses
                          if w.get("state") == "RUNNING" and w.get("enable_serverless_compute")]
    running_any = [w for w in warehouses if w.get("state") == "RUNNING"]
    stopped_serverless = [w for w in warehouses if w.get("enable_serverless_compute")]
    pick = (running_serverless or running_any or stopped_serverless or warehouses)[0]
    return pick["id"]


def run_sql(warehouse_id: str, stmt: str) -> dict:
    """Returns ``{state, error, data}``. ``data`` is the data_array on success.

    Generous 50s wait_timeout so the first query rides out a serverless
    cold-start; subsequent queries return quickly.
    """
    payload = {"warehouse_id": warehouse_id, "statement": stmt, "wait_timeout": "50s"}
    Path("/tmp/_approve_dpia_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_approve_dpia_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"state": "ERR", "error": r.stderr[:400], "data": None}
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return {"state": state,
                "error": d.get("status", {}).get("error", {}).get("message", "")[:400],
                "data": None}
    return {"state": "OK", "error": None,
            "data": d.get("result", {}).get("data_array", [])}


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------


def list_drafts(warehouse_id: str) -> int:
    """Print the open draft DPIAs awaiting review."""
    r = run_sql(warehouse_id, f"""
        SELECT run_id,
               generated_at,
               generated_by,
               model_endpoint,
               ROUND(latency_seconds, 2) AS latency_s,
               artifact_path
        FROM {CATALOG}.compliance.dpia_runs
        WHERE status = 'draft'
        ORDER BY generated_at DESC
    """)
    if r["state"] != "OK":
        print(f"✗ query failed: {r['error']}", file=sys.stderr)
        return 1
    rows = r["data"] or []
    if not rows:
        print(f"No draft DPIAs awaiting review in {CATALOG}.compliance.dpia_runs.")
        return 0
    print(f"Draft DPIAs awaiting review ({len(rows)}):")
    print("-" * 110)
    for run_id, ts, by, model, latency, path in rows:
        print(f"  run_id      = {run_id}")
        print(f"  generated   = {ts}  by {by}")
        print(f"  model       = {model}  ({latency}s)")
        print(f"  artifact    = {path}")
        print("-" * 110)
    print()
    print("Approve with:")
    print(f"  python3 scripts/approve_dpia.py --run-id <run_id> --reviewer <your-email>")
    return 0


def fetch_run(warehouse_id: str, run_id: str) -> dict | None:
    """Return the row for a given run_id, or None if no such row exists."""
    # run_id is regex-validated upstream (12 hex chars), so direct
    # interpolation is safe; SQL injection isn't possible from the CLI
    # caller without bypassing the validator.
    r = run_sql(warehouse_id, f"""
        SELECT run_id, generated_at, generated_by, status,
               reviewed_by, reviewed_at, artifact_path,
               model_endpoint, prompt_version
        FROM {CATALOG}.compliance.dpia_runs
        WHERE run_id = '{run_id}'
    """)
    if r["state"] != "OK":
        raise RuntimeError(f"query failed: {r['error']}")
    rows = r["data"] or []
    if not rows:
        return None
    cols = ["run_id", "generated_at", "generated_by", "status",
            "reviewed_by", "reviewed_at", "artifact_path",
            "model_endpoint", "prompt_version"]
    return dict(zip(cols, rows[0]))


def preview_artifact(artifact_path: str) -> None:
    """``databricks fs cat`` the artifact JSON and print a snippet of dpia_text.

    Best-effort: prints a warning and continues if the volume is unreadable
    (which can happen during deploy churn). The reviewer can then decide
    whether to approve based on the row metadata alone.
    """
    try:
        out = _databricks("fs", "cat", f"dbfs:{artifact_path}")
        artifact = json.loads(out)
    except Exception as e:
        print(f"⚠ could not preview artifact at {artifact_path}: {e}", file=sys.stderr)
        return
    text = artifact.get("dpia_text", "")
    if not text:
        print(f"⚠ artifact has no dpia_text field — nothing to preview.", file=sys.stderr)
        return
    print("─" * 80)
    print(f"DPIA PREVIEW (first {PREVIEW_CHARS} chars)")
    print("─" * 80)
    print(text[:PREVIEW_CHARS])
    if len(text) > PREVIEW_CHARS:
        print(f"\n... ({len(text) - PREVIEW_CHARS} more chars in {artifact_path})")
    print("─" * 80)


def approve(warehouse_id: str, run_id: str, reviewer: str, notes: str | None) -> int:
    """Issue the UPDATE. ``run_id`` is regex-validated upstream; reviewer
    + notes are escaped here against the only injection surface (single
    quotes in user-supplied strings)."""
    safe_reviewer = reviewer.replace("'", "''")
    notes_clause = ""
    if notes:
        safe_notes = notes.replace("'", "''")
        notes_clause = f", notes = '{safe_notes}'"
    r = run_sql(warehouse_id, f"""
        UPDATE {CATALOG}.compliance.dpia_runs
        SET status = 'approved',
            reviewed_by = '{safe_reviewer}',
            reviewed_at = current_timestamp(){notes_clause}
        WHERE run_id = '{run_id}' AND status = 'draft'
    """)
    if r["state"] != "OK":
        print(f"✗ UPDATE failed: {r['error']}", file=sys.stderr)
        return 1
    return 0


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        description="Approve a DPIA draft, stamping reviewer identity and timestamp.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python3 scripts/approve_dpia.py --list-drafts\n"
            "  python3 scripts/approve_dpia.py --run-id 7a3f9c2e1b4d --reviewer reviewer@example.com\n"
            "  python3 scripts/approve_dpia.py --run-id 7a3f9c2e1b4d --reviewer reviewer@example.com --notes 'Q1 sign-off'\n"
        ),
    )
    p.add_argument("--run-id", help="12-char hex run_id from compliance.dpia_runs")
    p.add_argument("--reviewer", help="reviewer email — stored in reviewed_by")
    p.add_argument("--notes", help="optional reviewer comments — stored in notes")
    p.add_argument("--no-preview", action="store_true",
                   help="skip the artifact preview before approving")
    p.add_argument("--list-drafts", action="store_true",
                   help="list all draft DPIAs and exit (--run-id and --reviewer ignored)")
    args = p.parse_args()

    warehouse_id = discover_warehouse()

    if args.list_drafts:
        return list_drafts(warehouse_id)

    if not args.run_id or not args.reviewer:
        p.error("--run-id and --reviewer are required (or pass --list-drafts)")

    if not RUN_ID_RE.match(args.run_id):
        print(f"✗ invalid --run-id format (expected 12 hex chars, got {args.run_id!r})",
              file=sys.stderr)
        return 1
    if not EMAIL_RE.match(args.reviewer):
        print(f"✗ invalid --reviewer email: {args.reviewer!r}", file=sys.stderr)
        return 1

    print(f"DPIA approval — run_id={args.run_id} reviewer={args.reviewer}")
    print("=" * 70)

    row = fetch_run(warehouse_id, args.run_id)
    if row is None:
        print(f"✗ run_id {args.run_id} not found in {CATALOG}.compliance.dpia_runs",
              file=sys.stderr)
        print(f"  (Use --list-drafts to see what's available.)", file=sys.stderr)
        return 1

    print(f"  generated_at  = {row['generated_at']}")
    print(f"  generated_by  = {row['generated_by']}")
    print(f"  status        = {row['status']}")
    print(f"  model         = {row['model_endpoint']}")
    print(f"  prompt_ver    = {row['prompt_version']}")
    print(f"  artifact      = {row['artifact_path']}")
    print()

    if row["status"] == "approved":
        print(f"⚠ already approved by {row['reviewed_by']} at {row['reviewed_at']}.")
        print(f"  No changes made (idempotent).")
        return 0
    if row["status"] == "superseded":
        print(f"✗ run is marked 'superseded' — a fresher DPIA has replaced it.",
              file=sys.stderr)
        print(f"  Approving a superseded run is not supported via this CLI.",
              file=sys.stderr)
        return 1
    if row["status"] != "draft":
        print(f"✗ unexpected status {row['status']!r}; expected 'draft'.",
              file=sys.stderr)
        return 1

    if not args.no_preview:
        preview_artifact(row["artifact_path"])
        print()

    rc = approve(warehouse_id, args.run_id, args.reviewer, args.notes)
    if rc != 0:
        return rc

    print(f"✓ run_id {args.run_id} approved by {args.reviewer}.")
    print(f"  status='approved', reviewed_at=now() — see {CATALOG}.compliance.dpia_runs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
