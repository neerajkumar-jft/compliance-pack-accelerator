-- Silver layer DDL for the Compliance Pack Accelerator
-- Bronze columns cast to correct types; companion pii_findings table holds PII metadata
-- Column names match generator output / Bronze layer exactly
--
-- Customer-level tables (employees_tagged, customers_tagged, users_tagged,
-- patients_tagged) carry a `jurisdiction` column that drives per-data-subject
-- rule routing — see ADR-0001 (docs/adr/0001-multi-jurisdiction-data-subject-routing.md).
-- The DLT silver materialiser (pipelines/medallion.py) derives the value from
-- whatever country signal is available in the source row, falling back to
-- 'IN' for the M1-era synthetic data which is uniformly Indian. M2 introduces
-- a 70/25/5 IN/GB/unmapped split in the synthetic generators.

-- ============================================================================
-- employees_tagged
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.employees_tagged (
    employee_id             STRING,
    first_name              STRING,
    last_name               STRING,
    email                   STRING,
    phone_number            STRING,
    date_of_birth           DATE,
    aadhaar_number          STRING,
    pan_number              STRING,
    passport_number         STRING,
    address                 STRING,
    city                    STRING,
    state                   STRING,
    country                 STRING,
    jurisdiction            STRING,        -- ADR-0001 routing key, derived from country
    postal_code             STRING,
    salary                  DECIMAL(10,2),
    bank_account            STRING,
    ifsc_code               STRING,
    department              STRING,
    designation             STRING,
    hire_date               DATE,
    manager_employee_id     STRING,
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- customers_tagged
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.customers_tagged (
    customer_id             STRING,
    full_name               STRING,
    email_address           STRING,
    mobile                  STRING,
    date_of_birth           DATE,
    aadhaar_number          STRING,
    pan_number              STRING,
    credit_card_number      STRING,
    cvv                     STRING,
    billing_address         STRING,
    city                    STRING,
    state                   STRING,
    country                 STRING,        -- ADR-0001 M2 source for jurisdiction derivation
    postal_code             STRING,
    loyalty_tier            STRING,
    loyalty_points          INT,
    preferred_language      STRING,
    registration_date       DATE,
    last_activity_date      DATE,
    account_holder_name     STRING,
    ip_address              STRING,
    jurisdiction            STRING,        -- ADR-0001 routing key (M1: hardcoded 'IN')
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- patients_tagged
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.patients_tagged (
    patient_id              STRING,
    medical_record_number   STRING,
    full_name               STRING,
    date_of_birth           DATE,
    gender                  STRING,
    aadhaar_number          STRING,
    nhs_number              STRING,        -- UK GDPR special-category PII (Art. 9)
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
    country                 STRING,        -- ADR-0001 M2 source for jurisdiction derivation
    last_visit_date         DATE,
    next_appointment        DATE,
    ward                    STRING,
    notes                   STRING,
    jurisdiction            STRING,        -- ADR-0001 routing key (derived from country in M2)
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- transactions_tagged
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.transactions_tagged (
    transaction_id          STRING,
    customer_id             STRING,
    transaction_date        TIMESTAMP,
    amount                  DECIMAL(12,2),
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
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- users_tagged
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.users_tagged (
    user_id                 STRING,
    username                STRING,
    email                   STRING,
    first_name              STRING,
    last_name               STRING,
    phone                   STRING,
    date_of_birth           DATE,
    ip_address              STRING,
    device_id               STRING,
    account_status          STRING,
    mfa_enabled             BOOLEAN,
    last_login              TIMESTAMP,
    created_at              TIMESTAMP,
    preferred_language      STRING,
    marketing_opt_in        BOOLEAN,
    terms_accepted_version  STRING,
    referral_source         STRING,
    country                 STRING,        -- ADR-0001 M2 source for jurisdiction derivation
    jurisdiction            STRING,        -- ADR-0001 routing key (derived from country in M2)
    _source_file            STRING      NOT NULL,
    _ingested_at            TIMESTAMP   NOT NULL,
    _source_hash            STRING      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- pii_findings - column-level PII discovery results
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.pii_findings (
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
    reviewed_at             TIMESTAMP
) USING DELTA;

-- ============================================================================
-- discovered_tables - table-level scan metadata
-- ============================================================================
CREATE TABLE IF NOT EXISTS dpdp_poc.silver.discovered_tables (
    table_id            STRING      NOT NULL,
    scan_job_id         STRING      NOT NULL,
    catalog_name        STRING      NOT NULL,
    schema_name         STRING      NOT NULL,
    table_name          STRING      NOT NULL,
    column_count        INT         NOT NULL,
    row_count           BIGINT      NOT NULL,
    pii_column_count    INT         NOT NULL,
    sensitivity_summary MAP<STRING, INT>,
    scanned_at          TIMESTAMP   NOT NULL
) USING DELTA;
