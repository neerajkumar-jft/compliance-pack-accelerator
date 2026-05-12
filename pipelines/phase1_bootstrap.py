# Databricks notebook source
# MAGIC %md
# MAGIC # DPDP POC — Phase 1 Bootstrap
# MAGIC
# MAGIC Single notebook that populates all compliance-layer tables and views on a
# MAGIC fresh workspace. Runs idempotently; safe to re-execute.
# MAGIC
# MAGIC **Prerequisites** (produced by the medallion DLT pipeline):
# MAGIC - `silver.employees_tagged` / `customers_tagged` / `patients_tagged` / `transactions_tagged` / `users_tagged`
# MAGIC - `silver.pii_findings`      (from classification_dlt.py)
# MAGIC - `silver.discovered_tables` (from medallion.py)
# MAGIC
# MAGIC **What this notebook produces:**
# MAGIC - `bronze.compliance_rules`  (9 DPDP rules)
# MAGIC - `bronze.data_sources`      (10 ingestion-source rows; classifier reads silver_table_name from here)
# MAGIC - `silver.compliance_gaps`   (~135 gaps, from rules × pii_findings)
# MAGIC - `compliance.consent_events_log` (1,000 synthetic consent events, deterministic seed=42)
# MAGIC - `compliance.notice_versions`    (1 notice: marketing_notice v1 en-IN)
# MAGIC - `compliance.dsr_requests`        (schema only — DSR app writes rows)
# MAGIC - `compliance.personal_data_register` (view on pii_findings)
# MAGIC - `compliance.has_active_consent()`   (UDF)
# MAGIC - `gold.marketing_eligible_principals` (view)
# MAGIC - `gold.consent_coverage_summary`      (view)
# MAGIC
# MAGIC On a fresh teammate workspace, after `bundle deploy` and the `run_medallion`
# MAGIC job, run this notebook to finish populating the compliance layer. After this
# MAGIC completes, `scripts/setup_all_personas.py` wires up the persona governance.

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 — Configuration

# COMMAND ----------

dbutils.widgets.text("catalog", "compliance_pack", "Unity Catalog name")
CATALOG = dbutils.widgets.get("catalog")
print(f"Catalog: {CATALOG}")

# ---------------------------------------------------------------------------
# Delta-Share-aware view update helpers
# ---------------------------------------------------------------------------
# `CREATE OR REPLACE VIEW` fails when the view is already published via a
# Delta Share — UC rejects the implicit DROP inside CREATE OR REPLACE. To
# keep phase1_bootstrap idempotent on re-runs, we:
#   1. Before view creation: find every share that contains our target views
#      and capture their current entry config (shared_as, partitions, etc.)
#   2. Temporarily REMOVE those entries from the share
#   3. Run CREATE OR REPLACE VIEW as normal
#   4. Re-ADD each entry with the original config (in a try/finally, so the
#      share is restored even if CREATE OR REPLACE raises)
#
# On first-deploy workspaces where no share exists, find_shares_containing()
# returns an empty list and the helpers no-op cleanly.

def _share_api():
    """Return (host, auth_headers) for direct REST calls to the Unity
    Catalog Shares API. Uses the notebook's own token so no credentials
    need to be configured at job level."""
    import os
    ctx_token = None
    ctx_host = None
    try:
        # Databricks notebook context
        dbutils_ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
        ctx_token = dbutils_ctx.apiToken().get()
        ctx_host = dbutils_ctx.apiUrl().get()
    except Exception:  # noqa: BLE001
        # Non-notebook context — fall back to env vars
        ctx_host = os.environ.get("DATABRICKS_HOST")
        ctx_token = os.environ.get("DATABRICKS_TOKEN")
    if not ctx_host or not ctx_token:
        return None, None
    return ctx_host.rstrip("/"), {"Authorization": f"Bearer {ctx_token}"}


def _find_shares_containing(view_fq: str) -> list:
    """Return [(share_name, object_dict)] for every share that contains view_fq.

    Uses the Unity Catalog Shares REST API directly (SDK method names vary
    across SDK versions; REST endpoints are stable).
    """
    import requests
    host, headers = _share_api()
    if not host:
        print(f"[share-helper] WARN: no auth context; skipping share detection for {view_fq}")
        return []

    r = requests.get(f"{host}/api/2.1/unity-catalog/shares", headers=headers, timeout=30)
    if r.status_code != 200:
        print(f"[share-helper] WARN: could not list shares ({r.status_code}); skipping")
        return []
    hits = []
    for s in (r.json().get("shares") or []):
        share_name = s.get("name")
        if not share_name:
            continue
        d = requests.get(
            f"{host}/api/2.1/unity-catalog/shares/{share_name}",
            params={"include_shared_data": "true"},
            headers=headers, timeout=30,
        )
        if d.status_code != 200:
            continue
        for obj in (d.json().get("objects") or []):
            if obj.get("name") == view_fq:
                hits.append((share_name, obj))
                break
    return hits


def _detach_from_shares(view_fqs: list) -> list:
    """For each fully-qualified view, find every share containing it and
    REMOVE the entry. Returns [(share_name, object_dict)] so the caller
    can re-ADD with the original config after the view is updated."""
    import json as _json
    import requests
    host, headers = _share_api()
    if not host:
        return []
    all_hits = []
    for vfq in view_fqs:
        for share_name, obj in _find_shares_containing(vfq):
            print(f"[unshare] removing {vfq} from share {share_name}")
            payload = {"updates": [{
                "action": "REMOVE",
                "data_object": {
                    "name": obj["name"],
                    "data_object_type": obj["data_object_type"],
                },
            }]}
            r = requests.patch(
                f"{host}/api/2.1/unity-catalog/shares/{share_name}",
                headers={**headers, "Content-Type": "application/json"},
                data=_json.dumps(payload), timeout=30,
            )
            if r.status_code >= 300:
                print(f"[unshare] WARN: REMOVE failed ({r.status_code}): {r.text[:200]}")
                continue
            all_hits.append((share_name, obj))
    return all_hits


def _reattach_to_shares(hits: list) -> None:
    """Re-ADD each (share, object) tuple with the original config. Called
    in a finally block — errors here log loudly but don't re-raise."""
    if not hits:
        return
    import json as _json
    import requests
    host, headers = _share_api()
    if not host:
        for share_name, obj in hits:
            print(f"[reshare] WARN: no auth context; manually re-add {obj['name']} to {share_name}")
        return

    for share_name, obj in hits:
        data_object = {
            "name": obj["name"],
            "data_object_type": obj["data_object_type"],
        }
        if obj.get("shared_as"):
            data_object["shared_as"] = obj["shared_as"]
        if obj.get("partitions"):
            data_object["partitions"] = obj["partitions"]
        payload = {"updates": [{"action": "ADD", "data_object": data_object}]}
        r = requests.patch(
            f"{host}/api/2.1/unity-catalog/shares/{share_name}",
            headers={**headers, "Content-Type": "application/json"},
            data=_json.dumps(payload), timeout=30,
        )
        if r.status_code >= 300:
            print(f"[reshare] ERROR: re-ADD failed for {obj['name']} in {share_name} "
                  f"({r.status_code}): {r.text[:200]}")
            print(f"[reshare] Manually re-add: databricks shares update {share_name} "
                  f"--json '{_json.dumps(payload)}'")
        else:
            print(f"[reshare] re-added {obj['name']} to {share_name}")


# ---------------------------------------------------------------------------
# Grant-preserving CREATE OR REPLACE VIEW helpers
# ---------------------------------------------------------------------------
# `CREATE OR REPLACE VIEW` silently drops every GRANT that was on the view.
# Personas who previously had SELECT lose it on every phase1_bootstrap
# re-run, which breaks their dashboards + Genie spaces until someone
# re-runs scripts/apply_persona_uc_grants.py. Mirror the Option-B share
# pattern: capture existing grants before the replace, restore after in a
# finally block.
#
# On first deploy the captured list is empty (view doesn't exist yet);
# helpers no-op cleanly.

