# §9 · Known pitfalls and mitigations

> ⚠️ **Pre-build planning document.** Sections on Lakebase tuning (9.4), Lakewatch (Private Preview, still unavailable), and Auto Loader tuning for the DLT medallion remain valid for a paid workspace but don't apply to the free-trial deploy path. **For deploying the POC today, follow [`docs/persona_deploy.md`](docs/persona_deploy.md).**

## 9.1 · THE architectural anti-pattern to avoid (from the accelerator)

The sinki.ai accelerator used a Python-native pattern that the Databricks SA correctly flagged. **Do not reintroduce this pattern** in the POC. The specific anti-patterns and their replacements:

### 9.1.0 · Databricks-native pattern checklist

Before any task, confirm you are using the Databricks-native pattern, not the accelerator's approach:

| Task | Databricks-native (correct) | Anti-pattern (refuse) |
|------|------------------------------|----------------------|
| Deploy workspace resources | Databricks Asset Bundle (`databricks bundle deploy`) | Manual UI clicks or notebook-run DDL |
| Medallion Bronze/Silver | Lakeflow Declarative Pipeline with `@dlt.table` | Standalone Auto Loader + Spark SQL notebook |
| PII classification at scale | Vectorized Spark SQL (§4.5.1) inside a DLT table | `df.limit(N).collect()` + Python loop |
| Source ingestion (real systems) | Lakehouse Federation / Lakeflow Connect / Delta Sharing | Python database connectors (pyodbc etc.) |
| Data quality rules | `@dlt.expect`, `@dlt.expect_or_drop` | Custom try/except in a batch job |
| OLTP writes (consent, DSR) | Lakebase + sync to Delta | Writing directly to Delta via batched streams |
| User-facing endpoints | Databricks App (apps/dsr_portal/) | Notebook-exposed endpoint |
| Stakeholder consumption | AI/BI dashboard + Genie | Screenshots of SQL editor output |
| Job orchestration | Databricks Workflows declared in `resources/jobs.yml` | Notebook-chained workflows |
| Tagging / governance | Unity Catalog column tags via `ALTER TABLE ... SET TAGS` | Out-of-band metadata tables |

If a task's implementation doesn't fit one of these patterns, stop and ask before proceeding — the Databricks-native path exists; you may just not have found it yet.

### 9.1.1 · Anti-pattern: Python database connectors

The accelerator's `src/connectors/` directory contains 33 files implementing connectors to JDBC databases, MongoDB, Kafka, SharePoint, Salesforce, Workday, ServiceNow, etc. The pattern is:

```python
# ANTI-PATTERN — do not do this
from src.connectors import create_connector
connector = create_connector(spark, source_type="postgres", config={...})
df = connector.read_table("customers")
df.write.saveAsTable(bronze_table)
```

**Replacement — Databricks-native ingestion patterns**:

- **For databases (Postgres, MySQL, SQL Server, Oracle)**: use **Lakehouse Federation**. Create a foreign catalog that queries the source in place, with Unity Catalog governance applied automatically. No Python connector required, no data movement if the workload supports it.
- **For SaaS (Salesforce, Workday, ServiceNow, Zendesk)**: use **Lakeflow Connect managed connectors**. Databricks operates the connector infrastructure; you configure the source credentials in Unity Catalog, not a Python config.
- **For files (S3, Azure Blob, GCS)**: use **Auto Loader** reading from a workspace volume. This POC uses exactly this pattern.
- **For other Databricks workspaces**: use **Delta Sharing**.
- **For Kafka/Kinesis streaming**: use **Structured Streaming readers** that are native to Spark.

Do not install `pyodbc`, `psycopg2`, `pymongo`, `salesforce-python`, or any Python client library for a database or SaaS system. If you find yourself wanting to, stop and reach for the Databricks-native alternative instead.

### 9.1.2 · Anti-pattern: collect-then-loop

The accelerator's Silver Discovery notebook (`02_Silver_Discovery.py` line 611) does:

```python
# ANTI-PATTERN — do not do this
sample_rows = df.limit(SAMPLE_SIZE).collect()
for col_name in columns:
    sample_values = [row[col_name] for row in sample_rows]
    detection = detect_pii(col_name, col_type, sample_values, patterns, MIN_CONFIDENCE)
```

This pulls 1,000 rows to the driver memory, then iterates columns in a Python loop that has no parallelism. It appears to work at small scale and falls over at real scale.

**Replacement — vectorized Spark SQL per the pattern in §4.5.1**:

```python
# CORRECT — stays on executors
def scan_column_regex(df, column_name, pattern):
    total = df.filter(F.col(column_name).isNotNull()).count()
    matched = df.filter(
        F.col(column_name).isNotNull() &
        (F.regexp_extract(F.col(column_name).cast("string"),
                          pattern.regex_pattern, 0) != "")
    ).count()
    return matched, matched / total if total else 0
```

