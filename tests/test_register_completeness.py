"""INT-01 · Register completeness.

Asserts the personal-data register is populated end-to-end from
classification through the register view:

1. `silver.pii_findings` has rows (at any confidence) for each
   CLASSIFIED_TABLES entry
2. Total finding count is within a sanity band
3. `compliance.personal_data_register` view returns rows
4. Register view covers every CLASSIFIED_TABLES entry

KNOWN GAP: `customers_tagged` is currently not scanned by the DLT
classifier (see discovered_tables). Until the classifier is fixed to
cover it, CLASSIFIED_TABLES lists only the 4 tables that are actually
classified. Add `customers_tagged` back once the classifier sweep
includes it.

Run:
    python3 tests/test_register_completeness.py
    python3 tests/test_register_completeness.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sql import rows_or_raise  # noqa: E402

ALL_SOURCE_TABLES = {
    "employees_tagged",
    "customers_tagged",
    "patients_tagged",
    "transactions_tagged",
    "users_tagged",
}
# Classifier covers all 5 source tables after the 2026-04-23 pipeline
# rebuild (medallion.py schema fixes + phase1_bootstrap restoration).
CLASSIFIED_TABLES = ALL_SOURCE_TABLES

MIN_TOTAL_FINDINGS = 20
MAX_TOTAL_FINDINGS = 200


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    print("INT-01 · Register completeness")
    print("=" * 70)

    checks: list[tuple[str, bool, str]] = []

    # Check 1 — each classified source table present in pii_findings
    rows = rows_or_raise(
        "SELECT table_name, COUNT(*) "
        "FROM compliance_pack.silver.pii_findings "
        "GROUP BY table_name"
    )
    seen = {r[0]: int(r[1]) for r in rows}
    missing = CLASSIFIED_TABLES - seen.keys()
    if args.verbose:
        print("\n[1] pii_findings per-table:")
        for t in sorted(ALL_SOURCE_TABLES):
            note = "" if t in CLASSIFIED_TABLES else "  (not in classifier sweep — known gap)"
            print(f"    {t:26s} {seen.get(t, 0)}{note}")
    checks.append((
        f"All {len(CLASSIFIED_TABLES)} classified source tables have findings",
        not missing,
        f"missing: {sorted(missing)}" if missing else "",
    ))

    # Check 2 — total findings in sanity band
    total = sum(seen.values())
    checks.append((
        f"Total PII findings in sanity band [{MIN_TOTAL_FINDINGS}, {MAX_TOTAL_FINDINGS}]",
        MIN_TOTAL_FINDINGS <= total <= MAX_TOTAL_FINDINGS,
        f"total={total}",
    ))

    # Check 3 — register view is populated
    rv = rows_or_raise(
        "SELECT COUNT(*), COUNT(DISTINCT source_table) "
        "FROM compliance_pack.compliance.personal_data_register"
    )
    register_rows = int(rv[0][0]) if rv else 0
    register_tables = int(rv[0][1]) if rv else 0
    if args.verbose:
        print(f"\n[3] personal_data_register: rows={register_rows}, distinct source_tables={register_tables}")
    checks.append((
        "compliance.personal_data_register view returns rows",
        register_rows > 0,
        f"rows={register_rows}",
    ))
    checks.append((
        f"Register view covers all {len(CLASSIFIED_TABLES)} classified source tables",
        register_tables >= len(CLASSIFIED_TABLES),
        f"distinct source_tables={register_tables}",
    ))

    # Report
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
    if ALL_SOURCE_TABLES - CLASSIFIED_TABLES:
        print("Known gap: classifier does not cover "
              f"{sorted(ALL_SOURCE_TABLES - CLASSIFIED_TABLES)}; tracked separately.")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
