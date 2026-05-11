# Databricks notebook source
# MAGIC %md
# MAGIC # Medallion DLT Pipeline
# MAGIC
# MAGIC Declarative Bronze + Silver layer for the DPDP POC. Replaces the manual
# MAGIC Auto Loader + Spark SQL approach from §3.4 with a Lakeflow Declarative
# MAGIC Pipeline. Benefits:
# MAGIC
# MAGIC - Automatic Unity Catalog lineage (no manual lineage tagging)
# MAGIC - Data quality expectations surfaced in the DLT monitoring UI
# MAGIC - Streaming + batch unified; Auto Loader drives Bronze directly
# MAGIC - Idempotent; `full_refresh = false` re-ingests only new files
# MAGIC - Pipeline graph visible in the workspace — useful on Day 14 demo
# MAGIC
# MAGIC Consumed by the `medallion_pipeline` resource in resources/pipelines.yml.

# COMMAND ----------

import dlt
from datetime import datetime, timezone

from pyspark.sql import functions as F
from pyspark.sql.types import StringType

# ---------------------------------------------------------------------------
# Configuration read from pipeline parameters
# ---------------------------------------------------------------------------
CATALOG = spark.conf.get("catalog", "dpdp_poc")
LANDING_ROOT = spark.conf.get("landing_volume_path", f"/Volumes/{CATALOG}/bronze/landing")
CHECKPOINT_ROOT = f"/Volumes/{CATALOG}/bronze/checkpoints"

# ---------------------------------------------------------------------------
# Per-data-subject jurisdiction routing (ADR-0001)
# ---------------------------------------------------------------------------
# Each customer-level silver table carries a `jurisdiction` column that drives
# rule routing in the compliance layer. Derived from any country signal
# present in the source row. The SQL CASE below mirrors the canonical Python
# mapping in governance_core.pack_loader.COUNTRY_TO_JURISDICTION — keep them
# in sync; a future ADR may refactor to a single shared source.
#
# Inline SQL chosen over a Python UDF to avoid serializer overhead in the DLT
# stream and to keep medallion.py runnable on a vanilla DLT cluster without
# repo-import wiring.

def jurisdiction_from(country_col: str):
    """Return a Spark Column expression mapping a country string to a
    jurisdiction code. NULL country → NULL jurisdiction (surfaced as a
    high-severity gap downstream).
    """
    c = F.upper(F.trim(F.col(country_col)))
    return (
        F.when(c.isin("IN", "IND", "INDIA"), F.lit("IN"))
         .when(c.isin("GB", "UK", "GBR", "UNITED KINGDOM",
                      "ENGLAND", "SCOTLAND", "WALES", "NORTHERN IRELAND"),
               F.lit("GB"))
         .when(c.isin("US", "USA", "UNITED STATES", "AMERICA"), F.lit("US"))
         .when(c.isin(
             "AT", "AUSTRIA", "BE", "BELGIUM", "BG", "BULGARIA",
             "HR", "CROATIA", "CY", "CYPRUS", "CZ", "CZECH REPUBLIC", "CZECHIA",
             "DK", "DENMARK", "EE", "ESTONIA", "FI", "FINLAND",
             "FR", "FRANCE", "DE", "GERMANY", "GR", "GREECE",
             "HU", "HUNGARY", "IE", "IRELAND", "IT", "ITALY",
             "LV", "LATVIA", "LT", "LITHUANIA", "LU", "LUXEMBOURG",
             "MT", "MALTA", "NL", "NETHERLANDS", "PL", "POLAND",
             "PT", "PORTUGAL", "RO", "ROMANIA", "SK", "SLOVAKIA",
             "SI", "SLOVENIA", "ES", "SPAIN", "SE", "SWEDEN",
             "IS", "ICELAND", "LI", "LIECHTENSTEIN", "NO", "NORWAY",
         ), F.lit("EU"))
         .otherwise(F.lit(None).cast(StringType()))
    )


# M1 had a JURISDICTION_HARDCODED_IN fallback for tables whose source rows
# carried no country signal. M2 retired it — the 70/25/5 IN/GB/unmapped
# synthetic mix means every customer-level table now writes a `country`
# column, and all four silver materialisers call jurisdiction_from('country')
# uniformly. Left as a comment in case a future pack needs a similar fallback.

