# Deploying Persona Dashboards + Agents + Governance in Your Workspace

Follow this guide to stand up the 4-persona demo (CCO, GC, CMO, CFO) in
a Databricks workspace you own. The scripts auto-detect your Databricks
email and wire everything to plus-addressed user accounts, so all four
login-reset emails land in your real inbox.

## One-time workstation setup (before first deploy)

The Databricks CLI ships with a bundled Terraform whose embedded PGP
signing key has expired, so `databricks bundle deploy` fails with
`unable to verify checksums signature: openpgp: key expired`. Fix is
to install Terraform locally and point the CLI at it:

```bash
# macOS (Homebrew)
brew install terraform

# or pick from https://developer.hashicorp.com/terraform/install
# for other OSes

# point the Databricks CLI at the system Terraform
echo 'export DATABRICKS_TF_EXEC_PATH=$(which terraform)' >> ~/.zshrc
source ~/.zshrc

# verify
echo "$DATABRICKS_TF_EXEC_PATH"   # → /opt/homebrew/bin/terraform or similar
```

Done once per workstation. Keep the env var set (append to
`~/.zshrc` / `~/.bashrc`) so future shells have it.

Separately, the CLI itself can also lag — keep it current:

```bash
brew upgrade databricks-cli    # or re-install from databricks.com/cli
databricks --version           # 0.288.0+ is what this repo is tested against
```

### `DATABRICKS_BUNDLE_ENGINE=direct` is required

Newer Databricks workspaces require the bundle's "direct" deployment
engine for Unity Catalog resources. Without it, `databricks bundle deploy`
fails with `Catalog resources are only supported with direct deployment
mode`. Set it alongside the Terraform path:

```bash
echo 'export DATABRICKS_BUNDLE_ENGINE=direct' >> ~/.zshrc
source ~/.zshrc
```

> **Note:** `scripts/deploy_all.sh` exports this internally — you only
> need to set it in your shell if you're invoking `databricks bundle deploy`
> directly (debugging) outside the orchestrator.

### `REGULATION_PACK` — primary pack selector (now multi-pack live)

As of ADR-0001 M1–M4 (2026-05-11) the platform is **multi-pack at runtime** —
every pack under `regulations/` loads simultaneously and routes per data
subject via the `jurisdiction` column. There's no longer a single "active"
pack; the four packs currently shipped (`dpdp_2023`, `uk_gdpr`, `eu_gdpr`,
`ccpa`) all load on every deploy.

```bash
# Legacy compatibility — selects which pack is the "primary" for any
# back-compat single-pack code path (e.g., `load()` / `active_pack()` in
# governance_core/pack_loader.py). The actual multi-pack runtime ignores it.
export REGULATION_PACK=dpdp_2023   # primary; per-row routing is unaffected
```

`phase1_bootstrap` calls `loaded_packs()` and MERGEs every pack's rules into
`bronze.compliance_rules` tagged by source. `dsr_erasure.py`, the `pii_patterns`
composition layer, and the DPIA template merge all consume the full pack set.
See `docs/modular_framework.html` for the framework and
`regulations/README.md` for the pack-authoring contract + semver bump rules.

All `databricks bundle` commands in this guide work with the multi-pack
default; setting `REGULATION_PACK` is optional.

## Prerequisites

0. **Free-trial workspaces: Lakebase is commented out.** If you're on a
   paid workspace with Lakebase entitlement and want to use the OLTP
   tier, uncomment `database_instances.consent_oltp` +
   `database_catalogs.consent_catalog` in `resources/catalog_and_storage.yml`
   and the `dsr_portal` block in `resources/apps.yml`. On free trial,
   skip this — the POC uses Delta tables for consent events and
   standalone DSR scripts (`scripts/dsr_*.py`) in place of the app.

1. **Databricks CLI configured** for your workspace:
   ```bash
   databricks current-user me
   # should return your email
   ```

2. **You are a workspace admin.** The user-creation step needs admin.
   Check in the admin UI → Identity and access → Users → your user →
   "Admin access" toggle should be On. The workspace owner and anyone
   in the `admins` group qualifies.

3. **Your mail provider supports plus-addressing.** Gmail, Microsoft
   365, Fastmail, most corporate mail servers do; some (older
   Exchange, strict aliases) don't. Test by sending a mail to
   `you+test@yourdomain.com` — if it lands in your inbox, you're good.
   If not, use `--override-email someone_else@domain` when running the
   user-setup script.

