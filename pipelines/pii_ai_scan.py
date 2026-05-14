# Databricks notebook source
# MAGIC %md
# MAGIC # AI-based PII Scan (weekly, sampled, cost-bounded)
# MAGIC
# MAGIC Companion to the regex-based DLT classifier in
# MAGIC `pipelines/classification_dlt.py`. Where regex finds *structured*
# MAGIC PII (Aadhaar, NHS number, credit card), this job uses Databricks'
# MAGIC `ai_classify` SQL function to find PII in *free-text* columns
# MAGIC (clinical notes, support tickets, product reviews) — content
# MAGIC where regex fundamentally cannot work.
# MAGIC
# MAGIC ## Cost model
# MAGIC
# MAGIC LLM calls are billed per row. To keep the cost predictable:
# MAGIC
# MAGIC - **Schedule**: weekly (vs the medallion's per-refresh cadence).
# MAGIC - **Sample cap**: `LIMIT sample_size` (default 1000) per
# MAGIC   (table, column) pair, not full-table.
# MAGIC - **Pattern-driven**: only scans columns matched by a pattern that
# MAGIC   has `ai_labels` populated (i.e., declared AI-classifiable in
# MAGIC   `regulations/<pack>/pii_patterns.py`). If no AI patterns are
# MAGIC   declared, the job exits cleanly with no LLM calls.
# MAGIC
# MAGIC At ~$0.005 per `ai_classify` call, a typical run scanning 5
# MAGIC columns × 1000 rows costs ~$25/week, ~$100/month. Tune via the
# MAGIC `sample_size` widget.
# MAGIC
# MAGIC ## Output
# MAGIC
# MAGIC Findings are written to `<catalog>.silver.pii_findings_ai` (NOT
# MAGIC the DLT-managed `pii_findings` materialized view). Same schema —
# MAGIC a UNION view over both tables is the recommended next step for
# MAGIC dashboard / DPIA consumers; keeping the tables separate now means
# MAGIC the DLT pipeline's incremental refresh semantics aren't disturbed
# MAGIC by appends from this job.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

dbutils.widgets.text("catalog", "compliance_pack", "Unity Catalog name")
dbutils.widgets.text("model_endpoint", "databricks-gpt-oss-120b",
                     "Endpoint to record in audit row (ai_classify uses Databricks' default model, not this one)")
dbutils.widgets.text("daily_pattern_budget", "1000",
                     "Max rows per AI pattern per day, divided equally across columns the pattern matches.")
dbutils.widgets.dropdown("mode", "apply", ["dry-run", "apply"],
                         "dry-run prints scan plan + cost estimate; apply runs LLM calls")
dbutils.widgets.text("table_filter", "",
                     "Optional comma-separated silver table name allowlist (empty = all)")

CATALOG               = dbutils.widgets.get("catalog")
MODEL_ENDPOINT        = dbutils.widgets.get("model_endpoint")
DAILY_PATTERN_BUDGET  = int(dbutils.widgets.get("daily_pattern_budget"))
MODE                  = dbutils.widgets.get("mode")
TABLE_FILTER          = [t.strip() for t in dbutils.widgets.get("table_filter").split(",") if t.strip()]

# Per-table primary key column. Required for the state-table join — a table
# without a stable PK can't track per-row scan state. New silver tables added
# by `notebooks/01_add_data_source.py` should be registered here too.
TABLE_PK_MAP = {
    "customers_tagged":                     "customer_id",
    "employees_tagged":                     "employee_id",
    "patients_tagged":                      "patient_id",
    "transactions_tagged":                  "transaction_id",
    "users_tagged":                         "user_id",
    "sf_accounts_tagged":                   "account_id",
    "sf_contacts_tagged":                   "contact_id",
    "sf_leads_tagged":                      "lead_id",
    "federation_lead_scoring_tagged":       "score_id",
    "federation_campaign_response_tagged":  "response_id",
}

