# §3 · Data contracts

> ⚠️ **Pre-build planning document.** Source-table schemas are still accurate and match the active `bronze.*` / `silver.*` tables. Ingestion-via-notebook details are superseded — ingestion runs inside the DLT medallion (`pipelines/medallion.py`).

## 3.1 · The base source tables (Auto Loader landing zone)

The original five source tables are adapted from the sinki.ai accelerator's demo dataset. They land as CSVs in the `bronze.landing` UC volume and are ingested via Auto Loader inside the medallion DLT pipeline (`pipelines/medallion.py`). This is the file-arrival landing-zone pattern — pattern 1 of 3.

The five tables are designed to exercise the full PII taxonomy (§4) across realistic enterprise entity patterns. An enterprise in any sector will have analogues of these five; the schemas are deliberately generic.

| Table | Domain | Row count target | PII categories exercised |
|-------|--------|-------------------|--------------------------|
| `employees` | HR master data | 2,000 | Government IDs, contact, financial, DOB |
| `customers` | CRM master data | 5,000 | Contact, financial, IP/device |
| `patients` | Health records | 1,500 | Health, insurance, contact, sensitive |
| `transactions` | Financial ledger | 10,000 | Financial, IP, minimal identity |
| `users` | Application user base | 3,000 | Contact, device, credentials, DOB |

Total ~21,500 rows across 5 tables. This fits comfortably in the trial workspace and makes PII classification tractable without hitting credit limits.

## 3.1b · Lakeflow Connect simulation (Salesforce — pattern 2 of 3)

Day 3 added a simulated Lakeflow Connect ingestion: synthetic Salesforce-shaped data (Lead, Contact, Account) is written **directly** into bronze + silver Delta tables by `scripts/seed_salesforce_data.py` — no Auto Loader. The visibly-different code path (CREATE OR REPLACE TABLE + INSERT instead of `@dlt.table` + `cloudFiles`) is the pattern signal: in production this is what a managed connector delivers.

| Table | Row count | India PII present |
|-------|-----------|--------------------|
| `bronze.sf_leads` / `silver.sf_leads_tagged` | 100 | Aadhaar, PAN, email, phone, mobile |
| `bronze.sf_contacts` / `silver.sf_contacts_tagged` | 60 | + IFSC bank code |
| `bronze.sf_accounts` / `silver.sf_accounts_tagged` | 30 | Company PAN, GST number, primary phone |

Generator: `generate_salesforce_data.py` (seed=43).

## 3.1c · Lakehouse Federation simulation (pattern 3 of 3)

Day 4 added a simulated foreign catalog: a `compliance_pack.federation_mock` schema holds Postgres-shape marketing tables, and `silver.federation_*_tagged` are **VIEWS** that select from them. The view-not-table shape signals "query flies, data stays put" — in production the view's source would be a UC foreign catalog.

| Table / view | Row count | Notes |
|--------------|-----------|-------|
| `federation_mock.lead_scoring` | 200 | lead_id cross-references `sf_leads.lead_id` (federation joins back to native) |
| `federation_mock.campaign_response` | 100 | Marketing attribution events |
| `silver.federation_lead_scoring_tagged` | (view) | Passthrough; classifier reads via `spark.table()` |
| `silver.federation_campaign_response_tagged` | (view) | Passthrough |

Generator: `generate_federation_data.py` (seed=44). All three patterns deliver into the same governance layer (classifier → `pii_findings` → `personal_data_register`).

## 3.2 · Source file format

The synthetic data generator (§6) writes CSV files to the landing zone. The exact file format contract:

