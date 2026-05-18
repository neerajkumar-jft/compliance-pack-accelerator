"""Idempotent UC bootstrap — first step on a fresh-workspace deploy.

Default-Storage workspaces reject ``CREATE CATALOG``/``SCHEMA``/``VOLUME``
issued via the DAB API (the resource blocks in
``resources/catalog_and_storage.yml`` are commented out for that reason).
This script does the same set of CREATE-IF-NOT-EXISTS via the SQL
statements API, so a teammate cloning the repo onto a virgin workspace
no longer has to hand-run them.

What it creates (or no-ops if already present):

    catalog  · compliance_pack
    schemas  · compliance_pack.{bronze, silver, compliance, gold}
    volumes  · compliance_pack.bronze.landing       (Auto Loader source CSVs)
             · compliance_pack.bronze.checkpoints   (Auto Loader checkpoint state)
             · compliance_pack.compliance.dsr_bundles    (DSR audit JSON)
             · compliance_pack.compliance.dpia_artifacts (DPIA generation artifacts)

(``federation_mock`` schema is created later by ``seed_federation_data.py``.)

Wired in as the ``bootstrap`` step at the head of ``scripts/deploy_all.sh``.

Usage:
    python3 scripts/bootstrap_catalog.py
    python3 scripts/bootstrap_catalog.py --dry-run
    COMPLIANCE_WAREHOUSE_ID=<id> python3 scripts/bootstrap_catalog.py
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CATALOG = os.environ.get("COMPLIANCE_CATALOG", "compliance_pack")

# Order matters: catalog → schemas → volumes.
STATEMENTS: list[tuple[str, str]] = [
    ("catalog",
     f"CREATE CATALOG IF NOT EXISTS {CATALOG} "
     f"COMMENT 'Compliance Pack POC. Covers Modules 01 (PII inventory) and 02 (consent).'"),
    ("schema bronze",
     f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.bronze "
     f"COMMENT 'Raw ingested layer — Auto Loader sources + ingestion-source registry'"),
    ("schema silver",
     f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.silver "
     f"COMMENT 'Typed, classified, governed — pii_findings, compliance_gaps, *_tagged tables'"),
    ("schema compliance",
     f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.compliance "
     f"COMMENT 'Compliance artifacts — consent log, notice versions, DSR requests, register'"),
    ("schema gold",
     f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.gold "
     f"COMMENT 'Aggregate views for dashboards + persona overview metrics'"),
    ("volume bronze.landing",
     f"CREATE VOLUME IF NOT EXISTS {CATALOG}.bronze.landing "
     f"COMMENT 'Landing zone — synthetic CSVs for Auto Loader to ingest'"),
    ("volume bronze.checkpoints",
     f"CREATE VOLUME IF NOT EXISTS {CATALOG}.bronze.checkpoints "
     f"COMMENT 'Auto Loader schema + offset state per silver table'"),
    ("volume compliance.dsr_bundles",
     f"CREATE VOLUME IF NOT EXISTS {CATALOG}.compliance.dsr_bundles "
     f"COMMENT 'DSR discovery + erasure audit JSON, one bundle per request'"),
    ("volume compliance.dpia_artifacts",
     f"CREATE VOLUME IF NOT EXISTS {CATALOG}.compliance.dpia_artifacts "
     f"COMMENT 'DPIA Auto-Generator artifacts, one JSON per run (paired with compliance.dpia_runs row)'"),
]


def _databricks(*args: str) -> str:
    r = subprocess.run(["databricks", *args], capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"databricks {' '.join(args)} failed: {r.stderr[:300]}")
    return r.stdout


def discover_warehouse() -> str:
    """Pick a serverless SQL warehouse — running or stopped.

    Unlike ``persona_config.get_warehouse_id()`` (which requires RUNNING),
    we accept STOPPED here because serverless warehouses auto-start when
    the first SQL statement hits them. Wait_timeout on the first query
    has to be generous enough to ride out the cold-start (~30-60s).
    """
    if env := os.environ.get("COMPLIANCE_WAREHOUSE_ID"):
        return env
    out = _databricks("warehouses", "list", "-o", "json")
    warehouses = json.loads(out)
    if not warehouses:
        raise RuntimeError(
            "no SQL warehouses found in this workspace. Create one first: "
            "Compute → SQL Warehouses → Create. The default 'Serverless Starter "
            "Warehouse' is fine."
        )
    # Prefer running serverless > running classic > stopped serverless > anything.
    running_serverless = [w for w in warehouses
                          if w.get("state") == "RUNNING" and w.get("enable_serverless_compute")]
    running_any        = [w for w in warehouses if w.get("state") == "RUNNING"]
    stopped_serverless = [w for w in warehouses if w.get("enable_serverless_compute")]
    pick = (running_serverless or running_any or stopped_serverless or warehouses)[0]
    return pick["id"]


def run_sql(warehouse_id: str, stmt: str) -> tuple[str, str]:
    """Returns (state, error_message). Uses a generous 60s wait_timeout
    so the first call can ride out a serverless cold-start."""
    payload = {"warehouse_id": warehouse_id, "statement": stmt, "wait_timeout": "50s"}
    Path("/tmp/_bootstrap_sql.json").write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", "@/tmp/_bootstrap_sql.json"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return ("ERR", r.stderr[:400])
    d = json.loads(r.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return (state, d.get("status", {}).get("error", {}).get("message", "")[:400])
    return ("OK", "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--dry-run", action="store_true",
                   help="print the plan; do not execute SQL")
    args = p.parse_args()

    warehouse_id = discover_warehouse()
    print(f"UC bootstrap — catalog `{CATALOG}` (warehouse {warehouse_id})")
    print("=" * 70)

    if args.dry_run:
        for label, stmt in STATEMENTS:
            print(f"  ⟳ would run  {label:30s} {stmt[:60]}...")
        return 0

    ok = failed = 0
    for label, stmt in STATEMENTS:
        state, err = run_sql(warehouse_id, stmt)
        marker = "✓" if state == "OK" else "✗"
        print(f"  {marker} {state:10s} {label}")
        if state == "OK":
            ok += 1
        else:
            failed += 1
            print(f"      → {err}")

    print()
    print("=" * 70)
    print(f"{ok} succeeded, {failed} failed")
    if failed == 0:
        print("\nUC bootstrap complete. Continue with `databricks bundle deploy`.")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
