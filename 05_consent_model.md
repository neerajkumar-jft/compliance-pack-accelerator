# §5 · Consent event model

> ⚠️ **Pre-build planning document.** The schema is accurate — on free-trial the table is a Delta table (`compliance.consent_events_log`) rather than Lakebase-backed, but every column and constraint described here holds. Lakebase-specific sections (rules, triggers, sync cadence) don't apply to the free-trial deploy.

## 5.1 · Why this section is the most important schema in the POC

Every other decision in the platform either depends on or reasons against the consent event schema. If this schema is wrong, the whole platform is wrong. If it drifts silently between channels, three years later the platform cannot answer "did this person consent to this processing on this date?" — which is the single question the DPBI will ask.

The invariant this section enforces: **every consent event captured on any channel writes to one Lakebase endpoint with one schema**. Web, mobile app, call center IVR, partner API, branch kiosk — all share this endpoint. Channel variation is in the UX; schema is identical.

## 5.2 · The `data_principals` table

A minimal principal registry in Lakebase. Not a full customer master — just enough to key consent events against a persistent identity.

```sql
CREATE TABLE IF NOT EXISTS public.data_principals (
    principal_id                UUID         PRIMARY KEY,
    external_identifier         VARCHAR(128) UNIQUE NOT NULL,   -- links to source system ID (e.g., 'CUST00123')
    principal_type              VARCHAR(32)  NOT NULL,           -- 'customer'|'employee'|'patient'|'user'|'prospect'
    age_verification_status     VARCHAR(32)  NOT NULL,           -- 'verified_adult'|'verified_minor'|'unverified'
    age_verification_method     VARCHAR(64),                      -- e.g., 'dob_declared', 'id_document_verified'
    parental_consent_id         UUID,                             -- non-null if principal is a minor
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by                  VARCHAR(64)  NOT NULL,            -- service principal identity
    last_modified_at            TIMESTAMPTZ  NOT NULL DEFAULT now()
);

CREATE INDEX idx_principals_external_id ON public.data_principals(external_identifier);
```

## 5.3 · The `notice_versions` table

Every consent notice ever shown has a versioned record. A consent event references which notice version was shown. This is what lets us answer "what exactly did this person agree to?" three years later.

```sql
CREATE TABLE IF NOT EXISTS public.notice_versions (
    notice_version_id       UUID         PRIMARY KEY,
    notice_id               VARCHAR(64)  NOT NULL,               -- stable ID across versions (e.g., 'marketing_notice')
    version_number          INTEGER      NOT NULL,
    language                CHAR(5)      NOT NULL,               -- BCP 47 (e.g., 'en-IN', 'hi-IN', 'kn-IN')
    published_at            TIMESTAMPTZ  NOT NULL,
    retired_at              TIMESTAMPTZ,                          -- null = currently live
    content_text            TEXT         NOT NULL,               -- full notice text as shown
    content_hash            VARCHAR(64)  NOT NULL,               -- SHA-256 of content_text
    legal_basis             VARCHAR(64)  NOT NULL,               -- 'consent'|'contract'|'legal_obligation'|'vital_interests'|'public_interest'|'legitimate_interest'
    purposes_covered        TEXT[]       NOT NULL,               -- array of purpose enum values
    retention_policy_ref    VARCHAR(128),                         -- reference to retention catalog entry (not implemented in POC)
    approved_by             VARCHAR(128) NOT NULL,                -- GC identity who signed off
    approved_at             TIMESTAMPTZ  NOT NULL,
    UNIQUE (notice_id, version_number, language)
);

CREATE INDEX idx_notice_currently_live ON public.notice_versions(notice_id, language)
    WHERE retired_at IS NULL;
```

For this POC, seed the table with one notice — `marketing_notice` v1 in `en-IN` — on Day 8. Multi-language notice generation is out of scope (§1.4).

## 5.4 · The `consent_events` table (the load-bearing schema)

