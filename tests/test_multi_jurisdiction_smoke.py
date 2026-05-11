"""Live-workspace smoke test for the multi-jurisdiction routing model (ADR-0001 M4).

This test asserts that the live workspace is actually doing per-data-subject
rule routing — not just that the code compiles. Runs against the active
Databricks workspace via the SQL Statement Execution API; expects
phase1_bootstrap to have been run after the M4 cut-over.

What's asserted:
  - silver.customers_tagged carries the `jurisdiction` column with both
    IN and GB values present (the M2 70/25/5 mix).
  - bronze.compliance_rules contains rules from BOTH dpdp_2023 and uk_gdpr
    (M2 multi-pack loader).
  - silver.compliance_gaps contains gaps tagged with regulation_pack from
    both packs (M2 multi-pack gap engine).
  - Retention semantics differ per pack: pack_for('IN') returns 730d
    marketing retention; pack_for('GB') returns 90d (loaded via the
    pack_loader, not from the workspace).

Run pre- or post-deploy. If the workspace hasn't been cut over to M2+ yet,
the test fails clearly identifying which assertion couldn't be satisfied.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from governance_core.pack_loader import pack_for, reset_cache  # noqa: E402
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()


def _sql(stmt: str, timeout_s: str = "50s") -> list[list]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt,
               "wait_timeout": timeout_s}
    path = Path("/tmp/_m4_smoke_sql.json")
    path.write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", f"@{path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"SQL API failed: {r.stderr[:300]}")
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "?")
    if state != "SUCCEEDED":
        err = d.get("status", {}).get("error", {}).get("message", "")[:300]
        raise RuntimeError(f"SQL state={state}: {err}")
    return d.get("result", {}).get("data_array", []) or []


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def _check(label: str, condition: bool, detail: str = "") -> bool:
    icon = "✓" if condition else "✗"
    print(f"  {icon} {label}" + (f" — {detail}" if detail else ""))
    return condition


def main() -> int:
    _section(f"M4 smoke — multi-jurisdiction routing (warehouse {WAREHOUSE_ID})")
    reset_cache()

    passed = 0
    failed = 0

    # 1. customers_tagged has both IN and GB jurisdiction values
    rows = _sql(
        "SELECT jurisdiction, COUNT(*) FROM dpdp_poc.silver.customers_tagged "
        "GROUP BY jurisdiction ORDER BY 2 DESC"
    )
    jur_mix = {r[0]: int(r[1]) for r in rows}
    in_count = jur_mix.get("IN", 0)
    gb_count = jur_mix.get("GB", 0)
    if _check(f"silver.customers_tagged has IN principals (count={in_count})",
              in_count >= 100):
        passed += 1
    else:
        failed += 1
    if _check(f"silver.customers_tagged has GB principals (count={gb_count})",
              gb_count >= 100):
        passed += 1
    else:
        failed += 1

    # 2. Multi-pack rules loaded into compliance_rules
    rows = _sql(
        "SELECT regulation_pack, COUNT(*) FROM dpdp_poc.bronze.compliance_rules "
        "GROUP BY regulation_pack"
    )
    rule_mix = {r[0]: int(r[1]) for r in rows}
    dpdp_rules = rule_mix.get("dpdp_2023", 0)
    uk_rules = rule_mix.get("uk_gdpr", 0)
    if _check(f"bronze.compliance_rules has DPDP rules (count={dpdp_rules})",
              dpdp_rules >= 9):
        passed += 1
    else:
        failed += 1
    if _check(f"bronze.compliance_rules has UK GDPR rules (count={uk_rules})",
              uk_rules >= 8):
        passed += 1
    else:
        failed += 1

    # 3. Gaps are tagged with regulation_pack from both packs
    rows = _sql(
        "SELECT regulation_pack, COUNT(*) FROM dpdp_poc.silver.compliance_gaps "
        "GROUP BY regulation_pack ORDER BY 2 DESC"
    )
    gap_mix = {r[0]: int(r[1]) for r in rows}
    dpdp_gaps = gap_mix.get("dpdp_2023", 0)
    uk_gaps = gap_mix.get("uk_gdpr", 0)
    if _check(f"silver.compliance_gaps tagged with dpdp_2023 (count={dpdp_gaps})",
              dpdp_gaps >= 50):
        passed += 1
    else:
        failed += 1
    if _check(f"silver.compliance_gaps tagged with uk_gdpr (count={uk_gaps})",
              uk_gaps >= 50):
        passed += 1
    else:
        failed += 1

    # 4. Per-jurisdiction retention semantics — pack-loader-side assertion,
    #    proves the architecture's per-row decision rule.
    in_pack = pack_for("IN")
    gb_pack = pack_for("GB")
    if _check("pack_for('IN') resolves to dpdp_2023",
              in_pack is not None and in_pack.code == "dpdp_2023",
              f"got {in_pack.code if in_pack else None!r}"):
        passed += 1
    else:
        failed += 1
    if _check("pack_for('GB') resolves to uk_gdpr",
              gb_pack is not None and gb_pack.code == "uk_gdpr",
              f"got {gb_pack.code if gb_pack else None!r}"):
        passed += 1
    else:
        failed += 1

    in_marketing = in_pack.retention_default("marketing_email") if in_pack else -1
    gb_marketing = gb_pack.retention_default("marketing_email") if gb_pack else -1
    if _check(
        f"DPDP marketing retention = 730d (IN principals), got {in_marketing}",
        in_marketing == 730,
    ):
        passed += 1
    else:
        failed += 1
    if _check(
        f"UK GDPR marketing retention = 90d (GB principals), got {gb_marketing}",
        gb_marketing == 90,
    ):
        passed += 1
    else:
        failed += 1
    if _check(
        f"Per-jurisdiction divergence proven: {in_marketing}d (IN) vs {gb_marketing}d (GB)",
        in_marketing != gb_marketing,
    ):
        passed += 1
    else:
        failed += 1

    print()
    print("=" * 70)
    if failed:
        print(f"FAIL · {failed}/{passed + failed} checks failed")
        return 1
    print(f"OK · {passed}/{passed + failed} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