- **Encoding**: UTF-8 without BOM
- **Line terminator**: `\n` (LF), not `\r\n`
- **Quoting**: RFC 4180 — fields containing commas, newlines, or double-quotes are enclosed in double-quotes; embedded double-quotes are escaped by doubling
- **Header row**: always present, column names matching the DDL exactly
- **Null representation**: empty string (not the literal word "NULL"); a value of `,,` represents a null
- **Date format**: ISO 8601 `YYYY-MM-DD` (e.g., `1985-03-15`)
- **Datetime format**: ISO 8601 `YYYY-MM-DDTHH:MM:SS` (e.g., `2024-01-15T10:30:00`); all times in IST, no timezone suffix
- **Decimal format**: period as separator (e.g., `85000.00`); no thousand separators
- **Compression**: gzip (`.csv.gz`)
- **Filename pattern**: `<table_name>_YYYYMMDD.csv.gz` (e.g., `employees_20260417.csv.gz`)

The synthetic generator must produce files matching this contract exactly. Any deviation is a bug, not an adaptation.

## 3.3 · Source schemas (Bronze tables mirror these 1:1)

### 3.3.1 · `employees`

```
employee_id         STRING    NOT NULL   -- e.g., 'EMP000001'
first_name          STRING    NOT NULL
last_name           STRING    NOT NULL
email               STRING    NOT NULL
phone_number        STRING    NOT NULL   -- e.g., '+91-9876543210'
date_of_birth       DATE      NOT NULL
aadhaar_number      STRING               -- nullable for non-Indian staff
pan_number          STRING               -- nullable for non-Indian staff
passport_number     STRING               -- nullable
address             STRING    NOT NULL
city                STRING    NOT NULL
state               STRING    NOT NULL
country             STRING    NOT NULL
postal_code         STRING    NOT NULL
salary              DECIMAL(10,2) NOT NULL
bank_account        STRING    NOT NULL
ifsc_code           STRING               -- nullable for non-Indian accounts
department          STRING    NOT NULL
designation         STRING    NOT NULL
hire_date           DATE      NOT NULL
manager_employee_id STRING               -- self-reference, nullable for C-suite
```

### 3.3.2 · `customers`

```
customer_id         STRING    NOT NULL   -- e.g., 'CUST00001'
full_name           STRING    NOT NULL
email_address       STRING    NOT NULL
mobile              STRING    NOT NULL
date_of_birth       DATE
credit_card_number  STRING               -- tokenized in reality; synthetic valid-format here
card_expiry         STRING               -- 'MM/YY'
cvv                 STRING               -- 3 or 4 digits
billing_address     STRING    NOT NULL
shipping_address    STRING    NOT NULL
city                STRING    NOT NULL
state               STRING    NOT NULL
country             STRING    NOT NULL
postal_code         STRING    NOT NULL
ip_address          STRING
device_id           STRING
loyalty_tier        STRING    NOT NULL   -- 'bronze'|'silver'|'gold'|'platinum'
loyalty_points      INT       NOT NULL
registration_date   TIMESTAMP NOT NULL
last_activity_date  TIMESTAMP
```

### 3.3.3 · `patients`

```
patient_id          STRING    NOT NULL   -- e.g., 'PAT00001'
patient_name        STRING    NOT NULL
dob                 DATE      NOT NULL
gender              STRING    NOT NULL
blood_group         STRING
contact_phone       STRING    NOT NULL
emergency_contact   STRING
email               STRING
address             STRING    NOT NULL
city                STRING    NOT NULL
state               STRING    NOT NULL
postal_code         STRING    NOT NULL
insurance_id        STRING
insurance_provider  STRING               -- e.g., 'Star Health', 'HDFC Ergo'
policy_type         STRING               -- 'individual'|'family'|'corporate'
medical_record_number STRING NOT NULL
primary_diagnosis   STRING               -- free text, triggers ai_classify
prescription        STRING               -- free text
allergies           STRING               -- free text
last_visit          DATE
next_appointment    DATE
```

### 3.3.4 · `transactions`

