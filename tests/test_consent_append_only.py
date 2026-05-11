"""INT-05 · Consent log append-only invariant.

The consent events log is the single tamper-evident record the platform
produces. DPDP §6 (consent) and §18 (audit trail) require that consent
grants/withdrawals cannot be modified after the fact — corrections are
themselves new events that supersede earlier ones.

Design nuance documented in T2.5 (docs/how_to_test.html): a withdrawal
lands as a NEW append event, and the ORIGINAL grant event is updated
to set its `superseded_by_event_id` pointer. That single-row UPDATE
scoped to one event_id is the only UPDATE pattern the design allows.
Mass UPDATEs or any DELETE indicate tampering.

Checks:
  1. Table exists and has history
  2. No DELETE operations ever (unconditional)
  3. Any UPDATE operations are scoped to a single event_id (supersession
     pattern), not mass updates

Run:
    python3 tests/test_consent_append_only.py
    python3 tests/test_consent_append_only.py --verbose
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _sql import rows_or_raise  # noqa: E402

TABLE = "compliance_pack.compliance.consent_events_log"

# An UPDATE is "supersession-scoped" if its predicate targets event_id.
# Example predicate from Delta history:
#   {"predicate":"[\"(event_id#50357 = evt_000000)\"]"}
SUPERSESSION_PREDICATE = re.compile(r"event_id[^=]*=\s*evt_\d+", re.IGNORECASE)


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    print("INT-05 · Consent log append-only invariant")
    print("=" * 70)

    rows = rows_or_raise(
        f"SELECT version, timestamp, operation, operationParameters "
        f"FROM (DESCRIBE HISTORY {TABLE}) "
        "ORDER BY version ASC"
    )

    if args.verbose:
        print(f"\nHistory of {TABLE}:")
        for r in rows:
            print(f"  v{r[0]:>3}  {r[1]}  {r[2]}")

    deletes: list[tuple[str, str]] = []
    unscoped_updates: list[tuple[str, str, str]] = []
    scoped_updates = 0
    for r in rows:
        version, ts = str(r[0]), str(r[1])
        op = (r[2] or "").upper()
        params = r[3] or ""
        if op == "DELETE":
            deletes.append((version, ts))
        elif op == "UPDATE":
            if SUPERSESSION_PREDICATE.search(params):
                scoped_updates += 1
            else:
                unscoped_updates.append((version, ts, params[:120]))

    table_present = len(rows) > 0

    print()
    print(f"  {'✓' if table_present else '✗'} Table {TABLE} exists and has history "
          f"({len(rows)} operations)")
    if not table_present:
        print("      history empty — phase1_bootstrap has not run")
        print("\nSummary: 0/3 checks passed")
        return 1

    ok_no_deletes = not deletes
    print(f"  {'✓' if ok_no_deletes else '✗'} No DELETE operations in history")
    for v, ts in deletes:
        print(f"      VIOLATION v{v} @ {ts}: DELETE")

    ok_scoped = not unscoped_updates
    print(f"  {'✓' if ok_scoped else '✗'} All UPDATEs are supersession-scoped "
          f"(single event_id predicate); scoped_updates={scoped_updates}")
    for v, ts, pred in unscoped_updates:
        print(f"      VIOLATION v{v} @ {ts}: UPDATE with unscoped predicate: {pred}")

    passed = int(table_present) + int(ok_no_deletes) + int(ok_scoped)
    print("\n" + "=" * 70)
    print(f"Summary: {passed}/3 checks passed")
    return 0 if passed == 3 else 1


if __name__ == "__main__":
    raise SystemExit(main())