print(f"Catalog:               {CATALOG}")
print(f"Model endpoint:        {MODEL_ENDPOINT}")
print(f"Daily pattern budget:  {DAILY_PATTERN_BUDGET} rows/pattern (split across matching columns)")
print(f"Mode:                  {MODE}")
print(f"Table filter:          {TABLE_FILTER or '(all silver tables)'}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Repo-root locator + pattern import
# MAGIC
# MAGIC Same pattern as `pipelines/dpia_generator.py`. Bundle sync places
# MAGIC the repo under `<bundle>/files/`; we walk up until we find the
# MAGIC `governance_core/` marker.

# COMMAND ----------

import os
import sys
import uuid
import time
import json
from datetime import datetime, timezone

def _locate_repo_root() -> str:
    here = os.path.dirname(os.path.abspath("__notebook__"))
    for _ in range(8):
        if os.path.isdir(os.path.join(here, "governance_core")):
            return here
        parent = os.path.dirname(here)
        if parent == here:
            break
        here = parent
    raise RuntimeError("Could not locate repo root containing governance_core/")

REPO_ROOT = _locate_repo_root()
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from governance_core.pack_loader import loaded_packs
from governance_core.pii_patterns.universal import (
    UNIVERSAL_PATTERNS,
    REVIEW_REQUIRED_THRESHOLD,
    calculate_confidence,
)

# Build the full pattern library = universal + every loaded pack's patterns
PATTERN_LIBRARY = list(UNIVERSAL_PATTERNS)
for pack in loaded_packs():
    PATTERN_LIBRARY.extend(pack.pii_patterns())

AI_PATTERNS = [p for p in PATTERN_LIBRARY if p.is_ai_classifiable()]
print(f"Patterns loaded: {len(PATTERN_LIBRARY)} total, {len(AI_PATTERNS)} AI-classifiable")

if not AI_PATTERNS:
    print("")
    print("No AI-classifiable patterns declared. To enable AI-based PII")
    print("detection, add a pattern with `ai_labels=[...]` to one of:")
    print("  - governance_core/pii_patterns/universal.py")
    print(f"  - regulations/<pack>/pii_patterns.py  (loaded packs: {[p.code for p in loaded_packs()]})")
    print("")
    print("Exiting cleanly — no LLM calls made.")
    dbutils.notebook.exit("no-ai-patterns")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Resolve target silver tables
# MAGIC
# MAGIC Same source as the regex DLT scanner: `bronze.data_sources` filtered
# MAGIC to active sources with a populated `silver_table_name`. If the
# MAGIC `table_filter` widget is set, intersect with that allowlist.

# COMMAND ----------

silver_rows = (
    spark.table(f"{CATALOG}.bronze.data_sources")
    .filter("is_active = true AND silver_table_name IS NOT NULL")
    .select("silver_table_name")
    .collect()
)
silver_tables = [r[0] for r in silver_rows]
if TABLE_FILTER:
    silver_tables = [t for t in silver_tables if t in TABLE_FILTER]
print(f"Silver tables to scan: {len(silver_tables)} → {silver_tables}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure target table exists
# MAGIC
# MAGIC Same column shape as `silver.pii_findings` for trivial UNION later.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.silver.pii_findings_ai (
    finding_id              STRING      NOT NULL,
    scan_job_id             STRING      NOT NULL,
    catalog_name            STRING      NOT NULL,
    schema_name             STRING      NOT NULL,
    table_name              STRING      NOT NULL,
    column_name             STRING      NOT NULL,
    column_data_type        STRING      NOT NULL,
    pii_category            STRING      NOT NULL,
    pii_type                STRING      NOT NULL,
    sensitivity_tier        STRING      NOT NULL,
    confidence              DOUBLE      NOT NULL,
    classifier_source       STRING      NOT NULL,
    match_rate              DOUBLE,
    regulations             ARRAY<STRING>   NOT NULL,
    sample_match_redacted   STRING,
    human_reviewed          BOOLEAN     NOT NULL,
    review_status           STRING,
    review_notes            STRING,
    discovered_at           TIMESTAMP   NOT NULL,
    reviewed_at             TIMESTAMP,
    -- ai-scan-specific fields (not in regex pii_findings)
    model_endpoint          STRING,
    sample_rows_scanned     BIGINT,
    ai_label_distribution   MAP<STRING, BIGINT>
) USING DELTA
COMMENT 'AI-based PII findings (ai_classify). Companion to pii_findings (regex). UNION the two for full inventory.'
""")
print(f"✓ {CATALOG}.silver.pii_findings_ai ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure pii_findings_all UNION view
# MAGIC
# MAGIC The view exposes the 20 common columns of `pii_findings` (regex,
# MAGIC DLT-managed) and `pii_findings_ai` (this notebook), plus 3 nullable
# MAGIC ai-only extras. Downstream consumers (dashboard tiles, DPIA prompt
# MAGIC builder, gap analysis) should read from this view instead of
# MAGIC `pii_findings` directly to see both regex AND ai findings in one
# MAGIC inventory.
# MAGIC
# MAGIC Created with CREATE OR REPLACE so every run keeps the view in sync
# MAGIC if the column shape ever evolves. Cheap (no data scan).

# COMMAND ----------

spark.sql(f"""
CREATE OR REPLACE VIEW {CATALOG}.silver.pii_findings_all
COMMENT 'UNION of pii_findings (regex/DLT) + pii_findings_ai (ai_classify). Read this for full PII inventory.'
AS
SELECT
    finding_id, scan_job_id, catalog_name, schema_name, table_name, column_name,
    column_data_type, pii_category, pii_type, sensitivity_tier,
    confidence, classifier_source, match_rate, regulations,
    sample_match_redacted, human_reviewed, review_status, review_notes,
    discovered_at, reviewed_at,
    CAST(NULL AS STRING)              AS model_endpoint,
    CAST(NULL AS BIGINT)              AS sample_rows_scanned,
    CAST(NULL AS MAP<STRING, BIGINT>) AS ai_label_distribution
FROM {CATALOG}.silver.pii_findings
UNION ALL
SELECT
    finding_id, scan_job_id, catalog_name, schema_name, table_name, column_name,
    column_data_type, pii_category, pii_type, sensitivity_tier,
    confidence, classifier_source, match_rate, regulations,
    sample_match_redacted, human_reviewed, review_status, review_notes,
    discovered_at, reviewed_at,
    model_endpoint, sample_rows_scanned, ai_label_distribution
FROM {CATALOG}.silver.pii_findings_ai
""")
print(f"✓ {CATALOG}.silver.pii_findings_all view ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Ensure per-row scan state table
# MAGIC
# MAGIC `pii_ai_scan_row_state` records which (table, column, pattern, source row)
# MAGIC tuples have already been classified — drives the daily-budget logic so
# MAGIC each row is classified at most once unless its `_ingested_at` advances.
# MAGIC Same DDL as in `phase1_bootstrap.py` for self-contained runs.

# COMMAND ----------

spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.pii_ai_scan_row_state (
    table_name           STRING NOT NULL,
    column_name          STRING NOT NULL,
    pattern_id           STRING NOT NULL,
    row_pk               STRING NOT NULL,
    classified_at        TIMESTAMP NOT NULL,
    source_ingested_at   TIMESTAMP NOT NULL,
    classified_label     STRING,
    last_scan_job_id     STRING NOT NULL
) USING DELTA
COMMENT 'Per-source-row state for pii_ai_scan. One row per (table, column, pattern, source row) once classified. NULL absence means never scanned.'
""")
print(f"✓ {CATALOG}.compliance.pii_ai_scan_row_state ready")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Build scan plan grouped by pattern
# MAGIC
# MAGIC The per-pattern budget gets divided equally across the columns each
# MAGIC pattern matches. For 1 pattern matching 3 columns: each column gets
# MAGIC ~333 rows/day. For 4 patterns × ~3 cols each: ≤4000 rows/day total.