```sql
CREATE TABLE IF NOT EXISTS public.consent_events (
    event_id                    UUID         PRIMARY KEY,
    data_principal_id           UUID         NOT NULL REFERENCES public.data_principals(principal_id),
    event_timestamp             TIMESTAMPTZ  NOT NULL,
    event_type                  VARCHAR(32)  NOT NULL,           -- 'granted'|'withdrawn'|'modified'|'expired'|'renewed'
    notice_version_id           UUID         NOT NULL REFERENCES public.notice_versions(notice_version_id),
    notice_language             CHAR(5)      NOT NULL,
    channel                     VARCHAR(32)  NOT NULL,           -- see channel enum §5.5
    purpose                     VARCHAR(64)  NOT NULL,           -- see purpose enum §5.6
    purpose_grant_status        VARCHAR(16)  NOT NULL,           -- 'granted'|'declined'|'pending'
    ip_address                  INET,
    user_agent                  TEXT,
    device_fingerprint          VARCHAR(128),
    consent_capture_method      VARCHAR(64)  NOT NULL,           -- 'checkbox'|'toggle'|'ivr_digit'|'signed_document'|'implicit_continue'|'parent_email_verification'
    retention_clock_start       TIMESTAMPTZ  NOT NULL,
    retention_duration_days     INTEGER      NOT NULL,
    partner_source_id           UUID,                             -- non-null for partner_api channel
    withdrawal_reason           VARCHAR(256),                     -- free text, nullable, only for 'withdrawn' events
    superseded_by_event_id      UUID,                             -- for 'modified' events, points to replacement
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by                  VARCHAR(64)  NOT NULL             -- service principal identity
);

CREATE INDEX idx_events_principal_purpose_time
    ON public.consent_events(data_principal_id, purpose, event_timestamp DESC);

CREATE INDEX idx_events_timestamp
    ON public.consent_events(event_timestamp);

CREATE INDEX idx_events_type
    ON public.consent_events(event_type)
    WHERE event_type IN ('withdrawn', 'expired');
```

The composite index `(data_principal_id, purpose, event_timestamp DESC)` supports the primary operational query: "what is this principal's current consent for this purpose?" — resolved by selecting the most recent event for that principal+purpose combination.

## 5.5 · Channel enumeration

The POC supports four channels (we need at least three for channel-parity demonstration). The full enum the Lakebase column accepts:

| Value | Description |
|-------|-------------|
| `web` | Desktop or mobile web browser form |
| `mobile_app` | Native iOS or Android application |
| `call_center` | Agent-assisted capture via customer service |
| `partner_api` | Programmatic capture from an integrated partner system |

Values not in this list must fail the write with a `CHECK` constraint. In production, additional channels (`branch_kiosk`, `ivr`, `sms_keyword`, `signed_document_upload`) would be added; for the POC, four is enough.

## 5.6 · Purpose enumeration

Six processing purposes are supported in the POC. Each is a discrete consent decision:

| Value | Description | Legal basis typical |
|-------|-------------|---------------------|
| `core_service` | Delivery of the product or service the principal signed up for | `contract` or `legitimate_interest` — typically no consent toggle |
| `marketing_email` | Promotional emails about this organization's products | `consent` — explicit toggle required |
| `marketing_sms` | Promotional SMS about this organization's products | `consent` — explicit toggle required |
| `analytics` | Product usage analytics for service improvement | `consent` or `legitimate_interest` depending on granularity |
| `third_party_sharing` | Sharing personal data with named third parties for their marketing | `consent` — always explicit |
| `product_personalization` | Personalizing the product experience based on behavior | `consent` — explicit toggle required |

The `purposes_covered` array in `notice_versions` must be a subset of these values. Attempting to write a consent event with a purpose outside this enum must fail.

## 5.7 · Delta sync topology

Lakebase is the OLTP write path. Delta is the immutable audit log and the query layer for everything downstream. The sync is one-way (Lakebase → Delta) and near-real-time.

### 5.7.1 · Delta sync table DDL

