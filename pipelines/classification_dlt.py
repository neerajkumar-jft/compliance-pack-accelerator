# Databricks notebook source
# MAGIC %md
# MAGIC # PII Classification DLT Table
# MAGIC
# MAGIC Classification runs as a DLT table in the same pipeline as the medallion.
# MAGIC The classifier reads every Silver table, applies the 16-pattern library
# MAGIC from `schemas/pii_patterns.py`, and writes findings to
# MAGIC `silver.pii_findings`.
# MAGIC
# MAGIC Uses the vectorized Spark SQL pattern from §4.5.1 — no driver-side
# MAGIC `.collect()`, no Python per-row loop. Each (table, column, pattern)
# MAGIC combination is one aggregation query that stays on executors.
# MAGIC
# MAGIC UC tag application is a separate step (apply_uc_tags.py) because
# MAGIC `ALTER TABLE ... SET TAGS` is a DDL operation and doesn't fit in DLT's
# MAGIC data-flow model.

# COMMAND ----------

import dlt
import uuid
import sys
from datetime import datetime
from pyspark.sql import functions as F
from pyspark.sql.types import (
    StructType, StructField, StringType, DoubleType, BooleanType,
    ArrayType, TimestampType,
)

# Add spec schemas/ to path so we can import the pattern library
sys.path.insert(0, "/Workspace/Repos/compliance_pack_spec/schemas")
sys.path.insert(0, "../schemas")  # relative path from pipelines/

from pii_patterns import (  # noqa: E402
    PATTERN_LIBRARY,
    calculate_confidence,
    redact_sample,
    AUTO_CLASSIFY_THRESHOLD,
    REVIEW_REQUIRED_THRESHOLD,
)

CATALOG = spark.conf.get("catalog", "compliance_pack")
SCAN_JOB_ID = str(uuid.uuid4())  # unique per pipeline run

# First-deploy fallback list. Used only if `bronze.data_sources` doesn't exist
# yet (workspace hasn't run phase1_bootstrap) — keeps a fresh deploy from
# silently producing zero findings. Once phase1_bootstrap.py §2.5 has seeded
# the table (10 rows: 5 Auto Loader + 3 Salesforce + 2 federation), the
# data_sources read below replaces this list entirely.
_FALLBACK_SILVER_TABLES = [
    "employees_tagged", "customers_tagged", "patients_tagged",
    "transactions_tagged", "users_tagged",
]


def _resolve_silver_tables() -> list[str]:
    """Return the list of silver tables to scan, sourced from
    ``bronze.data_sources`` when seeded, otherwise the fallback list.

    The .collect() here is a tiny metadata read (≤20 rows) and stays on
    the driver; the "no driver-side .collect()" rule in the module
    docstring applies to data scans, not configuration lookups.
    """
    try:
        rows = (
            spark.table(f"{CATALOG}.bronze.data_sources")
            .filter("is_active = true AND silver_table_name IS NOT NULL")
            .select("silver_table_name")
            .collect()
        )
        if rows:
            return [r[0] for r in rows]
        print(f"WARNING: {CATALOG}.bronze.data_sources is empty; using fallback list")
    except Exception as exc:
        print(f"WARNING: cannot read {CATALOG}.bronze.data_sources ({exc}); using fallback list")
    return _FALLBACK_SILVER_TABLES


SILVER_TABLES = _resolve_silver_tables()
print(f"classification_dlt: scanning {len(SILVER_TABLES)} silver objects → {SILVER_TABLES}")

# ---------------------------------------------------------------------------
# Findings schema
# ---------------------------------------------------------------------------

_FINDINGS_SCHEMA = StructType([
    StructField("finding_id",            StringType(), False),
    StructField("scan_job_id",           StringType(), False),
    StructField("catalog_name",          StringType(), False),
    StructField("schema_name",           StringType(), False),
    StructField("table_name",            StringType(), False),
    StructField("column_name",           StringType(), False),
    StructField("column_data_type",      StringType(), False),
    StructField("pii_category",          StringType(), False),
    StructField("pii_type",              StringType(), False),
    StructField("sensitivity_tier",      StringType(), False),
    StructField("confidence",            DoubleType(), False),
    StructField("classifier_source",     StringType(), False),
    StructField("match_rate",            DoubleType(), True),
    StructField("regulations",           ArrayType(StringType()), False),
    StructField("sample_match_redacted", StringType(), True),
    StructField("human_reviewed",        BooleanType(), False),
    StructField("review_status",         StringType(), True),
    StructField("review_notes",          StringType(), True),
    StructField("discovered_at",         TimestampType(), False),
    StructField("reviewed_at",           TimestampType(), True),
])


# ---------------------------------------------------------------------------
# Core scan: vectorized Spark SQL per (column, pattern)
# ---------------------------------------------------------------------------

