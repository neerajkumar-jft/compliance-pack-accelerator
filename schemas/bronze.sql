-- Bronze layer DDL for DPDP POC
-- All columns kept as STRING to tolerate CSV quirks; typing happens in Silver
-- Column names match the synthetic data generator output exactly

-- ============================================================================
-- employees
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_employees (
    employee_id         STRING,
    first_name          STRING,
    last_name           STRING,
    email               STRING,
    phone_number        STRING,
    date_of_birth       STRING,
    aadhaar_number      STRING,
    pan_number          STRING,
    passport_number     STRING,
    address             STRING,
    city                STRING,
    state               STRING,
    country             STRING,
    postal_code         STRING,
    salary              STRING,
    bank_account        STRING,
    ifsc_code           STRING,
    department          STRING,
    designation         STRING,
    hire_date           STRING,
    manager_employee_id STRING,
    _rescued_data       STRING,
    _source_file        STRING      NOT NULL,
    _ingested_at        TIMESTAMP   NOT NULL,
    _source_hash        STRING      NOT NULL,
    _ingested_at_date   DATE        GENERATED ALWAYS AS (CAST(_ingested_at AS DATE))
) USING DELTA
  PARTITIONED BY (_ingested_at_date)
  TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- ============================================================================
-- customers
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_customers (
    customer_id         STRING,
    full_name           STRING,
    email_address       STRING,
    mobile              STRING,
    date_of_birth       STRING,
    aadhaar_number      STRING,
    pan_number          STRING,
    credit_card_number  STRING,
    cvv                 STRING,
    billing_address     STRING,
    city                STRING,
    state               STRING,
    postal_code         STRING,
    loyalty_tier        STRING,
    loyalty_points      STRING,
    preferred_language  STRING,
    registration_date   STRING,
    last_activity_date  STRING,
    account_holder_name STRING,
    ip_address          STRING,
    _rescued_data       STRING,
    _source_file        STRING      NOT NULL,
    _ingested_at        TIMESTAMP   NOT NULL,
    _source_hash        STRING      NOT NULL,
    _ingested_at_date   DATE        GENERATED ALWAYS AS (CAST(_ingested_at AS DATE))
) USING DELTA
  PARTITIONED BY (_ingested_at_date)
  TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- ============================================================================
-- patients
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_patients (
    patient_id              STRING,
    medical_record_number   STRING,
    full_name               STRING,
    date_of_birth           STRING,
    gender                  STRING,
    aadhaar_number          STRING,
    phone                   STRING,
    email                   STRING,
    emergency_contact_name  STRING,
    emergency_contact_phone STRING,
    blood_group             STRING,
    primary_diagnosis       STRING,
    current_prescription    STRING,
    insurance_provider      STRING,
    insurance_id            STRING,
    allergies               STRING,
    attending_physician     STRING,
    last_visit_date         STRING,
    next_appointment        STRING,
    ward                    STRING,
    notes                   STRING,
    _rescued_data           STRING,
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL,
    _ingested_at_date       DATE        GENERATED ALWAYS AS (CAST(_ingested_at AS DATE))
) USING DELTA
  PARTITIONED BY (_ingested_at_date)
  TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- ============================================================================
-- transactions
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_transactions (
    transaction_id          STRING,
    customer_id             STRING,
    transaction_date        STRING,
    amount                  STRING,
    currency                STRING,
    transaction_type        STRING,
    status                  STRING,
    payment_method          STRING,
    card_last_four          STRING,
    merchant_name           STRING,
    merchant_category       STRING,
    ip_address              STRING,
    device_id               STRING,
    account_holder_name     STRING,
    location                STRING,
    _rescued_data           STRING,
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL,
    _ingested_at_date       DATE        GENERATED ALWAYS AS (CAST(_ingested_at AS DATE))
) USING DELTA
  PARTITIONED BY (_ingested_at_date)
  TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- ============================================================================
-- users
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.source_users (
    user_id             STRING,
    username            STRING,
    email               STRING,
    first_name          STRING,
    last_name           STRING,
    phone               STRING,
    date_of_birth       STRING,
    ip_address          STRING,
    device_id           STRING,
    account_status      STRING,
    mfa_enabled         STRING,
    last_login          STRING,
    created_at          STRING,
    preferred_language  STRING,
    marketing_opt_in    STRING,
    terms_accepted_version STRING,
    referral_source     STRING,
    _rescued_data       STRING,
    _source_file        STRING      NOT NULL,
    _ingested_at        TIMESTAMP   NOT NULL,
    _source_hash        STRING      NOT NULL,
    _ingested_at_date   DATE        GENERATED ALWAYS AS (CAST(_ingested_at AS DATE))
) USING DELTA
  PARTITIONED BY (_ingested_at_date)
  TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true');

-- ============================================================================
-- data_sources metadata
-- ============================================================================
-- This DDL is the spec; the runtime CREATE lives in
-- pipelines/phase1_bootstrap.py §2 (because nothing auto-executes
-- schemas/bronze.sql). Phase1 also seeds 10 canonical rows (§2.5) and
-- the classifier in pipelines/classification_dlt.py reads
-- silver_table_name from here to discover which silver objects to scan.
-- ingestion_pattern values: 'auto_loader' | 'direct_write' | 'federation_view'.
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.data_sources (
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
    silver_table_name   STRING    COMMENT 'Silver-layer table or view that mirrors this source. Classifier scans this column.',
    primary_key_column  STRING    COMMENT 'Primary-key column on silver_table_name. Required for the AI scan (pipelines/pii_ai_scan.py) per-row state join; rows with NULL are skipped at AI-scan time.'
) USING DELTA;
