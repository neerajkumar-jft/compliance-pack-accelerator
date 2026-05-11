# Databricks notebook source
# MAGIC %md
# MAGIC # Retention Enforcement — DPDP §8(7)
# MAGIC
# MAGIC Deletes consent events whose retention clock has expired:
# MAGIC `event_timestamp + retention_duration_days < current_date()`.
# MAGIC
# MAGIC The `retention_duration_days` field is set at capture time (default 730
# MAGIC days = 2 years in our POC). When the clock expires, DPDP §8(7) requires
# MAGIC deletion "without undue delay" unless retention is legally required for
# MAGIC another purpose. This job enforces that.
# MAGIC
# MAGIC **Two-stage operation:**
# MAGIC   1. `--dry-run` mode (default): count what would be deleted, emit a
# MAGIC       report row into `compliance.retention_audit`, do NOT mutate.
# MAGIC   2. `--apply` mode: DELETE expired rows + VACUUM to reclaim storage
# MAGIC       and make the data unrecoverable.
# MAGIC
# MAGIC Intended to run weekly via a scheduled job. In dry-run mode it's safe
# MAGIC to run more frequently for observability.

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

dbutils.widgets.text("catalog", "compliance_pack", "Unity Catalog name")
dbutils.widgets.dropdown("mode", "dry-run", ["dry-run", "apply"], "Execution mode")
dbutils.widgets.text("vacuum_retention_hours", "168",
                     "VACUUM retention in hours (min 168 = 7 days)")

CATALOG = dbutils.widgets.get("catalog")
MODE = dbutils.widgets.get("mode")
VACUUM_HOURS = int(dbutils.widgets.get("vacuum_retention_hours"))

print(f"Catalog: {CATALOG}")
print(f"Mode:    {MODE}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 1 — Identify events past retention

# COMMAND ----------

from pyspark.sql import functions as F

expired_query = f"""
    SELECT
        event_id,
        data_principal_id,
        purpose,
        event_type,
        event_timestamp,
        retention_duration_days,
        DATE_ADD(CAST(event_timestamp AS DATE), retention_duration_days) AS retention_deadline,
        DATEDIFF(CURRENT_DATE(), DATE_ADD(CAST(event_timestamp AS DATE), retention_duration_days)) AS days_overdue
    FROM {CATALOG}.compliance.consent_events_log
    WHERE DATE_ADD(CAST(event_timestamp AS DATE), retention_duration_days) < CURRENT_DATE()
"""

expired = spark.sql(expired_query)
expired_count = expired.count()
print(f"Events past retention: {expired_count}")

if expired_count > 0:
    print("\nSample of expired events (up to 10):")
    expired.orderBy(F.col("days_overdue").desc()).show(10, truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 2 — Write a retention-audit record

# COMMAND ----------

# Create the audit table if it doesn't exist (tiny, low UC quota impact)
spark.sql(f"""
CREATE TABLE IF NOT EXISTS {CATALOG}.compliance.retention_audit (
    audit_id                STRING    NOT NULL,
    run_timestamp           TIMESTAMP NOT NULL,
    mode                    STRING    NOT NULL,
    catalog_name            STRING    NOT NULL,
    events_identified       BIGINT    NOT NULL,
    events_deleted          BIGINT    NOT NULL,
    oldest_event_date       DATE,
    newest_expired_date     DATE,
    vacuum_retention_hours  INT,
    run_by                  STRING    NOT NULL,
    notes                   STRING
) USING DELTA
""")

import uuid
from datetime import datetime, timezone

stats = expired.selectExpr(
    "MIN(CAST(event_timestamp AS DATE)) AS oldest",
    "MAX(retention_deadline) AS newest_expired"
).first()

audit_row = spark.createDataFrame([(
    str(uuid.uuid4()),
    datetime.now(timezone.utc),
    MODE,
    CATALOG,
    expired_count,
    0,  # updated below after actual DELETE if apply mode
    stats["oldest"] if stats else None,
    stats["newest_expired"] if stats else None,
    VACUUM_HOURS,
    spark.sql("SELECT current_user()").first()[0],
    "dry-run only — no deletions" if MODE == "dry-run" else "apply mode — DELETE + VACUUM executed below",
)], schema="audit_id string, run_timestamp timestamp, mode string, catalog_name string, "
        "events_identified bigint, events_deleted bigint, oldest_event_date date, "
        "newest_expired_date date, vacuum_retention_hours int, run_by string, notes string")

audit_row.write.mode("append").saveAsTable(f"{CATALOG}.compliance.retention_audit")
print(f"✓ retention_audit row written")

# COMMAND ----------

# MAGIC %md
# MAGIC ## 3 — Apply deletion (only when mode = 'apply')

# COMMAND ----------

if MODE == "dry-run":
    print("Dry-run mode — no data deleted.")
    print("To apply: re-run with widget mode = 'apply'.")
else:
    if expired_count == 0:
        print("No expired events — nothing to DELETE.")
    else:
        # DELETE first
        result = spark.sql(f"""
            DELETE FROM {CATALOG}.compliance.consent_events_log
            WHERE DATE_ADD(CAST(event_timestamp AS DATE), retention_duration_days) < CURRENT_DATE()
        """)
        print(f"✓ DELETE executed; {expired_count} rows removed from active table")

        # VACUUM to reclaim storage + make deleted rows unrecoverable
        # (Default retention is 7 days; override via widget. Less than 7 days
        # requires setting spark.databricks.delta.retentionDurationCheck.enabled=false.)
        spark.sql(f"""
            VACUUM {CATALOG}.compliance.consent_events_log
            RETAIN {VACUUM_HOURS} HOURS
        """)
        print(f"✓ VACUUM executed with {VACUUM_HOURS}-hour retention")

        # Update the audit row with actual deletion count
        spark.sql(f"""
            UPDATE {CATALOG}.compliance.retention_audit
            SET events_deleted = {expired_count},
                notes = CONCAT(notes, ' — DELETE+VACUUM completed at ',
                               CAST(current_timestamp() AS STRING))
            WHERE audit_id = '{audit_row.first()["audit_id"]}'
        """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## 4 — Summary

# COMMAND ----------

print(f"=== Retention enforcement summary ===")
print(f"  Mode: {MODE}")
print(f"  Events identified as expired: {expired_count}")
print(f"  Events deleted: {expired_count if MODE == 'apply' and expired_count > 0 else 0}")

print(f"\nRecent retention-audit runs:")
spark.sql(f"""
    SELECT run_timestamp, mode, events_identified, events_deleted, run_by
    FROM {CATALOG}.compliance.retention_audit
    ORDER BY run_timestamp DESC
    LIMIT 5
""").show(truncate=False)