def _capture_grants_on(table_fq: str) -> list:
    """Return [(principal, privilege)] for every non-ownership grant on
    table_fq. If the view doesn't exist or SHOW GRANTS fails for any
    other reason, return [] — the caller will no-op on restore."""
    try:
        rows = spark.sql(f"SHOW GRANTS ON TABLE {table_fq}").collect()
    except Exception as e:  # noqa: BLE001
        # Table doesn't exist yet (first deploy) or query failed — either
        # way we have nothing to restore.
        print(f"[capture_grants] {table_fq}: no grants to preserve ({str(e)[:120]})")
        return []
    captured = []
    for r in rows:
        # Column order varies slightly across UC versions; SHOW GRANTS
        # conventionally returns Principal, ActionType, ObjectType,
        # ObjectKey. Access by field name when available, else index.
        try:
            principal = r["Principal"] if "Principal" in r.asDict() else r[0]
            privilege = r["ActionType"] if "ActionType" in r.asDict() else r[1]
        except Exception:
            principal, privilege = r[0], r[1]
        if not principal or not privilege:
            continue
        # Skip ownership — the deployer already owns the new view.
        if privilege.upper() in ("OWN", "OWNERSHIP"):
            continue
        captured.append((principal, privilege))
    if captured:
        print(f"[capture_grants] {table_fq}: captured {len(captured)} grant(s)")
    return captured


def _restore_grants_on(table_fq: str, saved: list) -> None:
    """Re-issue each captured grant. Errors are logged but not re-raised
    (a stale persona principal shouldn't break the whole bootstrap)."""
    if not saved:
        return
    for principal, privilege in saved:
        stmt = f"GRANT {privilege} ON TABLE {table_fq} TO `{principal}`"
        try:
            spark.sql(stmt)
            print(f"[restore_grants] {privilege} ON {table_fq} -> {principal}")
        except Exception as e:  # noqa: BLE001
            print(f"[restore_grants] WARN: failed to restore grant: {stmt}: {str(e)[:200]}")


# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 — Create Delta tables if missing

# COMMAND ----------

# bronze.compliance_rules + silver.compliance_gaps
#
# Both tables gain a `regulation_pack` column in M2 (ADR-0001) so rule rows
# and gap rows are tagged with their source pack — required so the dashboard
# can slice by pack and so an Indian principal's gap (DPDP rule) is
# distinguishable from a UK principal's gap (UK GDPR rule) on the same column.
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.bronze.compliance_rules (
    rule_id               STRING  NOT NULL,
    rule_type             STRING  NOT NULL,
    severity              STRING  NOT NULL,
    regulations           ARRAY<STRING> NOT NULL,
    applicable_categories ARRAY<STRING> NOT NULL,
    description           STRING  NOT NULL,
    remediation           STRING  NOT NULL,
    is_active             BOOLEAN NOT NULL,
    regulation_pack       STRING                 -- ADR-0001 M2: source pack code
) USING DELTA
  COMMENT 'Multi-pack compliance gap detection rules (DPDP, UK GDPR, ...)'
""")

# Backfill regulation_pack on pre-M2 tables (CREATE TABLE IF NOT EXISTS is a
# no-op when the table already exists; the ALTER below adds the column on
# existing deployments without losing data).
spark.sql(f"""
ALTER TABLE {CATALOG}.bronze.compliance_rules
ADD COLUMNS (regulation_pack STRING)
""") if "regulation_pack" not in [
    f.name for f in spark.table(f"{CATALOG}.bronze.compliance_rules").schema
] else None

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.silver.compliance_gaps (
    gap_id          STRING    NOT NULL,
    scan_job_id     STRING    NOT NULL,
    table_name      STRING    NOT NULL,
    column_name     STRING    NOT NULL,
    pii_type        STRING    NOT NULL,
    pii_category    STRING    NOT NULL,
    rule_id         STRING    NOT NULL,
    rule_type       STRING    NOT NULL,
    severity        STRING    NOT NULL,
    regulation      STRING    NOT NULL,
    description     STRING    NOT NULL,
    remediation     STRING    NOT NULL,
    detected_at     TIMESTAMP NOT NULL,
    regulation_pack STRING                          -- ADR-0001 M2: source pack code
) USING DELTA
""")

spark.sql(f"""
ALTER TABLE {CATALOG}.silver.compliance_gaps
ADD COLUMNS (regulation_pack STRING)
""") if "regulation_pack" not in [
    f.name for f in spark.table(f"{CATALOG}.silver.compliance_gaps").schema
] else None

# compliance.notice_versions + consent_events_log + dsr_requests
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.notice_versions (
    notice_version_id STRING NOT NULL,
    notice_id         STRING NOT NULL,
    version_number    INT    NOT NULL,
    language          STRING NOT NULL,
    legal_basis       STRING NOT NULL,
    notice_text       STRING NOT NULL,
    purposes_covered  ARRAY<STRING> NOT NULL,
    effective_from    TIMESTAMP NOT NULL,
    effective_to      TIMESTAMP,
    approved_by       STRING NOT NULL,
    created_at        TIMESTAMP NOT NULL
) USING DELTA
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.consent_events_log (
    event_id               STRING    NOT NULL,
    data_principal_id      STRING    NOT NULL,
    event_type             STRING    NOT NULL,
    event_timestamp        TIMESTAMP NOT NULL,
    notice_id              STRING    NOT NULL,
    notice_version         INT       NOT NULL,
    notice_language        STRING    NOT NULL,
    channel                STRING    NOT NULL,
    purpose                STRING    NOT NULL,
    purpose_grant_status   STRING    NOT NULL,
    ip_address             STRING,
    user_agent             STRING,
    consent_capture_method STRING    NOT NULL,
    retention_clock_start  TIMESTAMP NOT NULL,
    retention_duration_days INT      NOT NULL,
    superseded_by_event_id STRING,
    partner_source_id      STRING,
    synced_at              TIMESTAMP NOT NULL,
    event_date             DATE GENERATED ALWAYS AS (CAST(event_timestamp AS DATE))
) USING DELTA
  PARTITIONED BY (event_date)
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true'
  )
""")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.dsr_requests (
    request_id               STRING    NOT NULL,
    data_principal_id        STRING    NOT NULL,
    request_type             STRING    NOT NULL,
    status                   STRING    NOT NULL,
    submitted_at             TIMESTAMP NOT NULL,
    verified_at              TIMESTAMP,
    discovery_completed_at   TIMESTAMP,
    execution_completed_at   TIMESTAMP,
    response_bundle_path     STRING,
    sla_deadline             TIMESTAMP NOT NULL,
    scope_purposes           ARRAY<STRING>,
    requester_email          STRING    NOT NULL,
    requester_language       STRING    NOT NULL,
    created_at               TIMESTAMP NOT NULL,
    updated_at               TIMESTAMP NOT NULL
) USING DELTA
""")

