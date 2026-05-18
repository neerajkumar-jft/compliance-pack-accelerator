"""Seed `bronze.data_sources` with the 10 canonical ingestion sources.

Runs BEFORE the medallion-pipeline refresh in deploy_all.sh so the
classifier (`pipelines/classification_dlt.py:_resolve_silver_tables`)
can resolve all 10 silver objects from `data_sources` instead of
falling back to its 5-table default list.

Why this is its own step (and not just inside phase1_bootstrap.py):

  Order in a fresh deploy is:
    medallion (creates 5 silver tables)  →  sf seed  →  federation seed
    →  REFRESH  →  phase1_bootstrap

  At REFRESH time, the classifier re-imports and runs
  `_resolve_silver_tables()`. If `data_sources` hasn't been seeded yet
  it falls back to the 5-table list, so the SF + federation silver
  objects (already created by the seeders) are skipped — pii_findings
  ends with 20 rows, not 36. Phase1_bootstrap can't seed data_sources
  early enough because it depends on pii_findings already being
  populated (compliance_gaps = rules × pii_findings).

  This script is the surgical extraction: just create + seed the
  registry, idempotently. Phase1_bootstrap.py keeps its own
  CREATE/ALTER/MERGE in §2 + §2.5 as belt-and-suspenders for
  workspaces that haven't run this script (legacy / partial deploys).

Idempotent — safe to run repeatedly:
  - CREATE TABLE IF NOT EXISTS
  - ALTER TABLE ADD COLUMNS (catches FIELDS_ALREADY_EXIST)
  - MERGE on source_id (UPDATE existing, INSERT new)

Usage:
    python3 scripts/seed_data_sources.py
    python3 scripts/seed_data_sources.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
CATALOG = os.environ.get("COMPLIANCE_CATALOG", "compliance_pack")

# DDL — mirrors schemas/bronze.sql + the CREATE in phase1_bootstrap.py §2.
CREATE_TABLE = f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.bronze.data_sources (
    source_id           STRING    NOT NULL,
    source_name         STRING    NOT NULL,
    source_type         STRING    NOT NULL,
    ingestion_pattern   STRING    NOT NULL,
    catalog_name        STRING,
    schema_name         STRING,
    landing_volume_path STRING,
    owner_email         STRING,
    is_active           BOOLEAN   NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    silver_table_name   STRING    COMMENT 'Silver-layer table or view that mirrors this source. Classifier scans this column.',
    primary_key_column  STRING    COMMENT 'Primary-key column on silver_table_name; required by the AI scan per-row state join.'
) USING DELTA
""".strip()

# Backwards-compat for workspaces deployed before each column landed. Each
# entry runs independently so a partial-migration workspace converges.
ALTER_ADD_COLUMNS = [
    (
        "silver_table_name",
        f"ALTER TABLE {CATALOG}.bronze.data_sources "
        f"ADD COLUMNS (silver_table_name STRING COMMENT "
        f"'Silver table or view the classifier scans for this source.')",
    ),
    (
        "primary_key_column",
        f"ALTER TABLE {CATALOG}.bronze.data_sources "
        f"ADD COLUMNS (primary_key_column STRING COMMENT "
        f"'Primary-key column on silver_table_name; required by the AI scan per-row state join.')",
    ),
]

# 10 canonical sources — same set phase1_bootstrap §2.5 seeds.
DATA_SOURCES_SEED = [
    # (source_id, source_name, source_type, ingestion_pattern, schema_name, landing_path_or_None, silver_table_name, primary_key_column)
    ("src_employees",         "Employees (HR master)",      "hr_master",            "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/employees",     "employees_tagged",                    "employee_id"),
    ("src_customers",         "Customers (CRM master)",     "crm",                  "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/customers",     "customers_tagged",                    "customer_id"),
    ("src_patients",          "Patients (health records)",  "health",               "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/patients",      "patients_tagged",                     "patient_id"),
    ("src_transactions",      "Transactions (ledger)",      "financial",            "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/transactions",  "transactions_tagged",                 "transaction_id"),
    ("src_users",             "Users (application)",        "application",          "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/users",         "users_tagged",                        "user_id"),
    ("src_sf_leads",          "Salesforce Leads",           "crm_external",         "direct_write",    "bronze",          None,                                               "sf_leads_tagged",                     "lead_id"),
    ("src_sf_contacts",       "Salesforce Contacts",        "crm_external",         "direct_write",    "bronze",          None,                                               "sf_contacts_tagged",                  "contact_id"),
    ("src_sf_accounts",       "Salesforce Accounts",        "crm_external",         "direct_write",    "bronze",          None,                                               "sf_accounts_tagged",                  "account_id"),
    ("src_lead_scoring",      "Lead Scoring (Postgres federation)",      "marketing_attribution", "federation_view", "federation_mock", None, "federation_lead_scoring_tagged",      "score_id"),
    ("src_campaign_response", "Campaign Response (Postgres federation)", "marketing_attribution", "federation_view", "federation_mock", None, "federation_campaign_response_tagged", "response_id"),
]