4. **The underlying Compliance Pack POC is deployed.** The persona layer is an
   addition on top of an already-working POC. Full first-deploy sequence
   is in the `Phase 1 — base POC deploy` section below; follow it in
   order before running the persona orchestrator.

5. **Catalog + schemas + volumes — automated.**
   On Default-Storage workspaces the DAB API cannot create catalogs
   (the bundle errors with `Please use the UI to create a catalog with
   Default Storage`), so the `catalogs` / `schemas` / `volumes` resource
   blocks in `resources/catalog_and_storage.yml` are commented out. The
   one-time SQL setup that used to live here is now done by
   `scripts/bootstrap_catalog.py` — invoked as the `bootstrap_uc` step at
   the head of `scripts/deploy_all.sh`. No manual catalog/schema/volume
   creation is needed.

   To run it standalone (e.g., to verify the workspace allows catalog
   creation before doing a full deploy):

   ```bash
   python3 scripts/bootstrap_catalog.py            # idempotent
   python3 scripts/bootstrap_catalog.py --dry-run  # preview the plan
   ```

6. **Serverless DLT + serverless jobs.** Newer workspaces enforce
   serverless compute for DLT pipelines and Workflow jobs. The bundle
   already declares `serverless: true` on `medallion_pipeline` and drops
   the `job_clusters:` block on the jobs. If your workspace still allows
   classic compute, these can be switched back (see git history).

## Phase 1 — base POC deploy (first time)

> **One-command path:** `scripts/deploy_all.sh` runs this entire sequence
> + Phase 2 personas + the post-deploy smoke test in one go. Use it
> unless you need step-by-step control. The detailed sequence below is
> kept for debugging / partial-replay / understanding-what-runs scenarios.

Once the prerequisites above are met, this is the end-to-end sequence
to get from an empty workspace to a populated POC, before running the
persona orchestrator. Takes ~15 minutes.

```bash
# 1. Deploy bundle resources (DLT pipeline, 3 jobs, dashboard)
databricks bundle deploy --target dev

# 2. Generate synthetic CSVs locally (deterministic, seed=42)
python3 generate_synthetic_data.py --output-dir /tmp/dpdp_landing

# 3. Upload CSVs into the landing volume (one subfolder per source table)
for tbl in employees customers patients transactions users; do
  databricks fs mkdir "dbfs:/Volumes/compliance_pack/bronze/landing/${tbl}" 2>/dev/null || true
  databricks fs cp --recursive --overwrite \
    "/tmp/dpdp_landing/${tbl}/" \
    "dbfs:/Volumes/compliance_pack/bronze/landing/${tbl}/"
done

# 4. Run the medallion DLT pipeline — Bronze (Auto Loader) → Silver
databricks bundle run run_medallion --target dev

# 5. Seed the Lakeflow Connect simulation (3 Salesforce-shape silver tables,
#    written directly — not via Auto Loader)
python3 scripts/seed_salesforce_data.py

# 6. Seed the Federation simulation (federation_mock schema + 2 silver views)
python3 scripts/seed_federation_data.py

# 7. Seed bronze.data_sources with all 10 canonical sources. MUST run
#    before the refresh below — the classifier reads silver_table_name
#    from data_sources at pipeline-load time; if data_sources is empty
#    when the refresh fires, the classifier falls back to a 5-table
#    list and silently skips the SF + federation silvers.
python3 scripts/seed_data_sources.py

# 8. Targeted full-refresh of pii_findings. Two reasons full-refresh, not
#    incremental: (a) on serverless DLT there is a materialized-view race
#    condition on the first run — pii_findings reads silver via
#    spark.table() but can execute before the silver flows commit,
#    producing empty findings; (b) SF + federation silvers aren't
#    DLT-managed, so DLT skips pii_findings on incremental updates.
PIPELINE_ID=$(databricks pipelines list-pipelines -o json | \
  jq -r '.[] | select(.name | endswith("compliance_pack_medallion")) | .pipeline_id')

databricks api post "/api/2.0/pipelines/${PIPELINE_ID}/updates" --json \
  '{"full_refresh_selection":["compliance_pack.silver.pii_findings"]}'
# Wait until the update reaches COMPLETED (check in the UI, or
# `databricks pipelines list-pipeline-events $PIPELINE_ID`, or use the
# poll loop in scripts/deploy_all.sh:do_refresh).

# 9. Bootstrap the compliance layer (rules, gaps, consent events, notices,
#    views, UDFs, marketing-eligible view, persona overview/histogram views)
databricks bundle run phase1_bootstrap --target dev

# 10. Apply UC column tags + column masks + persona row filter
python3 scripts/apply_uc_tags.py
python3 scripts/apply_pii_masks.py
python3 scripts/apply_persona_row_filters.py

# 8. (Optional) Generate notices in the pack's other languages via the
#    foundation model endpoint. For DPDP this adds 7 watermarked notices
#    (bn-IN, te-IN, mr-IN, gu-IN, kn-IN, ml-IN, pa-IN) on top of the
#    3 hand-authored ones (en-IN, hi-IN, ta-IN). Re-runnable with
#    --overwrite, scope to one language via --language <code>, preview
#    prompts via --dry-run.
python3 scripts/generate_multilang_notices.py
```

