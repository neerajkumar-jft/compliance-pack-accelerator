# §11 · Deployment (Databricks Asset Bundle)

> ⚠️ **Pre-build planning document.** The deploy sequence in §11.4 references jobs that were never authored (`init_lakebase_schema`, `ingest_synthetic_data`, `materialize_register_and_tags`, `generate_consent_events`, `sync_consent_to_delta`, `process_dsr_request`, `e2e_verify`) and the DSR-portal Databricks App that was dropped for free-trial compatibility. **For deploying the POC today, follow [`docs/persona_deploy.md`](docs/persona_deploy.md) and [`README.md`](README.md).** The actual active bundle resources are in `resources/catalog_and_storage.yml`, `resources/pipelines.yml`, `resources/jobs.yml` (only `run_medallion`, `phase1_bootstrap`, `retention_enforcement`), and `resources/dashboards.yml`.

## 11.1 · Why this section exists

The rest of the spec describes *what* the POC does. This section describes *how it deploys*. The deployment story is a first-class architectural concern — a POC that takes three days to set up but one command to tear down is not easy to deploy; a POC that takes one command in each direction is.

The canonical deployment path is a Databricks Asset Bundle (DAB). Every resource the POC creates — Unity Catalog objects, Lakebase instance, DLT pipeline, Workflow jobs, Databricks App, AI/BI dashboard — is declared as code in the bundle. Deployment is one command. Tear-down is one command. Re-deployment after a bad day is one command.

This is the architectural commitment that makes the POC Databricks-native. It is not a deployment convenience; it is the reason the POC hangs together.

## 11.2 · What a DAB gives us

The Databricks Asset Bundle (`databricks.yml` at the repo root plus `resources/*.yml`) replaces every manual setup step from earlier sections with a declarative resource block. Concretely:

| Manual step before DAB | Replaced by |
|------------------------|-------------|
| `CREATE CATALOG compliance_pack` in a notebook | `resources.catalogs.compliance_pack_catalog` in `catalog_and_storage.yml` |
| `CREATE SCHEMA bronze, silver, gold, compliance, federation_mock` | `resources.schemas.*` blocks (federation_mock is created on demand by `scripts/seed_federation_data.py`) |
| Provisioning a Lakebase instance in the UI | `resources.database_instances.consent_oltp` |
| Hand-writing an Auto Loader notebook | The `medallion_pipeline` resource plus `pipelines/medallion.py` |
| Creating 7 Workflow jobs in the UI | `resources.jobs.*` in `jobs.yml` |
| Hosting the DSR portal as a notebook-exposed endpoint | `resources.apps.dsr_portal` plus `apps/dsr_portal/` |
| Building the Day 14 dashboard in SQL Editor | `resources.dashboards.*` plus `dashboards/dpdp_compliance.lvdash.json` |
| Applying grants in Unity Catalog Explorer | `permissions` blocks in each resource |

There is no manual UI interaction in the deployment path. If you find yourself clicking in the Databricks UI to create something the POC depends on, stop — either add it to the bundle, or raise a scope question.

## 11.3 · Repository layout for DAB

```
compliance_pack_spec/
├── databricks.yml                 ← DAB root; deploy entry point
├── resources/
│   ├── catalog_and_storage.yml    ← catalogs, schemas, volumes, Lakebase
│   ├── pipelines.yml              ← DLT medallion pipeline
│   ├── jobs.yml                   ← all Workflow jobs
│   ├── apps.yml                   ← Databricks App (DSR portal)
│   └── dashboards.yml             ← AI/BI dashboard
├── pipelines/
│   ├── medallion.py               ← DLT Bronze + Silver
│   ├── classification_dlt.py      ← DLT pii_findings table
│   ├── init_lakebase.py           ← one-shot Lakebase DDL
│   ├── generate_synthetic.py      ← synthetic data generator
│   ├── generate_consent_events.py ← 1000 consent events
│   ├── sync_lakebase_to_delta.py  ← Lakebase → Delta sync
│   ├── apply_uc_tags.py           ← UC column tag application
│   ├── dsr_discovery.py           ← DSR stage 1
│   ├── dsr_execute.py             ← DSR stage 2 (erasure)
│   └── dsr_bundle.py              ← DSR stage 3 (bundle generation)
├── apps/dsr_portal/
│   ├── app.yaml                   ← Databricks App definition
│   ├── main.py                    ← FastAPI entrypoint
│   ├── requirements.txt
│   └── README.md
└── dashboards/
    └── dpdp_compliance.lvdash.json
```