# COMMAND ----------

# Group scan tuples by pattern_id so we can divide budget across the columns
# each pattern matches.
pattern_to_tuples = {}  # pattern_id -> list of {table, column, dtype, pattern, pk}
for table in silver_tables:
    if table not in TABLE_PK_MAP:
        print(f"  ⚠ no PK mapping for {table}, skipping")
        continue
    fq = f"{CATALOG}.silver.{table}"
    try:
        schema = spark.table(fq).schema
    except Exception as exc:
        print(f"  ⚠ skip {fq}: {exc}")
        continue
    for field in schema.fields:
        if field.name.startswith("_"):
            continue
        if str(field.dataType.simpleString()) != "string":
            continue  # ai_classify is text-only
        for pattern in AI_PATTERNS:
            if pattern.matches_column_name(field.name):
                pattern_to_tuples.setdefault(pattern.pattern_id, []).append({
                    "table": table,
                    "column": field.name,
                    "dtype": str(field.dataType.simpleString()),
                    "pattern": pattern,
                    "pk": TABLE_PK_MAP[table],
                })

scan_plan_size = sum(len(t) for t in pattern_to_tuples.values())
total_budget = len(pattern_to_tuples) * DAILY_PATTERN_BUDGET
print(f"Scan plan: {len(pattern_to_tuples)} patterns × {scan_plan_size // max(len(pattern_to_tuples),1)} avg cols each = {scan_plan_size} (table, column, pattern) tuples")
for pid, tuples in pattern_to_tuples.items():
    per_col = DAILY_PATTERN_BUDGET // len(tuples)
    print(f"  pattern {pid}: {len(tuples)} columns × {per_col} rows/col/day = {DAILY_PATTERN_BUDGET}/day budget")
    for t in tuples[:5]:
        print(f"      → {t['table']}.{t['column']}")
    if len(tuples) > 5:
        print(f"      ... and {len(tuples)-5} more")