```
transaction_id          STRING    NOT NULL   -- e.g., 'TXN00000001'
customer_id             STRING    NOT NULL   -- FK to customers.customer_id
account_number          STRING    NOT NULL
account_holder_name     STRING    NOT NULL
transaction_date        TIMESTAMP NOT NULL
amount                  DECIMAL(12,2) NOT NULL
currency                STRING    NOT NULL   -- ISO 4217 ('INR', 'USD', ...)
transaction_type        STRING    NOT NULL   -- 'PURCHASE'|'TRANSFER'|'WITHDRAWAL'|'REFUND'|'DEPOSIT'
merchant_name           STRING
merchant_category_code  STRING
card_last_four          STRING
ip_address              STRING
location_city           STRING
location_country        STRING
status                  STRING    NOT NULL   -- 'SUCCESS'|'FAILED'|'PENDING'|'REVERSED'
```

### 3.3.5 · `users`

```
user_id             STRING    NOT NULL   -- e.g., 'USR00001'
username            STRING    NOT NULL
password_hash       STRING    NOT NULL   -- bcrypt; not strictly PII but sensitive
email               STRING    NOT NULL
phone               STRING    NOT NULL
first_name          STRING    NOT NULL
last_name           STRING    NOT NULL
date_of_birth       DATE      NOT NULL
gender              STRING
profile_picture_url STRING
last_login_ip       STRING
device_fingerprint  STRING
mfa_enabled         BOOLEAN   NOT NULL
mfa_method          STRING               -- 'sms'|'totp'|'email'|null
created_at          TIMESTAMP NOT NULL
last_login          TIMESTAMP
account_status      STRING    NOT NULL   -- 'active'|'suspended'|'deleted'|'pending'
```

## 3.4 · Bronze layer DDL

Bronze tables mirror the source schema with three metadata columns appended. They are populated by Auto Loader streaming from the landing zone volume.

For each source table, the Bronze DDL follows this pattern (full DDL in `schemas/bronze.sql`):

```sql
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_employees (
    -- All source columns as above, with all types STRING to tolerate CSV quirks
    employee_id STRING,
    first_name STRING,
    last_name STRING,
    -- ... etc.
    -- Metadata columns
    _source_file     STRING   NOT NULL,   -- populated by Auto Loader
    _ingested_at     TIMESTAMP NOT NULL,  -- populated by Auto Loader
    _source_hash     STRING   NOT NULL    -- SHA-256 of source row, for idempotency
) USING DELTA
  PARTITIONED BY (_ingested_at_date GENERATED ALWAYS AS CAST(_ingested_at AS DATE));
```

**Why all columns are STRING in Bronze**: CSV files don't carry types; date/number fields that fail parsing would either be silently nulled or fail the entire ingest. Keeping Bronze schemas as STRING and casting in Silver means malformed rows are preserved and visible, not lost. Silver's transformation layer is where data quality is enforced.

**The `_source_hash` column** is computed as `sha2(concat_ws('|', <all source columns>), 256)`. It lets us detect exact-duplicate rows from re-ingestion and maintain idempotency.

### Auto Loader configuration — inside the DLT pipeline

Bronze ingestion runs inside the Lakeflow Declarative Pipeline (`pipelines/medallion.py`), not as a standalone Auto Loader notebook. The DLT `@dlt.table` declaration wraps Auto Loader with lineage, monitoring, and data quality built in:

```python
# pipelines/medallion.py — actual executable, not a spec snippet
@dlt.table(
    name="source_employees",
    comment="Bronze: raw employees CSV. Append-only; Auto Loader tracks new files.",
    table_properties={"quality": "bronze"},
)
def source_employees():
    return _auto_loader_stream("employees")

def _auto_loader_stream(table_name: str):
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
    )
```

The `trigger(availableNow=True)` pattern is not used here — DLT handles trigger semantics through the pipeline's `continuous: false` configuration in `resources/pipelines.yml`.