### 9.1.3 · Anti-pattern: eager dependency install

The accelerator's `install_python_deps_for_connector(source_type)` call (line 210 of `02_Silver_Discovery.py`) installs Python packages at runtime, a side-effect in the notebook. This fails idempotency tests and makes reruns unpredictable.

**Replacement**: install all dependencies as cluster libraries (per §2.7) before any notebook runs. Never pip-install at runtime.

### 9.1.4 · Anti-pattern: `gc.collect()` as memory management

The accelerator calls `gc.collect()` every 10 tables (line 659). This is a symptom of the collect-then-loop anti-pattern — the driver is filling with collected rows. With vectorized execution, gc is not needed.

**Replacement**: don't accumulate on the driver; don't need gc.

## 9.2 · Databricks free trial workspace limits

The trial workspace imposes constraints that are easy to hit by accident:

### 9.2.1 · Compute budget

The trial workspace has a finite credit balance that must last 14 days. The consumers:

- Cluster uptime for interactive work (largest share)
- Workflow job runs
- `ai_classify` / `ai_extract` model-serving calls
- Lakebase compute

**Mitigations**:
- Single shared cluster with 30-min auto-termination (§2.9)
- No auto-scaling, no GPU
- Gate `ai_classify` behind a `LIMIT 100` on classification scan to prevent runaway cost
- Monitor daily credit consumption and alert at 60% burn through Day 7

### 9.2.2 · Feature availability

Private Preview features are unavailable in the trial. Confirmed unavailable:
- Lakewatch agentic SIEM (Private Preview as of March 2026)
- Some Agent Bricks beta features

Generally available and confirmed working:
- Unity Catalog with column tags
- Lakebase (GA since February 2026)
- Auto Loader
- Delta Live Tables
- `ai_classify` and `ai_extract`
- Databricks Apps (basic deployment)

If during build you discover a feature is unavailable, stop and raise before working around it.

### 9.2.3 · Networking

The trial workspace has no custom networking (no VPC peering, no private endpoints to external systems). This is why the POC uses synthetic data in a workspace volume, not a real source system with a database connection.

## 9.3 · Auto Loader quirks

### 9.3.1 · Schema inference with CSVs

Auto Loader's default schema inference infers types from the first N rows. For our CSVs this produces brittle behavior (date columns parsed as TIMESTAMP sometimes, STRING other times).

**Mitigation**: set `cloudFiles.inferColumnTypes = false` and keep Bronze as STRING everywhere (§3.4). Type conversion happens explicitly in Silver.

### 9.3.2 · Schema evolution on retroactive changes

If a new column is added to the source CSV, Auto Loader will not pick it up automatically unless schema evolution is enabled. Enable:

```python
.option("cloudFiles.schemaEvolutionMode", "addNewColumns")
```

### 9.3.3 · Checkpoint corruption

If Auto Loader's checkpoint directory is manually deleted or partially modified, the stream may start re-ingesting files or may silently skip new ones. **Never** delete anything from `/Volumes/compliance_pack/bronze/_checkpoints/` manually.

**Mitigation**: if you need to reset ingestion, truncate the target Bronze table AND delete the checkpoint directory together, in that order.

## 9.4 · Lakebase specifics

### 9.4.1 · Connection pooling defaults

Lakebase's default connection pool is small (~10 connections). For the POC's 1,000-event generator running single-threaded, this is fine. For load tests with concurrent writes, you would need to tune up.

**POC mitigation**: generate the 1,000 events sequentially. Do not parallelize the consent event generator.

### 9.4.2 · Sync latency variation

The Lakebase→Delta sync's 60-second refresh is the baseline; actual latency varies 30-120 seconds depending on workspace load. For the 5-minute withdrawal propagation SLA, this has ~4 minutes of margin and is safe. Do not reduce the sync interval below 60 seconds; the trial workspace may throttle.

### 9.4.3 · Auth integration

Use the Databricks-integrated auth for Lakebase connections, not a separate JDBC URL with stored password. The integration handles credential rotation automatically.

## 9.5 · Delta VACUUM retention interaction

### 9.5.1 · The 7-day default retention

Delta's default VACUUM retention is 7 days, meaning `VACUUM <table>` without a retention argument will not remove files newer than 7 days. For the DSR verification demo on Day 14, this means the time-travel proof breaks if we use defaults — the "data is gone" check fails because the old files are still physically present.

**POC mitigation**: set `spark.databricks.delta.retentionDurationCheck.enabled = false` immediately before `VACUUM ... RETAIN 0 HOURS` in the DSR execution path, and re-enable immediately after. This is documented in §7.6.

**Important caveat**: this override defeats Delta's safety check. Using it incorrectly can make time travel impossible for tables under active use. In the POC we only override it for the specific tables being erased as part of a DSR, and only in the DSR execution step.

### 9.5.2 · Time-travel queries against vacuumed tables

