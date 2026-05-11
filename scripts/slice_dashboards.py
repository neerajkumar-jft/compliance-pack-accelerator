"""Slice the 10-page dpdp_compliance dashboard into per-persona dashboards.

Reads dashboards/dpdp_compliance.lvdash.json, keeps only the pages each
persona needs (identified by page displayName), and writes one JSON file
per persona under dashboards/personas/. All top-level datasets are
preserved — trimming datasets is not worth the risk of missing a
transitive reference for a ~200KB file.

For CMO and CFO, a new persona-specific canvas page is appended so that
consent-audience (CMO) and penalty-exposure (CFO) tiles live inside the
same Lakeview dashboard as the sliced pages. These extra pages are
generated as minimal JSON stubs that reference new datasets appended to
the datasets array.

Usage:
    python scripts/slice_dashboards.py                   # write files only
    python scripts/slice_dashboards.py --persona cco     # only one persona
    python scripts/slice_dashboards.py --upload          # also publish
                                                         # via Lakeview API

The --upload flag requires the databricks CLI to be configured. After a
successful upload, dashboard IDs are written to
dashboards/personas/.dashboard_ids.json so the persona portal app can
read them at deploy time.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import uuid
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DASHBOARD = REPO_ROOT / "dashboards" / "dpdp_compliance.lvdash.json"
OUTPUT_DIR = REPO_ROOT / "dashboards" / "personas"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_config import get_warehouse_id, get_catalog  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
CATALOG = get_catalog()

# Page displayName → persona mapping. Order within each list is the
# order pages will appear in the sliced dashboard.
PERSONA_PAGES: dict[str, list[str]] = {
    "cco": [
        "Global Filters",
        "Executive Overview",
        "PII Discovery",
        "Compliance Analysis",
        "Data Inventory",
        "Detection Quality",
        # DPIA — CCO owns DPDP compliance posture; reads via dashboard.
        # Approve action lives in the Databricks Review App, not here.
        "DPIA",
    ],
    "gc": [
        "Global Filters",
        "Executive Overview",
        "Compliance Analysis",
        "Sensitive Data Exposure",
        "Access Patterns",
        # DPIA — GC is one of the two approver roles. Sees the full
        # cadence + status in their Genie chat too (configs/genie/gc.yaml).
        "DPIA",
    ],
    "cmo": [
        "Global Filters",
        "Executive Overview",
        # No DPIA — marketing has no business need; the Compliance App
        # is also intentionally CAN_USE-denied for CMO.
    ],
    "cfo": [
        "Global Filters",
        "Executive Overview",
        "Compliance Analysis",
        # DPIA — CFO has audience-only visibility (penalty-exposure
        # adjacency). View-only in the App (Approve hidden by role gate).
        "DPIA",
    ],
}

# Extra pages built from scratch for personas whose needs aren't
# covered by the existing dashboard. Each entry is a (page_dict,
# [dataset_dict, ...]) tuple produced by the builder functions below.
PERSONA_EXTRA_PAGES_BUILDERS = {
    "cmo": "build_cmo_consent_page",
    "cfo": "build_cfo_penalty_page",
}


def load_source() -> dict:
    with SOURCE_DASHBOARD.open() as f:
        return json.load(f)


def slice_pages(source: dict, wanted_names: list[str]) -> list[dict]:
    """Return source pages matching the wanted displayNames, preserving
    the order given in wanted_names. Missing names are skipped with a
    warning rather than erroring — the source may evolve."""
    by_name = {p.get("displayName"): p for p in source["pages"]}
    ordered: list[dict] = []
    for name in wanted_names:
        page = by_name.get(name)
        if page is None:
            print(f"  [warn] page '{name}' not found in source dashboard", file=sys.stderr)
            continue
        ordered.append(page)
    return ordered


# ---------------------------------------------------------------------------
# Builders for persona-specific pages that don't exist in the source
# ---------------------------------------------------------------------------

def _new_id() -> str:
    """Generate a hex id matching the style used in the source (8 chars)."""
    return uuid.uuid4().hex[:8]


def _counter_widget(
    widget_name: str,
    dataset_name: str,
    field_name: str,
    title: str,
    description: str = "",
    position: dict | None = None,
) -> dict:
    """Minimal counter tile. `field_name` must exist in the dataset query."""
    return {
        "widget": {
            "name": widget_name,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": dataset_name,
                        "fields": [{"name": field_name, "expression": f"`{field_name}`"}],
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 2,
                "widgetType": "counter",
                "frame": {"showTitle": True, "title": title, "description": description},
                "encodings": {"value": {"fieldName": field_name, "displayName": title}},
            },
        },
        "position": position or {"x": 0, "y": 0, "width": 2, "height": 2},
    }


def _table_widget(
    widget_name: str,
    dataset_name: str,
    columns: list[tuple[str, str]],
    title: str,
    position: dict | None = None,
) -> dict:
    """Minimal table tile. columns = [(field_name, displayName), ...]."""
    field_list = [{"name": c[0], "expression": f"`{c[0]}`"} for c in columns]
    col_specs = [
        {"fieldName": c[0], "displayName": c[1], "type": "string", "order": idx}
        for idx, c in enumerate(columns)
    ]
    return {
        "widget": {
            "name": widget_name,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": dataset_name,
                        "fields": field_list,
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 1,
                "widgetType": "table",
                "frame": {"showTitle": True, "title": title},
                "encodings": {"columns": col_specs},
            },
        },
        "position": position or {"x": 0, "y": 0, "width": 6, "height": 6},
    }


def build_cmo_consent_page() -> tuple[dict, list[dict]]:
    """Build a CMO-focused page: marketing-eligible audience, consent
    rates by purpose, withdrawal trend."""
    ds_eligible = _new_id()
    ds_by_purpose = _new_id()
    ds_withdrawals = _new_id()

    datasets = [
        {
            "name": ds_eligible,
            "displayName": "cmo_marketing_eligible_count",
            "queryLines": [
                f"SELECT COUNT(*) AS eligible_principals FROM {CATALOG}.gold.marketing_eligible_principals;",
            ],
        },
        {
            "name": ds_by_purpose,
            "displayName": "cmo_consent_by_purpose",
            "queryLines": [
                "WITH latest AS (\n",
                "  SELECT data_principal_id, purpose, purpose_grant_status,\n",
                "         ROW_NUMBER() OVER (PARTITION BY data_principal_id, purpose ORDER BY event_time DESC) AS rn\n",
                f"  FROM {CATALOG}.compliance.consent_events_log\n",
                ")\n",
                "SELECT purpose,\n",
                "       SUM(CASE WHEN purpose_grant_status='granted' THEN 1 ELSE 0 END) AS granted,\n",
                "       SUM(CASE WHEN purpose_grant_status='withdrawn' THEN 1 ELSE 0 END) AS withdrawn,\n",
                "       SUM(CASE WHEN purpose_grant_status='declined' THEN 1 ELSE 0 END) AS declined\n",
                "FROM latest WHERE rn=1 GROUP BY purpose ORDER BY purpose;",
            ],
        },
        {
            "name": ds_withdrawals,
            "displayName": "cmo_withdrawal_trend",
            "queryLines": [
                "SELECT DATE_TRUNC('day', event_time) AS day,\n",
                "       COUNT(*) AS withdrawals\n",
                f"FROM {CATALOG}.compliance.consent_events_log\n",
                "WHERE purpose='marketing_email' AND purpose_grant_status='withdrawn'\n",
                "GROUP BY DATE_TRUNC('day', event_time) ORDER BY day;",
            ],
        },
    ]

    page = {
        "name": _new_id(),
        "displayName": "Consent & Audience",
        "layout": [
            _counter_widget(
                "cmo-eligible", ds_eligible, "eligible_principals",
                "Marketing-Eligible Principals",
                "Principals with active marketing_email consent",
                {"x": 0, "y": 0, "width": 3, "height": 3},
            ),
            _table_widget(
                "cmo-by-purpose", ds_by_purpose,
                [("purpose", "Purpose"), ("granted", "Granted"),
                 ("withdrawn", "Withdrawn"), ("declined", "Declined")],
                "Consent Status by Purpose (latest state per principal)",
                {"x": 3, "y": 0, "width": 3, "height": 3},
            ),
            _table_widget(
                "cmo-withdrawals", ds_withdrawals,
                [("day", "Day"), ("withdrawals", "Marketing Withdrawals")],
                "Marketing Withdrawal Trend",
                {"x": 0, "y": 3, "width": 6, "height": 4},
            ),
        ],
        "pageType": "PAGE_TYPE_CANVAS",
    }
    return page, datasets


def build_cfo_penalty_page() -> tuple[dict, list[dict]]:
    """Build a CFO-focused page: ₹-denominated exposure, gap counts
    weighted by penalty severity. The penalty lookup is inlined as SQL
    CASE since we don't have a penalties table yet."""
    ds_exposure = _new_id()
    ds_severity = _new_id()

    # DPDP penalties (₹ crore): critical → 250, high → 150, medium → 50, low → 5
    penalty_case = (
        "CASE severity "
        "WHEN 'critical' THEN 250 "
        "WHEN 'high' THEN 150 "
        "WHEN 'medium' THEN 50 "
        "ELSE 5 END"
    )

    datasets = [
        {
            "name": ds_exposure,
            "displayName": "cfo_penalty_exposure_total",
            "queryLines": [
                f"SELECT ROUND(SUM({penalty_case}) / 100, 1) AS exposure_hundreds_cr\n",
                f"FROM {CATALOG}.silver.compliance_gaps;",
            ],
        },
        {
            "name": ds_severity,
            "displayName": "cfo_gaps_by_severity_with_penalty",
            "queryLines": [
                f"SELECT severity, COUNT(*) AS gap_count, MAX({penalty_case}) AS penalty_ceiling_cr\n",
                f"FROM {CATALOG}.silver.compliance_gaps GROUP BY severity\n",
                "ORDER BY CASE severity WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END;",
            ],
        },
    ]

    page = {
        "name": _new_id(),
        "displayName": "Penalty Exposure",
        "layout": [
            _counter_widget(
                "cfo-exposure", ds_exposure, "exposure_hundreds_cr",
                "Total Penalty Exposure (₹ hundred crore)",
                "Sum of DPDP penalty ceilings for all open gaps",
                {"x": 0, "y": 0, "width": 3, "height": 3},
            ),
            _table_widget(
                "cfo-severity", ds_severity,
                [("severity", "Severity"), ("gap_count", "Open Gaps"),
                 ("penalty_ceiling_cr", "Per-Gap Ceiling (₹ Cr)")],
                "Gaps by Severity with Per-Incident Penalty Ceiling",
                {"x": 3, "y": 0, "width": 5, "height": 4},
            ),
        ],
        "pageType": "PAGE_TYPE_CANVAS",
    }
    return page, datasets