**Why DLT instead of a standalone notebook:**
- Lineage between Bronze and Silver is visible in Unity Catalog automatically (no manual tagging)
- Data quality `@dlt.expect` rules produce metrics on the DLT monitoring dashboard for free
- Idempotent rerun; `full_refresh=false` reprocesses only new landing-zone files
- The pipeline graph is a natural demo artifact on Day 14 — stakeholders can see the flow

The standalone Auto Loader pattern remains valid for reference; it is what the DLT decorator wraps. But Claude Code should not write a standalone Auto Loader notebook. Use the DLT pipeline.

## 3.5 · Silver layer DDL

Silver tables are the Bronze schema with:

1. All columns cast to their correct types (dates parsed, decimals typed)
2. The metadata columns preserved
3. PII tag metadata columns appended per tagged column

Instead of a separate PII tag column per source column (which would double the schema width), we use a **companion `pii_findings` table** keyed by (catalog, schema, table, column). This matches the accelerator's pattern and keeps the Silver tables narrow.

### 3.5.1 · The tagged Silver tables

Each Silver table has the same columns as Bronze (typed correctly) plus these three metadata columns preserved from Bronze:

```
_source_file     STRING
_ingested_at     TIMESTAMP
_source_hash     STRING
```

DDL pattern in `schemas/silver.sql`.

### 3.5.2 · `pii_findings` — the column-level discovery results

This table is adapted from the accelerator's `silver.pii_findings` (lines 356-372 of `02_Silver_Discovery.py`) with minor improvements. It is the source of truth for every PII decision the platform has made.

```sql
CREATE TABLE IF NOT EXISTS compliance_pack.silver.pii_findings (
    finding_id              STRING    NOT NULL,     -- UUID per finding
    scan_job_id             STRING    NOT NULL,     -- UUID per classification run
    catalog_name            STRING    NOT NULL,     -- always 'compliance_pack'
    schema_name             STRING    NOT NULL,     -- always 'silver' for POC
    table_name              STRING    NOT NULL,
    column_name             STRING    NOT NULL,
    column_data_type        STRING    NOT NULL,
    pii_category            STRING    NOT NULL,     -- one of the 9 categories from §4
    pii_type                STRING    NOT NULL,     -- specific type (e.g., 'aadhaar')
    sensitivity_tier        STRING    NOT NULL,     -- 'critical'|'high'|'medium'|'low'
    confidence              DOUBLE    NOT NULL,     -- 0.0 to 1.0
    classifier_source       STRING    NOT NULL,     -- 'column_hint'|'regex'|'hybrid'|'ai_classify'|'manual'
    match_rate              DOUBLE,                 -- what fraction of sampled values matched regex, null for column-hint-only
    regulations             ARRAY<STRING> NOT NULL, -- always includes 'DPDP'; may include 'GDPR', 'HIPAA', etc.
    sample_match_redacted   STRING,                 -- one redacted sample, for audit (e.g., 'XXXX-XXXX-XX23')
    human_reviewed          BOOLEAN   NOT NULL,     -- default false; true after CCO review
    review_status           STRING,                 -- null|'approved'|'rejected'|'reclassified'
    review_notes            STRING,
    discovered_at           TIMESTAMP NOT NULL,
    reviewed_at             TIMESTAMP
) USING DELTA;
```

Note the `sample_match_redacted` column — it stores an illustrative but redacted example, not the raw sensitive value. The accelerator's version stored raw matches; for DPDP we must redact (e.g., show `XXXX-XXXX-XX23` not `2345 6789 0123`).

### 3.5.3 · `discovered_tables` — table-level scan metadata

```sql
CREATE TABLE IF NOT EXISTS compliance_pack.silver.discovered_tables (
    table_id            STRING    NOT NULL,
    scan_job_id         STRING    NOT NULL,
    catalog_name        STRING    NOT NULL,
    schema_name         STRING    NOT NULL,
    table_name          STRING    NOT NULL,
    column_count        INT       NOT NULL,
    row_count           BIGINT    NOT NULL,
    pii_column_count    INT       NOT NULL,         -- count of columns flagged in pii_findings
    sensitivity_summary MAP<STRING, INT>,           -- e.g., {'critical': 3, 'high': 2, ...}
    scanned_at          TIMESTAMP NOT NULL
) USING DELTA;
```

