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
                     "Foundation model endpoint for ai_classify")
dbutils.widgets.text("sample_size", "1000",
                     "Rows sampled per (table, column) — caps cost")
dbutils.widgets.dropdown("mode", "apply", ["dry-run", "apply"],
                         "dry-run prints scan plan + cost estimate; apply runs LLM calls")
dbutils.widgets.text("table_filter", "",
                     "Optional comma-separated silver table name allowlist (empty = all)")

CATALOG        = dbutils.widgets.get("catalog")
MODEL_ENDPOINT = dbutils.widgets.get("model_endpoint")
SAMPLE_SIZE    = int(dbutils.widgets.get("sample_size"))
MODE           = dbutils.widgets.get("mode")
TABLE_FILTER   = [t.strip() for t in dbutils.widgets.get("table_filter").split(",") if t.strip()]

print(f"Catalog:        {CATALOG}")
print(f"Model endpoint: {MODEL_ENDPOINT}")
print(f"Sample size:    {SAMPLE_SIZE} rows per (table, column) pair")
print(f"Mode:           {MODE}")
print(f"Table filter:   {TABLE_FILTER or '(all silver tables)'}")

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
# MAGIC ## Build scan plan: (table, column, pattern) tuples

# COMMAND ----------

scan_plan = []
for table in silver_tables:
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
                scan_plan.append((table, field.name, str(field.dataType.simpleString()), pattern))

print(f"Scan plan: {len(scan_plan)} (table, column, pattern) tuples")
for tbl, col, dtype, pat in scan_plan[:20]:
    print(f"  {tbl}.{col} → pattern={pat.pattern_id} labels={pat.ai_labels}")
if len(scan_plan) > 20:
    print(f"  ... and {len(scan_plan) - 20} more")

estimated_calls = len(scan_plan) * SAMPLE_SIZE
estimated_usd   = estimated_calls * 0.005
print(f"\nEstimated cost: {estimated_calls:,} ai_classify calls × ~$0.005 ≈ ${estimated_usd:.2f}")

if MODE == "dry-run":
    print("\nMODE=dry-run — exiting without LLM calls.")
    dbutils.notebook.exit(json.dumps({
        "mode": "dry-run",
        "scan_plan_size": len(scan_plan),
        "estimated_calls": estimated_calls,
        "estimated_usd": round(estimated_usd, 2),
    }))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Execute scan: run ai_classify per (table, column, pattern), aggregate findings

# COMMAND ----------

SCAN_JOB_ID  = str(uuid.uuid4())
DISCOVERED_AT = datetime.now(timezone.utc)
findings = []

for table, col_name, col_dtype, pattern in scan_plan:
    fq = f"{CATALOG}.silver.{table}"
    labels_sql = ", ".join(f"'{lbl}'" for lbl in pattern.ai_labels)
    # The "negative" label is the last entry by convention — anything else
    # counts as a positive PII signal. Pack authors should put a clearly-
    # negative label last (e.g., 'non_medical', 'not_pii').
    negative_label = pattern.ai_labels[-1]

    sql = f"""
        WITH sampled AS (
            SELECT {col_name} AS val
            FROM {fq}
            WHERE {col_name} IS NOT NULL
            LIMIT {SAMPLE_SIZE}
        ),
        classified AS (
            SELECT val,
                   ai_classify(val, ARRAY({labels_sql}), endpoint => '{MODEL_ENDPOINT}') AS lbl
            FROM sampled
        )
        SELECT
            COUNT(*)                                            AS sampled,
            SUM(CASE WHEN lbl != '{negative_label}' THEN 1 ELSE 0 END) AS positive,
            collect_list(lbl)                                   AS label_dist,
            FIRST(CASE WHEN lbl != '{negative_label}' THEN val END, true) AS sample_match
        FROM classified
    """
    try:
        t0 = time.time()
        row = spark.sql(sql).collect()[0]
        elapsed = time.time() - t0
    except Exception as exc:
        print(f"  ✗ {table}.{col_name} ({pattern.pattern_id}) failed: {str(exc)[:200]}")
        continue

    sampled = int(row["sampled"] or 0)
    positive = int(row["positive"] or 0)
    if sampled == 0:
        print(f"  ⏭  {table}.{col_name} ({pattern.pattern_id}): empty column, skip")
        continue

    match_rate = positive / sampled
    # Same confidence calc as the regex scanner — column hint always matches
    # here (pattern was selected by matches_column_name), value match if any
    # row tested positive.
    confidence = calculate_confidence(
        column_match=True,
        value_match=positive > 0,
        match_rate=match_rate,
    )

    print(f"  ✓ {table}.{col_name} ({pattern.pattern_id}): "
          f"{positive}/{sampled} positive ({match_rate:.1%}), "
          f"conf={confidence:.2f}, {elapsed:.1f}s")

    if confidence < REVIEW_REQUIRED_THRESHOLD:
        continue  # below threshold — not a finding

    # Aggregate label distribution for the finding row
    from collections import Counter
    label_counts = dict(Counter(row["label_dist"] or []))

    # Redacted sample (truncate + ellipsis; full redaction is the regex
    # scanner's redact_sample helper which is pii_type-specific. Free text
    # warrants a simpler approach — show first 60 chars).
    sample_redacted = None
    if row["sample_match"]:
        s = str(row["sample_match"])
        sample_redacted = (s[:60] + "...") if len(s) > 60 else s

    findings.append({
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
        "sample_rows_scanned":   sampled,
        "ai_label_distribution": label_counts,
    })

print(f"\nFindings emitted: {len(findings)}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Append findings to pii_findings_ai

# COMMAND ----------

if findings:
    findings_df = spark.createDataFrame(findings)
    findings_df.write.mode("append").saveAsTable(f"{CATALOG}.silver.pii_findings_ai")
    print(f"✓ Appended {len(findings)} findings to {CATALOG}.silver.pii_findings_ai (scan_job_id={SCAN_JOB_ID})")
else:
    print(f"No findings above threshold ({REVIEW_REQUIRED_THRESHOLD}). No rows appended.")

dbutils.notebook.exit(json.dumps({
    "mode": "apply",
    "scan_job_id": SCAN_JOB_ID,
    "scan_plan_size": len(scan_plan),
    "findings_emitted": len(findings),
    "model_endpoint": MODEL_ENDPOINT,
}))