After step 7, the POC is fully populated and demo-ready. You should see:
- 20 rows in `silver.pii_findings` (across all 5 source tables)
- 92 rows in `silver.compliance_gaps` (critical/high/medium breakdown)
- 1,000 events in `compliance.consent_events_log`
- `compliance.personal_data_register` view returning the full register
- UC column tags on 20 columns; column masks on 17+ PII columns

Now proceed to the persona layer below.

## One-command deploy

```bash
python3 scripts/setup_all_personas.py
```

That runs all six underlying scripts in order, aborts on the first
failure, and prints a checklist of the manual steps at the end.
Takes about 3 minutes on a cold workspace.

### Flags

```bash
python3 scripts/setup_all_personas.py --dry-run
#   prints the command sequence without executing anything

python3 scripts/setup_all_personas.py --from grants
#   start from step 'grants' (skip slice/genie/instr/attach/users)
#   useful when a single step failed and you've fixed it

python3 scripts/setup_all_personas.py --skip slice --skip attach
#   skip individual steps; step keys are:
#     slice · genie · instr · attach · users · grants · acls
```

### What each step does

If you need to run them individually:

```bash
# 1. slice   — Slice master dashboard into 4 persona-specific ones + upload/update
python3 scripts/slice_dashboards.py --upload

# 2. genie   — Create 4 persona Genie spaces (scoped to each persona's tables)
python3 scripts/setup_persona_genie_spaces.py

# 3. instr   — Apply knowledge-store config (instructions, filters, measures,
#              dimensions, example queries) from configs/genie/*.yaml to each space
python3 scripts/configure_persona_genie_instructions.py

# 4. attach  — Add 'Ask the X Agent' link tiles to each dashboard
python3 scripts/attach_genie_to_dashboards.py

# 5. users   — Create 4 plus-addressed workspace users (auto-detects deployer)
python3 scripts/setup_persona_users.py
#   (use --override-email other@domain if your mail doesn't support plus-addressing)

# 6. grants  — Apply UC SELECT grants to each persona user
python3 scripts/apply_persona_uc_grants.py

# 7. acls    — Warehouse CAN_USE + dashboard CAN_READ (embed_credentials=false) +
#              Genie CAN_RUN for each persona user
python3 scripts/apply_persona_workspace_acls.py
```

All eight scripts (the seven above + the orchestrator) are
**idempotent** — re-running any of them is safe. Dashboards update
in place instead of duplicating, users are skipped if they exist,
grants no-op if already applied.

### Optional — workspace-wide capabilities beyond the persona layer

These aren't part of the orchestrator because they aren't
persona-specific — they're workspace-level governance or external-
facing capabilities. Run them manually if needed:

```bash
# Delta Share for external auditors — 3 read-only objects
# (personal_data_register view + notice_versions table + compliance_gaps table).
# The script also tries to add gold.consent_coverage_summary as a fourth object
# but Databricks Delta Sharing platform-rejects views built on top of UC row-
# filtered tables; the script logs ✗ ADD VIEW and continues with the 3 that
# succeeded. See docs/test_results.html → T16.1 for the full explanation.
python3 scripts/create_audit_share.py

# DSR discovery (DPDP §11 access request)
python3 scripts/dsr_discovery.py --principal-id <id> --verbose

# DSR erasure (DPDP §12(b) — DESTRUCTIVE; requires --confirm)
python3 scripts/dsr_erasure.py --principal-id <id> --request-id <dsr_id> --confirm

# Re-apply column masks if they drift (phase1_bootstrap already runs them)
python3 scripts/apply_pii_masks.py
```

### DPIA Review app (apps/dpia_review/)

