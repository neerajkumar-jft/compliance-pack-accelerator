# §2 · Runtime and environment

> ⚠️ **Pre-build planning document.** Lakebase setup, `dpdp-poc-builder` service-principal grants, and the 7-job workflow list in §2.8 don't apply on the free-trial deploy path. **For deploying today, follow [`docs/persona_deploy.md`](docs/persona_deploy.md).**

## 2.0 · The canonical setup path — read this first

**Every environment resource this section describes is provisioned by `databricks bundle deploy`.** The bundle (`databricks.yml` at the repo root) is the authoritative declaration of the POC's runtime environment. See §11 for the deployment walkthrough.

This section exists as **reference material** — so you understand what the bundle creates, why it creates it that way, and what to do if you need to intervene manually. The subsections below describe the target state; the bundle is the mechanism that achieves it.

Three consequences of this layering:

1. **Do not run the SQL in `schemas/*.sql` directly unless rolling back.** The DDL is executed by the appropriate bundle job (`init_lakebase_schema` for Lakebase tables, the DLT pipeline for Delta tables, the `materialize_register_and_tags` job for the register view). Manual execution risks state drift.

2. **Do not create jobs or pipelines through the UI.** All of them are declared in `resources/jobs.yml` and `resources/pipelines.yml`. If something is needed, add it to the bundle; don't click it into existence.

3. **Do not grant permissions via the UI.** Grants are declared in the `permissions` blocks of the resource YAML files. Manual grants cause the next `bundle deploy` to either overwrite them or flag them as drift.

The one exception is per-sprint data-state operations (rolling back a bad classification run, resetting the synthetic DSR principal for a repeat demo) — these are documented in `runbook/rollback.md` and are not part of the declarative state.

## 2.1 · Target workspace

The POC runs on a **Databricks free trial workspace** provisioned fresh for this engagement. Before Day 1 of implementation work, confirm the following are in place:

- Workspace provisioned on AWS, Azure, or GCP (the spec is cloud-agnostic; the human collaborator chooses based on where the sample data will live)
- Region selected with Lakebase availability (confirm via Databricks documentation current at time of setup)
- Workspace admin access granted to the human collaborator, who will delegate sub-admin privileges to the service principal Claude Code will use
- Compute budget reviewed; the trial workspace has a capped credit balance that must last the full 14 days

Before any build work begins, read `reference/databricks_trial_limits.md` in full. If you discover a limit you did not know about mid-build, stop and raise it with your human collaborator before working around it.

## 2.2 · Databricks Runtime version

Use **Databricks Runtime 15.4 LTS or later** for all clusters and workflows. This version is the minimum that supports:

