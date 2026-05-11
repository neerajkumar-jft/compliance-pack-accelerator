#!/usr/bin/env bash
# Wipe both sides of a deploy_all.sh deployment so the next run starts clean.
#
# Asymmetric cleanup is the silent killer of "fresh-workspace" tests: drop
# the catalog in the UI but leave .databricks/bundle/<target>/ on disk and
# DAB carries phantom resource IDs forward into the next deploy ("resource
# already exists" / "resource X not found" errors that are confusing because
# the workspace genuinely doesn't have that resource anymore).
#
# Usage:
#   scripts/clean.sh                       # print the plan; do nothing
#   scripts/clean.sh --yes                 # wipe both sides
#   scripts/clean.sh --workspace-only --yes
#   scripts/clean.sh --local-only --yes
#
# What gets wiped by default (both sides):
#
#   WORKSPACE
#     databricks bundle destroy --target dev --auto-approve
#       (drops bundle-managed pipelines + jobs + dashboards)
#     DROP CATALOG IF EXISTS compliance_pack CASCADE
#       (catalog + 5 schemas + all tables/views/volumes — bootstrap_catalog.py
#        creates the catalog outside DAB so bundle destroy alone leaves it)
#
#   LOCAL
#     rm -rf .databricks/bundle/<target>/
#       (DAB resource-id cache — stale ids cause phantom-resource errors)
#
# What is NOT wiped (deliberately):
#   - The 4 plus-addressed persona users (CCO/GC/CMO/CFO@plus-addressing).
#     setup_all_personas.py is idempotent on re-runs, and teammates often
#     share persona accounts across deploys. To delete them, do it manually
#     in the workspace admin console.
#   - The S3 external location / storage credential / external volume
#     (jft_s3, jft_databricks_storage, etc.) — pre-existing, not POC-owned.
#   - Any catalog other than $DPDP_CATALOG.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

CATALOG="${DPDP_CATALOG:-compliance_pack}"
TARGET="${DPDP_TARGET:-dev}"

LOCAL=1
WORKSPACE=1
YES=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --workspace-only) LOCAL=0; shift ;;
    --local-only)     WORKSPACE=0; shift ;;
    --yes|-y)         YES=1; shift ;;
    -h|--help)        sed -n '2,32p' "$0"; exit 0 ;;
    *)                echo "unknown flag: $1" >&2; exit 64 ;;
  esac
done

LOCAL_BUNDLE_DIR="$REPO_ROOT/.databricks/bundle/$TARGET"
DROP_SQL="DROP CATALOG IF EXISTS $CATALOG CASCADE"

# ---------- print plan ----------
echo "Plan (catalog=$CATALOG, target=$TARGET):"
if [[ "$WORKSPACE" == 1 ]]; then
  echo "  [W] databricks bundle destroy --target $TARGET --auto-approve"
  echo "  [W] $DROP_SQL"
fi
if [[ "$LOCAL" == 1 ]]; then
  if [[ -d "$LOCAL_BUNDLE_DIR" ]]; then
    echo "  [L] rm -rf $LOCAL_BUNDLE_DIR"
  else
    echo "  [L] (skip) $LOCAL_BUNDLE_DIR does not exist"
  fi
fi
echo ""

if [[ "$YES" != 1 ]]; then
  echo "Add --yes to actually wipe."
  exit 0
fi

# ---------- workspace side ----------
if [[ "$WORKSPACE" == 1 ]]; then
  echo "▶ bundle destroy"
  # Don't fail the script if bundle destroy errors — the catalog DROP that
  # follows usually cleans up whatever the bundle couldn't.
  databricks bundle destroy --target "$TARGET" --auto-approve 2>&1 \
    | tail -5 || true

  echo ""
  echo "▶ $DROP_SQL"
  # Pick any serverless warehouse (running or stopped — auto-starts).
  WAREHOUSE_ID="$(databricks warehouses list -o json \
    | python3 -c '
import json, sys
ws = json.load(sys.stdin)
running    = [w for w in ws if w.get("state") == "RUNNING" and w.get("enable_serverless_compute")]
serverless = [w for w in ws if w.get("enable_serverless_compute")]
pick = (running or serverless or ws)
if not pick:
    raise SystemExit("no SQL warehouses in this workspace")
print(pick[0]["id"])
')"
  payload="$(python3 -c "
import json
print(json.dumps({
    'warehouse_id': '$WAREHOUSE_ID',
    'statement':    '$DROP_SQL',
    'wait_timeout': '50s',
}))
")"
  echo "$payload" > /tmp/_clean_drop.json
  result="$(databricks api post /api/2.0/sql/statements --json @/tmp/_clean_drop.json)"
  state="$(echo "$result" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",{}).get("state","?"))')"
  if [[ "$state" == "SUCCEEDED" ]]; then
    echo "  ✓ catalog dropped"
  else
    err="$(echo "$result" | python3 -c 'import json,sys;print(json.load(sys.stdin).get("status",{}).get("error",{}).get("message",""))')"
    echo "  ✗ $state — $err"
  fi
fi

# ---------- local side ----------
if [[ "$LOCAL" == 1 ]]; then
  echo ""
  if [[ -d "$LOCAL_BUNDLE_DIR" ]]; then
    echo "▶ rm -rf $LOCAL_BUNDLE_DIR"
    rm -rf "$LOCAL_BUNDLE_DIR"
    echo "  ✓ local DAB state removed"
  else
    echo "▶ (skip local) $LOCAL_BUNDLE_DIR does not exist"
  fi
fi

echo ""
echo "✓ clean complete."
echo "  Next: scripts/deploy_all.sh"
