"""Regenerate silver.compliance_gaps from silver.pii_findings_all.

Mirrors the gap-generation logic in `pipelines/phase1_bootstrap.py`
section 4 (the (finding × rule) cross-product), but as a standalone
SQL-API script so deploy_all.sh can re-trigger gap generation AFTER
the AI scan has populated `silver.pii_findings_ai`.

Why this exists:
  phase1_bootstrap runs early in the deploy chain (right after the
  medallion). At that point pii_findings_ai is empty (the AI scan
  hasn't run yet), so the gaps generated from pii_findings_all only
  reflect regex findings. Then pii_ai_first_run populates AI
  findings — but compliance_gaps is now stale.

  Rather than re-running the full phase1_bootstrap (which is heavy:
  re-seeds 1000 consent events, rebuilds gold views, etc.), this
  script does ONLY the gap regeneration: TRUNCATE compliance_gaps +
  INSERT from the (pii_findings_all × compliance_rules) cross-join.

  Usage in deploy_all.sh: scheduled AFTER pii_ai_first_run and BEFORE
  dpia_first_run, so the seed DPIA's gap analysis reflects the full
  inventory (regex + AI).

Idempotent: TRUNCATE+INSERT, safe to re-run any time.

Usage:
    python3 scripts/refresh_compliance_gaps.py
    python3 scripts/refresh_compliance_gaps.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
CATALOG = "compliance_pack"


def sql(stmt: str) -> tuple[str, list, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "50s"}
    Path("/tmp/_refresh_gaps_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_refresh_gaps_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", [], r.stderr[:300])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        msg = d.get("status", {}).get("error", {}).get("message", "")[:400]
        return (state, [], msg)
    return ("OK", d.get("result", {}).get("data_array", []) or [], "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="Print the SQL + counts that would change, do not mutate.")
    args = p.parse_args()

    # Pre-flight: confirm pii_findings_all is queryable + count current state
    state, rows, err = sql(f"""
        SELECT
          (SELECT COUNT(*) FROM {CATALOG}.silver.pii_findings_all) AS findings_total,
          (SELECT COUNT(*) FROM {CATALOG}.silver.pii_findings_all WHERE classifier_source='ai_classify') AS ai_findings,
          (SELECT COUNT(*) FROM {CATALOG}.silver.compliance_gaps) AS gaps_before
    """)
    if state != "OK":
        print(f"error: pre-flight query failed: [{state}] {err}", file=sys.stderr)
        return 1
    findings_total, ai_findings, gaps_before = rows[0]
    print(f"refresh_compliance_gaps:")
    print(f"  pii_findings_all rows:      {findings_total}")
    print(f"  of which AI findings:       {ai_findings}")
    print(f"  compliance_gaps before:     {gaps_before}")

    if args.dry_run:
        print("(dry-run; no statements executed)")
        return 0

    scan_job_id = f"gap_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    # SQL Statements API calls are independent sessions — TEMP VIEW from
    # one call isn't visible in the next. Inline the SELECT into the
    # INSERT so it runs as one statement.

    # Step 1: TRUNCATE (idempotent — keeps gaps deterministic in row count)
    state, _, err = sql(f"TRUNCATE TABLE {CATALOG}.silver.compliance_gaps")
    if state != "OK":
        print(f"error: TRUNCATE failed: [{state}] {err}", file=sys.stderr)
        return 1

    # Step 2: INSERT directly from the cross-product (mirrors phase1_bootstrap section 4)
    state, _, err = sql(f"""
        INSERT INTO {CATALOG}.silver.compliance_gaps
        SELECT
            uuid() AS gap_id,
            '{scan_job_id}' AS scan_job_id,
            f.table_name,
            f.column_name,
            f.pii_type,
            f.pii_category,
            r.rule_id,
            r.rule_type,
            r.severity,
            r.regulations[0] AS regulation,
            r.description,
            r.remediation,
            current_timestamp() AS detected_at,
            r.regulation_pack
        FROM {CATALOG}.silver.pii_findings_all f
        CROSS JOIN {CATALOG}.bronze.compliance_rules r
        WHERE r.is_active = true
          AND array_contains(r.applicable_categories, f.pii_category)
    """)
    if state != "OK":
        print(f"error: INSERT failed: [{state}] {err}", file=sys.stderr)
        return 1

    # Step 3: report
    state, rows, err = sql(f"SELECT COUNT(*) FROM {CATALOG}.silver.compliance_gaps")
    if state != "OK":
        print(f"error: COUNT failed: [{state}] {err}", file=sys.stderr)
        return 1
    gaps_after = int(rows[0][0])
    print(f"  compliance_gaps after:      {gaps_after} (Δ {gaps_after - int(gaps_before):+d})")
    print(f"  scan_job_id:                {scan_job_id}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