```sql
-- Created in Unity Catalog, auto-maintained by Lakebase sync
CREATE TABLE IF NOT EXISTS compliance_pack.compliance.consent_events_log (
    -- All columns from public.consent_events preserved with matching types
    event_id                    STRING    NOT NULL,
    data_principal_id           STRING    NOT NULL,
    event_timestamp             TIMESTAMP NOT NULL,
    event_type                  STRING    NOT NULL,
    notice_version_id           STRING    NOT NULL,
    notice_language             STRING    NOT NULL,
    channel                     STRING    NOT NULL,
    purpose                     STRING    NOT NULL,
    purpose_grant_status        STRING    NOT NULL,
    ip_address                  STRING,
    user_agent                  STRING,
    device_fingerprint          STRING,
    consent_capture_method      STRING    NOT NULL,
    retention_clock_start       TIMESTAMP NOT NULL,
    retention_duration_days     INT       NOT NULL,
    partner_source_id           STRING,
    withdrawal_reason           STRING,
    superseded_by_event_id      STRING,
    created_at                  TIMESTAMP NOT NULL,
    created_by                  STRING    NOT NULL,
    -- Sync metadata added by Delta sync
    _sync_ingestion_time        TIMESTAMP NOT NULL,
    _lakebase_version           BIGINT    NOT NULL,
    _delta_version              BIGINT    NOT NULL
) USING DELTA
  PARTITIONED BY (DATE(event_timestamp))
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 2 years',
    'delta.deletedFileRetentionDuration' = 'interval 2 years'
  );
```

Two properties matter for DPDP defensibility:

- `delta.enableChangeDataFeed = true` — lets downstream suppression consume an event stream of new/changed events
- `delta.logRetentionDuration = 2 years` — time travel must work for at least the DPDP inspection lookback window

### 5.7.2 · Sync configuration

Configure the Lakebase → Delta sync via Databricks UI or API:

- Source: `compliance_pack_consent.public.consent_events`
- Destination: `compliance_pack.compliance.consent_events_log`
- Refresh interval: 60 seconds
- Mode: append-only (respects the immutability guarantee — consent events are never updated or deleted in Lakebase either)

Do **not** configure the sync for mutable semantics. The `consent_events` table is append-only by design; event_type='modified' writes a new row rather than updating an existing one.

## 5.8 · Withdrawal propagation

The 5-minute withdrawal propagation demonstration (Artifact 2 from §1.2) works as follows:

1. A `withdrawn` event is written to Lakebase `consent_events` at time T
2. The Lakebase→Delta sync picks it up within 60 seconds, delivers to Delta by T+60s
3. A Databricks Workflow triggered on the Change Data Feed (or on a 60-second schedule reading the feed) computes an updated `marketing_eligible_principals` Gold view
4. The Gold view is used by any downstream marketing process to filter audiences

The critical Gold view:

```sql
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
    WHERE purpose IN ('marketing_email', 'marketing_sms')
)
SELECT
    data_principal_id,
    purpose,
    event_timestamp AS consent_effective_from
FROM latest_consent
WHERE rn = 1
  AND event_type = 'granted'
  AND purpose_grant_status = 'granted';
```

This view shows **only** principals whose *latest* consent event for each marketing purpose is `granted`. A withdrawal automatically excludes them — no additional logic needed.

## 5.9 · Consent query API (the operational read path)

Downstream systems don't query `consent_events_log` directly. They call a single helper function:

```sql
CREATE OR REPLACE FUNCTION compliance_pack.compliance.has_active_consent(
    principal_external_id STRING,
    purpose_name STRING
) RETURNS BOOLEAN
RETURN (
    SELECT COALESCE(
        (SELECT event_type = 'granted' AND purpose_grant_status = 'granted'
         FROM compliance_pack.compliance.consent_events_log ce
         JOIN <principal lookup>
         WHERE <principal match>
           AND ce.purpose = purpose_name
         ORDER BY ce.event_timestamp DESC
         LIMIT 1),
        false
    )
);
```

(Exact principal lookup depends on the principal registry sync, which is simpler in this POC since we use `external_identifier` directly.)

Use this function whenever a downstream process needs to check consent. Do not reinvent the "latest consent wins" logic in application code.

## 5.10 · What NOT to do

- **Do not** allow UPDATEs to `consent_events`. The table is append-only. A `modified` event is a new row.
- **Do not** allow DELETEs to `consent_events`. Withdrawal is recorded as a `withdrawn` event, not a deletion.
- **Do not** create per-channel variations of the consent schema. One schema, all channels.
- **Do not** let `purpose_grant_status` be null. Every event has an explicit status even for `withdrawn` events (the status of the decision being recorded).
- **Do not** store the raw principal external identifier in `consent_events`. It links through `data_principals.principal_id`, a UUID. The external ID lives in one place.

Now proceed to `06_synthetic_data.md`.
