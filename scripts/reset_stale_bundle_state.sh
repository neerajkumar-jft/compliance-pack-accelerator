#!/usr/bin/env bash
# Detect and clear stale bundle state when redeploying against a different
# workspace.
#
# `databricks bundle deploy` caches resource IDs (dashboards, jobs, DLT
# pipelines, apps) in .databricks/bundle/<target>/resources.json, keyed by
# resource name. Those IDs are *workspace-specific*. If the same bundle is
# later deployed against a different workspace (new free-tier account, a
# teammate's workspace, a fresh trial after the previous one expired), the
# CLI tries to GET each cached ID on the new workspace and aborts:
#
#     Error: failed to get dashboard "dpdp_compliance_dashboard"
#     Unable to find dashboard [01f1484e84f21c968834a85342bf1e5f]
#
# We record the workspace host the cache was last deployed against in a
# sidecar file. On the next deploy, if the host doesn't match, we wipe the
# cache so the CLI will discover state fresh from the new workspace.
#
# The sidecar lives next to (not inside) the cache dir so the bundle CLI
# never touches it during its own operations.
#
# Usage:
#   scripts/reset_stale_bundle_state.sh           # checks the dev target
#   scripts/reset_stale_bundle_state.sh prod      # checks a different target
#
# Idempotent: if host matches, exits with no changes.

set -euo pipefail

TARGET="${1:-dev}"
REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
STATE_DIR="$REPO_ROOT/.databricks/bundle/$TARGET"
SIDECAR="$REPO_ROOT/.databricks/bundle/.${TARGET}-host"

# Read current host from databricks.yml. We grep the literal `host:` line
# under targets.<target>.workspace because DAB does not allow variable
# interpolation on auth fields — the host is always a literal in YAML.
CURRENT_HOST="$(grep -E '^[[:space:]]+host: https://dbc-' "$REPO_ROOT/databricks.yml" | head -n1 | sed -E 's/^[[:space:]]+host: //')"

if [[ -z "$CURRENT_HOST" ]]; then
  echo "  ⚠ stale-state check: could not determine workspace host from databricks.yml — skipping" >&2
  exit 0
fi

# Fresh deploy (no state dir yet) — just record current host and exit
if [[ ! -d "$STATE_DIR" ]]; then
  mkdir -p "$(dirname "$SIDECAR")"
  echo "$CURRENT_HOST" > "$SIDECAR"
  echo "  ✓ stale-state check: fresh state dir, recorded host $CURRENT_HOST"
  exit 0
fi

# State dir exists; compare against sidecar
if [[ -f "$SIDECAR" ]]; then
  PREVIOUS_HOST="$(cat "$SIDECAR")"
  if [[ "$PREVIOUS_HOST" == "$CURRENT_HOST" ]]; then
    # Same workspace recorded — but ALSO probe the workspace to verify
    # the bundle's deployed files actually exist there. This catches the
    # case where someone wiped the workspace externally (manual catalog
    # drop / `databricks workspace delete`) without running
    # `databricks bundle destroy`. Host hasn't changed, so the host-only
    # check above wouldn't fire — but the cache's sync-snapshots think
    # files are already in sync, so the next `bundle deploy` skips file
    # upload (silent failure). Then DLT/job runs hit NOTEBOOK_NOT_FOUND.
    BUNDLE_NAME="dpdp-poc"   # matches `bundle.name` in databricks.yml
    ME="$(databricks current-user me 2>/dev/null \
          | python3 -c 'import json,sys; print(json.load(sys.stdin).get("userName",""))' 2>/dev/null)"
    if [[ -n "$ME" ]]; then
      EXPECTED_FILES_PATH="/Workspace/Users/${ME}/.bundle/${BUNDLE_NAME}/${TARGET}/files/pipelines"
      if ! databricks workspace list "$EXPECTED_FILES_PATH" >/dev/null 2>&1; then
        echo "  ▶ stale-state check: workspace bundle files missing at $EXPECTED_FILES_PATH"
        echo "    (host unchanged but workspace was wiped externally)"
        rm -rf "$STATE_DIR"
        mkdir -p "$(dirname "$SIDECAR")"
        echo "$CURRENT_HOST" > "$SIDECAR"
        echo "  ✓ stale-state check: cleared $STATE_DIR/ — next deploy will re-upload all files"
        exit 0
      fi
    fi
    # Same workspace AND files still present — incremental deploy is correct, keep cache
    echo "  ✓ stale-state check: cache matches current workspace — keep"
    exit 0
  fi
  echo "  ▶ stale-state check: workspace change detected for target '$TARGET'"
  echo "      previously deployed: $PREVIOUS_HOST"
  echo "      now deploying to:    $CURRENT_HOST"
  echo "    Stale cached IDs from previous workspace would cause"
  echo "    'failed to get dashboard / pipeline / job' errors."
else
  # State exists without a sidecar (cloned repo with leftover cache, or
  # cache predates this script). Safer to clear than risk stale IDs.
  echo "  ▶ stale-state check: state dir exists without sidecar — clearing"
fi

rm -rf "$STATE_DIR"
mkdir -p "$(dirname "$SIDECAR")"
echo "$CURRENT_HOST" > "$SIDECAR"
echo "  ✓ stale-state check: cleared $STATE_DIR/ and recorded new host"