The `pipelines/` directory holds the executable notebooks and scripts that the DAB resources reference. The bundle itself is metadata; the pipelines are where the actual logic lives.

## 11.4 · The deployment sequence

From a fresh terminal with Databricks CLI installed and configured:

```bash
# 1. Clone / unpack the spec repo
cd /path/to/compliance_pack_spec

# 2. Validate the bundle (catches schema errors before deploy)
databricks bundle validate --target dev

# 3. Deploy everything
databricks bundle deploy --target dev

# 4. Initialize Lakebase schema (once per fresh deployment)
databricks bundle run init_lakebase_schema --target dev

# 5. Generate synthetic data
databricks bundle run ingest_synthetic_data --target dev

# 6. Run the medallion pipeline (Bronze → Silver → pii_findings)
databricks bundle run run_medallion --target dev

# 7. Apply UC tags + materialize the register view
databricks bundle run materialize_register_and_tags --target dev

# --- Module 01 artifacts are now live; check the dashboard ---

# 8. Generate consent events (Day 9)
databricks bundle run generate_consent_events --target dev

# 9. Enable Lakebase → Delta sync
databricks bundle run sync_consent_to_delta --target dev

# --- Module 02 artifact live ---

# 10. Process a DSR via the app (Day 11)
#     Open the DSR portal URL from the deployment output
#     Submit a request for customer_04217
#     The process_dsr_request job triggers automatically
```

Total elapsed time from clone to live demo: roughly 90 minutes, with most of it waiting for the pipeline and classification to finish. No manual clicks in any step.

## 11.5 · What `databricks bundle deploy` actually does

Mapping the command to the resources it creates:

```
bundle deploy
  ├── uploads pipelines/*.py to /Workspace/.../.bundle/dpdp-poc/dev/pipelines/
  ├── uploads apps/dsr_portal/ to the Databricks Apps workspace volume
  ├── creates catalog compliance_pack (resources/catalog_and_storage.yml)
  │   ├── schema bronze, silver, gold, compliance
  │   ├── volume bronze.landing, bronze.checkpoints, compliance.dsr_bundles
  │   └── grants to service principal
  ├── creates Lakebase instance dpdp-poc-consent
  │   └── creates database compliance_pack_consent
  ├── creates DLT pipeline compliance_pack_medallion (resources/pipelines.yml)
  │   └── with libraries pointing at pipelines/medallion.py, pipelines/classification_dlt.py
  ├── creates Workflow jobs (resources/jobs.yml)
  │   ├── init_lakebase_schema
  │   ├── ingest_synthetic_data
  │   ├── run_medallion
  │   ├── materialize_register_and_tags
  │   ├── generate_consent_events
  │   ├── sync_consent_to_delta (scheduled, initially paused)
  │   ├── process_dsr_request
  │   └── e2e_verify
  ├── creates Databricks App dpdp-dsr-portal (resources/apps.yml)
  │   └── with resource grants (Lakebase, UC, job trigger)
  └── creates AI/BI dashboard "DPDP Compliance POC" (resources/dashboards.yml)
```

Every one of these is declarative. Redeploying doesn't create duplicates — it converges the workspace to the declared state.

## 11.6 · Tear-down

```bash
databricks bundle destroy --target dev
```

Removes everything the bundle created, in reverse dependency order. The only state that persists is the Unity Catalog audit log (which is a workspace-level resource, not a bundle resource). If you need to nuke even the audit trail of the POC, that requires workspace-level administrative action.

## 11.7 · Environments (`dev` vs `prod`)

Two targets are declared in `databricks.yml`:

**`dev`** — the trial workspace target. Used for the 14-day POC. `mode: development` means the bundle prefixes resource names with the deploying user's identity to avoid conflicts with other developers. Default target for `databricks bundle deploy`.