# compliance.dpia_runs — DPIA Auto-Generator audit table (Phase 1+ work).
#
# Created here at deploy time so it exists BEFORE the personas step
# runs `setup_persona_genie_spaces.py`, which validates that every
# table in a Genie space's data_sources allowlist actually exists.
# Without this eager creation, the GC space (which now lists
# compliance_pack.compliance.dpia_runs) would 404 on creation, because the
# `_ensure_audit_table()` lazy-create path inside
# `governance_core.dpia.run_dpia_generation` only fires on the FIRST
# DPIA generation run — which happens AFTER personas setup.
#
# Kept in sync with `governance_core/dpia.py::_AUDIT_TABLE_DDL`. If
# the schema changes, edit both — the lazy-create path inside
# run_dpia_generation still uses _AUDIT_TABLE_DDL for the
# notebook-demo and ad-hoc-trigger cases. Phase 5 may unify these
# under a single public constant.
# Defensive volume creation. The canonical source for this volume is
# scripts/bootstrap_catalog.py (the `bootstrap_uc` step of deploy_all.sh).
# However, anyone running `deploy_all.sh --from bootstrap` (a common
# shortcut on existing deploys) skips bootstrap_uc, which previously meant
# the volume was missing and `do_app_perms` failed with
# RESOURCE_DOES_NOT_EXIST when granting READ VOLUME on it. Creating it
# here as well makes phase1_bootstrap self-contained for the DPIA artefact
# write-path: dpia_generator writes JSON+PDF into this volume, then
# inserts a dpia_runs row pointing at the artefact_path. Idempotent.
spark.sql(f"""
CREATE VOLUME IF NOT EXISTS {CATALOG}.compliance.dpia_artifacts
COMMENT 'DPIA Auto-Generator artifacts, one JSON per run (paired with compliance.dpia_runs row)'
""")
print("✓ compliance.dpia_artifacts volume ensured (DPIA artefact storage)")

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.dpia_runs (
    run_id              STRING    NOT NULL,
    generated_at        TIMESTAMP NOT NULL,
    generated_by        STRING    NOT NULL,
    catalog_name        STRING    NOT NULL,
    model_endpoint      STRING    NOT NULL,
    prompt_module       STRING    NOT NULL,
    prompt_version      STRING    NOT NULL,
    regulation_pack     STRING,
    context_snapshot    STRING    NOT NULL,
    dpia_text           STRING    NOT NULL,
    dpia_sections       MAP<STRING, STRING>,
    parse_error         STRING,
    artifact_path       STRING    NOT NULL,
    latency_seconds     DOUBLE,
    status              STRING    NOT NULL,
    reviewed_by         STRING,
    reviewed_at         TIMESTAMP,
    notes               STRING
) USING DELTA
TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
)
""")
print("✓ compliance.dpia_runs ensured (DPIA Phase 1 audit table)")

# bronze.data_sources — ingestion-source registry. Classifier reads
# silver_table_name from here (see classification_dlt._resolve_silver_tables).
# DDL mirrors schemas/bronze.sql; created here because schemas/bronze.sql
# is not auto-executed by any pipeline. Fresh-workspace deploys hit this
# CREATE; existing workspaces no-op (and the §2.5 ALTER below covers
# legacy workspaces deployed before silver_table_name existed).
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.bronze.data_sources (
    source_id           STRING    NOT NULL,
    source_name         STRING    NOT NULL,
    source_type         STRING    NOT NULL,
    ingestion_pattern   STRING    NOT NULL,
    catalog_name        STRING,
    schema_name         STRING,
    landing_volume_path STRING,
    owner_email         STRING,
    is_active           BOOLEAN   NOT NULL,
    created_at          TIMESTAMP NOT NULL,
    updated_at          TIMESTAMP NOT NULL,
    silver_table_name   STRING    COMMENT 'Silver-layer table or view that mirrors this source. Classifier scans this column.'
) USING DELTA
""")

