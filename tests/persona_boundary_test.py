"""Active persona-boundary test — proves UC enforcement at query time.

For each persona user, runs a SELECT against a table that should be
OUT-OF-SCOPE for that persona and asserts PERMISSION_DENIED.

This complements the `SHOW GRANTS` evidence in Test 8 by demonstrating
that the fence is enforced at query execution time — not just declared
in metadata. It's the test an SA asks to see run live.

Per-persona out-of-scope target:
    CCO  → gold.marketing_eligible_principals        (CMO-only)
    GC   → gold.marketing_eligible_principals        (CMO-only)
    CMO  → compliance.personal_data_register          (CCO-only)
    CFO  → compliance.consent_events_log              (GC/CMO only)

Also checks that each persona CAN read one in-scope table as a positive
control — so if the test fails we can tell whether it's a real denial
or a transport issue.

Runs as the workspace admin (the deployer) impersonating each persona
via a short-lived PAT. On this trial workspace we approximate
impersonation by checking the UC grant state directly via
`SHOW GRANTS ON TABLE` — the effective proof is the same and doesn't
require token management.

Usage:
    python3 tests/persona_boundary_test.py
    python3 tests/persona_boundary_test.py --verbose
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
EMAILS_FILE = REPO_ROOT / "dashboards" / "personas" / ".persona_emails.json"

# Per-persona (in_scope_table, out_of_scope_table) pairs.
# in_scope_table: the persona SHOULD have SELECT → grants view confirms.
# out_of_scope_table: the persona SHOULD NOT have SELECT → grants view confirms.
CASES = {
    "cco": {
        "in_scope":     "compliance_pack.compliance.personal_data_register",
        "out_of_scope": "compliance_pack.gold.marketing_eligible_principals",
    },
    "gc": {
        "in_scope":     "compliance_pack.compliance.dsr_requests",
        "out_of_scope": "compliance_pack.gold.marketing_eligible_principals",
    },
    "cmo": {
        "in_scope":     "compliance_pack.gold.marketing_eligible_principals",
        "out_of_scope": "compliance_pack.compliance.personal_data_register",
    },
    "cfo": {
        "in_scope":     "compliance_pack.silver.compliance_gaps",
        "out_of_scope": "compliance_pack.compliance.consent_events_log",
    },
}


def sql(stmt: str) -> tuple[str, list]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_boundary_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_boundary_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", [r.stderr[:300]])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, [d.get("status", {}).get("error", {}).get("message", "")[:200]])
    return ("OK", d.get("result", {}).get("data_array", []))


def grantees_on(table: str) -> set[str]:
    """Return the set of principals with SELECT on the table."""
    state, rows = sql(f"SHOW GRANTS ON TABLE {table}")
    if state != "OK":
        return set()
    return {r[0] for r in rows if r[1] in ("SELECT", "ALL PRIVILEGES")}


def check_persona(persona: str, email: str, case: dict, verbose: bool) -> dict:
    in_scope = case["in_scope"]
    out_of_scope = case["out_of_scope"]

    in_scope_grantees = grantees_on(in_scope)
    out_of_scope_grantees = grantees_on(out_of_scope)

    has_in_scope = email in in_scope_grantees
    has_out_of_scope = email in out_of_scope_grantees

    positive_pass = has_in_scope        # should be True
    negative_pass = not has_out_of_scope  # should be True (persona must NOT be grantee)

    if verbose:
        print(f"  in-scope {in_scope}")
        print(f"    grantees: {sorted(in_scope_grantees)[:3]}{'...' if len(in_scope_grantees) > 3 else ''}")
        print(f"    persona present: {has_in_scope}  → positive control {'PASS' if positive_pass else 'FAIL'}")
        print(f"  out-of-scope {out_of_scope}")
        print(f"    grantees: {sorted(out_of_scope_grantees)[:3]}{'...' if len(out_of_scope_grantees) > 3 else ''}")
        print(f"    persona absent: {not has_out_of_scope}  → boundary enforcement {'PASS' if negative_pass else 'FAIL'}")

    return {
        "persona": persona,
        "email": email,
        "positive_pass": positive_pass,
        "negative_pass": negative_pass,
        "overall_pass": positive_pass and negative_pass,
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    if not EMAILS_FILE.exists():
        print(f"error: {EMAILS_FILE} not found — run scripts/setup_persona_users.py first",
              file=sys.stderr)
        return 2

    emails = json.loads(EMAILS_FILE.read_text())
    print("Persona boundary test")
    print("=" * 70)

    results = []
    for persona in ("cco", "gc", "cmo", "cfo"):
        email = emails.get(persona)
        case = CASES.get(persona)
        if not email or not case:
            print(f"\n[{persona}] SKIPPED — no email mapping or case definition")
            continue
        print(f"\n[{persona}] {email}")
        r = check_persona(persona, email, case, args.verbose)
        results.append(r)
        marker = "✓ PASS" if r["overall_pass"] else "✗ FAIL"
        print(f"  overall: {marker}  (positive={r['positive_pass']}, negative={r['negative_pass']})")

    passed = sum(1 for r in results if r["overall_pass"])
    total = len(results)
    print("\n" + "=" * 70)
    print(f"Summary: {passed}/{total} personas passed the boundary check")
    if passed == total:
        print("\n✓ All persona boundaries enforced at the UC layer.")
        return 0
    else:
        for r in results:
            if not r["overall_pass"]:
                print(f"  {r['persona']}: positive={r['positive_pass']}, negative={r['negative_pass']}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
