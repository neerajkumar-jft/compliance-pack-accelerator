"""Seed synthetic Salesforce data into bronze + silver UC tables.

Represents what Lakeflow Connect ingestion delivers in production: typed
Delta tables landing directly in the catalog without an Auto Loader
streaming pipeline. The classifier (registered separately in
``pipelines/classification_dlt.py``) scans the silver layer and produces
findings, identical treatment to the file-arrival sources.

Idempotent: every run does CREATE OR REPLACE on the 6 tables. Re-running
this against a freshly-deployed catalog or a populated one is safe — the
data is regenerated deterministically from seed=43.

Usage:
    python3 scripts/seed_salesforce_data.py
    python3 scripts/seed_salesforce_data.py --dry-run
    python3 scripts/seed_salesforce_data.py --catalog compliance_pack
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from generate_salesforce_data import generate  # noqa: E402
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
DEFAULT_CATALOG = "compliance_pack"
INSERT_BATCH_SIZE = 25  # keeps each INSERT statement under ~30KB

# ---------------------------------------------------------------------------
# Table schemas — bronze + silver share the same shape
# ---------------------------------------------------------------------------
SCHEMAS = {
    "sf_leads": [
        ("lead_id",        "STRING NOT NULL"),
        ("first_name",     "STRING"),
        ("last_name",      "STRING"),
        ("email",          "STRING"),
        ("phone",          "STRING"),
        ("mobile",         "STRING"),
        ("company",        "STRING"),
        ("industry",       "STRING"),
        ("title",          "STRING"),
        ("lead_status",    "STRING"),
        ("lead_source",    "STRING"),
        ("lead_score",     "INT"),
        ("annual_revenue", "DECIMAL(15,2)"),
        ("num_employees",  "INT"),
        ("city",           "STRING"),
        ("state",          "STRING"),
        ("country",        "STRING"),
        ("postal_code",    "STRING"),
        ("aadhaar",        "STRING"),
        ("pan",            "STRING"),
        ("created_date",   "DATE"),
    ],
    "sf_contacts": [
        ("contact_id",          "STRING NOT NULL"),
        ("account_id",          "STRING"),
        ("first_name",          "STRING"),
        ("last_name",           "STRING"),
        ("email",               "STRING"),
        ("phone",               "STRING"),
        ("mobile",              "STRING"),
        ("title",               "STRING"),
        ("mailing_city",        "STRING"),
        ("mailing_state",       "STRING"),
        ("mailing_country",     "STRING"),
        ("mailing_postal_code", "STRING"),
        ("aadhaar",             "STRING"),
        ("pan",                 "STRING"),
        ("date_of_birth",       "DATE"),
        ("ifsc",                "STRING"),
        ("created_date",        "DATE"),
    ],
    "sf_accounts": [
        ("account_id",         "STRING NOT NULL"),
        ("name",                "STRING"),
        ("industry",            "STRING"),
        ("annual_revenue",      "DECIMAL(15,2)"),
        ("num_employees",       "INT"),
        ("billing_city",        "STRING"),
        ("billing_state",       "STRING"),
        ("billing_country",     "STRING"),
        ("billing_postal_code", "STRING"),
        ("company_pan",         "STRING"),
        ("gst_number",          "STRING"),
        ("primary_phone",       "STRING"),
        ("website",             "STRING"),
        ("created_date",        "DATE"),
    ],
}

# Map generator object key → (bronze table, silver table, schema key)
TABLES = {
    "leads":    ("sf_leads",    "sf_leads_tagged",    "sf_leads"),
    "contacts": ("sf_contacts", "sf_contacts_tagged", "sf_contacts"),
    "accounts": ("sf_accounts", "sf_accounts_tagged", "sf_accounts"),
}


# ---------------------------------------------------------------------------
# Subprocess wrapper around `databricks api post /api/2.0/sql/statements`
# ---------------------------------------------------------------------------

def run_sql(stmt: str, label: str = "") -> tuple[str, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "50s"}
    Path("/tmp/_sf_seed.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_sf_seed.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", r.stderr[:400])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, d.get("status", {}).get("error", {}).get("message", "")[:400])
    return ("OK", "")


# ---------------------------------------------------------------------------
# DDL + DML helpers
# ---------------------------------------------------------------------------

def column_def(schema_key: str) -> str:
    return ", ".join(f"`{name}` {ty}" for name, ty in SCHEMAS[schema_key])


def ddl_create(catalog: str, schema_layer: str, tbl: str, schema_key: str) -> str:
    fq = f"{catalog}.{schema_layer}.{tbl}"
    return (
        f"CREATE OR REPLACE TABLE {fq} ({column_def(schema_key)}) "
        f"USING DELTA TBLPROPERTIES ('quality' = '{schema_layer}')"
    )


def _sql_literal(value, sql_type: str) -> str:
    """Render a Python value as a SQL literal matching the column type."""
    if value is None:
        return "NULL"
    if sql_type.startswith("INT") or sql_type.startswith("DECIMAL"):
        return str(value)
    if sql_type.startswith("DATE"):
        return f"DATE'{value}'"
    # default: string-ish — escape single quotes
    return "'" + str(value).replace("'", "''") + "'"


def insert_values(catalog: str, schema_layer: str, tbl: str, schema_key: str,
                  rows: list[dict]) -> list[str]:
    """Return one INSERT statement per batch of INSERT_BATCH_SIZE rows."""
    columns = SCHEMAS[schema_key]
    col_list = ", ".join(f"`{name}`" for name, _ in columns)
    fq = f"{catalog}.{schema_layer}.{tbl}"
    statements: list[str] = []
    for start in range(0, len(rows), INSERT_BATCH_SIZE):
        batch = rows[start:start + INSERT_BATCH_SIZE]
        values_clauses = []
        for row in batch:
            literals = ", ".join(_sql_literal(row.get(name), ty) for name, ty in columns)
            values_clauses.append(f"({literals})")
        statements.append(
            f"INSERT INTO {fq} ({col_list}) VALUES " + ",\n".join(values_clauses)
        )
    return statements


def silver_promote(catalog: str, bronze_tbl: str, silver_tbl: str) -> str:
    return (
        f"INSERT INTO {catalog}.silver.{silver_tbl} "
        f"SELECT * FROM {catalog}.bronze.{bronze_tbl}"
    )


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", default=DEFAULT_CATALOG)
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan; do not execute SQL")
    p.add_argument("--seed", type=int, default=43)
    args = p.parse_args()

    print(f"Seed Salesforce data → {args.catalog} (warehouse {WAREHOUSE_ID})")
    print("=" * 70)

    payload = generate(seed=args.seed)
    counts = {k: len(v) for k, v in payload.items()}
    print(f"Generated: {counts}\n")

    plan: list[tuple[str, str]] = []

    # 1. CREATE OR REPLACE — 3 bronze + 3 silver
    for obj_key, (bronze_tbl, silver_tbl, schema_key) in TABLES.items():
        plan.append((f"create bronze.{bronze_tbl}",
                     ddl_create(args.catalog, "bronze", bronze_tbl, schema_key)))
        plan.append((f"create silver.{silver_tbl}",
                     ddl_create(args.catalog, "silver", silver_tbl, schema_key)))

    # 2. Insert into bronze (batched)
    for obj_key, (bronze_tbl, _silver_tbl, schema_key) in TABLES.items():
        rows = payload[obj_key]
        for i, stmt in enumerate(insert_values(args.catalog, "bronze",
                                                bronze_tbl, schema_key, rows), 1):
            plan.append((f"insert bronze.{bronze_tbl} batch {i}", stmt))

    # 3. Promote bronze → silver (single SELECT per table)
    for obj_key, (bronze_tbl, silver_tbl, _schema_key) in TABLES.items():
        plan.append((f"promote → silver.{silver_tbl}",
                     silver_promote(args.catalog, bronze_tbl, silver_tbl)))

    print(f"Plan: {len(plan)} statements\n")

    if args.dry_run:
        for label, stmt in plan:
            head = stmt.replace("\n", " ")[:100]
            print(f"  • {label:40s} {head}…")
        return 0

    ok = failed = 0
    for label, stmt in plan:
        state, err = run_sql(stmt, label=label)
        marker = "✓" if state == "OK" else "✗"
        print(f"  {marker} {state:10s} {label}")
        if state == "OK":
            ok += 1
        else:
            failed += 1
            print(f"      → {err}")

    print("\n" + "=" * 70)
    print(f"{ok} succeeded, {failed} failed")
    if failed == 0:
        print("\nVerification queries:")
        for obj_key, (bronze_tbl, silver_tbl, _) in TABLES.items():
            print(f"  SELECT COUNT(*) FROM {args.catalog}.bronze.{bronze_tbl};   -- expect {counts[obj_key]}")
            print(f"  SELECT COUNT(*) FROM {args.catalog}.silver.{silver_tbl};   -- expect {counts[obj_key]}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
