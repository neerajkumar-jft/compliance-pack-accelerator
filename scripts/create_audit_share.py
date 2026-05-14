"""Create a Delta Share that exposes read-only audit evidence to
external auditors without granting them workspace access.

Idempotent: uses CREATE SHARE IF NOT EXISTS + ALTER SHARE ... ADD
{TABLE,VIEW} which errors harmlessly if the object is already in the
share. To remove an object, use ALTER SHARE ... REMOVE {TABLE,VIEW}.

Share contents:
    - compliance_pack.compliance.personal_data_register (view)
      Living PII register — what personal data we hold, where, and why
    - compliance_pack.silver.compliance_gaps (table)
      All detected compliance gaps with severity and remediation guidance
    - compliance_pack.compliance.notice_versions (table)
      Every consent notice ever presented to principals (multi-language)
    - compliance_pack.gold.consent_coverage_summary (view)
      Consent grant rates per purpose, aggregate — no individual PII

Not included (intentionally):
    - consent_events_log — individual-level; share an aggregated view only
    - pii_findings — detection metadata includes redacted samples; keep internal
    - pii_findings_ai / pii_findings_all / pii_ai_scan_row_state — same
      reasoning: each row carries `sample_match_redacted` (truncated value)
      + per-row classification labels. AI findings reach auditors through
      the `personal_data_register` view (already shared), which exposes the
      column-level inventory but not raw samples or per-row classifications.
    - system.access.audit — workspace-managed, share via CLEAN_ROOM if needed

Usage:
    python3 scripts/create_audit_share.py
    python3 scripts/create_audit_share.py --drop       # tear down
    python3 scripts/create_audit_share.py --list       # show current state
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from persona_config import get_warehouse_id  # noqa: E402

SHARE_NAME = "dpdp_audit_view_share"
WAREHOUSE_ID = get_warehouse_id()

SHARE_OBJECTS = [
    ("VIEW",  "compliance_pack.compliance.personal_data_register"),
    ("TABLE", "compliance_pack.silver.compliance_gaps"),
    ("TABLE", "compliance_pack.compliance.notice_versions"),
    ("VIEW",  "compliance_pack.gold.consent_coverage_summary"),
]


def sql(stmt: str) -> tuple[str, list | str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_share_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_share_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", r.stderr[:300])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, d.get("status", {}).get("error", {}).get("message", "")[:300])
    return ("OK", d.get("result", {}).get("data_array", []))


def run_create() -> int:
    state, _ = sql(
        f"CREATE SHARE IF NOT EXISTS {SHARE_NAME} "
        f"COMMENT 'Read-only audit bundle for external reviewers — "
        f"PII register, compliance gaps, notice versions, consent summary.'"
    )
    print(f"  {'✓' if state == 'OK' else '✗'} CREATE SHARE: {state}")

    for kind, obj in SHARE_OBJECTS:
        state, detail = sql(f"ALTER SHARE {SHARE_NAME} ADD {kind} {obj}")
        if state == "OK":
            print(f"  ✓ ADD {kind:5s} {obj}")
        elif "already exists" in str(detail).lower():
            print(f"  ✓ ADD {kind:5s} {obj} (already present)")
        else:
            print(f"  ✗ ADD {kind:5s} {obj}: {state} — {detail}")

    state, rows = sql(f"SHOW ALL IN SHARE {SHARE_NAME}")
    if state == "OK":
        print(f"\nShare contents ({len(rows)} objects):")
        for r in rows:
            print(f"  {r[1]:5s}  {r[2]}")
    return 0


def run_drop() -> int:
    state, _ = sql(f"DROP SHARE IF EXISTS {SHARE_NAME}")
    print(f"  {'✓' if state == 'OK' else '✗'} DROP SHARE: {state}")
    return 0


def run_list() -> int:
    state, rows = sql(f"SHOW ALL IN SHARE {SHARE_NAME}")
    if state != "OK":
        print(f"  share not found or unreadable: {rows}")
        return 1
    print(f"Share {SHARE_NAME}: {len(rows)} objects")
    for r in rows:
        print(f"  {r[1]:5s}  {r[2]}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--drop", action="store_true", help="tear down the share")
    p.add_argument("--list", action="store_true", help="show current share contents")
    args = p.parse_args()
    if args.drop:
        return run_drop()
    if args.list:
        return run_list()
    return run_create()


if __name__ == "__main__":
    raise SystemExit(main())
