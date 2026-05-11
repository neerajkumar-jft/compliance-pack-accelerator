-- Delta sync destination for Lakebase consent_events (§5.7.1)
-- This table is auto-maintained by the Lakebase-to-Delta sync configured per §5.7.2
-- Do not write to this table directly; all writes come from the sync

CREATE TABLE IF NOT EXISTS compliance_pack.compliance.consent_events_log (
    event_id                    STRING      NOT NULL,
    data_principal_id           STRING      NOT NULL,
    event_timestamp             TIMESTAMP   NOT NULL,
    event_type                  STRING      NOT NULL,
    notice_version_id           STRING      NOT NULL,
    notice_language             STRING      NOT NULL,
    channel                     STRING      NOT NULL,
    purpose                     STRING      NOT NULL,
    purpose_grant_status        STRING      NOT NULL,
    ip_address                  STRING,
    user_agent                  STRING,
    device_fingerprint          STRING,
    consent_capture_method      STRING      NOT NULL,
    retention_clock_start       TIMESTAMP   NOT NULL,
    retention_duration_days     INT         NOT NULL,
    partner_source_id           STRING,
    withdrawal_reason           STRING,
    superseded_by_event_id      STRING,
    created_at                  TIMESTAMP   NOT NULL,
    created_by                  STRING      NOT NULL,
    -- Sync metadata
    _sync_ingestion_time        TIMESTAMP   NOT NULL,
    _lakebase_version           BIGINT      NOT NULL,
    _delta_version              BIGINT      NOT NULL,
    event_date                  DATE        GENERATED ALWAYS AS (CAST(event_timestamp AS DATE))
) USING DELTA
  PARTITIONED BY (event_date)
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days',
    'delta.deletedFileRetentionDuration' = 'interval 730 days',
    'delta.autoOptimize.optimizeWrite' = 'true',
    'delta.autoOptimize.autoCompact' = 'true'
  );

-- Sync destination for dsr_requests (smaller table, doesn't need partitioning)
CREATE TABLE IF NOT EXISTS compliance_pack.compliance.dsr_requests (
    request_id              STRING      NOT NULL,
    data_principal_id       STRING      NOT NULL,
    request_type            STRING      NOT NULL,
    identifier_type         STRING      NOT NULL,
    raw_identifier          STRING      NOT NULL,
    scope_purposes          ARRAY<STRING>,
    requester_email         STRING      NOT NULL,
    preferred_language      STRING      NOT NULL,
    submitted_at            TIMESTAMP   NOT NULL,
    verification_token      STRING      NOT NULL,
    verification_verified_at TIMESTAMP,
    sla_deadline            TIMESTAMP   NOT NULL,
    status                  STRING      NOT NULL,
    next_action             STRING,
    discovery_completed_at  TIMESTAMP,
    execution_completed_at  TIMESTAMP,
    response_bundle_path    STRING,
    rejection_reason        STRING,
    created_at              TIMESTAMP   NOT NULL,
    _sync_ingestion_time    TIMESTAMP   NOT NULL,
    _lakebase_version       BIGINT      NOT NULL,
    _delta_version          BIGINT      NOT NULL
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );

-- ============================================================================
-- The marketing_eligible_principals Gold view from §5.8
-- Consumed by downstream marketing processes to filter audiences
-- ============================================================================
CREATE OR REPLACE VIEW compliance_pack.gold.marketing_eligible_principals AS
WITH latest_consent AS (
    SELECT
        data_principal_id,
        purpose,
        event_type,
        purpose_grant_status,
        event_timestamp,
        ROW_NUMBER() OVER (
            PARTITION BY data_principal_id, purpose
            ORDER BY event_timestamp DESC
        ) AS rn
    FROM compliance_pack.compliance.consent_events_log
    WHERE purpose IN ('marketing_email','marketing_sms')
)
SELECT
    data_principal_id,
    purpose,
    event_timestamp AS consent_effective_from
FROM latest_consent
WHERE rn = 1
  AND event_type = 'granted'
  AND purpose_grant_status = 'granted';

-- ============================================================================
-- has_active_consent helper function from §5.9
-- Downstream systems call this instead of reinventing the "latest wins" logic
-- ============================================================================
CREATE OR REPLACE FUNCTION compliance_pack.compliance.has_active_consent(
    principal_external_id STRING,
    purpose_name STRING
) RETURNS BOOLEAN
RETURN (
    SELECT COALESCE(
        (
            SELECT ce.event_type = 'granted'
               AND ce.purpose_grant_status = 'granted'
            FROM compliance_pack.compliance.consent_events_log ce
            JOIN compliance_pack.silver.customers_tagged c
                ON c.customer_id = principal_external_id
            WHERE ce.purpose = purpose_name
            ORDER BY ce.event_timestamp DESC
            LIMIT 1
        ),
        false
    )
);