After aggressive VACUUM, `VERSION AS OF <old_version>` queries on the affected table may fail because the underlying files are gone. **Run the "before" time-travel query BEFORE running the VACUUM**, capture the count, then proceed with VACUUM. See the test INT-03 pattern in §8.3.

## 9.6 · Unity Catalog tag application gotchas

### 9.6.1 · The APPLY TAG privilege

`ALTER TABLE ... SET TAGS` requires the `APPLY TAG` privilege on the catalog. Without it, the call fails with a misleading generic error. Grant explicitly in §2.3.

### 9.6.2 · Tag values cannot contain certain characters

UC tag values are STRING but with restrictions: no colons, no backticks, no single quotes. The PII type and sensitivity values from our taxonomy are all safe, but if extending the vocabulary, sanitize.

### 9.6.3 · Tags don't appear instantly in information_schema

After `ALTER TABLE ... SET TAGS`, the `system.information_schema.column_tags` view may lag by up to 30 seconds. Tests that run immediately after tagging may fail transiently. Add a 30-second wait in test INT-06.

## 9.7 · Classification result quality issues

### 9.7.1 · False positive on low-entropy numeric columns

A column of 10-digit customer IDs may match the phone regex. A column of 16-digit transaction IDs may match the credit card regex. Both are false positives.

**Mitigation**: priority and column-hint matching tend to resolve these correctly (a column named `customer_id` doesn't match phone hints). But review the low-confidence findings bucket (0.5-0.65) for these patterns before the demo.

### 9.7.2 · Aadhaar false positive on 12-digit tax numbers

Some Indian tax-related IDs are 12 digits starting with 2-9, triggering the Aadhaar regex.

**Mitigation**: if extending to real data, add a Verhoeff checksum validator that runs after the regex match. For POC synthetic data, the generator produces only real Aadhaar format, so this is safe.

### 9.7.3 · `ai_classify` cost on long text fields

The `primary_diagnosis` column in `patients` is free text. Running `ai_classify` on all 1,500 patient rows may consume a meaningful chunk of the trial's classification credits.

**Mitigation**: classify only a sample of 100 rows on Day 4 (§4.5.2). If the sample classification is consistent, the column is classified at the column level, not per-row. Full-row scoring is a Phase 1 activity, not a POC activity.

## 9.8 · Common classification errors to watch for

| Symptom | Likely cause | Fix |
|---------|--------------|-----|
| Aadhaar column classified as phone | Column hint matching prioritized phone wrong | Tighten phone column hints to exclude generic `number` |
| Email column not classified | Column hint mismatch | Ensure `email_address` is in email hints |
| Transaction ID flagged as credit card | Regex matches 16-digit numerics | Verify column hints are primary; transaction IDs don't match CC hints |
| CVV column not flagged | Regex too liberal (matches too many short numerics) | Rely on column hints for CVV; skip regex |
| IP address column with nulls gets low confidence | Match rate calc includes nulls | Filter nulls before computing match rate |

## 9.9 · Workflow and job reliability

### 9.9.1 · Retry storms

A failing job configured with retries can consume the trial's credits quickly. Set `max_retries = 1` or `2` on every job, not default (typically 3 or unlimited).

### 9.9.2 · Stuck jobs

A job can get stuck in a "running" state if the underlying cluster died. Periodically check job status during long operations; cancel manually if a job is running > 30 minutes against what should be a 5-minute workload.

### 9.9.3 · Idempotency

Every job in the POC must be idempotent. If re-run against the same input, it must produce the same output without creating duplicate rows, stale entries, or partial state.

**Idempotency patterns used**:
- Classification job: `INSERT INTO pii_findings` with a `scan_job_id`; each run's findings are a distinct batch
- Register view: always shows latest `scan_job_id` only
- Consent event sync: append-only; same event_id twice produces one row (sync deduplicates)
- DSR execution: guarded by request_id; re-running a completed DSR is a no-op

## 9.10 · Secret handling

The POC has no real secrets (synthetic data, integrated Lakebase auth), but develop good habits:

- **Never** hardcode credentials in notebook cells
- **Never** print Lakebase connection strings in logs
- **Never** commit notebooks with widget values containing secrets

If you find yourself needing a secret for Phase 1 work, add it to the `dpdp-poc` secret scope (§2.6) and access via `dbutils.secrets.get()`.

## 9.11 · Data leakage in demos

The Day 14 demo will be shown to stakeholders. Make sure:

- The sample_match_redacted column is actually redacted (§4.7)
- The data_export.json for the DSR is only shown for the synthetic `customer_04217`, not any real-looking principal
- The Delta time-travel demonstration uses synthetic data only
- Screenshots or recordings of the demo don't leak any realistic-looking PII

Synthetic but realistic-looking Aadhaar numbers (e.g., `2345 6789 0123`) are technically fine to show, but caveat this in the demo narrative: "all data in this demo is synthetic, produced by a Faker generator."

Now proceed to `10_runbook.md`.