# ---------------------------------------------------------------------------
# Helper: Auto Loader stream for a single source table
# ---------------------------------------------------------------------------
def _auto_loader_stream(table_name: str):
    """Return a streaming DataFrame reading CSVs from the landing volume."""
    return (
        spark.readStream
            .format("cloudFiles")
            .option("cloudFiles.format", "csv")
            .option("cloudFiles.schemaLocation", f"{CHECKPOINT_ROOT}/{table_name}/_schema")
            .option("cloudFiles.inferColumnTypes", "false")  # keep as STRING for Bronze
            .option("header", "true")
            .option("multiLine", "true")
            .option("escape", '"')
            .option("rescuedDataColumn", "_rescued_data")
            .load(f"{LANDING_ROOT}/{table_name}/")
            .withColumn("_ingested_at", F.current_timestamp())
            .withColumn("_source_hash",
                        F.sha2(F.concat_ws("|", *[F.col(c).cast(StringType())
                                                  for c in spark.read
                                                              .option("header", "true")
                                                              .csv(f"{LANDING_ROOT}/{table_name}/")
                                                              .columns]), 256))
    )

# ===========================================================================
# BRONZE — raw CSV ingestion, all STRING
# ===========================================================================

@dlt.table(
    name="source_employees",
    comment="Bronze: raw employees CSV. Append-only; Auto Loader tracks new files.",
    table_properties={
        "quality": "bronze",
        "delta.autoOptimize.optimizeWrite": "true",
        "delta.autoOptimize.autoCompact": "true",
    },
)
def source_employees():
    return _auto_loader_stream("employees")


@dlt.table(
    name="source_customers",
    comment="Bronze: raw customers CSV.",
    table_properties={"quality": "bronze"},
)
def source_customers():
    return _auto_loader_stream("customers")


@dlt.table(
    name="source_patients",
    comment="Bronze: raw patients CSV.",
    table_properties={"quality": "bronze"},
)
def source_patients():
    return _auto_loader_stream("patients")


@dlt.table(
    name="source_transactions",
    comment="Bronze: raw transactions CSV.",
    table_properties={"quality": "bronze"},
)
def source_transactions():
    return _auto_loader_stream("transactions")


@dlt.table(
    name="source_users",
    comment="Bronze: raw users CSV.",
    table_properties={"quality": "bronze"},
)
def source_users():
    return _auto_loader_stream("users")


# ===========================================================================
# SILVER — typed, quality-checked
# ===========================================================================
# Each Silver table has `@dlt.expect*` rules that become data-quality metrics
# on the DLT monitoring dashboard. Violations beyond `expect_or_drop` cause
# rows to be dropped; `expect` alone logs violations without dropping.

@dlt.table(
    name="employees_tagged",
    comment="Silver: typed employees with PII columns. Companion pii_findings has the classification metadata.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
)
@dlt.expect_or_drop("valid_employee_id", "employee_id IS NOT NULL AND length(employee_id) > 0")
@dlt.expect("valid_email_format", "email RLIKE '^[^@]+@[^@]+\\\\.[^@]+$'")
@dlt.expect("has_aadhaar_or_passport", "aadhaar_number IS NOT NULL OR passport_number IS NOT NULL OR country != 'India'")
@dlt.expect("valid_hire_date", "hire_date <= current_date()")
def employees_tagged():
    return (
        dlt.read_stream("source_employees")
            .withColumn("date_of_birth", F.to_date("date_of_birth"))
            .withColumn("hire_date",     F.to_date("hire_date"))
            .withColumn("salary",        F.col("salary").cast("decimal(10,2)"))
            .withColumn("jurisdiction",  jurisdiction_from("country"))  # ADR-0001
            .drop("_rescued_data")
    )


@dlt.table(
    name="customers_tagged",
    comment="Silver: typed customers.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
)
@dlt.expect_or_drop("valid_customer_id", "customer_id IS NOT NULL")
@dlt.expect("valid_mobile_digits", "length(regexp_replace(mobile, '[^0-9]', '')) BETWEEN 10 AND 14")
@dlt.expect("valid_loyalty_tier", "loyalty_tier IN ('bronze','silver','gold','platinum')")
def customers_tagged():
    return (
        dlt.read_stream("source_customers")
            .withColumn("date_of_birth",      F.to_date("date_of_birth"))
            .withColumn("loyalty_points",     F.col("loyalty_points").cast("int"))
            .withColumn("registration_date",  F.to_timestamp("registration_date"))
            .withColumn("last_activity_date", F.to_timestamp("last_activity_date"))
            .withColumn("jurisdiction",       jurisdiction_from("country"))  # ADR-0001 M2
            .drop("_rescued_data")
    )


