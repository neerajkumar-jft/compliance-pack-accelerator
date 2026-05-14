"""Apply Unity Catalog grants to the 4 persona demo users.

Each persona user gets:
  - USE_CATALOG on compliance_pack
  - USE_SCHEMA on every schema whose tables it needs
  - SELECT on the allowlisted tables for that persona

Why emails, not groups? This trial workspace isn't identity-federated
to its account metastore, so UC rejects workspace-group principals
(including the default `users` group). User emails are accepted. If
account-level groups are created later (at accounts.cloud.databricks.com),
swap `PERSONA_EMAILS` for `PERSONA_GROUPS` and the SQL is identical.

Grants are the same UC scoping declared in each Genie space's
data_sources. Because UC enforces at query time, a viewer on the CCO
dashboard as the CMO user gets permission-denied on CCO-only tables —
regardless of dashboard ACL mistakes.

Idempotent: re-applying grants is a no-op. Safe to re-run after editing
the allowlist.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_config import get_catalog, get_warehouse_id  # noqa: E402

CATALOG = get_catalog()
WAREHOUSE_ID = get_warehouse_id()

# Same domain-scoped allowlists as scripts/setup_persona_genie_spaces.py.
# These are the tables the persona's Genie agent is allowed to query —
# i.e. the agent boundary. Keep in sync with the Genie setup script.
PERSONA_TABLES: dict[str, list[str]] = {
    "cco": [
        "compliance_pack.compliance.personal_data_register",
        "compliance_pack.silver.pii_findings",
        # AI-PII surfaces (added when pii_ai_scan landed) — CCO needs full
        # visibility into both regex and AI inventories. The UNION view
        # `pii_findings_all` is the day-to-day query surface; the underlying
        # `pii_findings_ai` and `pii_ai_scan_row_state` enable drill-down
        # for audit (per-row classification history, model + run metadata).
        "compliance_pack.silver.pii_findings_all",
        "compliance_pack.silver.pii_findings_ai",
        "compliance_pack.compliance.pii_ai_scan_row_state",
        "compliance_pack.silver.compliance_gaps",
        "compliance_pack.silver.discovered_tables",
        "compliance_pack.gold.consent_coverage_summary",
        # Lakeflow Connect simulation (silver tables)
        "compliance_pack.silver.sf_leads_tagged",
        "compliance_pack.silver.sf_contacts_tagged",
        "compliance_pack.silver.sf_accounts_tagged",
        # Federation simulation — silver views + their federation_mock backing
        # tables (UC requires SELECT on both layers when the view's underlying
        # rows are queried at runtime, depending on runtime behavior).
        "compliance_pack.silver.federation_lead_scoring_tagged",
        "compliance_pack.silver.federation_campaign_response_tagged",
        "compliance_pack.federation_mock.lead_scoring",
        "compliance_pack.federation_mock.campaign_response",
    ],
    "gc": [
        "compliance_pack.silver.compliance_gaps",
        "compliance_pack.compliance.consent_events_log",
        "compliance_pack.compliance.notice_versions",
        "compliance_pack.compliance.dsr_requests",
        # GC owns DPDP §10 sign-off — DPIA history is the artifact they
        # approve. Kept in sync with the Genie data_sources allowlist in
        # scripts/setup_persona_genie_spaces.py PERSONA_DEFS['gc']['tables'].
        "compliance_pack.compliance.dpia_runs",
    ],
    "cmo": [
        "compliance_pack.gold.marketing_eligible_principals",
        "compliance_pack.compliance.consent_events_log",
    ],
    "cfo": [
        "compliance_pack.silver.compliance_gaps",
        "compliance_pack.silver.discovered_tables",
    ],
}

# Shared aggregate views referenced by the Executive Overview tiles on
# every persona's dashboard (risk_score, compliance_score, sensitivity
# histogram, top-line counts). Strictly narrower than the raw
# pii_findings / compliance_gaps tables — the views expose only
# pre-aggregated numbers, no per-column PII metadata.
#
# This replaces the prior SHARED_OVERVIEW_TABLES = [pii_findings,
# compliance_gaps]. Non-CCO personas no longer have SELECT on the raw
# tables via the shared list; CCO still has them via its own
# PERSONA_TABLES (domain) and GC/CFO still have compliance_gaps via
# their own domain. The leakage flagged in docs/persona_governance.md
# (CMO/GC/CFO could SELECT pii_type, source_column FROM pii_findings)
# is closed for CMO entirely and for GC/CFO on pii_findings.
SHARED_OVERVIEW_TABLES: list[str] = [
    "compliance_pack.gold.persona_overview_metrics",
    "compliance_pack.gold.persona_sensitivity_histogram",
]

EMAILS_FILE = REPO_ROOT / "dashboards" / "personas" / ".persona_emails.json"


def run_sql(statement: str) -> dict:
    payload = {
        "warehouse_id": WAREHOUSE_ID,
        "statement": statement,
        "wait_timeout": "30s",
    }
    payload_path = Path("/tmp/_grant_sql.json")
    payload_path.write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return {"error": r.stderr[:500]}
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state")
    if state != "SUCCEEDED":
        return {"error": d.get("status", {}).get("error", {}).get("message", "")[:500]}
    return {"ok": True}


def grant(statement: str) -> None:
    res = run_sql(statement)
    if "error" in res:
        print(f"  FAILED: {statement}")
        print(f"          → {res['error']}")
    else:
        print(f"  ok: {statement}")


def main() -> int:
    if not EMAILS_FILE.exists():
        print(f"error: {EMAILS_FILE} not found — create persona users first", file=sys.stderr)
        return 1
    persona_emails = json.loads(EMAILS_FILE.read_text())

    for persona, domain_tables in PERSONA_TABLES.items():
        email = persona_emails.get(persona)
        if not email:
            print(f"[{persona}] no email mapping, skipping")
            continue
        grantee = f"`{email}`"
        print(f"\n=== {persona} → {email} ===")

        # Merge domain-scoped + shared-overview tables. Dedup + sort.
        all_tables = sorted(set(domain_tables) | set(SHARED_OVERVIEW_TABLES))

        # Catalog
        grant(f"GRANT USE CATALOG ON CATALOG {CATALOG} TO {grantee}")

        # Schemas (dedup across both lists)
        schemas = sorted({t.split(".")[1] for t in all_tables})
        for schema in schemas:
            grant(f"GRANT USE SCHEMA ON SCHEMA {CATALOG}.{schema} TO {grantee}")

        # Tables
        for table in all_tables:
            grant(f"GRANT SELECT ON TABLE {table} TO {grantee}")

    print("\nDone. To verify (as admin):")
    print(f"  SHOW GRANTS TO `{persona_emails['cco']}`;")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
