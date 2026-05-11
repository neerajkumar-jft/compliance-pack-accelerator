"""INT-06 · Unity Catalog column tags applied to PII.

The classification pipeline writes `pii_type`, `pii_category`, and
`sensitivity` as UC column tags on Silver tables. These tags are how
other tools (Catalog Explorer, downstream masks, lineage-aware
discovery) find PII without re-running classification.

This test queries `system.information_schema.column_tags` and asserts:

1. The canonical three tag names are present on Silver tables
2. At least MIN_TAGGED_COLUMNS distinct (table, column) pairs carry
   PII tags (proxy for "classification reached the full source set")
3. Tags exist on each of the 5 expected Silver tables
4. The `compliance_pack.silver.*` tables have tags on typical critical
   columns: aadhaar, pan, email

Note: system.information_schema.column_tags may lag by up to 30s after
ALTER TABLE ... SET TAGS. If this test runs immediately after
re-classification, retry after a short wait.

Run:
    python3 tests/test_uc_tags_applied.py
    python3 tests/test_uc_tags_applied.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sql import rows_or_raise  # noqa: E402

CLASSIFIED_TABLES = {
    "employees_tagged",
    "customers_tagged",
    "patients_tagged",
    "transactions_tagged",
    "users_tagged",
}

EXPECTED_TAG_NAMES = {"pii_type", "pii_category", "sensitivity"}
MIN_TAGGED_COLUMNS = 14
# customers_tagged has aadhaar + pan columns that we'd hit once classifier
# coverage extends; until then, these hints must land on the other tables.
CRITICAL_HINTS = {"aadhaar", "pan", "email"}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    print("INT-06 · UC column tags applied to PII")
    print("=" * 70)

    tag_names_csv = ", ".join(f"'{t}'" for t in EXPECTED_TAG_NAMES)
    rows = rows_or_raise(
        "SELECT table_name, column_name, tag_name, tag_value "
        "FROM system.information_schema.column_tags "
        "WHERE catalog_name = 'compliance_pack' "
        "  AND schema_name = 'silver' "
        f"  AND tag_name IN ({tag_names_csv}) "
        "ORDER BY table_name, column_name, tag_name"
    )

    if args.verbose:
        print(f"\nFound {len(rows)} tag rows on compliance_pack.silver.*:")
        for r in rows[:50]:
            print(f"  {r[0]:25s}  {r[1]:25s}  {r[2]:15s}  {r[3]}")
        if len(rows) > 50:
            print(f"  ... ({len(rows) - 50} more)")

    tagged_columns = {(r[0], r[1]) for r in rows}
    tag_names_seen = {r[2] for r in rows}
    tagged_tables = {r[0] for r in rows}

    checks: list[tuple[str, bool, str]] = []

    # Check 1 — all three expected tag names observed
    missing_names = EXPECTED_TAG_NAMES - tag_names_seen
    checks.append((
        f"Expected tag names present: {sorted(EXPECTED_TAG_NAMES)}",
        not missing_names,
        f"missing: {sorted(missing_names)}" if missing_names else "",
    ))

    # Check 2 — minimum tagged column count
    checks.append((
        f"At least {MIN_TAGGED_COLUMNS} distinct columns carry PII tags",
        len(tagged_columns) >= MIN_TAGGED_COLUMNS,
        f"tagged_columns={len(tagged_columns)}",
    ))

    # Check 3 — all classified source tables have at least one tagged column
    missing_tables = CLASSIFIED_TABLES - tagged_tables
    checks.append((
        f"All {len(CLASSIFIED_TABLES)} classified source tables have tagged columns",
        not missing_tables,
        f"missing: {sorted(missing_tables)}" if missing_tables else "",
    ))

    # Check 4 — critical-PII hints are tagged somewhere
    tagged_column_names = {col.lower() for _, col in tagged_columns}
    missing_hints = {
        hint for hint in CRITICAL_HINTS
        if not any(hint in c for c in tagged_column_names)
    }
    checks.append((
        f"Critical PII hints tagged at least once: {sorted(CRITICAL_HINTS)}",
        not missing_hints,
        f"missing: {sorted(missing_hints)}" if missing_hints else "",
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
    if passed != len(checks):
        print("\nHint: tag propagation can lag 10–30s after ALTER TABLE ... SET TAGS.")
        print("      If you just re-ran classification, wait 30s and retry.")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