def run_sql(stmt: str, label: str = "") -> tuple[str, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "30s"}
    Path("/tmp/_seed_ds.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_seed_ds.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", r.stderr[:300])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, d.get("status", {}).get("error", {}).get("message", "")[:400])
    return ("OK", "")


def _sql_lit(value, sql_type: str = "STRING") -> str:
    if value is None:
        return "NULL"
    if sql_type == "BOOLEAN":
        return "TRUE" if value else "FALSE"
    if sql_type == "TIMESTAMP":
        return f"TIMESTAMP'{value}'"
    return "'" + str(value).replace("'", "''") + "'"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    print(f"Seed bronze.data_sources — catalog `{CATALOG}` (warehouse {WAREHOUSE_ID})")
    print("=" * 70)

    # 1. Create table (idempotent)
    if args.dry_run:
        print("  ⟳ would: CREATE TABLE IF NOT EXISTS bronze.data_sources")
    else:
        state, err = run_sql(CREATE_TABLE)
        if state != "OK":
            print(f"  ✗ CREATE TABLE: {state}\n      → {err}")
            return 1
        print("  ✓ table ready")

    # 2. Add new columns on legacy workspaces (catch FIELDS_ALREADY_EXIST)
    if args.dry_run:
        print(f"  ⟳ would: ALTER TABLE … ADD COLUMNS ({', '.join(c for c, _ in ALTER_ADD_COLUMNS)}) — fine if already present")
    else:
        for col_name, alter_sql in ALTER_ADD_COLUMNS:
            state, err = run_sql(alter_sql)
            if state == "OK":
                print(f"  ✓ {col_name} column added (legacy workspace)")
            elif state == "FAILED" and ("already exists" in err.lower() or "fields_already_exist" in err.lower()):
                print(f"  · {col_name} column already present — no-op")
            else:
                print(f"  ✗ ALTER ADD COLUMNS ({col_name}): {state}\n      → {err}")
                return 1

    # 3. MERGE the 10 canonical rows
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    values = []
    for r in DATA_SOURCES_SEED:
        sid, name, src_type, pattern, schema, landing, silver, pk = r
        values.append(
            "(" + ", ".join([
                _sql_lit(sid),
                _sql_lit(name),
                _sql_lit(src_type),
                _sql_lit(pattern),
                _sql_lit(CATALOG),
                _sql_lit(schema),
                _sql_lit(landing),
                _sql_lit("compliance-pack-team@example.com"),
                _sql_lit(True, "BOOLEAN"),
                _sql_lit(now, "TIMESTAMP"),
                _sql_lit(now, "TIMESTAMP"),
                _sql_lit(silver),
                _sql_lit(pk),
            ]) + ")"
        )
    merge = (
        f"MERGE INTO {CATALOG}.bronze.data_sources AS t "
        f"USING (SELECT * FROM VALUES " + ",\n  ".join(values) + " "
        f"AS s(source_id, source_name, source_type, ingestion_pattern, "
        f"catalog_name, schema_name, landing_volume_path, owner_email, "
        f"is_active, created_at, updated_at, silver_table_name, primary_key_column)) AS s "
        f"ON t.source_id = s.source_id "
        f"WHEN MATCHED THEN UPDATE SET "
        f"  source_name = s.source_name, source_type = s.source_type, "
        f"  ingestion_pattern = s.ingestion_pattern, catalog_name = s.catalog_name, "
        f"  schema_name = s.schema_name, landing_volume_path = s.landing_volume_path, "
        f"  owner_email = s.owner_email, is_active = s.is_active, "
        f"  updated_at = s.updated_at, silver_table_name = s.silver_table_name, "
        f"  primary_key_column = s.primary_key_column "
        f"WHEN NOT MATCHED THEN INSERT *"
    )
    if args.dry_run:
        print(f"  ⟳ would: MERGE {len(DATA_SOURCES_SEED)} rows into bronze.data_sources")
        return 0

    state, err = run_sql(merge)
    if state != "OK":
        print(f"  ✗ MERGE: {state}\n      → {err}")
        return 1
    print(f"  ✓ MERGE complete — {len(DATA_SOURCES_SEED)} canonical rows")

    # 4. Verify
    state, err = run_sql(f"SELECT COUNT(*) FROM {CATALOG}.bronze.data_sources WHERE is_active = true AND silver_table_name IS NOT NULL")
    print(f"\n  Active rows with silver_table_name: see workspace (verification query ran {state})")

    print()
    print("=" * 70)
    print(f"✓ {len(DATA_SOURCES_SEED)} sources seeded; classifier will pick them up on next pipeline update")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
