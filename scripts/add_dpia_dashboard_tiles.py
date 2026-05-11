"""Add a 'DPIA' page (2 tiles) to the master Lakeview dashboard.

Modifies ``dashboards/dpdp_compliance.lvdash.json`` in place. The
modified JSON is the deployable artifact — ``databricks bundle deploy``
picks it up and updates the live dashboard via Lakeview API.

CLAUDE.md says "edit the dashboard JSON via API, not by hand" — this
script is the API-equivalent: a programmatic, reviewable, source-
controlled mutation that doesn't risk hand-editing typos in 177KB of
JSON. Idempotent: skips additions whose target id is already present.

Why a NEW page instead of slotting tiles into existing pages:
  - Existing pages have hand-tuned grid layouts; inserting a tile in
    the middle of one risks breaking neighbour positioning.
  - A dedicated "DPIA" page is a clean diff (~140 lines added) and
    matches how dashboards typically grow over time.
  - The compliance team navigating to "DPIA" is a clearer mental
    model than hunting for a DPIA tile on a multi-tile page.

Usage:
    python3 scripts/add_dpia_dashboard_tiles.py            # apply
    python3 scripts/add_dpia_dashboard_tiles.py --dry-run  # show plan, no write

After applying, deploy with:
    databricks bundle deploy --target dev
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DASHBOARD_PATH = REPO_ROOT / "dashboards" / "dpdp_compliance.lvdash.json"


# ---------------------------------------------------------------------
# Datasets (SQL queries that back the tiles)
# ---------------------------------------------------------------------

LATEST_DPIA_DATASET: dict = {
    "name": "dpia_latest_001",
    "displayName": "dpia_latest",
    "queryLines": [
        "SELECT\n",
        "  CONCAT('Q', QUARTER(generated_at), ' ', YEAR(generated_at)) AS quarter,\n",
        "  generated_at,\n",
        "  status,\n",
        "  COALESCE(reviewed_by, '—') AS reviewer,\n",
        "  reviewed_at,\n",
        "  ROUND(latency_seconds, 1) AS latency_s,\n",
        "  CASE WHEN parse_error IS NOT NULL THEN '⚠ parse error' ELSE 'OK' END AS parse_status,\n",
        "  prompt_version,\n",
        "  regulation_pack\n",
        "FROM compliance_pack.compliance.dpia_runs\n",
        "ORDER BY generated_at DESC\n",
        "LIMIT 1\n",
    ],
}

DPIA_HISTORY_DATASET: dict = {
    "name": "dpia_history_001",
    "displayName": "dpia_history",
    "queryLines": [
        "SELECT\n",
        "  CONCAT('Q', QUARTER(generated_at), ' ', YEAR(generated_at)) AS quarter,\n",
        "  generated_at,\n",
        "  run_id,\n",
        "  status,\n",
        "  COALESCE(reviewed_by, '—') AS reviewer,\n",
        "  reviewed_at,\n",
        "  ROUND(latency_seconds, 1) AS latency_s,\n",
        "  CASE WHEN parse_error IS NOT NULL THEN '⚠ parse error' ELSE 'OK' END AS parse_status\n",
        "FROM compliance_pack.compliance.dpia_runs\n",
        "ORDER BY generated_at DESC\n",
        "LIMIT 8\n",
    ],
}


# ---------------------------------------------------------------------
# Widgets (the tile definitions referencing the datasets above)
# ---------------------------------------------------------------------

def _table_widget(*, name: str, title: str, dataset_name: str,
                  fields: list[tuple[str, str, str]],
                  position: dict) -> dict:
    """Build a Lakeview table widget shape consistent with existing widgets.

    ``fields`` is a list of ``(field_name, expression, display_name)``
    tuples. Lakeview's table widget v2 spec expects ``encodings.columns``
    entries with ``fieldName`` + ``displayName`` only — adding a
    ``type: "string"`` key (as a v1 spec would have it) silently produces
    "Visualization has no fields selected." on the rendered widget,
    which is what bit us on the first deploy.
    """
    return {
        "widget": {
            "name": name,
            "queries": [
                {
                    "name": "main_query",
                    "query": {
                        "datasetName": dataset_name,
                        "fields": [
                            {"name": fn, "expression": expr}
                            for fn, expr, _ in fields
                        ],
                        "disaggregated": True,
                    },
                }
            ],
            "spec": {
                "version": 2,  # v2 schema; v1 silently breaks rendering
                "frame": {
                    "showTitle": True,
                    "title": title,
                    "headerAlignment": "center",
                },
                "widgetType": "table",
                "encodings": {
                    "columns": [
                        {"fieldName": fn, "displayName": dn}
                        for fn, _, dn in fields
                    ],
                },
            },
        },
        "position": position,
    }


LATEST_DPIA_WIDGET = _table_widget(
    name="dpia-latest-summary",
    title="Latest DPIA",
    dataset_name="dpia_latest_001",
    fields=[
        ("quarter",         "`quarter`",         "Quarter"),
        ("generated_at",    "`generated_at`",    "Generated at"),
        ("status",          "`status`",          "Status"),
        ("reviewer",        "`reviewer`",        "Reviewer"),
        ("reviewed_at",     "`reviewed_at`",     "Reviewed at"),
        ("latency_s",       "`latency_s`",       "Latency (s)"),
        ("parse_status",    "`parse_status`",    "Parse status"),
        ("prompt_version",  "`prompt_version`",  "Prompt version"),
        ("regulation_pack", "`regulation_pack`", "Regulation pack"),
    ],
    position={"x": 0, "y": 0, "width": 12, "height": 3},
)

DPIA_HISTORY_WIDGET = _table_widget(
    name="dpia-history-table",
    title="DPIA history (last 8 runs)",
    dataset_name="dpia_history_001",
    fields=[
        ("quarter",      "`quarter`",      "Quarter"),
        ("generated_at", "`generated_at`", "Generated at"),
        ("run_id",       "`run_id`",       "Run ID"),
        ("status",       "`status`",       "Status"),
        ("reviewer",     "`reviewer`",     "Reviewer"),
        ("reviewed_at",  "`reviewed_at`",  "Reviewed at"),
        ("latency_s",    "`latency_s`",    "Latency (s)"),
        ("parse_status", "`parse_status`", "Parse status"),
    ],
    position={"x": 0, "y": 3, "width": 12, "height": 6},
)


# ---------------------------------------------------------------------
# Page wrapping the two widgets
# ---------------------------------------------------------------------

DPIA_PAGE: dict = {
    "name": "pg_dpia_001",
    "displayName": "DPIA",
    "layout": [LATEST_DPIA_WIDGET, DPIA_HISTORY_WIDGET],
    "pageType": "PAGE_TYPE_CANVAS",
}


# ---------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------


def _has_dataset(dashboard: dict, ds_name: str) -> bool:
    return any(d.get("name") == ds_name for d in dashboard.get("datasets", []))


def _has_page(dashboard: dict, page_name: str) -> bool:
    return any(p.get("name") == page_name for p in dashboard.get("pages", []))


def _drop_dataset(dashboard: dict, ds_name: str) -> None:
    dashboard["datasets"] = [d for d in dashboard.get("datasets", []) if d.get("name") != ds_name]


def _drop_page(dashboard: dict, page_name: str) -> None:
    dashboard["pages"] = [p for p in dashboard.get("pages", []) if p.get("name") != page_name]


def apply(dry_run: bool = False, force: bool = False) -> int:
    if not DASHBOARD_PATH.exists():
        print(f"error: {DASHBOARD_PATH} not found", file=sys.stderr)
        return 1

    with DASHBOARD_PATH.open() as f:
        dashboard = json.load(f)

    has_latest = _has_dataset(dashboard, LATEST_DPIA_DATASET["name"])
    has_history = _has_dataset(dashboard, DPIA_HISTORY_DATASET["name"])
    has_dpia_page = _has_page(dashboard, DPIA_PAGE["name"])

    if has_latest and has_history and has_dpia_page and not force:
        print("Nothing to add — dashboard already has the DPIA datasets + page.")
        print("(Pass --force to overwrite — useful when the widget shape changes.)")
        return 0

    print("Plan:")
    if force:
        print(f"  ✗ remove existing dataset/page (force mode)")
    if not has_latest or force:
        print(f"  + dataset {LATEST_DPIA_DATASET['displayName']}")
    if not has_history or force:
        print(f"  + dataset {DPIA_HISTORY_DATASET['displayName']}")
    if not has_dpia_page or force:
        print(f"  + page    {DPIA_PAGE['displayName']} (2 tiles)")

    if dry_run:
        print("\n(dry-run — no write)")
        return 0

    # In force mode, drop existing entries first so we apply a fresh shape.
    if force:
        _drop_dataset(dashboard, LATEST_DPIA_DATASET["name"])
        _drop_dataset(dashboard, DPIA_HISTORY_DATASET["name"])
        _drop_page(dashboard, DPIA_PAGE["name"])
        # Re-check; everything should be missing now
        has_latest = has_history = has_dpia_page = False

    if not has_latest:
        dashboard.setdefault("datasets", []).append(LATEST_DPIA_DATASET)
    if not has_history:
        dashboard.setdefault("datasets", []).append(DPIA_HISTORY_DATASET)
    if not has_dpia_page:
        dashboard.setdefault("pages", []).append(DPIA_PAGE)

    with DASHBOARD_PATH.open("w") as f:
        json.dump(dashboard, f, indent=2)
        f.write("\n")

    print(f"\nWrote {DASHBOARD_PATH.relative_to(REPO_ROOT)}")
    print("Deploy with: databricks bundle deploy --target dev")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--dry-run", action="store_true", help="print plan, do not write")
    p.add_argument("--force", action="store_true",
                   help="overwrite existing DPIA page/datasets — use after editing widget shape")
    args = p.parse_args()
    return apply(dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    raise SystemExit(main())
