#!/usr/bin/env bash
# One-shot end-to-end deploy: zero-state workspace → fully governed POC.
#
# Wraps every step in the README "Phase 1" + Day 3 (SF) + Day 4 (federation)
# sequence behind a single command. Each step is idempotent — re-running
# `deploy_all.sh` against an already-deployed workspace finishes in seconds.
#
# Usage:
#   scripts/deploy_all.sh                  # full deploy
#   scripts/deploy_all.sh --from masks     # start from the masks step
#   scripts/deploy_all.sh --skip-multilang # skip the optional translation step
#   scripts/deploy_all.sh --smoke-only     # only run the post-deploy smoke test
#
# Steps (in order):
#   bootstrap_uc  bootstrap_catalog.py (create compliance_pack + schemas + volumes — idempotent)
#   bundle        databricks bundle deploy --target dev
#   synthetic     generate + upload Auto Loader CSVs
#   medallion     bundle run run_medallion (5 silver tables + pii_findings)
#   sf            seed Salesforce simulation tables (Day 3)
#   federation    seed federation_mock + silver views (Day 4)
#   seed_ds       seed bronze.data_sources with 10 canonical sources (must
#                 run BEFORE refresh — classifier reads silver_table_name
#                 from data_sources at pipeline-load time; if empty it
#                 falls back to a 5-table list and skips sf/federation)
#   refresh       pipeline update so classifier picks up sf_* + federation_*
#   bootstrap     bundle run phase1_bootstrap (compliance layer)
#   tags          apply_uc_tags.py
#   masks         apply_pii_masks.py
#   filters       apply_persona_row_filters.py
#   multilang     generate_multilang_notices.py (optional, foundation-model call)
#   agents        setup_agent_bricks.py (verifies serving endpoint + MLflow experiment + prompts module)
#   smoke         run tests/test_post_deploy_smoke.py
#   personas      setup_all_personas.py (4 sliced dashboards, 4 Genie spaces, 4 users, UC grants, ACLs)
#   app_deploy    databricks apps deploy dpdp-dpia-review — the bundle deploy
#                 registers the app shell + uploads source, but does NOT start
#                 the app. Without this step Streamlit never boots and visitors
#                 see "App Not Available". Source path is the bundle sync target.
#   app_perms     grant_dpia_app_permissions.py (DPIA Review app's runtime SP gets
#                 SELECT+MODIFY on dpia_runs, READ VOLUME on dpia_artifacts,
#                 warehouse CAN_USE; app gets CAN_USE for CCO/GC/CFO. Closes the
#                 Phase 4 Theme 3 gap where the app deployed but didn't function
#                 until a deployer hand-ran the grants.)
#   dpia_first_run  bundle run dpia_generator — seed a draft DPIA so the app has
#                 something to render on day one. The quarterly cron (UNPAUSED
#                 on deploy) takes over from the next Jan/Apr/Jul/Oct boundary.
#   pii_ai_first_run  bundle run pii_ai_scan — Day-1 AI-PII findings so
#                 personal_data_register / DPIA / dashboard reflect AI-
#                 discovered free-text PII immediately. Daily 03:00 IST cron
#                 takes over for ongoing scans + backfill.
#
# Prerequisites: Databricks CLI configured (`databricks current-user me`
# must succeed), workspace_host already set in databricks.yml (run
# scripts/configure_workspace_host.sh first if needed).

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_ROOT"

# --- env defaults ---------------------------------------------------------
export DATABRICKS_BUNDLE_ENGINE="${DATABRICKS_BUNDLE_ENGINE:-direct}"
export REGULATION_PACK="${REGULATION_PACK:-dpdp_2023}"

LANDING_DIR="${LANDING_DIR:-/tmp/dpdp_landing}"

# --- argument parsing -----------------------------------------------------
FROM=""
SKIP_MULTILANG=0
SMOKE_ONLY=0

