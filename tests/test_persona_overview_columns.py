"""Column-contract test for gold.persona_overview_metrics.

Dashboards created by `scripts/slice_dashboards.py` embed SQL that reads
named columns from `compliance_pack.gold.persona_overview_metrics`. If the view
in `pipelines/phase1_bootstrap.py` drops or renames one of those columns,
the CFO/CMO/GC persona dashboards render
`[UNRESOLVED_COLUMN] column <name> cannot be resolved` on their
Executive Overview tiles — and nothing in the current test suite catches
it (persona_boundary_test asserts grants, not view shape).

This test asserts that every column name the slicer references is
actually present in the live view. Update `REQUIRED_COLUMNS` if the
slicer's rewrites are extended in `DATASET_REWRITES_NON_CCO`.

Source of the expected column list:
    scripts/slice_dashboards.py :: DATASET_REWRITES_NON_CCO

Run:
    python3 tests/test_persona_overview_columns.py
    python3 tests/test_persona_overview_columns.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sql import rows_or_raise  # noqa: E402

VIEW = "compliance_pack.gold.persona_overview_metrics"

# The column names any non-CCO dashboard tile may read via the slicer.
# Derived mechanically from DATASET_REWRITES_NON_CCO in slice_dashboards.py —
# grep for SELECT targets + fully-qualified FROM compliance_pack.gold.persona_overview_metrics.
REQUIRED_COLUMNS = {
    # risk_scores tile
    "risk_score",
    "compliance_score",
    "risk_level",
    # last_scan_info tile
    "last_scan_time",
    "days_since_last_scan",
    # executive_summary tile
    "total_tables",
    "pii_columns",
    "critical_pii",
    "high_pii",
    "avg_confidence",
    "total_gaps",
    "critical_gaps",
    # high_risk_tables tile
    # (pii_columns, critical_pii, high_pii already in the set)
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    print("persona_overview_metrics — column contract with slice_dashboards.py")
    print("=" * 70)

    rows = rows_or_raise(f"DESCRIBE {VIEW}")
    actual = {r[0] for r in rows if not r[0].startswith("#")}

    if args.verbose:
        print(f"\nLive columns ({len(actual)}):")
        for c in sorted(actual):
            flag = " ← required by slicer" if c in REQUIRED_COLUMNS else ""
            print(f"  {c}{flag}")

    missing = REQUIRED_COLUMNS - actual
    extra_required_not_present = REQUIRED_COLUMNS - actual

    checks: list[tuple[str, bool, str]] = []
    checks.append((
        f"All {len(REQUIRED_COLUMNS)} slicer-referenced columns are present in {VIEW}",
        not missing,
        f"missing: {sorted(missing)}" if missing else "",
    ))

    # Also assert the view is queryable end-to-end with the exact SQL the slicer emits.
    slicer_queries = [
        ("risk_scores", f"SELECT risk_score, compliance_score, risk_level FROM {VIEW}"),
        ("last_scan_info", (
            f"SELECT 'aggregate' AS last_scan_job_id, last_scan_time, "
            f"1 AS total_scans, days_since_last_scan FROM {VIEW}"
        )),
        ("executive_summary", (
            f"SELECT total_tables AS tables_scanned, pii_columns AS total_pii_columns, "
            f"critical_pii, high_pii AS high_sensitivity_pii, avg_confidence, "
            f"total_gaps, critical_gaps FROM {VIEW}"
        )),
        ("high_risk_tables", (
            f"SELECT 'x' AS full_table_name, pii_columns, critical_pii AS critical_count, "
            f"high_pii AS high_count, "
            f"CAST((critical_pii*3 + high_pii*2) AS INT) AS risk_score FROM {VIEW}"
        )),
    ]
    for tile_name, q in slicer_queries:
        try:
            rows_or_raise(q)
            query_ok = True
            err = ""
        except RuntimeError as e:
            query_ok = False
            err = str(e)[:180]
        checks.append((
            f"Slicer query for tile '{tile_name}' runs without error",
            query_ok, err,
        ))

    print()
    passed = 0
    for name, ok, detail in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name}")
        if not ok and detail:
            print(f"      {detail}")
        if ok:
            passed += 1

    print("\n" + "=" * 70)
    print(f"Summary: {passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
