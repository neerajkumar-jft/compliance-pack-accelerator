"""Apply Unity Catalog column tags based on silver.pii_findings.

For each row in `compliance_pack.silver.pii_findings`, runs
`ALTER TABLE ... ALTER COLUMN ... SET TAGS (pii_type=..., pii_category=...,
sensitivity=...)` against the corresponding silver table.

Idempotent: SET TAGS overwrites existing values; a re-run is safe.

Background: DLT can't apply column tags from inside a flow (SET TAGS is
DDL), so the classifier writes findings to pii_findings and this script
projects them onto UC. Run once per pipeline refresh, or after dropping
+ re-materializing silver tables (which wipes tags).

Usage:
    python3 scripts/apply_uc_tags.py
    python3 scripts/apply_uc_tags.py --dry-run
    python3 scripts/apply_uc_tags.py --verbose
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

WAREHOUSE_ID = get_warehouse_id()
CATALOG = "compliance_pack"
SCHEMA = "silver"


def sql(stmt: str) -> tuple[str, list, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_uc_tags_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_uc_tags_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", [], r.stderr[:300])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        msg = d.get("status", {}).get("error", {}).get("message", "")[:300]
        return (state, [], msg)
    return ("OK", d.get("result", {}).get("data_array", []) or [], "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    state, rows, err = sql(
        f"SELECT table_name, column_name, pii_type, pii_category, sensitivity_tier "
        f"FROM {CATALOG}.{SCHEMA}.pii_findings"
    )
    if state != "OK":
        print(f"error reading pii_findings: [{state}] {err}", file=sys.stderr)
        return 1

    print(f"apply_uc_tags: {len(rows)} findings → ALTER COLUMN SET TAGS")
    if args.dry_run:
        print("(dry-run; no statements executed)")

    applied = 0
    failed = 0
    for r in rows:
        table, column, pii_type, pii_category, sensitivity = r
        stmt = (
            f"ALTER TABLE {CATALOG}.{SCHEMA}.{table} "
            f"ALTER COLUMN {column} SET TAGS ("
            f"'pii_type' = '{pii_type}', "
            f"'pii_category' = '{pii_category}', "
            f"'sensitivity' = '{sensitivity}')"
        )
        if args.verbose or args.dry_run:
            print(f"  {table}.{column}  ({pii_type}, {sensitivity})")
        if args.dry_run:
            continue
        s, _, e = sql(stmt)
        if s == "OK":
            applied += 1
        else:
            failed += 1
            print(f"  FAILED  {table}.{column}: {e[:150]}", file=sys.stderr)

    if args.dry_run:
        return 0

    print(f"applied: {applied}  failed: {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