## 3.6 · Personal data register view

The register — artifact 1 from §1.2 — is a view that joins `pii_findings` with the Unity Catalog system tables to produce the stakeholder-facing output.

```sql
CREATE OR REPLACE VIEW compliance_pack.compliance.personal_data_register AS
SELECT
    f.catalog_name || '.' || f.schema_name || '.' || f.table_name AS fully_qualified_table,
    f.table_name                    AS source_table,
    f.column_name                   AS source_column,
    f.column_data_type              AS data_type,
    f.pii_category                  AS pii_category,
    f.pii_type                      AS pii_type,
    f.sensitivity_tier              AS sensitivity_tier,
    f.classifier_source             AS classifier_source,
    f.confidence                    AS classification_confidence,
    f.regulations                   AS applicable_regulations,
    COALESCE(t.comment, '(not assigned)') AS data_owner,
    dt.row_count                    AS row_count,
    f.human_reviewed                AS human_reviewed,
    f.review_status                 AS review_status,
    f.discovered_at                 AS last_scanned_at
FROM compliance_pack.silver.pii_findings f
LEFT JOIN compliance_pack.silver.discovered_tables dt
    ON dt.scan_job_id = f.scan_job_id
    AND dt.table_name = f.table_name
LEFT JOIN system.information_schema.tables t
    ON t.table_catalog = f.catalog_name
    AND t.table_schema = f.schema_name
    AND t.table_name = f.table_name
WHERE f.scan_job_id IN (
    -- latest scan job only
    SELECT scan_job_id
    FROM compliance_pack.silver.pii_findings
    WHERE discovered_at = (SELECT MAX(discovered_at) FROM compliance_pack.silver.pii_findings)
);
```

The view shows only the latest scan; if a column disappears from the source system, it falls out of the register automatically on the next scan.

## 3.7 · Unity Catalog tags applied to columns

In addition to the `pii_findings` table as the relational source of truth, the classification job applies Unity Catalog column tags so that lineage tools, access-policy engines, and downstream queries can reason about PII without joining to `pii_findings`.

The tags applied per PII column:

- `pii_type` — specific type (e.g., `aadhaar`, `email`, `credit_card`)
- `pii_category` — one of the 9 categories from §4
- `sensitivity` — `critical` | `high` | `medium` | `low`
- `classifier_source` — `column_hint` | `regex` | `hybrid` | `ai_classify` | `manual`
- `dpdp_applicable` — always `true` for this POC

Applied via:

```sql
ALTER TABLE compliance_pack.silver.employees_tagged
ALTER COLUMN aadhaar_number
SET TAGS (
    'pii_type' = 'aadhaar',
    'pii_category' = 'direct_identifier_government',
    'sensitivity' = 'critical',
    'classifier_source' = 'hybrid',
    'dpdp_applicable' = 'true'
);
```

This is the accelerator's pattern (from `02_Silver_Discovery.py` line 721) preserved as-is. It is architecturally correct.

## 3.8 · What NOT to do

- **Do not** use pandas `to_sql` or any Python-side write path to Delta. All writes go through Spark DataFrames.
- **Do not** collect sample data to the driver with `.collect()` for classification. Classification runs vectorized via pandas UDFs or Spark SQL `regexp_extract_all` — see §4.
- **Do not** create per-PII-type columns in the Silver tables. The companion `pii_findings` table is the source of truth; tags are the lineage layer.
- **Do not** skip the `sample_match_redacted` redaction step. The accelerator stored raw matches; for DPDP we must redact.

Now proceed to `04_pii_taxonomy.md`.