**`prod`** — placeholder for Phase 1 deployment to the customer's production workspace. `mode: production` enforces naming conventions and requires a service principal run-as identity. Not used during the POC sprint; activated when Phase 1 starts.

Switching targets does not change any Python or SQL code. The same bundle deploys to dev and prod; what changes is workspace host, run-as identity, and the implicit naming prefix. This is the forward-compatibility story the POC commits to: what we prove on trial deploys unchanged to production.

## 11.8 · Bundle variable resolution

The bundle uses variables for values that vary per deployment:

```yaml
variables:
  catalog_name: { default: compliance_pack }
  lakebase_instance_name: { default: dpdp-poc-consent }
  notification_email: { default: dpdp-poc-team@example.com }
  ...
```

Override at deploy time:
```bash
databricks bundle deploy --target dev --var catalog_name=custom_catalog
```

Or set in a per-target block for permanent overrides. Avoid using variables for things that should be spec invariants — the nine PII categories, the six consent purposes, the four capture channels are intentionally hardcoded and should not be knobs.

## 11.9 · Trial-workspace-specific constraints

Some bundle resources that work in production workspaces have constraints in the trial:

- **Lakebase instance size**: forced to `CU_1` (smallest) to stay within the trial credit envelope
- **DLT pipeline clusters**: `num_workers: 0` (single-node) for the same reason
- **SQL warehouse**: the dashboard resource requires a serverless SQL warehouse; the trial workspace provides one by default, but confirm before deploy
- **Scheduled jobs** (like `sync_consent_to_delta`): deploy with `pause_status: PAUSED` so they don't immediately burn credits; enable manually on Day 8

See `reference/databricks_trial_limits.md` for the full constraints list.

## 11.10 · Verifying the deployment

After `databricks bundle deploy` completes, run the environment verification notebook (`tests/verify_environment.md`). Every check should pass; if any fail, the bundle deploy did not fully succeed — fix before proceeding.

Additionally, the bundle itself can be inspected:

```bash
# List all resources the bundle manages
databricks bundle summary --target dev

# Check a specific resource
databricks bundle summary --target dev -o json | jq '.resources.jobs.run_medallion'
```

## 11.11 · When DAB is not enough

Three operations still require manual action outside the bundle:

1. **Workspace provisioning itself** — the trial workspace must exist before DAB can deploy into it. Handled in Day 0 setup (`runbook/setup_day_00.md`).

2. **Lakebase sync table creation** — Lakebase→Delta sync tables are declared through a Databricks UI flow in the current release. The bundle configures the job that will consume the sync, but the sync table itself may need manual setup on Day 8. This is a gap that closes as the Databricks Lakebase resource matures.

3. **Ad-hoc data resets during the sprint** — rolling back a classification run, for example, is a data-state operation, not a bundle-state operation. Handled in `runbook/rollback.md`.

Everything else is `bundle deploy` / `bundle destroy`. The two commands are the POC's canonical lifecycle.

## 11.12 · Where this fits in the six-module future

The POC bundle deploys Modules 01 and 02 at full spec scope. Phase 1 extends the same bundle with additional resources:

- **Module 03 (Rights Hub)**: extend `resources/apps.yml` with the full DSR portal; extend `pipelines/` with Zone 1 discovery and split execution; extend `resources/jobs.yml` with the DSR residual scheduler
- **Module 04 (Breach Detection)**: add a Lakewatch-backed resource once Private Preview opens; add breach-response jobs
- **Module 05 (Compliance Audit)**: add the scoring engine as a new DLT pipeline reading from all modules; add the DPIA generation job using Agent Bricks
- **Module 06 (Retention)**: extend `resources/jobs.yml` with retention purge schedulers; add the tokenization vault integration

Each module becomes a new `resources/*.yml` file and a new subdirectory under `pipelines/`. The bundle grows; the deployment contract (one command to deploy, one to destroy) does not change. This is why DAB is load-bearing for the architecture: it is the contract that scales from a 14-day POC to a six-module platform without a re-architecture.

Now return to the specific module details in `03_data_contracts.md` (which references the DLT pipeline from this section) through `07_dsr_execution.md` (which references the Databricks App from this section).
