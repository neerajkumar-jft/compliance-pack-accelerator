"""DSR end-to-end test — chains dsr_discovery → dsr_erasure (dry-run) → audit-bundle assertions.

Non-destructive by default: uses the documented test principal
``customer_04217`` (Oeshi Desai) and runs the erasure half in count-only
mode so the demo data survives. The actual --confirm erasure is a
separate manual demo step in the runbook; this test verifies the chain
works without consuming the principal.

What this guards against (per gap 4.3):

  - Discovery finds the right principal across the right tables
  - The bundle is well-formed (audit evidence shape contract)
  - Erasure's count agrees with discovery (no off-by-one between the
    two scripts' table sets)
  - compliance.dsr_requests is writable (request-id INSERT works on a
    throwaway record that we then DELETE)
  - Persona-table-list parity — both scripts iterate the same set of
    silver/compliance tables for the principal

Run:
    python3 tests/test_dsr_e2e.py
    python3 tests/test_dsr_e2e.py --verbose
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "tests"))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from _sql import sql, rows_or_raise  # noqa: E402
from dsr_discovery import discover_principal, DEFAULT_PRINCIPAL_TABLES  # noqa: E402
from dsr_erasure import count_rows, ERASABLE_TABLES                     # noqa: E402

# Documented DSR test principal (CLAUDE.md, README, 06_synthetic_data.md).
# 1 customer row + 20 transactions + 4 consent events = 25 PII-bearing rows.
PRINCIPAL_ID = "customer_04217"
MIN_TOTAL_ROWS = 15  # slack for synthetic-data regen (post-rename baseline: 18 for customer_04217)


def _has_row_count(scan_results: list, table_suffix: str) -> int | None:
    """Return row_count from scan_results entry whose table FQN ends with `table_suffix`."""
    for entry in scan_results:
        if entry.get("table", "").endswith(table_suffix) and entry.get("state") == "OK":
            return entry.get("row_count", 0)
    return None


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    print(f"DSR e2e — principal {PRINCIPAL_ID}")
    print("=" * 70)

    checks: list[tuple[str, bool, str]] = []

    # 1. Discovery returns a well-formed bundle
    bundle = discover_principal(PRINCIPAL_ID, verbose=args.verbose)
    required_keys = {"principal_id", "discovered_at", "catalog", "scan_results", "total_matching_rows"}
    missing = required_keys - bundle.keys()
    checks.append((
        f"Discovery bundle has all required keys {sorted(required_keys)}",
        not missing,
        f"missing: {sorted(missing)}" if missing else "",
    ))

    # 2. Bundle is JSON-serializable (audit evidence contract — must persist as JSON)
    try:
        json.dumps(bundle, default=str)
        json_ok = True; json_err = ""
    except Exception as e:
        json_ok = False; json_err = str(e)[:200]
    checks.append((
        "Bundle is JSON-serializable (audit-evidence contract)",
        json_ok,
        json_err,
    ))

    # 3. Discovery finds the principal in expected tables
    n_customers     = _has_row_count(bundle["scan_results"], "customers_tagged")
    n_transactions  = _has_row_count(bundle["scan_results"], "transactions_tagged")
    n_consent       = _has_row_count(bundle["scan_results"], "consent_events_log")
    checks.append((
        "Discovery: ≥1 customers_tagged row for principal",
        n_customers is not None and n_customers >= 1,
        f"got {n_customers}",
    ))
    checks.append((
        "Discovery: ≥12 transactions_tagged rows for principal",
        n_transactions is not None and n_transactions >= 12,
        f"got {n_transactions}",
    ))
    checks.append((
        "Discovery: ≥1 consent_events_log row for principal",
        n_consent is not None and n_consent >= 1,
        f"got {n_consent}",
    ))

    # 4. total_matching_rows agrees with the per-table sum
    expected_total = sum(
        e.get("row_count", 0) for e in bundle["scan_results"] if e.get("state") == "OK"
    )
    checks.append((
        f"Discovery total_matching_rows == sum of per-table counts (expected={expected_total})",
        bundle["total_matching_rows"] == expected_total,
        f"bundle.total={bundle['total_matching_rows']}",
    ))
    checks.append((
        f"Discovery total ≥ {MIN_TOTAL_ROWS}",
        expected_total >= MIN_TOTAL_ROWS,
        f"got {expected_total}",
    ))

    # 5. Erasure dry-run agrees with discovery (no off-by-one between table sets)
    erasure_counts = count_rows(PRINCIPAL_ID)  # {fq_name: row_count}
    erasure_total = sum(c for c in erasure_counts.values() if c >= 0)
    # Erasure may scan a SUBSET of the discovery tables (some tables are
    # PRESERVE_TABLES — kept for legal retention). Total erasure ≤ discovery.
    checks.append((
        "Erasure dry-run total ≤ discovery total (preserve list respected)",
        erasure_total <= bundle["total_matching_rows"],
        f"erasure={erasure_total} vs discovery={bundle['total_matching_rows']}",
    ))
    checks.append((
        f"Erasure dry-run reports ≥1 row to delete",
        erasure_total >= 1,
        f"got {erasure_total}",
    ))

    # 6. Persona-table-list parity — every ERASABLE_TABLES entry maps to
    #    a discovery table (catches drift between the two scripts).
    discovery_fqs = {e["table"] for e in bundle["scan_results"]}
    drift = []
    catalog = bundle["catalog"]
    for schema, table, _col in ERASABLE_TABLES:
        fq = f"{catalog}.{schema}.{table}"
        if fq not in discovery_fqs:
            drift.append(fq)
    checks.append((
        "Every erasure-target table is also a discovery target (no drift)",
        not drift,
        f"erasure-only tables: {drift}" if drift else "",
    ))

    # 7. compliance.dsr_requests is writable — proves the erasure audit path
    #    can record a row. Uses a throwaway request_id and DELETEs immediately.
    test_req = f"smoke_dsr_{uuid.uuid4().hex[:8]}"
    insert_stmt = (
        f"INSERT INTO compliance_pack.compliance.dsr_requests "
        f"(request_id, data_principal_id, request_type, status, submitted_at, "
        f" sla_deadline, requester_email, requester_language, created_at, updated_at) "
        f"VALUES ('{test_req}', '{PRINCIPAL_ID}', 'erasure', 'completed', "
        f" CURRENT_TIMESTAMP(), DATE_ADD(CURRENT_DATE(), 30), "
        f" 'test@example.com', 'en-IN', CURRENT_TIMESTAMP(), CURRENT_TIMESTAMP())"
    )
    state, _, err = sql(insert_stmt)
    insert_ok = state == "OK"
    if insert_ok:
        sql(f"DELETE FROM compliance_pack.compliance.dsr_requests WHERE request_id = '{test_req}'")
    checks.append((
        "compliance.dsr_requests accepts an erasure-audit row INSERT",
        insert_ok,
        err if not insert_ok else "",
    ))

    if args.verbose:
        print(f"\n  Discovery scan tables ({len(DEFAULT_PRINCIPAL_TABLES)}):")
        for entry in bundle["scan_results"]:
            print(f"    - {entry['table']}: {entry.get('row_count', '?')} rows")
        print(f"\n  Erasure-target counts:")
        for fq, n in erasure_counts.items():
            print(f"    - {fq}: {n}")

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
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