def _scan_column_regex(df, column_name: str, pattern) -> tuple[int, float]:
    """Vectorized regex match rate using regexp_extract. Stays on executors."""
    col = F.col(column_name).cast("string")
    totals = df.agg(
        F.count(F.when(col.isNotNull() & (col != ""), 1)).alias("total"),
        F.count(F.when(col.isNotNull() & (col != "") &
                       (F.regexp_extract(col, pattern.regex_pattern, 0) != ""), 1)).alias("matched"),
    ).collect()[0]
    total = totals["total"] or 0
    matched = totals["matched"] or 0
    return matched, (matched / total if total else 0.0)


def _sample_one_match(df, column_name: str, pattern) -> str | None:
    """Return a single matching value (small collect, 1 row only) for redaction."""
    col = F.col(column_name).cast("string")
    sample = (
        df.filter(col.isNotNull() & (col != "") &
                  (F.regexp_extract(col, pattern.regex_pattern, 0) != ""))
          .limit(1)
          .select(col)
          .collect()
    )
    return sample[0][0] if sample else None


def _scan_table(catalog: str, table_name: str) -> list:
    """Scan all columns in one Silver table; return list of finding dicts."""
    # NOTE: reads from the UC table (post-commit snapshot) rather than via
    # dlt.read(). Using dlt.read() returns an uncommitted view of the silver
    # flow that's still materializing concurrently, which produces empty scans.
    # The tradeoff: DLT's dependency graph doesn't know pii_findings depends
    # on the *_tagged tables, so on a FULL refresh the scan may see stale
    # data. On incremental runs (the default for run_medallion) this works
    # correctly because silver tables are committed before pii_findings runs.
    fq_name = f"{catalog}.silver.{table_name}"
    df = spark.table(fq_name)
    schema = df.schema
    findings = []

    for field in schema.fields:
        col_name = field.name
        if col_name.startswith("_"):  # skip metadata columns
            continue
        col_type = str(field.dataType.simpleString())

        # Step 1: column hint matches (pure Python, no data touched)
        col_matches = [p for p in PATTERN_LIBRARY if p.matches_column_name(col_name)]
        col_matches.sort(key=lambda p: p.priority, reverse=True)

        # Step 2: for every pattern (with or without hint), try regex
        scored = []
        for pattern in PATTERN_LIBRARY:
            column_match = pattern in col_matches
            value_match = False
            match_rate = 0.0
            sample = None

            if pattern.regex_pattern:
                matched, rate = _scan_column_regex(df, col_name, pattern)
                if matched > 0:
                    value_match = True
                    match_rate = rate
                    sample = _sample_one_match(df, col_name, pattern)

            confidence = calculate_confidence(column_match, value_match, match_rate)
            if confidence >= REVIEW_REQUIRED_THRESHOLD:
                scored.append((pattern, confidence, value_match, match_rate, sample))

        # Step 3: conflict resolution — highest priority wins, tie-broken by confidence
        if not scored:
            continue
        scored.sort(key=lambda s: (s[0].priority, s[1]), reverse=True)
        pattern, confidence, value_match, match_rate, sample = scored[0]

        classifier_source = (
            "hybrid" if (pattern in col_matches and value_match)
            else "column_hint" if pattern in col_matches
            else "regex"
        )

        redacted = redact_sample(sample, pattern.pii_type) if sample else None

        findings.append({
            "finding_id":            str(uuid.uuid4()),
            "scan_job_id":           SCAN_JOB_ID,
            "catalog_name":          catalog,
            "schema_name":           "silver",
            "table_name":            table_name,
            "column_name":           col_name,
            "column_data_type":      col_type,
            "pii_category":          pattern.category,
            "pii_type":              pattern.pii_type,
            "sensitivity_tier":      pattern.sensitivity,
            "confidence":            float(confidence),
            "classifier_source":     classifier_source,
            "match_rate":            float(match_rate) if value_match else None,
            "regulations":           pattern.regulations,
            "sample_match_redacted": redacted,
            "human_reviewed":        False,
            "review_status":         None,
            "review_notes":          None,
            "discovered_at":         datetime.now(),
            "reviewed_at":           None,
        })

    return findings


# ---------------------------------------------------------------------------
# DLT table: pii_findings
# ---------------------------------------------------------------------------

@dlt.table(
    name="pii_findings",
    comment="Column-level PII classification results. One row per (table, column) pair with confidence ≥ 0.65.",
    table_properties={"quality": "silver"},
)
def pii_findings():
    """Produce all findings across all Silver tables in this pipeline run."""
    all_findings = []
    for tbl in SILVER_TABLES:
        try:
            all_findings.extend(_scan_table(CATALOG, tbl))
        except Exception as exc:
            # Log but don't fail the pipeline — other tables still classify
            print(f"WARNING: failed to scan {tbl}: {exc}")

    if not all_findings:
        # DLT requires a non-empty return; emit an empty DataFrame with schema
        return spark.createDataFrame([], _FINDINGS_SCHEMA)

    return spark.createDataFrame(all_findings, _FINDINGS_SCHEMA)
