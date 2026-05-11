"""Seed simulated federation data into a `federation_mock` schema + silver views.

Represents the Lakehouse Federation pattern: data lives in an external
system (here, a `federation_mock` schema standing in for a Postgres
marketing DB) and is exposed to the rest of the lakehouse through silver
**views** that select from it. Visible code shape (CREATE VIEW vs CREATE
TABLE) is the demo signal — governance applies regardless of where the
underlying rows live.

Idempotent: CREATE OR REPLACE on tables, CREATE OR REPLACE VIEW on
silver, every run.

Layout produced:

  compliance_pack.federation_mock.lead_scoring        (Delta, 200 rows)
  compliance_pack.federation_mock.campaign_response   (Delta, 100 rows)
  compliance_pack.silver.federation_lead_scoring_tagged       (VIEW)
  compliance_pack.silver.federation_campaign_response_tagged  (VIEW)

Usage:
    python3 scripts/seed_federation_data.py
    python3 scripts/seed_federation_data.py --dry-run
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

from generate_federation_data import generate  # noqa: E402
from persona_config import get_warehouse_id  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
DEFAULT_CATALOG = "compliance_pack"
INSERT_BATCH_SIZE = 25

SCHEMAS = {
    "lead_scoring": [
        ("score_id",            "STRING NOT NULL"),
        ("lead_id",             "STRING"),
        ("email",               "STRING"),
        ("first_name",          "STRING"),
        ("last_name",           "STRING"),
        ("phone",               "STRING"),
        ("company",             "STRING"),
        ("score",               "INT"),
        ("score_band",          "STRING"),
        ("engagement_count",    "INT"),
        ("last_activity_date",  "DATE"),
        ("created_at",          "TIMESTAMP"),
    ],
    "campaign_response": [
        ("response_id",         "STRING NOT NULL"),
        ("lead_id",             "STRING"),
        ("campaign_id",         "STRING"),
        ("campaign_name",       "STRING"),
        ("channel",             "STRING"),
        ("email",               "STRING"),
        ("response_type",       "STRING"),
        ("response_timestamp",  "TIMESTAMP"),
    ],
}

# (foreign table name, silver view name)
TABLES = [
    ("lead_scoring",      "federation_lead_scoring_tagged"),
    ("campaign_response", "federation_campaign_response_tagged"),
]


def run_sql(stmt: str, label: str = "") -> tuple[str, str]:
    payload = {"warehouse_id": WAREHOUSE_ID, "statement": stmt, "wait_timeout": "50s"}
    Path("/tmp/_fed_seed.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_fed_seed.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", r.stderr[:400])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, d.get("status", {}).get("error", {}).get("message", "")[:400])
    return ("OK", "")


def column_def(table: str) -> str:
    return ", ".join(f"`{name}` {ty}" for name, ty in SCHEMAS[table])


def ddl_create_table(catalog: str, table: str) -> str:
    fq = f"{catalog}.federation_mock.{table}"
    return (
        f"CREATE OR REPLACE TABLE {fq} ({column_def(table)}) "
        f"USING DELTA TBLPROPERTIES ('quality' = 'federation_source')"
    )


def ddl_create_view(catalog: str, foreign_table: str, view_name: str) -> str:
    """Silver view that selects from the foreign-mock table.

    In production the underlying source would be a Postgres foreign
    table reached through a UC Connection + foreign catalog. The
    view text is the same shape: SELECT * FROM <foreign_catalog>.<…>.
    """
    fq_view   = f"{catalog}.silver.{view_name}"
    fq_source = f"{catalog}.federation_mock.{foreign_table}"
    return (
        f"CREATE OR REPLACE VIEW {fq_view} AS "
        f"SELECT * FROM {fq_source}"
    )


def _sql_literal(value, sql_type: str) -> str:
    if value is None:
        return "NULL"
    if sql_type.startswith("INT") or sql_type.startswith("DECIMAL"):
        return str(value)
    if sql_type.startswith("DATE"):
        return f"DATE'{value}'"
    if sql_type.startswith("TIMESTAMP"):
        return f"TIMESTAMP'{value}'"
    return "'" + str(value).replace("'", "''") + "'"


def insert_values(catalog: str, table: str, rows: list[dict]) -> list[str]:
    columns = SCHEMAS[table]
    col_list = ", ".join(f"`{name}`" for name, _ in columns)
    fq = f"{catalog}.federation_mock.{table}"
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


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--catalog", default=DEFAULT_CATALOG)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--seed", type=int, default=44)
    args = p.parse_args()

    print(f"Seed federation data → {args.catalog}.federation_mock + silver views")
    print(f"   warehouse: {WAREHOUSE_ID}")
    print("=" * 70)

    payload = generate(seed=args.seed)
    counts = {k: len(v) for k, v in payload.items()}
    print(f"Generated: {counts}\n")

    plan: list[tuple[str, str]] = []

    # 1. Schema
    plan.append(("create schema federation_mock",
                 f"CREATE SCHEMA IF NOT EXISTS {args.catalog}.federation_mock "
                 f"COMMENT 'Stand-in for an externally-federated marketing DB. "
                 f"In production, replace these tables with a UC foreign catalog.'"))

    # 2. CREATE OR REPLACE foreign-mock tables
    for foreign_table, _ in TABLES:
        plan.append((f"create federation_mock.{foreign_table}",
                     ddl_create_table(args.catalog, foreign_table)))

    # 3. INSERT data into foreign-mock tables
    for foreign_table, _ in TABLES:
        rows = payload[foreign_table]
        for i, stmt in enumerate(insert_values(args.catalog, foreign_table, rows), 1):
            plan.append((f"insert federation_mock.{foreign_table} batch {i}", stmt))

    # 4. CREATE OR REPLACE silver views (the federation projection layer)
    for foreign_table, view_name in TABLES:
        plan.append((f"create silver.{view_name} (VIEW)",
                     ddl_create_view(args.catalog, foreign_table, view_name)))

    print(f"Plan: {len(plan)} statements\n")

    if args.dry_run:
        for label, stmt in plan:
            print(f"  • {label:50s} {stmt[:80]}…")
        return 0

    ok = failed = 0
    for label, stmt in plan:
        state, err = run_sql(stmt)
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
        for foreign_table, view_name in TABLES:
            print(f"  SELECT COUNT(*) FROM {args.catalog}.federation_mock.{foreign_table};   -- expect {counts[foreign_table]}")
            print(f"  SELECT COUNT(*) FROM {args.catalog}.silver.{view_name};   -- view passthrough, same count")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