while [[ $# -gt 0 ]]; do
  case "$1" in
    --from)            FROM="$2"; shift 2 ;;
    --skip-multilang)  SKIP_MULTILANG=1; shift ;;
    --smoke-only)      SMOKE_ONLY=1; shift ;;
    -h|--help)         sed -n '2,37p' "$0"; exit 0 ;;
    *)                 echo "unknown flag: $1" >&2; exit 64 ;;
  esac
done

# --- preflight ------------------------------------------------------------
if ! databricks current-user me >/dev/null 2>&1; then
  echo "✗ Databricks CLI not authenticated. Run \`databricks configure --host …\` first." >&2
  exit 1
fi

# Detect a workspace change (e.g. fresh trial, teammate's workspace, expired
# free-edition workspace re-provisioned) and clear stale local bundle state
# so `databricks bundle deploy` doesn't try to update workspace-specific
# resource IDs from the previous deploy.
"$REPO_ROOT/scripts/reset_stale_bundle_state.sh" dev

step_active=0
should_run() {
  [[ -z "$FROM" ]] && return 0
  if [[ "$step_active" == 1 ]]; then return 0; fi
  if [[ "$1" == "$FROM" ]]; then step_active=1; return 0; fi
  return 1
}

run_step() {
  local label="$1"; shift
  if ! should_run "$label"; then
    echo "  ⟳ skip   $label"
    return 0
  fi
  echo ""
  echo "──────────────────────────────────────────────────────────────────────"
  echo "▶ $label"
  echo "──────────────────────────────────────────────────────────────────────"
  "$@"
}

# --- step impls -----------------------------------------------------------
do_bootstrap_uc() {
  python3 scripts/bootstrap_catalog.py
}

do_bundle() {
  databricks bundle deploy --target dev
}

do_synthetic() {
  python3 generate_synthetic_data.py --output-dir "$LANDING_DIR"
  for tbl in employees customers patients transactions users; do
    databricks fs mkdir "dbfs:/Volumes/compliance_pack/bronze/landing/${tbl}" 2>/dev/null || true
    databricks fs cp --recursive --overwrite \
      "${LANDING_DIR}/${tbl}/" \
      "dbfs:/Volumes/compliance_pack/bronze/landing/${tbl}/"
  done
}

do_medallion() {
  databricks bundle run run_medallion --target dev
}

do_sf() {
  python3 scripts/seed_salesforce_data.py
}

do_federation() {
  python3 scripts/seed_federation_data.py
}

do_seed_ds() {
  # Seed bronze.data_sources with all 10 canonical entries BEFORE the
  # refresh, so classification_dlt._resolve_silver_tables() resolves to
  # the full 10-object list (not the 5-table fallback). Without this
  # step on a fresh workspace, the targeted full-refresh of pii_findings
  # in `do_refresh` re-imports the classifier — which queries an empty
  # data_sources, falls back to 5 tables, and produces 20 findings
  # instead of 36 even though SF + federation silvers exist.
  python3 scripts/seed_data_sources.py
}

do_refresh() {
  # Targeted full-refresh of pii_findings, then poll until the update
  # COMPLETES. Two reasons we can't just do an incremental refresh here:
  #
  #   1. DLT race on fresh workspaces — pii_findings reads silver tables
  #      via spark.table() and can execute before the silver flows commit
  #      on the first medallion run, producing zero findings. A targeted
  #      full-refresh on pii_findings re-computes it against the
  #      now-committed silver state.
  #
  #   2. SF + federation tables/views aren't part of the DLT pipeline
  #      (they're populated by the seed scripts in steps 5+6, not Auto
  #      Loader). DLT doesn't know they changed, so an incremental update
  #      would skip pii_findings entirely. Forcing a full-refresh makes
  #      the classifier re-scan all 10 silver objects.
  local pid update_id state attempt
  pid="$(databricks pipelines list-pipelines -o json \
        | python3 -c 'import json,sys;print(next(p["pipeline_id"] for p in json.load(sys.stdin) if p["name"].endswith("compliance_pack_medallion")))')"

  update_id="$(databricks api post "/api/2.0/pipelines/${pid}/updates" \
    --json '{"full_refresh_selection": ["compliance_pack.silver.pii_findings"]}' \
    | python3 -c 'import json,sys;print(json.load(sys.stdin)["update_id"])')"
  echo "  full_refresh_selection on pii_findings: update_id=${update_id}"

  # Poll up to ~10 minutes (60 × 10s) — cold-start serverless DLT can take 5+ min.
  for attempt in $(seq 1 60); do
    state="$(databricks api get "/api/2.0/pipelines/${pid}/updates/${update_id}" \
      | python3 -c 'import json,sys;print(json.load(sys.stdin)["update"]["state"])')"
    case "$state" in
      COMPLETED)        echo "  ✓ pipeline COMPLETED (poll ${attempt})"; return 0 ;;
      FAILED|CANCELED)  echo "  ✗ pipeline ${state} (poll ${attempt})"; return 1 ;;
      *)                printf "  … %-15s (poll %2d)\r" "${state}" "${attempt}"; sleep 10 ;;
    esac
  done
  echo "  ✗ timed out waiting for pipeline completion"
  return 1
}