- Unity Catalog with column-level tags (required by §4's tagging approach)
- `ai_classify` and `ai_extract` functions (required by §4's LLM classification path)
- Lakebase sync tables (required by §5's Delta sync topology)
- Delta Lake table features needed for time travel reproducibility

Do not use ML Runtime unless a specific task requires it; the standard runtime is adequate for this POC and uses fewer credits.

## 2.3 · Unity Catalog structure

Create the following Unity Catalog structure on Day 1. Every table, view, and function in the POC lives under this hierarchy:

```
compliance_pack/                           (top-level catalog)
├── bronze/                         (raw ingested data)
│   ├── source_employees            (mirrors source file 1:1)
│   ├── source_customers
│   ├── source_patients
│   ├── source_transactions
│   ├── source_users
│   └── data_sources                (metadata about what is ingested)
├── silver/                         (PII-tagged, classified)
│   ├── employees_tagged            (employees with PII metadata columns)
│   ├── customers_tagged
│   ├── patients_tagged
│   ├── transactions_tagged
│   ├── users_tagged
│   ├── pii_findings                (column-level discovery results)
│   └── discovered_tables           (table-level scan metadata)
├── gold/                           (consent-aware, masked)
│   └── marketing_eligible_principals   (example consent-filtered view)
└── compliance/                     (the stakeholder-facing layer)
    ├── personal_data_register      (view over silver.*_tagged + pii_findings)
    ├── consent_events_log          (Delta copy of Lakebase consent events)
    └── dsr_requests                (DSR intake and fulfillment log)
```

Each schema has a specific purpose; do not cross boundaries. Bronze is write-once from ingestion. Silver is materialized by the classification job. Gold is derived from Silver + consent decisions. Compliance is views only — no writes from build code.

### Unity Catalog grants required

Grant the service principal (see §2.6) the following at the catalog level:

```sql
GRANT USE CATALOG ON CATALOG compliance_pack TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA compliance_pack.bronze TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA compliance_pack.silver TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA compliance_pack.gold TO `<service-principal-name>`;
GRANT USE SCHEMA ON SCHEMA compliance_pack.compliance TO `<service-principal-name>`;
GRANT CREATE TABLE, CREATE VIEW, MODIFY ON SCHEMA compliance_pack.bronze TO `<service-principal-name>`;
GRANT CREATE TABLE, CREATE VIEW, MODIFY ON SCHEMA compliance_pack.silver TO `<service-principal-name>`;
GRANT CREATE TABLE, CREATE VIEW, MODIFY ON SCHEMA compliance_pack.gold TO `<service-principal-name>`;
GRANT CREATE VIEW, MODIFY ON SCHEMA compliance_pack.compliance TO `<service-principal-name>`;
GRANT APPLY TAG ON CATALOG compliance_pack TO `<service-principal-name>`;
```

The `APPLY TAG` privilege is what lets the classification job call `ALTER TABLE ... SET TAGS` to annotate columns with PII metadata. Without it, tagging fails silently with a confusing permission error.

## 2.4 · Lakebase configuration

Create a Lakebase instance for the consent OLTP tier:

- Instance name: `dpdp-poc-consent`
- Size: smallest available tier (trial workspace; this is a demo, not a load test)
- Database name: `compliance_pack_consent`
- Initial schema: `public`
- Connection method: Databricks SQL connection (uses the workspace's integrated auth; do not create a separate JDBC URL and password)

Tables to create in Lakebase on Day 8 (DDL in `schemas/consent_events.sql` and `schemas/notice_versions.sql`):

- `consent_events` — the main event log
- `notice_versions` — the versioned consent notices
- `data_principals` — the minimal principal registry (id, created_at, age_verification_status)
- `dsr_requests` — intake queue for data subject rights requests

Set up a Lakebase→Delta sync table for `consent_events` and `dsr_requests` with a 60-second refresh interval. The Delta copies live at `compliance_pack.compliance.consent_events_log` and `compliance_pack.compliance.dsr_requests` respectively. Schema in `schemas/consent_events_delta.sql`.

## 2.5 · AI functions and Agent Bricks

The POC uses two Databricks AI functions for unstructured-adjacent PII classification:

- **`ai_classify(text, labels)`** for category assignment on long-form text fields (e.g., the `address` column, the `diagnosis` column in patients)
- **`ai_extract(text, schema)`** for extracting entities where Presidio's regex-based approach under-performs (rarely needed in this POC's structured data but kept as a fallback)

Agent Bricks is **out of scope for this POC** (per §1.4); no DPIA drafting, no DPBI notification composition, no notice translation. The endpoints need not be provisioned.

## 2.6 · Authentication and secrets

### Service principal

Create a Databricks service principal for the POC:

- Display name: `dpdp-poc-builder`
- Use this identity for every notebook run, every workflow execution, every API call made by Claude Code
- Never use a personal user token; the audit log must attribute every action to this service principal

Configure OAuth token-based auth rather than personal access tokens. The trial workspace supports OAuth; using it sidesteps the token-rotation friction that slows down longer engagements.

### Secret scope

Create one secret scope named `dpdp-poc` for any secrets the build requires. The POC is designed to minimize secrets — we use synthetic data and Lakebase native auth — but the scope must exist for:

- Any future source-system credentials (none expected in the POC)
- The Lakebase connection string if we choose not to use integrated auth
- API keys for any demo integrations (none expected)

If you reach for a secret that is not yet configured, stop and raise it with your human collaborator before creating one.

## 2.7 · Python dependencies

The POC uses these Python packages. Install them via a cluster init script or cluster library configuration:

```
presidio-analyzer==2.2.355
presidio-anonymizer==2.2.355
faker==33.3.1
pandas==2.2.3
```

Presidio versions prior to 2.2.x have breaking API changes. Pin to the exact version above.

Do **not** install: pyodbc, psycopg2, pymongo, kafka-python, snowflake-connector-python, salesforce-python, or any other Python database/SaaS connector. This POC uses Databricks-native ingestion patterns throughout; adding Python connectors recreates the architectural anti-pattern flagged in `09_known_pitfalls.md` section 9.1.

## 2.8 · Workflow and job configuration

Create a Databricks Workflow named `dpdp-poc-build` with these jobs (defined in detail in individual sections):

| Job name | Triggered by | Spec reference |
|----------|--------------|----------------|
| `ingest_synthetic_data` | Manual (Day 1) | §3.2, §6 |
| `classify_pii` | After ingest completes | §4 |
| `materialize_register` | After classify completes | §3.5 |
| `generate_consent_events` | Manual (Day 9) | §5, §6 |
| `sync_consent_to_delta` | 60-second schedule | §5.4 |
| `process_dsr_request` | API-triggered (Day 11) | §7 |

All jobs run as the service principal `dpdp-poc-builder`. All jobs log to Unity Catalog audit tables automatically. No job should have a retry count above 2 — the goal during POC is to surface failures, not hide them.

## 2.9 · Cluster configuration

Use a single shared cluster for the entire POC to keep credit consumption predictable:

- Cluster mode: Single node
- Runtime: Databricks Runtime 15.4 LTS
- Node type: smallest available (e.g., `i3.xlarge` on AWS, `Standard_DS3_v2` on Azure)
- Auto-termination: 30 minutes
- Libraries: Presidio Analyzer, Presidio Anonymizer, Faker (per §2.7)
- Policies: none (trial workspace does not require cluster policies)

Do not create separate clusters for different tasks. Do not use autoscaling. Do not use GPU instances. The POC fits comfortably on a single small node because our data volumes are intentionally modest.

## 2.10 · Source data location

Synthetic source data (generated per §6) is written as CSV files to a workspace volume:

```
/Volumes/compliance_pack/bronze/landing/
├── employees/      (employees_YYYYMMDD.csv.gz)
├── customers/
├── patients/
├── transactions/
└── users/
```

Auto Loader reads from these volume paths into Bronze Delta tables. The volume must exist before Day 1 ingestion starts; create it via:

```sql
CREATE VOLUME IF NOT EXISTS compliance_pack.bronze.landing;
```

Grant `READ VOLUME` and `WRITE VOLUME` to the service principal.

## 2.11 · Environment verification script

Before starting Day 1 work, run the environment verification script at `tests/verify_environment.md`. It checks:

- Workspace reachable via the configured auth
- Catalog `compliance_pack` exists and has the expected schemas
- Service principal has expected grants
- Lakebase instance reachable
- Required Python packages installed on the cluster
- Volume `/Volumes/compliance_pack/bronze/landing/` exists and is writable
- `ai_classify` and `ai_extract` functions callable

If any check fails, fix the environment before starting build work. Do not start Day 1 against a partially-configured environment; the failure modes are confusing and waste hours.

Now proceed to `03_data_contracts.md`.
