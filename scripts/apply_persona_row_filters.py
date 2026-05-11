"""Apply persona-aware Unity Catalog row filters.

Runs each statement in `schemas/persona_row_filters.sql` sequentially via
the SQL statements API. Idempotent: CREATE OR REPLACE FUNCTION + ALTER
TABLE ... SET ROW FILTER can be re-applied safely.

Policy on `compliance_pack.compliance.consent_events_log`:
    admin / CCO / GC / CFO personas → see every row
    CMO persona                     → see only notice_language='en-IN'
    any other user                  → see every row (grant-gated separately)

See schemas/persona_row_filters.sql for the full rationale and the
migration path to account-level groups.

Usage:
    python3 scripts/apply_persona_row_filters.py
    python3 scripts/apply_persona_row_filters.py --dry-run
    python3 scripts/apply_persona_row_filters.py --verbose
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SQL_FILE = REPO_ROOT / "schemas" / "persona_row_filters.sql"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()


def split_statements(sql: str) -> list[str]:
    """Split on top-level ';'. The one CREATE FUNCTION body here is a
    simple RETURN expression, so no semicolons inside it — a naive split
    is safe. Keeps the splitter consistent with apply_pii_masks.py."""
    # Drop blank/comment-only lines so we don't end up with empty statements.
    cleaned_lines = []
    for line in sql.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        cleaned_lines.append(line)
    cleaned = "\n".join(cleaned_lines)
    parts = [p.strip() for p in cleaned.split(";")]
    return [re.sub(r"\s+", " ", p) for p in parts if p]


def sql(stmt: str) -> tuple[str, list, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_row_filter_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_row_filter_sql.json"],
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

    raw = SQL_FILE.read_text()
    statements = split_statements(raw)
    print(f"apply_persona_row_filters: {len(statements)} statement(s) from {SQL_FILE.name}")
    if args.dry_run:
        for i, s in enumerate(statements, 1):
            print(f"  [{i}] {s[:120]}{'...' if len(s) > 120 else ''}")
        return 0

    ok = 0
    failed = 0
    for i, stmt in enumerate(statements, 1):
        state, _, err = sql(stmt)
        head = stmt[:80] + ("..." if len(stmt) > 80 else "")
        if state == "OK":
            ok += 1
            if args.verbose:
                print(f"  [{i}] ✓ OK         {head}")
        else:
            failed += 1
            print(f"  [{i}] ✗ {state:<10} {head}")
            print(f"        error: {err[:200]}")

    print(f"\n{ok} succeeded, {failed} failed")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