# Builder lookup
EXTRA_BUILDERS = {
    "cmo": build_cmo_consent_page,
    "cfo": build_cfo_penalty_page,
}


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Dataset rewrites for non-CCO slices
# ---------------------------------------------------------------------------
# The master dashboard's Executive Overview tiles query silver.pii_findings
# and silver.compliance_gaps directly. Non-CCO personas don't have SELECT on
# pii_findings (and CMO also lacks compliance_gaps) — see
# scripts/apply_persona_uc_grants.py → SHARED_OVERVIEW_TABLES. To keep the
# Executive Overview tiles rendering for those personas, we rewrite 5
# datasets to query aggregate Gold views created by phase1_bootstrap.py:
#   - compliance_pack.gold.persona_overview_metrics       (1-row scorecard)
#   - compliance_pack.gold.persona_sensitivity_histogram  (4-row tier breakdown)
#
# Each rewrite emits the same output columns as the original so widgets
# keep their field bindings. CCO keeps raw SQL because it also queries
# pii_findings on other pages (PII Discovery, Detection Quality).
DATASET_REWRITES_NON_CCO: dict[str, str] = {
    "risk_scores": (
        "SELECT risk_score, compliance_score, risk_level "
        "FROM compliance_pack.gold.persona_overview_metrics"
    ),
    "last_scan_info": (
        "SELECT 'aggregate' AS last_scan_job_id, last_scan_time, "
        "1 AS total_scans, days_since_last_scan "
        "FROM compliance_pack.gold.persona_overview_metrics"
    ),
    "executive_summary": (
        "SELECT total_tables AS tables_scanned, pii_columns AS total_pii_columns, "
        "critical_pii, high_pii AS high_sensitivity_pii, avg_confidence, "
        "total_gaps, critical_gaps "
        "FROM compliance_pack.gold.persona_overview_metrics"
    ),
    "pii_sensitivity_distribution": (
        "SELECT sensitivity_tier, count FROM compliance_pack.gold.persona_sensitivity_histogram "
        "ORDER BY CASE sensitivity_tier WHEN 'critical' THEN 1 WHEN 'high' THEN 2 "
        "WHEN 'medium' THEN 3 ELSE 4 END"
    ),
    # Coarsened: no table names returned (those would leak per-table PII counts).
    # The tile still renders with a single summary row pointing the user at CCO.
    "high_risk_tables": (
        "SELECT 'Aggregate summary — table-level breakdown available to CCO' "
        "AS full_table_name, pii_columns, critical_pii AS critical_count, "
        "high_pii AS high_count, CAST((critical_pii*3 + high_pii*2) AS INT) AS risk_score "
        "FROM compliance_pack.gold.persona_overview_metrics"
    ),
}