do_bootstrap() {
  databricks bundle run phase1_bootstrap --target dev
}

do_tags()    { python3 scripts/apply_uc_tags.py;  }
do_masks()   { python3 scripts/apply_pii_masks.py; }
do_filters() { python3 scripts/apply_persona_row_filters.py; }

do_multilang() {
  if [[ "$SKIP_MULTILANG" == 1 ]]; then
    echo "  --skip-multilang: not generating translated notices"
    return 0
  fi
  python3 scripts/generate_multilang_notices.py
}

do_agents() {
  python3 scripts/setup_agent_bricks.py
}

do_smoke() {
  python3 tests/test_post_deploy_smoke.py
}

do_personas() {
  # Phase 2 orchestrator: 4 sliced dashboards + 4 Genie spaces + 4 users +
  # UC grants + warehouse/dashboard/Genie ACLs. Idempotent.
  python3 scripts/setup_all_personas.py
}

_app_compute_state() {
  # Read compute_status.state for an app. Echoes "?" on parse failure
  # so the caller's string-compare is always well-defined.
  databricks apps get "$1" 2>/dev/null \
    | python3 -c 'import json,sys
try:
    d = json.load(sys.stdin)
    print(d.get("compute_status",{}).get("state","?"))
except Exception:
    print("?")'
}

do_app_deploy() {
  # `databricks bundle deploy` registers the app metadata + uploads the
  # source via sync.include, but does NOT trigger the actual app
  # deployment (that's a separate `databricks apps deploy` call).
  # Without this step the app shell exists at the URL but Streamlit
  # never starts → "App Not Available" page for any visitor.
  #
  # Furthermore: `databricks apps deploy` requires the app's compute to
  # be RUNNING. A freshly-created app starts in STOPPED state, so we
  # have to start it first and wait for the cold-start to complete
  # (≤ 5 min). Without this, the very first deploy fails with:
  #   "Cannot deploy app <name> as it is not in RUNNING state."
  #
  # Source path matches the bundle sync target (apps/** lands under
  # <bundle>/files/apps/dpia_review). Idempotent — on subsequent
  # deploys the app is already RUNNING, the start-and-poll block is
  # skipped, and `databricks apps deploy` produces a fresh SNAPSHOT
  # deployment cleanly.
  local me
  me="$(databricks current-user me 2>/dev/null \
        | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')"

  # The Databricks Apps API uses ACTIVE (not RUNNING) as the healthy
  # compute_status.state value. The deploy CLI's error message mentions
  # "RUNNING state" but that's prose — the API enum is ACTIVE.
  # Observed states: STOPPED, STARTING, ACTIVE, STOPPING, ERROR.
  local state
  state="$(_app_compute_state dpdp-dpia-review)"

  if [[ "$state" != "ACTIVE" ]]; then
    echo "  ▶ app compute is $state — starting (cold-start ~1-2 min)"
    databricks apps start dpdp-dpia-review >/dev/null 2>&1 || true

    local i
    for i in $(seq 1 60); do
      sleep 5
      state="$(_app_compute_state dpdp-dpia-review)"
      echo "    … app compute state=$state (poll $i)"
      [[ "$state" == "ACTIVE" ]] && break
      if [[ "$state" == "ERROR" ]]; then
        echo "  ✗ app compute reached ERROR — aborting" >&2
        databricks apps get dpdp-dpia-review >&2
        return 1
      fi
    done

    if [[ "$state" != "ACTIVE" ]]; then
      echo "  ✗ app did not reach ACTIVE within 5 min (last state: $state)" >&2
      return 1
    fi
    echo "  ✓ app compute is ACTIVE"
  else
    echo "  ✓ app compute already ACTIVE — skip start"
  fi

  databricks apps deploy dpdp-dpia-review \
    --source-code-path "/Workspace/Users/${me}/.bundle/dpdp-poc/dev/files/apps/dpia_review"
}