The Phase 4 Theme 3 Databricks App provides the human-review surface
for `compliance.dpia_runs` — a Streamlit UI that captures the verified
Databricks user identity for the approval action (replacing the Phase 2
CLI's self-asserted-reviewer flag). The app is declared in
`resources/apps.yml` and its source lives at `apps/dpia_review/`.

**Deployment is fully automated** by `scripts/deploy_all.sh`:

```bash
scripts/deploy_all.sh
# … runs every step incl. bundle, personas, and finally:
# step `app_perms` — invokes scripts/grant_dpia_app_permissions.py to
# provision the app's runtime SP with the four grants it needs to
# actually function (UC SELECT+MODIFY on dpia_runs, UC READ_VOLUME on
# dpia_artifacts, workspace CAN_USE on the warehouse, app CAN_USE for
# CCO+GC+CFO personas — CMO is intentionally excluded).
```

To re-apply just the app permissions (e.g., after rotating personas):

```bash
python3 scripts/grant_dpia_app_permissions.py            # apply
python3 scripts/grant_dpia_app_permissions.py --dry-run  # plan only
```

The script is idempotent — UC GRANTs no-op on re-grant and workspace
ACL PATCHes add to existing grantees rather than replace them. UC
remembers across re-deploys, so after the first run the grants
persist.

**In-app role gate** — the Approve button is shown only when the
logged-in user's email matches a workspace user with a plus-addressed
DPDP persona email (`<base>+compliance-cco@<domain>` or `+compliance-gc@`). The app
discovers these at runtime via SCIM (listing workspace users matching
the `+compliance-` pattern), so re-running `scripts/setup_persona_users.py`
auto-updates the role gate on the next app request — no app restart
needed. CFO can open the app but sees view-only mode (Approve button
hidden); CMO can't open the app at all.

**Verifying end-to-end** after `deploy_all.sh` finishes:
1. Trigger a generator run — see "Quarterly DPIA workflow" below.
2. Open the app URL (from the `databricks bundle deploy` output, or
   `databricks apps get compliance-dpia-review -o json | jq .url`).
3. Sign in as the CCO or GC persona — you should see the run with
   status='draft', open the detail view, click Approve. Audit row
   flips to status='approved' with `reviewed_by` set to the verified
   email (not what's typed anywhere).
4. Sign in as CFO — you should see the same row but the Approve
   button is hidden.
5. Sign in as CMO — you should hit a permission denied opening the
   app at all.

### Quarterly DPIA workflow (DPDP §10 / GDPR Art. 35)

The `dpia_generator` job (in `resources/jobs.yml`) auto-drafts a Data
Protection Impact Assessment from live UC metadata once a quarter, and
appends one audit row per run to `compliance.dpia_runs` with
`status='draft'`. A human reviews the draft and runs the approval CLI
to flip the status to `approved`.

```bash
# 1. Trigger the generator manually for the first run, or wait for the
#    quarterly cron (the schedule ships PAUSED; unpause via the Jobs UI
#    or `databricks jobs update` once you've verified an end-to-end run).
databricks bundle run dpia_generator --target dev

# 2. List pending drafts
python3 scripts/approve_dpia.py --list-drafts

# 3. Approve a specific draft (artifact preview prints first; --no-preview
#    to skip; --notes "..." to attach reviewer comments)
python3 scripts/approve_dpia.py \
    --run-id <12-char-hex> \
    --reviewer <your-email>
```

The CLI is idempotent: re-running on an already-approved run prints the
existing reviewer + timestamp and exits 0 without changes. Approving a
non-existent or `superseded` run exits 1 with a clear error.

**Service-principal grant** — required only when the bundle's `run_as`
is switched from a workspace user to a service principal (the default
in `databricks.yml` is the deployer's user, so this isn't needed
out-of-the-box). The SP must hold `CAN_QUERY` on the foundation-model
serving endpoint configured in the job's `model_endpoint` parameter
(default `databricks-gpt-oss-120b`); without that grant the scheduled
run hits a 403 and the DPIA silently fails to generate.

```bash
# Find the endpoint's permission resource id
ENDPOINT_ID=$(databricks api get \
    /api/2.0/serving-endpoints/databricks-gpt-oss-120b -o json | jq -r .id)

# Grant CAN_QUERY to the SP (replace <sp-app-id>)
databricks api put /api/2.0/permissions/serving-endpoints/${ENDPOINT_ID} \
    --json '{"access_control_list":[{"service_principal_name":"<sp-app-id>","permission_level":"CAN_QUERY"}]}'
```

Note on masks and residency filter: both are applied automatically by
`phase1_bootstrap` — these standalone scripts exist for re-application
or targeted updates (e.g., after adding new PII columns).

## Manual one-time setup per persona user

After script 4 creates the users, you need to:

1. **Toggle entitlements** in the admin UI (not yet scripted):
   - Open `https://<your-workspace>.cloud.databricks.com/settings/workspace/identity-and-access/users`
   - For each of the 4 new users (e.g. `you+compliance-cco@...`):
     - **Consumer access → On** (required for dashboards + Genie spaces)
     - **Workspace access → Off** (optional; cleaner persona semantics)
     - Leave Admin access **Off** and Databricks SQL access **On**

2. **Set a password** via the Forgot-password flow (admin-settable
   passwords are disabled on most modern workspaces):
   - Open the workspace login URL in an incognito window
   - Enter the persona email (e.g. `you+compliance-cco@...`)
   - Click "Forgot password"
   - Check your real inbox — plus-addressing delivers the reset link
   - Click the link, set a password, log in → you're now the persona user

Repeat the login test per persona to confirm the boundaries:

- Log in as CCO → see CCO dashboard → try to open the CMO dashboard URL → 403 ✓
- Click "Ask the CCO Agent" banner → lands in CCO Genie space
- Type a cross-scope question ("can I email customer X?") → agent
  either refuses or returns nothing meaningful (Genie's data_sources
  allowlist doesn't include `marketing_eligible_principals` for CCO)

## Files the scripts produce

All live under `dashboards/personas/` (gitignored by convention,
regenerated per workspace):

```
.dashboard_ids.json     persona → Lakeview dashboard id
.genie_space_ids.json   persona → Genie space id
.persona_emails.json    persona → plus-addressed email
```

Downstream scripts read these to stay in sync; never edit by hand.

## Troubleshooting

**`setup_persona_users.py` fails with 403.** You're not a workspace
admin. Ask the account owner to add you, or have them run the script.

**`setup_persona_users.py` succeeds but UC grants fail with
`PRINCIPAL_DOES_NOT_EXIST`.** The email isn't recognized by UC. This
happens if the email doesn't match the workspace's SCIM user store
exactly. Check `databricks users list | grep dpdp-` and make sure the
4 users exist with the exact plus-addressed emails. Case-sensitive.

**Dashboard tile shows a permission error for a persona user.** The
tile queries a table not in that persona's UC grants. Two options:
either add the table to the persona's allowlist in
`scripts/apply_persona_uc_grants.py` (broadens the UI-render
boundary), or remove the tile from the persona's dashboard slice
(`scripts/slice_dashboards.py` — would need a per-page trim). The
Genie space's `data_sources` allowlist is independent of this and
stays narrowly scoped.

**"Forgot password" link doesn't arrive in your inbox.** Either plus-
addressing isn't supported by your mail provider (use
`--override-email`) or the workspace has password-login disabled
entirely (check with an account admin — you may need to rely on the
API-proof demo path instead of live logins; see
`docs/persona_governance.md` for details).

**Login succeeds but clicking a dashboard shows "you don't have
access".** You forgot to turn on **Consumer access** in the admin UI
for that user. Required.

## Cleaning up / starting over

If you want to wipe the persona layer and start fresh:

```bash
# Trash the Genie spaces (one API call each, listed by .genie_space_ids.json)
python3 -c "
import json, subprocess, pathlib
p = pathlib.Path('dashboards/personas/.genie_space_ids.json')
for sid in json.loads(p.read_text()).values():
    subprocess.run(['databricks','genie','trash-space', sid])
p.unlink()
"

# Delete the persona users (one API call each)
python3 -c "
import json, subprocess
out = subprocess.run(['databricks','users','list','-o','json'], capture_output=True, text=True).stdout
for u in json.loads(out):
    if '+compliance-' in u.get('userName',''):
        subprocess.run(['databricks','users','delete', u['id']])
        print('deleted', u['userName'])
"

# Delete the sliced dashboards (one API call each)
python3 -c "
import json, subprocess, pathlib
p = pathlib.Path('dashboards/personas/.dashboard_ids.json')
for did in json.loads(p.read_text()).values():
    subprocess.run(['databricks','api','delete', f'/api/2.0/lakeview/dashboards/{did}'])
p.unlink()
"

# UC grants auto-orphan when users are deleted, but to be tidy you can
# re-run scripts/apply_persona_uc_grants.py — it'll skip missing
# personas cleanly.
```

The master dashboard, catalog, schemas, and tables are untouched by
this cleanup.