def apply_dataset_rewrites(datasets: list, persona: str) -> None:
    """In-place rewrite of specific dataset SQL for non-CCO personas."""
    if persona == "cco":
        return
    for ds in datasets:
        new_sql = DATASET_REWRITES_NON_CCO.get(ds.get("displayName"))
        if new_sql:
            ds["queryLines"] = [new_sql]


def slice_for_persona(source: dict, persona: str) -> dict:
    sliced_pages = slice_pages(source, PERSONA_PAGES[persona])
    datasets = list(source["datasets"])  # shallow copy; we'll append

    if persona in EXTRA_BUILDERS:
        extra_page, extra_datasets = EXTRA_BUILDERS[persona]()
        sliced_pages.append(extra_page)
        datasets.extend(extra_datasets)

    # Apply non-CCO dataset rewrites so Executive Overview tiles query
    # the aggregate Gold views instead of raw pii_findings/compliance_gaps.
    apply_dataset_rewrites(datasets, persona)

    return {"datasets": datasets, "pages": sliced_pages}


def dashboard_exists(dashboard_id: str) -> bool:
    r = subprocess.run(
        ["databricks", "api", "get", f"/api/2.0/lakeview/dashboards/{dashboard_id}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    try:
        return json.loads(r.stdout).get("lifecycle_state") == "ACTIVE"
    except Exception:
        return False


def upload_dashboard(persona: str, path: Path, existing_id: str | None = None) -> str:
    """Create (or update) a Lakeview dashboard from the JSON and
    return its id. Idempotent: if existing_id is provided and the
    dashboard is still ACTIVE, PATCHes in place; otherwise creates
    a new one."""
    title = f"DPDP — {persona.upper()} View"
    serialized = path.read_text()

    if existing_id and dashboard_exists(existing_id):
        # Update in place so re-runs don't create duplicates
        payload = {"display_name": title,
                   "warehouse_id": WAREHOUSE_ID,
                   "serialized_dashboard": serialized}
        payload_path = Path(f"/tmp/_dash_{persona}.json")
        payload_path.write_text(json.dumps(payload))
        r = subprocess.run(
            ["databricks", "api", "patch",
             f"/api/2.0/lakeview/dashboards/{existing_id}",
             "--json", f"@{payload_path}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"dashboard PATCH failed for {persona}: {r.stderr}")
        dashboard_id = existing_id
        action = "updated"
    else:
        payload = {"display_name": title,
                   "warehouse_id": WAREHOUSE_ID,
                   "serialized_dashboard": serialized}
        payload_path = Path(f"/tmp/_dash_{persona}.json")
        payload_path.write_text(json.dumps(payload))
        r = subprocess.run(
            ["databricks", "api", "post", "/api/2.0/lakeview/dashboards",
             "--json", f"@{payload_path}"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            raise RuntimeError(f"dashboard create failed for {persona}: {r.stderr}")
        data = json.loads(r.stdout)
        dashboard_id = data.get("dashboard_id") or data.get("id")
        if not dashboard_id:
            raise RuntimeError(f"no dashboard_id in response for {persona}: {r.stdout[:500]}")
        action = "created"

    # (Re-)publish. embed_credentials default here is True; the
    # governance script flips it to False once persona ACLs are set.
    pub_payload = {"warehouse_id": WAREHOUSE_ID, "embed_credentials": True}
    subprocess.run(
        ["databricks", "api", "post",
         f"/api/2.0/lakeview/dashboards/{dashboard_id}/published",
         "--json", json.dumps(pub_payload)],
        capture_output=True, text=True, check=True,
    )
    print(f"[{persona}] dashboard {action}: {dashboard_id}")
    return dashboard_id


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", choices=list(PERSONA_PAGES.keys()),
                        help="only slice for this persona; default: all")
    parser.add_argument("--upload", action="store_true",
                        help="publish each sliced dashboard via Lakeview API")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    source = load_source()
    personas = [args.persona] if args.persona else list(PERSONA_PAGES.keys())

    dashboard_ids: dict[str, str] = {}
    ids_file = OUTPUT_DIR / ".dashboard_ids.json"
    if ids_file.exists():
        dashboard_ids = json.loads(ids_file.read_text())

    for persona in personas:
        print(f"[{persona}] slicing pages: {PERSONA_PAGES[persona]}")
        sliced = slice_for_persona(source, persona)
        out_path = OUTPUT_DIR / f"{persona}.lvdash.json"
        out_path.write_text(json.dumps(sliced, indent=2))
        print(f"[{persona}] wrote {out_path} ({out_path.stat().st_size:,} bytes, "
              f"{len(sliced['pages'])} pages, {len(sliced['datasets'])} datasets)")

        if args.upload:
            existing = dashboard_ids.get(persona)
            dash_id = upload_dashboard(persona, out_path, existing_id=existing)
            dashboard_ids[persona] = dash_id

    if args.upload:
        ids_file.write_text(json.dumps(dashboard_ids, indent=2))
        print(f"\nWrote dashboard IDs to {ids_file}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
