-- Lakebase consent_events DDL - the load-bearing schema (§5.4)
-- Runs in the Lakebase instance `compliance-pack-consent`, database `compliance_pack_consent`, schema `public`

-- ============================================================================
-- data_principals - minimal principal registry
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.data_principals (
    principal_id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    external_identifier         VARCHAR(128) UNIQUE NOT NULL,
    principal_type              VARCHAR(32)  NOT NULL,
    age_verification_status     VARCHAR(32)  NOT NULL,
    age_verification_method     VARCHAR(64),
    parental_consent_id         UUID,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by                  VARCHAR(64)  NOT NULL,
    last_modified_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT chk_principal_type
        CHECK (principal_type IN ('customer','employee','patient','user','prospect')),
    CONSTRAINT chk_age_verification_status
        CHECK (age_verification_status IN ('verified_adult','verified_minor','unverified')),
    CONSTRAINT chk_minor_has_parental
        CHECK (age_verification_status != 'verified_minor' OR parental_consent_id IS NOT NULL)
);

CREATE INDEX IF NOT EXISTS idx_principals_external_id
    ON public.data_principals(external_identifier);
CREATE INDEX IF NOT EXISTS idx_principals_type
    ON public.data_principals(principal_type);

-- ============================================================================
-- consent_events - the core event log
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.consent_events (
    event_id                    UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    data_principal_id           UUID         NOT NULL REFERENCES public.data_principals(principal_id),
    event_timestamp             TIMESTAMPTZ  NOT NULL,
    event_type                  VARCHAR(32)  NOT NULL,
    notice_version_id           UUID         NOT NULL,
    notice_language             CHAR(5)      NOT NULL,
    channel                     VARCHAR(32)  NOT NULL,
    purpose                     VARCHAR(64)  NOT NULL,
    purpose_grant_status        VARCHAR(16)  NOT NULL,
    ip_address                  INET,
    user_agent                  TEXT,
    device_fingerprint          VARCHAR(128),
    consent_capture_method      VARCHAR(64)  NOT NULL,
    retention_clock_start       TIMESTAMPTZ  NOT NULL,
    retention_duration_days     INTEGER      NOT NULL,
    partner_source_id           UUID,
    withdrawal_reason           VARCHAR(256),
    superseded_by_event_id      UUID,
    created_at                  TIMESTAMPTZ  NOT NULL DEFAULT now(),
    created_by                  VARCHAR(64)  NOT NULL,

    CONSTRAINT chk_event_type
        CHECK (event_type IN ('granted','withdrawn','modified','expired','renewed')),
    CONSTRAINT chk_channel
        CHECK (channel IN ('web','mobile_app','call_center','partner_api')),
    CONSTRAINT chk_purpose
        CHECK (purpose IN (
            'core_service','marketing_email','marketing_sms',
            'analytics','third_party_sharing','product_personalization'
        )),
    CONSTRAINT chk_purpose_grant_status
        CHECK (purpose_grant_status IN ('granted','declined','pending')),
    CONSTRAINT chk_retention_duration_nonneg
        CHECK (retention_duration_days >= 0),
    CONSTRAINT chk_partner_channel
        CHECK ((channel = 'partner_api' AND partner_source_id IS NOT NULL)
               OR (channel != 'partner_api')),
    CONSTRAINT chk_withdrawal_has_reason_optional
        CHECK (event_type != 'withdrawn' OR withdrawal_reason IS NOT NULL
               OR TRUE)  -- reason encouraged but not enforced
);

-- The composite index for "latest consent for this principal+purpose"
CREATE INDEX IF NOT EXISTS idx_events_principal_purpose_time
    ON public.consent_events(data_principal_id, purpose, event_timestamp DESC);

CREATE INDEX IF NOT EXISTS idx_events_timestamp
    ON public.consent_events(event_timestamp);

CREATE INDEX IF NOT EXISTS idx_events_withdrawn
    ON public.consent_events(event_type, event_timestamp DESC)
    WHERE event_type IN ('withdrawn','expired');

CREATE INDEX IF NOT EXISTS idx_events_channel_partner
    ON public.consent_events(channel, partner_source_id)
    WHERE channel = 'partner_api';

-- ============================================================================
-- Enforce append-only: block UPDATE and DELETE on consent_events
-- ============================================================================
CREATE OR REPLACE RULE no_update_consent_events
    AS ON UPDATE TO public.consent_events
    DO INSTEAD NOTHING;

CREATE OR REPLACE RULE no_delete_consent_events
    AS ON DELETE TO public.consent_events
    DO INSTEAD NOTHING;

COMMENT ON TABLE public.consent_events IS
    'Append-only consent event log. UPDATEs and DELETEs blocked by rule. Modifications recorded as new event rows.';

-- ============================================================================
-- dsr_requests - intake queue for data subject rights requests
-- ============================================================================
CREATE TABLE IF NOT EXISTS public.dsr_requests (
    request_id              VARCHAR(32)  PRIMARY KEY,
    data_principal_id       UUID         NOT NULL REFERENCES public.data_principals(principal_id),
    request_type            VARCHAR(32)  NOT NULL,
    identifier_type         VARCHAR(32)  NOT NULL,
    raw_identifier          VARCHAR(256) NOT NULL,
    scope_purposes          TEXT[],
    requester_email         VARCHAR(256) NOT NULL,
    preferred_language      CHAR(5)      NOT NULL,
    submitted_at            TIMESTAMPTZ  NOT NULL,
    verification_token      VARCHAR(512) NOT NULL,
    verification_verified_at TIMESTAMPTZ,
    sla_deadline            TIMESTAMPTZ  NOT NULL,
    status                  VARCHAR(32)  NOT NULL,
    next_action             VARCHAR(64),
    discovery_completed_at  TIMESTAMPTZ,
    execution_completed_at  TIMESTAMPTZ,
    response_bundle_path    TEXT,
    rejection_reason        TEXT,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now(),

    CONSTRAINT chk_request_type
        CHECK (request_type IN ('access','correction','erasure','combined')),
    CONSTRAINT chk_status
        CHECK (status IN (
            'accepted','verified','discovering','executing',
            'completed','rejected','failed'
        ))
);

CREATE INDEX IF NOT EXISTS idx_dsr_principal
    ON public.dsr_requests(data_principal_id);
CREATE INDEX IF NOT EXISTS idx_dsr_status_submitted
    ON public.dsr_requests(status, submitted_at DESC);