do_app_perms() {
  # Phase 4 Theme 3 closure: provision runtime permissions for the
  # DPIA Review app. Runs after `personas` so .persona_emails.json is
  # guaranteed to exist. Idempotent — UC GRANTs no-op on re-grant,
  # workspace ACL PATCHes add to existing grantees.
  python3 scripts/grant_dpia_app_permissions.py
}

do_dpia_first_run() {
  # Trigger one DPIA generation right after deploy so the workspace
  # has a draft artifact in compliance.dpia_runs immediately — the
  # DPIA Review app then has something to render on day one rather
  # than an empty list. The quarterly cron in resources/jobs.yml
  # (UNPAUSED on deploy) takes over from the next quarterly boundary.
  #
  # NOT idempotent — every deploy creates an additional draft row
  # (each gets a unique run_id). Acceptable for the POC since
  # re-deploys are rare and the CCO/GC reviewer can supersede stale
  # drafts via the app. If this becomes noisy, gate behind a
  # `SELECT COUNT(*) FROM dpia_runs` check before triggering.
  databricks bundle run dpia_generator --target dev
}

do_pii_ai_first_run() {
  # Trigger one AI-PII scan right after deploy so personal_data_register
  # already shows AI-discovered findings on day one. Without this step,
  # only regex findings appear until the daily cron at 03:00 IST runs
  # for the first time — which on a fresh demo workspace can be hours
  # away. Mirrors the dpia_first_run pattern above.
  #
  # Idempotent: per-row state in compliance.pii_ai_scan_row_state means
  # re-runs only classify NEW rows. Day-1 cost: up to N_patterns × 1000
  # ai_classify calls (1000–2000 typical). On the free-tier workspace
  # ai_classify is bundled into SQL warehouse compute, $0 separate cost.
  databricks bundle run pii_ai_scan --target dev
}

# --- orchestration --------------------------------------------------------
if [[ "$SMOKE_ONLY" == 1 ]]; then
  do_smoke
  exit $?
fi

run_step bootstrap_uc do_bootstrap_uc
run_step bundle       do_bundle
run_step synthetic    do_synthetic
run_step medallion    do_medallion
run_step sf           do_sf
run_step federation   do_federation
run_step seed_ds      do_seed_ds
run_step refresh      do_refresh
run_step bootstrap    do_bootstrap
run_step tags         do_tags
run_step masks        do_masks
run_step filters      do_filters
run_step multilang    do_multilang
run_step agents       do_agents
run_step smoke        do_smoke
run_step personas     do_personas
run_step app_deploy   do_app_deploy
run_step app_perms    do_app_perms
# pii_ai_first_run BEFORE dpia_first_run: the seed DPIA reads
# personal_data_register (UNION view); running the AI scan first means
# the Day-1 DPIA already cites AI-discovered findings instead of having
# to wait for the next quarterly cron.
run_step pii_ai_first_run do_pii_ai_first_run
run_step dpia_first_run do_dpia_first_run

echo ""
echo "══════════════════════════════════════════════════════════════════════"
echo "✓ deploy_all complete"
echo "══════════════════════════════════════════════════════════════════════"