print("✓ Delta tables ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2.5 — Seed `bronze.data_sources`
# MAGIC
# MAGIC One row per silver object the classifier should scan. The classifier
# MAGIC reads this list dynamically; previously it was hardcoded in
# MAGIC `pipelines/classification_dlt.py:SILVER_TABLES`. Closing this gap
# MAGIC removes the need to edit the classifier when a new ingestion path
# MAGIC is onboarded — adding a row here is sufficient.
# MAGIC
# MAGIC Idempotent: MERGE on `source_id` so reruns are safe.

# COMMAND ----------

# Make sure the new column exists on workspaces deployed before 2026-04-27.
# Safe to re-run — UC errors with FIELDS_ALREADY_EXIST and we ignore that.
try:
    spark.sql(
        f"ALTER TABLE {CATALOG}.bronze.data_sources "
        f"ADD COLUMNS (silver_table_name STRING COMMENT "
        f"'Silver table or view the classifier scans for this source.')"
    )
except Exception as exc:
    if "already exists" not in str(exc).lower() and "fields_already_exist" not in str(exc).lower():
        raise
    # column already present from a prior run — fine

# source_id naming follows the existing notebooks/01_add_data_source.py convention
# (src_<entity>) so this MERGE updates the 5 base rows in place rather than
# duplicating them on workspaces that already ran the new-source notebook.
DATA_SOURCES_SEED = [
    # Auto Loader file-arrival landing zone (5 base sources)
    ("src_employees",          "Employees (HR master)",      "hr_master",             "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/employees",     "employees_tagged"),
    ("src_customers",          "Customers (CRM master)",     "crm",                   "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/customers",     "customers_tagged"),
    ("src_patients",           "Patients (health records)",  "health",                "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/patients",      "patients_tagged"),
    ("src_transactions",       "Transactions (ledger)",      "financial",             "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/transactions",  "transactions_tagged"),
    ("src_users",              "Users (application)",        "application",           "auto_loader",     "bronze",          f"/Volumes/{CATALOG}/bronze/landing/users",         "users_tagged"),
    # Lakeflow Connect simulation (3 SF objects)
    ("src_sf_leads",           "Salesforce Leads",           "crm_external",          "direct_write",    "bronze",          None,                                               "sf_leads_tagged"),
    ("src_sf_contacts",        "Salesforce Contacts",        "crm_external",          "direct_write",    "bronze",          None,                                               "sf_contacts_tagged"),
    ("src_sf_accounts",        "Salesforce Accounts",        "crm_external",          "direct_write",    "bronze",          None,                                               "sf_accounts_tagged"),
    # Federation simulation (2 foreign-mock tables → silver views)
    ("src_lead_scoring",       "Lead Scoring (Postgres federation)",      "marketing_attribution", "federation_view", "federation_mock", None, "federation_lead_scoring_tagged"),
    ("src_campaign_response",  "Campaign Response (Postgres federation)", "marketing_attribution", "federation_view", "federation_mock", None, "federation_campaign_response_tagged"),
]

from pyspark.sql import Row
from datetime import datetime as _dt

_now = _dt.utcnow()
_seed_rows = [
    Row(
        source_id=r[0], source_name=r[1], source_type=r[2], ingestion_pattern=r[3],
        catalog_name=CATALOG, schema_name=r[4], landing_volume_path=r[5],
        owner_email="dpdp-poc-team@example.com", is_active=True,
        created_at=_now, updated_at=_now, silver_table_name=r[6],
    )
    for r in DATA_SOURCES_SEED
]

spark.createDataFrame(_seed_rows).createOrReplaceTempView("_data_sources_seed")
spark.sql(f"""
    MERGE INTO {CATALOG}.bronze.data_sources AS t
    USING _data_sources_seed AS s
    ON t.source_id = s.source_id
    WHEN MATCHED THEN UPDATE SET
        source_name         = s.source_name,
        source_type         = s.source_type,
        ingestion_pattern   = s.ingestion_pattern,
        catalog_name        = s.catalog_name,
        schema_name         = s.schema_name,
        landing_volume_path = s.landing_volume_path,
        owner_email         = s.owner_email,
        is_active           = s.is_active,
        updated_at          = s.updated_at,
        silver_table_name   = s.silver_table_name
    WHEN NOT MATCHED THEN INSERT *
""")

_n = spark.table(f"{CATALOG}.bronze.data_sources").count()
print(f"✓ bronze.data_sources seeded — {_n} rows ({len(DATA_SOURCES_SEED)} expected)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 — Load 9 DPDP compliance rules

# COMMAND ----------

# Rules are loaded from the active regulation pack:
#   regulations/<REGULATION_PACK>/rules.yaml
#
# Default pack is `dpdp_2023`. On a UK GDPR / CCPA / PIPEDA deployment,
# set REGULATION_PACK at job level and re-run — no code change needed here.
#
# Path resolution: the Databricks bundle syncs the whole repo to
# /Workspace/.../{.bundle/<name>/<target>/files}/. The notebook's working
# directory at runtime is the `pipelines/` subdir; its parent is the repo
# root, which is where `governance_core/` and `regulations/` sit.
import os as _os
import sys as _sys

def _locate_repo_root() -> str:
    # Try the notebook's parent directory, then a few common fallbacks.
    candidates = []
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
        nb_path = ctx.notebookPath().get()          # e.g. /Workspace/.../files/pipelines/phase1_bootstrap
        if nb_path:
            # Workspace Files paths map to /Workspace/<path>
            fs_path = "/Workspace" + nb_path if not nb_path.startswith("/Workspace") else nb_path
            # notebook sits in <repo>/pipelines/; repo root is parent of parent
            candidates.append(_os.path.dirname(_os.path.dirname(fs_path)))
    except Exception:  # noqa: BLE001
        pass
    candidates.extend([_os.getcwd(), _os.path.dirname(_os.getcwd()), "/Workspace", "."])
    for c in candidates:
        if c and _os.path.isdir(_os.path.join(c, "governance_core")):
            return c
    raise RuntimeError(
        "Cannot locate repo root with governance_core/ dir. "
        f"Checked: {candidates}. Make sure the bundle sync includes governance_core/** and regulations/**."
    )

_repo_root = _locate_repo_root()
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

# ADR-0001 M2: load every pack under regulations/ and emit their rules into
# bronze.compliance_rules tagged with the source pack code. Gaps in §4 then
# join through the principal's jurisdiction to apply only the rules of the
# pack governing that principal.
from governance_core.pack_loader import loaded_packs  # noqa: E402
_packs = loaded_packs()
print(f"Loaded {len(_packs)} regulation pack(s): {[p.code for p in _packs]}")

# Primary-pack alias: used by the notices / consent-purpose / retention /
# residency code paths below that haven't been refactored to multi-pack yet.
# ADR-0001 defers per-pack notice rendering + residency union to a later
# phase; rules + gaps are the multi-pack surfaces M2-M4 deliver.
_pack = _packs[0] if _packs else None
if _pack is None:
    raise RuntimeError(
        "No regulation packs found under regulations/. At least one pack "
        "(e.g., regulations/dpdp_2023/) must be present for phase1_bootstrap."
    )

# ADR-0001 Q3: validate that every jurisdiction value present in the live
# silver layer corresponds to a loaded pack. Observational only — never
# fails phase1; the report prints to stdout for the deploy log + CI guard.
try:
    from governance_core.pack_loader import (
        validate_jurisdictions,
        format_validation_report,
    )
    _observed_rows = spark.sql(
        f"SELECT DISTINCT jurisdiction FROM {CATALOG}.silver.customers_tagged"
    ).collect()
    _observed = {r["jurisdiction"] for r in _observed_rows}
    _validation_report = validate_jurisdictions(_observed, packs=_packs)
    print(format_validation_report(_validation_report, observed_count=len(_observed)))
    if _validation_report["unmapped_unknown"]:
        print(
            f"  ⚠ WARNING: {len(_validation_report['unmapped_unknown'])} unknown jurisdiction "
            f"code(s) present in silver.customers_tagged — author the corresponding "
            f"pack(s) or fix the data."
        )
except Exception as _e:  # noqa: BLE001
    print(f"  (jurisdiction validation skipped: {type(_e).__name__}: {_e})")

ALL_RULES: list[tuple[dict, str]] = []
for _p in _packs:
    rules_in_pack = _p.rules()
    print(f"  → {len(rules_in_pack)} rules from regulations/{_p.code}/rules.yaml")
    ALL_RULES.extend((r, _p.code) for r in rules_in_pack)
print(f"  total: {len(ALL_RULES)} rule rows across all packs")

from pyspark.sql import Row
# Use the target table's schema explicitly. On serverless Spark/Connect,
# schema inference from a list of Rows fails with CANNOT_DETERMINE_TYPE
# when a field is None across all sample rows — cheaper to just use the
# declared schema of the already-created target table.
_rules_schema = spark.table(f"{CATALOG}.bronze.compliance_rules").schema
rules_df = spark.createDataFrame([
    Row(
        rule_id=r["rule_id"],
        rule_type=r["rule_type"],
        severity=r["severity"],
        regulations=r["regulations"],
        applicable_categories=r["applicable_categories"],
        description=r["description"],
        remediation=r["remediation"],
        is_active=True,
        regulation_pack=pack_code,
    )
    for r, pack_code in ALL_RULES
], schema=_rules_schema)

# ADR-0001 M4 cutover: on the first multi-pack deploy, an existing dpdp-only
# bronze.compliance_rules table has rows with regulation_pack=NULL (added by
# ALTER TABLE). A composite-key MERGE on (rule_id, regulation_pack) wouldn't
# match those NULL-tagged rows to the new dpdp_2023-tagged source rows and
# would create duplicates. TRUNCATE + INSERT below is safe because rules are
# pure data — the pack YAMLs are the source of truth, this table is just
# their materialisation.
spark.sql(f"TRUNCATE TABLE {CATALOG}.bronze.compliance_rules")

rules_df.createOrReplaceTempView("_rules_src")
spark.sql(f"""
INSERT INTO {CATALOG}.bronze.compliance_rules
SELECT * FROM _rules_src
""")
print(f"✓ {spark.table(f'{CATALOG}.bronze.compliance_rules').count()} compliance rules loaded (multi-pack)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 — Generate compliance gaps (rules × pii_findings)
# MAGIC
# MAGIC Produces one gap per (PII finding × rule) pair where the rule's
# MAGIC `applicable_categories` array contains the finding's `pii_category`.

# COMMAND ----------

import uuid
from datetime import datetime

scan_job_id = f"gap_scan_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

# ADR-0001 M2: gaps are now multi-pack. Each (finding × rule) pair where the
# rule's applicable_categories includes the finding's pii_category produces
# one gap row, tagged with the rule's source pack (regulation_pack column).
# Downstream queries filter by regulation_pack to slice per regulation; the
# dashboard's jurisdiction filter composes with this through the
# customers/users/patients .jurisdiction columns (a UK-GDPR gap is only
# actionable against GB principals, etc.).
spark.sql(f"""
CREATE OR REPLACE TEMP VIEW _gap_candidates AS
SELECT
    uuid() AS gap_id,
    '{scan_job_id}' AS scan_job_id,
    f.table_name,
    f.column_name,
    f.pii_type,
    f.pii_category,
    r.rule_id,
    r.rule_type,
    r.severity,
    r.regulations[0] AS regulation,
    r.description,
    r.remediation,
    current_timestamp() AS detected_at,
    r.regulation_pack
FROM {CATALOG}.silver.pii_findings f
CROSS JOIN {CATALOG}.bronze.compliance_rules r
WHERE r.is_active = true
  AND array_contains(r.applicable_categories, f.pii_category)
""")

# Replace previous gap scan to keep the table deterministic in row count
spark.sql(f"TRUNCATE TABLE {CATALOG}.silver.compliance_gaps")
spark.sql(f"INSERT INTO {CATALOG}.silver.compliance_gaps SELECT * FROM _gap_candidates")

total_gaps = spark.table(f"{CATALOG}.silver.compliance_gaps").count()
print(f"✓ {total_gaps} compliance gaps generated")
print()
spark.sql(f"""
    SELECT severity, COUNT(*) AS n FROM {CATALOG}.silver.compliance_gaps
    GROUP BY severity ORDER BY CASE severity
      WHEN 'critical' THEN 1 WHEN 'high' THEN 2 WHEN 'medium' THEN 3 ELSE 4 END
""").show()

# COMMAND ----------

# MAGIC %md
# MAGIC ## 5 — Seed notice_versions (1 row — marketing_notice v1 en-IN)

# COMMAND ----------

from datetime import timezone

# Notices are loaded from the active regulation pack:
#   regulations/<REGULATION_PACK>/notices.yaml
#
# Each entry is hydrated into a Spark Row matching compliance.notice_versions.
# Adding languages / new notices is a yaml edit in the pack — no code change.
purposes = _pack.default_purposes() or [
    "core_service", "marketing_email", "marketing_sms",
    "analytics", "third_party_sharing", "product_personalization",
]


def _parse_iso(s):
    """ISO 8601 -> aware datetime. Accepts None and passes through."""
    if s is None:
        return None
    # fromisoformat in py3.11+ handles offsets; older fallbacks handle 'Z'.
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


notice_rows = [
    Row(
        notice_version_id=n["notice_version_id"],
        notice_id=n["notice_id"],
        version_number=int(n["version_number"]),
        language=n["language"],
        legal_basis=n["legal_basis"],
        notice_text=n["notice_text"],
        purposes_covered=list(n.get("purposes_covered") or purposes),
        effective_from=_parse_iso(n["effective_from"]),
        effective_to=_parse_iso(n.get("effective_to")),
        approved_by=n.get("approved_by"),
        created_at=_parse_iso(n["created_at"]),
    )
    for n in _pack.notices()
]
print(f"  → {len(notice_rows)} notice(s) loaded from regulations/{_pack.code}/notices.yaml")
# Use the target table's schema (same rationale as the rules createDataFrame above).
_notice_schema = spark.table(f"{CATALOG}.compliance.notice_versions").schema
spark.createDataFrame(notice_rows, schema=_notice_schema).createOrReplaceTempView("_notice_src")
spark.sql(f"""
MERGE INTO {CATALOG}.compliance.notice_versions t
USING _notice_src s ON t.notice_version_id = s.notice_version_id
WHEN MATCHED THEN UPDATE SET *
WHEN NOT MATCHED THEN INSERT *
""")
print(f"✓ {spark.table(f'{CATALOG}.compliance.notice_versions').count()} notice version(s) seeded (en-IN, hi-IN, ta-IN)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 6 — Generate 1,000 consent events (deterministic, seed=42)
# MAGIC
# MAGIC Includes the designated DSR test principal `customer_04217`
# MAGIC (Oeshi Desai) with the exact pattern used by tests:
# MAGIC marketing_email granted→withdrawn, analytics granted, third_party_sharing declined.

# COMMAND ----------

import random
from datetime import timedelta

random.seed(42)

# Pull customer_ids from silver.customers_tagged to keep principals real
all_customers = [r.customer_id for r in
                 spark.table(f"{CATALOG}.silver.customers_tagged")
                      .select("customer_id").collect()]

# Target: 1000 events across ~292 distinct principals
principals = random.sample(all_customers, min(292, len(all_customers)))

# Purposes come from the pack (same source as notices.yaml default_purposes).
PURPOSES = _pack.default_purposes() or [
    "core_service", "marketing_email", "marketing_sms",
    "analytics", "third_party_sharing", "product_personalization",
]
CHANNELS = ["web", "mobile_app", "phone", "in_person"]

# Per-purpose retention defaults from the pack's retention_defaults.yaml.
# Previously this was a single 730-day scalar applied to every event;
# the pack supports varying retention by purpose (e.g. 365 days for
# core_service vs. 730 for marketing_email).
RETENTION_BY_PURPOSE = {p: _pack.retention_default(p) for p in PURPOSES}

# Grant-rate weights per purpose (matches the expected coverage)
GRANT_RATE = {
    "core_service": 0.97, "analytics": 0.80, "marketing_email": 0.66,
    "marketing_sms": 0.57, "product_personalization": 0.58,
    "third_party_sharing": 0.39,
}

base_time = datetime(2026, 2, 1, tzinfo=timezone.utc)
events = []

# First: the 4 canonical customer_04217 events (tests depend on exact shape)
TEST_PID = "customer_04217"
ev3_id = "evt_000003"  # marketing_email withdrawal event id
events.extend([
    ("evt_000001", TEST_PID, "granted",   base_time + timedelta(days=1,  hours=3),
     "marketing_notice", 1, "en-IN", "mobile_app", "marketing_email",   "granted",
     None, None, "opt_in_toggle", base_time + timedelta(days=1, hours=3),
     RETENTION_BY_PURPOSE["marketing_email"], ev3_id, None),
    ("evt_000002", TEST_PID, "granted",   base_time + timedelta(days=1,  hours=3, minutes=1),
     "marketing_notice", 1, "en-IN", "mobile_app", "analytics",          "granted",
     None, None, "opt_in_toggle", base_time + timedelta(days=1, hours=3, minutes=1),
     RETENTION_BY_PURPOSE["analytics"], None, None),
    (ev3_id,       TEST_PID, "withdrawn", base_time + timedelta(days=30, hours=11),
     "marketing_notice", 1, "en-IN", "mobile_app", "marketing_email",   "withdrawn",
     None, None, "opt_out_toggle", base_time + timedelta(days=30, hours=11),
     RETENTION_BY_PURPOSE["marketing_email"], None, None),
    ("evt_000004", TEST_PID, "declined",  base_time + timedelta(days=1,  hours=3, minutes=2),
     "marketing_notice", 1, "en-IN", "mobile_app", "third_party_sharing", "declined",
     None, None, "opt_in_toggle", base_time + timedelta(days=1, hours=3, minutes=2),
     RETENTION_BY_PURPOSE["third_party_sharing"], None, None),
])

event_seq = 5
for pid in principals:
    # Each principal gets a grant event per a random subset of purposes
    num_purposes = random.randint(2, 6)
    pid_purposes = random.sample(PURPOSES, num_purposes)
    for purpose in pid_purposes:
        if pid == TEST_PID:
            continue  # canonical events already added
        granted = random.random() < GRANT_RATE[purpose]
        status = "granted" if granted else "declined"
        event_type = "granted" if granted else "declined"
        ts = base_time + timedelta(days=random.randint(0, 60),
                                    hours=random.randint(0, 23),
                                    minutes=random.randint(0, 59))
        events.append((
            f"evt_{event_seq:06d}", pid, event_type, ts,
            "marketing_notice", 1, "en-IN",
            random.choice(CHANNELS), purpose, status,
            None, None, "opt_in_toggle" if granted else "opt_out_toggle",
            ts, RETENTION_BY_PURPOSE[purpose], None, None,
        ))
        event_seq += 1
        if event_seq > 1000:
            break
    if event_seq > 1000:
        break

# Cap to exactly 1000
events = events[:1000]
print(f"Generated {len(events)} events across {len({e[1] for e in events})} principals")

# Target schema minus the GENERATED column (event_date) which we can't
# supply from client-side data.
_events_target_schema = spark.table(f"{CATALOG}.compliance.consent_events_log").schema
from pyspark.sql.types import StructType as _StructType
_events_schema = _StructType([f for f in _events_target_schema.fields if f.name != "event_date"])
events_df = spark.createDataFrame([
    Row(event_id=e[0], data_principal_id=e[1], event_type=e[2], event_timestamp=e[3],
        notice_id=e[4], notice_version=e[5], notice_language=e[6], channel=e[7],
        purpose=e[8], purpose_grant_status=e[9], ip_address=e[10], user_agent=e[11],
        consent_capture_method=e[12], retention_clock_start=e[13],
        retention_duration_days=e[14], superseded_by_event_id=e[15],
        partner_source_id=e[16], synced_at=datetime.now(timezone.utc))
    for e in events
], schema=_events_schema)

# Replace-all pattern (TRUNCATE + INSERT) — keeps re-runs deterministic
spark.sql(f"TRUNCATE TABLE {CATALOG}.compliance.consent_events_log")
events_df.write.mode("append").saveAsTable(f"{CATALOG}.compliance.consent_events_log")
total = spark.table(f"{CATALOG}.compliance.consent_events_log").count()
print(f"✓ {total} consent events loaded")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 7 — Compliance views and UDFs

# COMMAND ----------

# personal_data_register view
# NOTE: discovered_tables' schema was simplified in medallion.py — the columns
# `scan_job_id` and `pii_column_count` were removed. We now compute the
# per-table PII column count inline from pii_findings and join on natural key
# (catalog/schema/table_name) only.
_register_select = f"""
WITH pii_column_counts AS (
  SELECT catalog_name, schema_name, table_name,
         COUNT(DISTINCT column_name) AS pii_column_count
  FROM {CATALOG}.silver.pii_findings
  GROUP BY catalog_name, schema_name, table_name
)
SELECT
    f.catalog_name || '.' || f.schema_name || '.' || f.table_name AS fully_qualified_table,
    f.catalog_name      AS source_catalog,
    f.schema_name       AS source_schema,
    f.table_name        AS source_table,
    f.column_name       AS source_column,
    f.column_data_type  AS data_type,
    f.pii_category,
    f.pii_type,
    f.sensitivity_tier,
    f.classifier_source,
    f.confidence        AS classification_confidence,
    f.match_rate,
    f.regulations       AS applicable_regulations,
    f.sample_match_redacted AS redacted_sample,
    dt.row_count        AS table_row_count,
    pcc.pii_column_count AS table_pii_column_count,
    f.human_reviewed,
    f.review_status,
    f.review_notes,
    f.discovered_at     AS last_scanned_at,
    f.reviewed_at       AS last_reviewed_at
FROM {CATALOG}.silver.pii_findings f
LEFT JOIN {CATALOG}.silver.discovered_tables dt
  ON dt.catalog_name = f.catalog_name
 AND dt.schema_name  = f.schema_name
 AND dt.table_name   = f.table_name
LEFT JOIN pii_column_counts pcc
  ON pcc.catalog_name = f.catalog_name
 AND pcc.schema_name  = f.schema_name
 AND pcc.table_name   = f.table_name
ORDER BY
  CASE f.sensitivity_tier
    WHEN 'critical' THEN 1 WHEN 'high' THEN 2
    WHEN 'medium'   THEN 3 WHEN 'low'  THEN 4 ELSE 5 END,
  f.table_name, f.column_name
"""

# Two preservation passes before the CREATE OR REPLACE block:
#
#   1. Shared-view detachment. CREATE OR REPLACE VIEW fails on views
#      published via Delta Share (UC rejects the implicit DROP).
#      _detach_from_shares removes them; finally block re-attaches.
#
#   2. Grant capture. CREATE OR REPLACE VIEW silently drops every grant
#      on the view — personas lose SELECT and their dashboards break
#      until someone re-runs apply_persona_uc_grants.py. Capture the
#      grants here; finally block restores them.
#
# On first deploy the captured lists are empty (view/share doesn't exist
# yet); both helpers no-op cleanly.
_shared_view_candidates = [
    f"{CATALOG}.compliance.personal_data_register",
    f"{CATALOG}.gold.consent_coverage_summary",
]
_detached_views = _detach_from_shares(_shared_view_candidates)

# Every view below gets CREATE OR REPLACE'd → capture any existing grants.
_grant_bearing_views = [
    f"{CATALOG}.compliance.personal_data_register",
    f"{CATALOG}.gold.marketing_eligible_principals",
    f"{CATALOG}.gold.consent_coverage_summary",
    f"{CATALOG}.gold.persona_overview_metrics",
    f"{CATALOG}.gold.persona_sensitivity_histogram",
]
_saved_grants = {v: _capture_grants_on(v) for v in _grant_bearing_views}

try:
    spark.sql(f"CREATE OR REPLACE VIEW {CATALOG}.compliance.personal_data_register AS {_register_select}")
    
    # has_active_consent UDF
    spark.sql(f"""
    CREATE OR REPLACE FUNCTION {CATALOG}.compliance.has_active_consent(
        principal_id STRING,
        purpose_name STRING
    ) RETURNS BOOLEAN
    RETURN (
      SELECT COALESCE(MAX(
        CASE WHEN purpose_grant_status = 'granted' THEN true ELSE false END
      ), false)
      FROM (
        SELECT purpose_grant_status,
               ROW_NUMBER() OVER (PARTITION BY data_principal_id, purpose
                                  ORDER BY event_timestamp DESC) rn
        FROM {CATALOG}.compliance.consent_events_log
        WHERE data_principal_id = principal_id AND purpose = purpose_name
      ) latest WHERE rn = 1
    )
    """)
    
    # marketing_eligible_principals gold view
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.marketing_eligible_principals AS
    WITH latest_consent AS (
      SELECT data_principal_id, purpose, event_type, purpose_grant_status, event_timestamp,
             ROW_NUMBER() OVER (PARTITION BY data_principal_id, purpose
                                ORDER BY event_timestamp DESC) AS rn
      FROM {CATALOG}.compliance.consent_events_log
      WHERE purpose IN ('marketing_email','marketing_sms','product_personalization')
    )
    SELECT data_principal_id, purpose, event_timestamp AS consent_effective_from
    FROM latest_consent
    WHERE rn = 1 AND purpose_grant_status = 'granted'
    """)
    
    # consent_coverage_summary gold view
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.consent_coverage_summary AS
    WITH latest AS (
      SELECT data_principal_id, purpose, purpose_grant_status,
             ROW_NUMBER() OVER (PARTITION BY data_principal_id, purpose
                                ORDER BY event_timestamp DESC) AS rn
      FROM {CATALOG}.compliance.consent_events_log
    )
    SELECT purpose,
           COUNT(*) AS total_principals,
           SUM(CASE WHEN purpose_grant_status = 'granted'   THEN 1 ELSE 0 END) AS granted,
           SUM(CASE WHEN purpose_grant_status = 'declined'  THEN 1 ELSE 0 END) AS declined,
           SUM(CASE WHEN purpose_grant_status = 'withdrawn' THEN 1 ELSE 0 END) AS withdrawn,
           ROUND(
             100.0 * SUM(CASE WHEN purpose_grant_status = 'granted' THEN 1 ELSE 0 END)
                 / NULLIF(COUNT(*), 0), 1) AS grant_rate_pct
    FROM latest WHERE rn = 1
    GROUP BY purpose
    ORDER BY total_principals DESC
    """)
    
    # persona_overview_metrics — the "shared overview" aggregate that
    # non-CCO personas can read without exposing raw pii_findings /
    # compliance_gaps. Consumed by scripts/slice_dashboards.py's
    # DATASET_REWRITES_NON_CCO — every column referenced by the slicer
    # must exist in this SELECT, otherwise the CFO/CMO/GC persona
    # dashboards render UNRESOLVED_COLUMN errors on the Executive
    # Overview tiles.
    #
    # Column contract (19 total) — if you add/remove columns here, also
    # update the allow-list in tests/test_persona_overview_columns.py:
    #   pii_agg:       pii_columns, total_tables,
    #                  critical_pii, high_pii, medium_pii, low_pii           (6)
    #   gap_agg:       total_gaps, critical_gaps, high_gaps, medium_gaps     (4)
    #   consent_agg:   consent_events, consent_principals                    (2)
    #   pii_scan_agg:  last_scan_time, days_since_last_scan, avg_confidence  (3)
    #   computed:      risk_score, compliance_score, risk_level, as_of       (4)
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.persona_overview_metrics AS
    WITH pii_agg AS (
      SELECT COUNT(*) AS pii_columns,
             COUNT(DISTINCT source_table) AS total_tables,
             SUM(CASE WHEN sensitivity_tier='critical' THEN 1 ELSE 0 END) AS critical_pii,
             SUM(CASE WHEN sensitivity_tier='high'     THEN 1 ELSE 0 END) AS high_pii,
             SUM(CASE WHEN sensitivity_tier='medium'   THEN 1 ELSE 0 END) AS medium_pii,
             SUM(CASE WHEN sensitivity_tier='low'      THEN 1 ELSE 0 END) AS low_pii
      FROM {CATALOG}.compliance.personal_data_register
    ), gap_agg AS (
      SELECT COUNT(*) AS total_gaps,
             COUNT(CASE WHEN severity='critical' THEN 1 END) AS critical_gaps,
             COUNT(CASE WHEN severity='high'     THEN 1 END) AS high_gaps,
             COUNT(CASE WHEN severity='medium'   THEN 1 END) AS medium_gaps
      FROM {CATALOG}.silver.compliance_gaps
    ), consent_agg AS (
      SELECT COUNT(*) AS consent_events,
             COUNT(DISTINCT data_principal_id) AS consent_principals
      FROM {CATALOG}.compliance.consent_events_log
    ), pii_scan_agg AS (
      SELECT MAX(discovered_at) AS last_scan_time,
             DATEDIFF(CURRENT_TIMESTAMP(), MAX(discovered_at)) AS days_since_last_scan,
             AVG(confidence) AS avg_confidence
      FROM {CATALOG}.silver.pii_findings
    )
    SELECT p.*, g.*, c.*,
           s.last_scan_time,
           s.days_since_last_scan,
           s.avg_confidence,
           LEAST(100,
             CAST((p.critical_pii*25 + p.high_pii*15
                 + g.critical_gaps*20 + g.high_gaps*10)
                 / GREATEST(1, p.total_tables*2) AS INT)) AS risk_score,
           GREATEST(0, 100 - LEAST(100,
             CAST((p.critical_pii*25 + p.high_pii*15
                 + g.critical_gaps*20 + g.high_gaps*10)
                 / GREATEST(1, p.total_tables*2) AS INT))) AS compliance_score,
           CASE
             WHEN LEAST(100,
                  CAST((p.critical_pii*25 + p.high_pii*15
                      + g.critical_gaps*20 + g.high_gaps*10)
                      / GREATEST(1, p.total_tables*2) AS INT)) >= 80 THEN 'CRITICAL'
             WHEN LEAST(100,
                  CAST((p.critical_pii*25 + p.high_pii*15
                      + g.critical_gaps*20 + g.high_gaps*10)
                      / GREATEST(1, p.total_tables*2) AS INT)) >= 60 THEN 'HIGH'
             WHEN LEAST(100,
                  CAST((p.critical_pii*25 + p.high_pii*15
                      + g.critical_gaps*20 + g.high_gaps*10)
                      / GREATEST(1, p.total_tables*2) AS INT)) >= 40 THEN 'MEDIUM'
             ELSE 'LOW'
           END AS risk_level,
           CURRENT_TIMESTAMP() AS as_of
    FROM pii_agg p CROSS JOIN gap_agg g CROSS JOIN consent_agg c CROSS JOIN pii_scan_agg s
    """)
    
    # persona_sensitivity_histogram — 4-row view (critical/high/medium/low)
    # used by non-CCO Executive Overview tiles that need a sensitivity
    # bar chart without querying pii_findings directly.
    #
    # Column name `sensitivity_tier` is intentional — matches silver.pii_findings
    # so dashboards that read from either source share the same column name and
    # widget chart-specs stay consistent across CCO (silver-backed) and the
    # other personas (gold-backed via this view).
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.persona_sensitivity_histogram AS
    SELECT 'critical' AS sensitivity_tier, critical_pii AS count
    FROM {CATALOG}.gold.persona_overview_metrics
    UNION ALL SELECT 'high',   high_pii   FROM {CATALOG}.gold.persona_overview_metrics
    UNION ALL SELECT 'medium', medium_pii FROM {CATALOG}.gold.persona_overview_metrics
    UNION ALL SELECT 'low',    low_pii    FROM {CATALOG}.gold.persona_overview_metrics
    """)

    # sensitive_data_exposure — backs the master dashboard's Sensitive Data
    # Exposure tab and the GC persona's same tab. Joins pii_findings with
    # the live grant set (system.information_schema.table_privileges) and
    # the live mask set (system.information_schema.column_masks). A PII
    # column granted to a non-admin grantee that has no mask is the highest
    # exposure tier; same column with a mask drops to medium.
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.sensitive_data_exposure AS
    SELECT
      pf.table_name,
      pf.column_name,
      pf.pii_type,
      tp.grantee,
      tp.privilege_type AS privilege,
      CASE
        WHEN pf.sensitivity_tier = 'critical' AND cm.column_name IS NULL THEN 'critical'
        WHEN pf.sensitivity_tier = 'high'     AND cm.column_name IS NULL THEN 'high'
        WHEN pf.sensitivity_tier IN ('critical','high') AND cm.column_name IS NOT NULL THEN 'medium'
        ELSE 'low'
      END AS exposure_level,
      pf.discovered_at AS detected_at
    FROM {CATALOG}.silver.pii_findings pf
    LEFT JOIN system.information_schema.table_privileges tp
      ON tp.table_catalog = pf.catalog_name
     AND tp.table_schema  = pf.schema_name
     AND tp.table_name    = pf.table_name
     AND tp.privilege_type = 'SELECT'
    LEFT JOIN system.information_schema.column_masks cm
      ON cm.table_catalog = pf.catalog_name
     AND cm.table_schema  = pf.schema_name
     AND cm.table_name    = pf.table_name
     AND cm.column_name   = pf.column_name
    WHERE tp.grantee IS NOT NULL
    """)

    # access_patterns — backs the master dashboard's Access Patterns tab
    # (and the GC persona's same tab). Materialized as a TABLE rather than
    # a view because the underlying scan over system.access.audit (~282k rows
    # in 30 days) is too slow to run on every dashboard tile refresh — a
    # snapshot updated each phase1_bootstrap run is the right cadence.
    # anomaly_flag = z-score > 2 vs the user's own access_count distribution,
    # OR an absolute >50 backstop for users with too few rows for a
    # meaningful stddev.
    spark.sql(f"""
    CREATE OR REPLACE TABLE {CATALOG}.gold.access_patterns AS
    WITH access_events AS (
      SELECT user_identity.email AS user_name,
        request_params['full_name_arg'] AS table_full_name,
        SPLIT_PART(request_params['full_name_arg'], '.', 3) AS table_name,
        event_time
      FROM system.access.audit
      WHERE service_name = 'unityCatalog'
        AND action_name = 'getTable'
        AND request_params['full_name_arg'] LIKE '{CATALOG}.%'
        AND event_date >= current_date() - INTERVAL 30 DAYS
    ),
    pii_tables AS (
      SELECT DISTINCT CONCAT('{CATALOG}.', schema_name, '.', table_name) AS table_full_name
      FROM {CATALOG}.silver.pii_findings
    ),
    agg AS (
      SELECT a.user_name, a.table_name, a.table_full_name,
        COUNT(*) AS access_count, MAX(a.event_time) AS last_accessed,
        (p.table_full_name IS NOT NULL) AS has_pii
      FROM access_events a
      LEFT JOIN pii_tables p ON p.table_full_name = a.table_full_name
      GROUP BY a.user_name, a.table_name, a.table_full_name, p.table_full_name
    ),
    user_stats AS (
      SELECT user_name, AVG(access_count) AS mean_access, STDDEV_POP(access_count) AS stddev_access
      FROM agg GROUP BY user_name
    )
    SELECT a.user_name, a.table_name, a.access_count, a.last_accessed, a.has_pii,
      CASE
        WHEN s.stddev_access > 0 AND a.access_count > s.mean_access + 2 * s.stddev_access THEN TRUE
        WHEN a.access_count > 50 THEN TRUE
        ELSE FALSE
      END AS anomaly_flag,
      current_timestamp() AS analyzed_at
    FROM agg a
    JOIN user_stats s ON s.user_name = a.user_name
    """)

    # data_lineage — backs the master dashboard's Data Lineage tab.
    # Sources from system.access.table_lineage (UC populates this for every
    # query). column_mapping is intentionally a comma-joined list of the
    # target table's PII columns rather than a true source→target column
    # map (UC does not record column-level lineage at the system-table tier).
    spark.sql(f"""
    CREATE OR REPLACE VIEW {CATALOG}.gold.data_lineage AS
    WITH dpdp_lineage AS (
      SELECT
        source_table_full_name AS source_table,
        target_table_full_name AS target_table,
        MAX(event_time) AS mapped_at
      FROM system.access.table_lineage
      WHERE (source_table_catalog = '{CATALOG}' OR target_table_catalog = '{CATALOG}')
        AND source_table_full_name IS NOT NULL
        AND target_table_full_name IS NOT NULL
      GROUP BY source_table_full_name, target_table_full_name
    ),
    target_pii AS (
      SELECT
        CONCAT(catalog_name,'.',schema_name,'.',table_name) AS target_table,
        CONCAT_WS(',', collect_set(column_name)) AS column_mapping
      FROM {CATALOG}.silver.pii_findings
      GROUP BY catalog_name, schema_name, table_name
    )
    SELECT
      l.source_table,
      l.target_table,
      COALESCE(p.column_mapping, '') AS column_mapping,
      (p.column_mapping IS NOT NULL) AS has_pii_flow,
      l.mapped_at
    FROM dpdp_lineage l
    LEFT JOIN target_pii p ON p.target_table = l.target_table
    """)

    print("✓ Views + UDF created")
finally:
    # Always try to re-attach + re-grant, even if view creation failed
    # — leaves share membership and grant state as they were rather
    # than silently empty.
    _reattach_to_shares(_detached_views)
    for _view_fq, _grants in _saved_grants.items():
        _restore_grants_on(_view_fq, _grants)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8 — Column masks (DPDP §5(2)) and cross-border row filter (DPDP §16)
# MAGIC
# MAGIC Workspace-level governance controls. All five mask UDFs + the
# MAGIC residency filter gate on `is_member('admins')` — workspace admins
# MAGIC (or the deploy SP) see raw values; everyone else sees redacted.

# COMMAND ----------

# Masks: 5 UDFs + ALTER TABLE ... SET MASK on 17 PII columns
mask_functions = [
    ("mask_email", """
        CASE WHEN is_member('admins') THEN val
             WHEN val IS NULL OR val = '' THEN val
             WHEN INSTR(val, '@') = 0 THEN '****'
             ELSE CONCAT(SUBSTR(val, 1, 1), '****@****',
                         SUBSTR(val, INSTR(val, '.'), LENGTH(val)))
        END"""),
    ("mask_phone", """
        CASE WHEN is_member('admins') THEN val
             WHEN val IS NULL OR val = '' THEN val
             WHEN LENGTH(val) < 4 THEN '****'
             ELSE CONCAT('******', SUBSTR(val, LENGTH(val) - 3, 4))
        END"""),
    ("mask_id_last4", """
        CASE WHEN is_member('admins') THEN val
             WHEN val IS NULL OR val = '' THEN val
             WHEN LENGTH(val) < 4 THEN '****'
             ELSE CONCAT('****', SUBSTR(val, LENGTH(val) - 3, 4))
        END"""),
    ("mask_full", """
        CASE WHEN is_member('admins') THEN val
             WHEN val IS NULL OR val = '' THEN val
             ELSE '<REDACTED>'
        END"""),
]
for name, body in mask_functions:
    spark.sql(f"""
        CREATE OR REPLACE FUNCTION {CATALOG}.compliance.{name}(val STRING)
        RETURNS STRING RETURN {body}
    """)

# Apply masks (ALTER TABLE ... SET MASK is idempotent; re-runs are a no-op)
mask_targets = [
    ("silver.employees_tagged",  "email",                    "mask_email"),
    ("silver.employees_tagged",  "phone_number",             "mask_phone"),
    ("silver.employees_tagged",  "aadhaar_number",           "mask_id_last4"),
    ("silver.employees_tagged",  "pan_number",               "mask_id_last4"),
    ("silver.employees_tagged",  "passport_number",          "mask_full"),
    ("silver.employees_tagged",  "bank_account",             "mask_id_last4"),
    ("silver.employees_tagged",  "ifsc_code",                "mask_full"),
    ("silver.customers_tagged",  "email_address",            "mask_email"),
    ("silver.customers_tagged",  "mobile",                   "mask_phone"),
    ("silver.users_tagged",      "email",                    "mask_email"),
    ("silver.users_tagged",      "phone",                    "mask_phone"),
    ("silver.patients_tagged",   "email",                    "mask_email"),
    ("silver.patients_tagged",   "phone",                    "mask_phone"),
    ("silver.patients_tagged",   "emergency_contact_phone",  "mask_phone"),
    ("silver.patients_tagged",   "aadhaar_number",           "mask_id_last4"),
    ("silver.patients_tagged",   "insurance_id",             "mask_id_last4"),
    ("silver.patients_tagged",   "medical_record_number",    "mask_id_last4"),
    ("silver.patients_tagged",   "primary_diagnosis",        "mask_full"),
    ("silver.patients_tagged",   "current_prescription",     "mask_full"),
    ("silver.patients_tagged",   "allergies",                "mask_full"),
]
for tbl, col, mask in mask_targets:
    spark.sql(
        f"ALTER TABLE {CATALOG}.{tbl} "
        f"ALTER COLUMN {col} SET MASK {CATALOG}.compliance.{mask}"
    )

print(f"✓ {len(mask_functions)} mask UDFs + {len(mask_targets)} column masks applied")

# Residency row filter — driven by the active pack's residency.yaml.
#   allowed_countries: [...]      → determines the UDF body
#   apply_filter_to: [{table,column}] → which silver tables get the filter
# Non-admin callers see only rows where `country` is in the allowed list.
# For DPDP this is ['India'] (§16); for UK GDPR the pack would list UK + EEA.
_allowed_countries = _pack.residency_allowed_countries() or ["India"]
_quoted_countries = ", ".join(f"'{c}'" for c in _allowed_countries)
spark.sql(f"""
    CREATE OR REPLACE FUNCTION {CATALOG}.compliance.residency_filter(country STRING)
    RETURNS BOOLEAN
    RETURN is_member('admins') OR country IN ({_quoted_countries})
""")
for _target in _pack.residency_apply_targets():
    _tbl = _target["table"]
    _col = _target["column"]
    # Relative table names (e.g. "silver.employees_tagged") are qualified with
    # the active CATALOG at render time so the pack stays catalog-agnostic.
    _fq = _tbl if _tbl.count(".") >= 2 else f"{CATALOG}.{_tbl}"
    spark.sql(
        f"ALTER TABLE {_fq} "
        f"SET ROW FILTER {CATALOG}.compliance.residency_filter ON ({_col})"
    )
    print(f"✓ Residency row filter applied to {_fq} on ({_col})")
print(f"✓ residency_filter allows: {_allowed_countries}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 8 — Verification summary

# COMMAND ----------

results = spark.sql(f"""
SELECT 'bronze.compliance_rules'               AS table_, COUNT(*) AS rows FROM {CATALOG}.bronze.compliance_rules
UNION ALL SELECT 'silver.compliance_gaps',                   COUNT(*)     FROM {CATALOG}.silver.compliance_gaps
UNION ALL SELECT 'compliance.notice_versions',               COUNT(*)     FROM {CATALOG}.compliance.notice_versions
UNION ALL SELECT 'compliance.consent_events_log',            COUNT(*)     FROM {CATALOG}.compliance.consent_events_log
UNION ALL SELECT 'compliance.dsr_requests (schema only)',    COUNT(*)     FROM {CATALOG}.compliance.dsr_requests
UNION ALL SELECT 'compliance.personal_data_register (view)', COUNT(*)     FROM {CATALOG}.compliance.personal_data_register
UNION ALL SELECT 'gold.marketing_eligible_principals (view)', COUNT(*)    FROM {CATALOG}.gold.marketing_eligible_principals
UNION ALL SELECT 'gold.consent_coverage_summary (view)',     COUNT(*)     FROM {CATALOG}.gold.consent_coverage_summary
""").collect()

print("Final row counts:")
for row in results:
    print(f"  {row.table_:50s} {row.rows}")

# Verify the DSR test principal fingerprint
print("\nDSR test principal (customer_04217) consent events:")
spark.sql(f"""
SELECT purpose, event_type, purpose_grant_status, event_timestamp
FROM {CATALOG}.compliance.consent_events_log
WHERE data_principal_id = 'customer_04217'
ORDER BY event_timestamp
""").show(truncate=False)