print(f"\nDaily LLM call cap: ≤ {total_budget:,} (= {len(pattern_to_tuples)} patterns × {DAILY_PATTERN_BUDGET})")

if MODE == "dry-run":
    print("\nMODE=dry-run — exiting without LLM calls.")
    dbutils.notebook.exit(json.dumps({
        "mode": "dry-run",
        "patterns": len(pattern_to_tuples),
        "scan_plan_size": scan_plan_size,
        "daily_call_cap": total_budget,
    }))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute scan: run ai_classify per (table, column, pattern), aggregate findings

# COMMAND ----------

SCAN_JOB_ID  = str(uuid.uuid4())
DISCOVERED_AT = datetime.now(timezone.utc)

# Get state-table schema once for createDataFrame typing
_state_schema = spark.table(f"{CATALOG}.compliance.pii_ai_scan_row_state").schema

scan_summary = []  # per-tuple summary for end-of-run reporting

for pattern_id, tuples in pattern_to_tuples.items():
    pattern = tuples[0]["pattern"]
    labels_sql = ", ".join(f"'{lbl}'" for lbl in pattern.ai_labels)
    # Negative label is the last entry by convention — anything else counts
    # as a positive PII signal. Pack authors should put a clearly-negative
    # label last (e.g., 'non_medical', 'not_pii').
    negative_label = pattern.ai_labels[-1]

    # Per-pattern budget split equally across this pattern's matching columns
    # (with remainder distributed to the first few columns).
    per_col = DAILY_PATTERN_BUDGET // len(tuples)
    extra   = DAILY_PATTERN_BUDGET % len(tuples)

    for i, t in enumerate(tuples):
        table     = t["table"]
        col_name  = t["column"]
        col_dtype = t["dtype"]
        pk_col    = t["pk"]
        budget    = per_col + (1 if i < extra else 0)
        fq        = f"{CATALOG}.silver.{table}"

        # State-aware SELECT: prioritize NULL state (never scanned), then
        # rows where source data has advanced since last scan; oldest first
        # within each priority bucket. NOTE: ai_classify uses Databricks'
        # managed default model and does NOT accept an `endpoint` parameter
        # (SQL API rejects with AI_FUNCTION_COMPILATION_ERROR). MODEL_ENDPOINT
        # is recorded in audit metadata only.
        scan_sql = f"""
            WITH eligible AS (
                SELECT t.{pk_col} AS row_pk, t.{col_name} AS val, t._ingested_at,
                       CASE WHEN s.classified_at IS NULL THEN 0 ELSE 1 END AS priority
                FROM {fq} t
                LEFT JOIN {CATALOG}.compliance.pii_ai_scan_row_state s
                    ON s.table_name = '{table}'
                   AND s.column_name = '{col_name}'
                   AND s.pattern_id = '{pattern_id}'
                   AND s.row_pk = CAST(t.{pk_col} AS STRING)
                WHERE t.{col_name} IS NOT NULL
                  AND (s.classified_at IS NULL
                       OR s.source_ingested_at < t._ingested_at)
            )
            SELECT row_pk, val, _ingested_at,
                   ai_classify(val, ARRAY({labels_sql})) AS lbl
            FROM eligible
            ORDER BY priority, _ingested_at
            LIMIT {budget}
        """
        try:
            t0 = time.time()
            scanned = spark.sql(scan_sql).collect()
            elapsed = time.time() - t0
        except Exception as exc:
            print(f"  ✗ {table}.{col_name} ({pattern_id}) failed: {str(exc)[:200]}")
            continue

        if not scanned:
            print(f"  ⏭  {table}.{col_name} ({pattern_id}): no eligible rows (already classified or empty), skip")
            continue

        # MERGE the just-scanned rows' state forward (insert new, update existing).
        state_rows = [{
            "table_name":         table,
            "column_name":        col_name,
            "pattern_id":         pattern_id,
            "row_pk":             str(r["row_pk"]),
            "classified_at":      DISCOVERED_AT,
            "source_ingested_at": r["_ingested_at"],
            "classified_label":   r["lbl"],
            "last_scan_job_id":   SCAN_JOB_ID,
        } for r in scanned]
        state_df = spark.createDataFrame(state_rows, schema=_state_schema)
        state_df.createOrReplaceTempView("_pii_ai_new_state")
        spark.sql(f"""
            MERGE INTO {CATALOG}.compliance.pii_ai_scan_row_state t
            USING _pii_ai_new_state s
              ON t.table_name = s.table_name
             AND t.column_name = s.column_name
             AND t.pattern_id = s.pattern_id
             AND t.row_pk = s.row_pk
            WHEN MATCHED THEN UPDATE SET
                classified_at      = s.classified_at,
                source_ingested_at = s.source_ingested_at,
                classified_label   = s.classified_label,
                last_scan_job_id   = s.last_scan_job_id
            WHEN NOT MATCHED THEN INSERT *
        """)

        # Recompute the column-level finding from CUMULATIVE state (not just
        # this scan's rows). Match_rate / confidence stabilize as more rows
        # accumulate over multiple daily runs.
        agg = spark.sql(f"""
            SELECT
                COUNT(*) AS scanned_total,
                SUM(CASE WHEN classified_label != '{negative_label}' THEN 1 ELSE 0 END) AS positive_total
            FROM {CATALOG}.compliance.pii_ai_scan_row_state
            WHERE table_name = '{table}'
              AND column_name = '{col_name}'
              AND pattern_id = '{pattern_id}'
        """).collect()[0]
        scanned_total  = int(agg["scanned_total"] or 0)
        positive_total = int(agg["positive_total"] or 0)
        match_rate     = positive_total / scanned_total if scanned_total else 0.0
        confidence     = calculate_confidence(
            column_match=True,
            value_match=positive_total > 0,
            match_rate=match_rate,
        )

        # Label distribution as a Python dict for the finding row's MAP column.
        # Filter NULL classified_label out — ai_classify occasionally returns
        # NULL (model couldn't decide / value was malformed), and Spark Connect
        # rejects None keys in MAP<STRING, BIGINT> with PySparkValueError.
        dist_rows = spark.sql(f"""
            SELECT classified_label, COUNT(*) AS cnt
            FROM {CATALOG}.compliance.pii_ai_scan_row_state
            WHERE table_name = '{table}' AND column_name = '{col_name}' AND pattern_id = '{pattern_id}'
              AND classified_label IS NOT NULL
            GROUP BY classified_label
        """).collect()
        label_distribution = {r["classified_label"]: int(r["cnt"])
                              for r in dist_rows if r["classified_label"] is not None}

        print(f"  ✓ {table}.{col_name} ({pattern_id}): "
              f"+{len(scanned)} this run / {scanned_total} cumulative, "
              f"match {match_rate:.1%}, conf {confidence:.2f}, {elapsed:.1f}s")

        scan_summary.append({
            "table": table, "column": col_name, "pattern_id": pattern_id,
            "scanned_this_run": len(scanned), "scanned_cumulative": scanned_total,
            "match_rate": match_rate, "confidence": confidence,
            "above_threshold": confidence >= REVIEW_REQUIRED_THRESHOLD,
        })

        if confidence < REVIEW_REQUIRED_THRESHOLD:
            # Below threshold — also remove any stale finding row (e.g., earlier
            # runs that crossed the threshold but no longer do).
            spark.sql(f"""
                DELETE FROM {CATALOG}.silver.pii_findings_ai
                WHERE table_name = '{table}' AND column_name = '{col_name}' AND pii_type = '{pattern.pii_type}'
            """)
            continue

        # Get a sample positive value for the redacted_sample column
        sample_match = spark.sql(f"""
            SELECT t.{col_name} AS val
            FROM {fq} t
            JOIN {CATALOG}.compliance.pii_ai_scan_row_state s
              ON s.row_pk = CAST(t.{pk_col} AS STRING)
             AND s.table_name = '{table}' AND s.column_name = '{col_name}' AND s.pattern_id = '{pattern_id}'
            WHERE s.classified_label != '{negative_label}'
            LIMIT 1
        """).collect()
        sample_redacted = None
        if sample_match:
            s = str(sample_match[0]["val"])
            sample_redacted = (s[:60] + "...") if len(s) > 60 else s

        # MERGE finding row — one row per (table, column, pii_type), updated
        # in place each scan to reflect cumulative state.
        finding_row = [{
            "finding_id":            str(uuid.uuid4()),
            "scan_job_id":           SCAN_JOB_ID,
            "catalog_name":          CATALOG,
            "schema_name":           "silver",
            "table_name":            table,
            "column_name":           col_name,
            "column_data_type":      col_dtype,
            "pii_category":          pattern.category,
            "pii_type":              pattern.pii_type,
            "sensitivity_tier":      pattern.sensitivity,
            "confidence":            confidence,
            "classifier_source":     "ai_classify",
            "match_rate":            match_rate,
            "regulations":           pattern.regulations,
            "sample_match_redacted": sample_redacted,
            "human_reviewed":        False,
            "review_status":         None,
            "review_notes":          None,
            "discovered_at":         DISCOVERED_AT,
            "reviewed_at":           None,
            "model_endpoint":        MODEL_ENDPOINT,
            "sample_rows_scanned":   scanned_total,
            "ai_label_distribution": label_distribution,
        }]
        finding_schema = spark.table(f"{CATALOG}.silver.pii_findings_ai").schema
        finding_df = spark.createDataFrame(finding_row, schema=finding_schema)
        finding_df.createOrReplaceTempView("_pii_ai_new_finding")
        spark.sql(f"""
            MERGE INTO {CATALOG}.silver.pii_findings_ai t
            USING _pii_ai_new_finding s
              ON t.table_name = s.table_name
             AND t.column_name = s.column_name
             AND t.pii_type = s.pii_type
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

print(f"\nScan summary: {len(scan_summary)} (table, column, pattern) tuples processed")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Exit summary
# MAGIC
# MAGIC State + finding rows already MERGEd in the loop above. Just emit a
# MAGIC structured exit value so the run page surfaces what happened.

# COMMAND ----------

above_threshold = sum(1 for s in scan_summary if s["above_threshold"])
total_scanned_this_run = sum(s["scanned_this_run"] for s in scan_summary)

dbutils.notebook.exit(json.dumps({
    "mode": "apply",
    "scan_job_id": SCAN_JOB_ID,
    "patterns": len(pattern_to_tuples),
    "tuples_processed": len(scan_summary),
    "rows_classified_this_run": total_scanned_this_run,
    "findings_above_threshold": above_threshold,
    "model_endpoint": MODEL_ENDPOINT,
}))