@dlt.table(
    name="patients_tagged",
    comment="Silver: typed patients with health PII.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
)
@dlt.expect_or_drop("valid_patient_id", "patient_id IS NOT NULL")
@dlt.expect("valid_mrn", "medical_record_number RLIKE '^MRN-[0-9]+$'")
@dlt.expect("valid_gender", "gender IN ('Male','Female','Other')")
def patients_tagged():
    return (
        dlt.read_stream("source_patients")
            .withColumn("date_of_birth",    F.to_date("date_of_birth"))
            .withColumn("last_visit_date",  F.to_date("last_visit_date"))
            .withColumn("next_appointment", F.to_date("next_appointment"))
            .withColumn("jurisdiction",     jurisdiction_from("country"))  # ADR-0001 M2
            .drop("_rescued_data")
    )


@dlt.table(
    name="transactions_tagged",
    comment="Silver: typed transactions.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
)
@dlt.expect_or_drop("valid_transaction_id", "transaction_id IS NOT NULL")
@dlt.expect("valid_amount", "amount IS NOT NULL AND amount > 0")
@dlt.expect("valid_status", "status IN ('SUCCESS','FAILED','PENDING','REVERSED')")
@dlt.expect("valid_txn_type", "transaction_type IN ('PURCHASE','TRANSFER','WITHDRAWAL','REFUND','DEPOSIT')")
def transactions_tagged():
    return (
        dlt.read_stream("source_transactions")
            .withColumn("transaction_date", F.to_timestamp("transaction_date"))
            .withColumn("amount",           F.col("amount").cast("decimal(12,2)"))
            .drop("_rescued_data")
    )


@dlt.table(
    name="users_tagged",
    comment="Silver: typed users.",
    table_properties={"quality": "silver", "delta.enableChangeDataFeed": "true"},
)
@dlt.expect_or_drop("valid_user_id", "user_id IS NOT NULL")
@dlt.expect("valid_account_status", "account_status IN ('active','suspended','deleted','pending')")
def users_tagged():
    return (
        dlt.read_stream("source_users")
            .withColumn("date_of_birth", F.to_date("date_of_birth"))
            .withColumn("mfa_enabled",   F.col("mfa_enabled").cast("boolean"))
            .withColumn("created_at",    F.to_timestamp("created_at"))
            .withColumn("last_login",    F.to_timestamp("last_login"))
            .withColumn("jurisdiction",  jurisdiction_from("country"))  # ADR-0001 M2
            .drop("_rescued_data")
    )


# ===========================================================================
# Companion: discovered_tables metadata
# ===========================================================================
# Pipeline-level metadata about what was scanned. Produced once per pipeline
# run. Classification findings (pii_findings) live in classification_dlt.py.

@dlt.table(
    name="discovered_tables",
    comment="Metadata about tables covered by the medallion pipeline. One row per (pipeline_run × table).",
    table_properties={"quality": "silver"},
)
def discovered_tables():
    """Produced as a static snapshot — pipeline writes one row per Silver table each run."""
    # Read each Silver table to capture row counts. DLT treats this as a
    # batch read since the Silver tables are streaming targets that have
    # already been materialized for this run.
    rows = []
    silver_tables = [
        "employees_tagged", "customers_tagged", "patients_tagged",
        "transactions_tagged", "users_tagged",
    ]
    scanned_at = datetime.now(timezone.utc)
    for tbl in silver_tables:
        df = dlt.read(tbl)
        count = df.count()
        cols = len(df.columns)
        rows.append((
            f"{CATALOG}_silver_{tbl}",
            CATALOG, "silver", tbl,
            cols, count,
            scanned_at,
        ))
    from pyspark.sql import Row
    return spark.createDataFrame(
        [Row(
            table_id=r[0], catalog_name=r[1], schema_name=r[2], table_name=r[3],
            column_count=r[4], row_count=r[5], scanned_at=r[6]
        ) for r in rows]
    )
