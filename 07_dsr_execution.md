# §7 · DSR execution

> ⚠️ **Pre-build planning document.** The DSR-portal Databricks App and `process_dsr_request` job were replaced on the free-trial path by standalone scripts: `scripts/dsr_discovery.py` (access/discovery) and `scripts/dsr_erasure.py` (DELETE+VACUUM). The DPDP §11/§12 flow and audit-trail shape remain accurate. **For running a DSR today, see [`docs/persona_deploy.md`](docs/persona_deploy.md).**

## 7.1 · Scope of the DSR module in this POC

The full Module 03 (rights hub) from the proposal is out of scope. What we build for the POC is a **minimum viable DSR stub** that proves the end-to-end path: intake → discovery → structured execution → response bundle. This is what Artifact 3 demonstrates.

Specifically in scope:
- Request intake via a simple REST API (Databricks Apps or a notebook-exposed endpoint)
- Unified discovery query against the 5 synthetic tables using Unity Catalog lineage
- Delta DELETE + VACUUM for structured erasure
- Retention-aware execution (some data erased, some scheduled as residual)
- Response bundle: data export, erasure certificate, retention schedule, audit trail
- Verification via Delta time travel (data was present before, absent after)

Specifically out of scope:
- Zone 1 document-level execution (SharePoint/email masking and de-indexing)
- Production identity verification (IDV provider integration)
- Cross-system propagation to vendors and processors
- Legal hold registry lookups (legal hold is always "none" in the POC)
- The full split-execution matrix from the proposal's Module 03 v2 design

## 7.2 · The DSR intake API

A single HTTP POST endpoint accepts DSR requests. Implemented as a Databricks App or a FastAPI served from a notebook, depending on what's simpler in the trial workspace.

### 7.2.1 · Request schema

```
POST /dsr/request
Content-Type: application/json

{
    "principal_identifier": "customer_04217",
    "identifier_type": "external_id",
    "request_type": "combined",
    "verification_token": "<token from stub IDV, see §7.3>",
    "scope_purposes": null,
    "requester_contact": {
        "email": "customer_04217@example.com",
        "preferred_language": "en-IN"
    },
    "submitted_at": "2026-04-27T10:30:00+05:30"
}
```

### Field definitions
- `principal_identifier`: the external ID from the source system (or a UUID)
- `identifier_type`: `external_id` | `principal_uuid` | `email` — how to resolve the identifier
- `request_type`: `access` | `correction` | `erasure` | `combined`
- `verification_token`: opaque token produced by the stub verification endpoint
- `scope_purposes`: array of purposes to apply the request to; null = all purposes
- `requester_contact`: where to deliver the response bundle
- `submitted_at`: the SLA timer starts from this timestamp

### 7.2.2 · Response schema

```json
{
    "request_id": "dsr_7f4a3c2b8e1d4f56",
    "status": "accepted",
    "sla_deadline": "2026-05-27T10:30:00+05:30",
    "next_action": "discovery_in_progress",
    "status_url": "/dsr/request/dsr_7f4a3c2b8e1d4f56"
}
```

The `sla_deadline` is 30 days from submission, per DPDP's default response window. In the POC we aim to complete in hours, not days — the 30-day SLA is documented but never actually tested.

### 7.2.3 · Request persistence

Every accepted request is written to Lakebase `public.dsr_requests` which syncs to Delta at `compliance_pack.compliance.dsr_requests`. Schema:

```sql
CREATE TABLE IF NOT EXISTS public.dsr_requests (
    request_id              VARCHAR(32)  PRIMARY KEY,
    data_principal_id       UUID         NOT NULL REFERENCES public.data_principals(principal_id),
    request_type            VARCHAR(32)  NOT NULL,
    identifier_type         VARCHAR(32)  NOT NULL,
    raw_identifier          VARCHAR(256) NOT NULL,       -- as submitted; for audit
    scope_purposes          TEXT[],
    requester_email         VARCHAR(256) NOT NULL,
    preferred_language      CHAR(5)      NOT NULL,
    submitted_at            TIMESTAMPTZ  NOT NULL,
    verification_token      VARCHAR(128) NOT NULL,
    verification_verified_at TIMESTAMPTZ,
    sla_deadline            TIMESTAMPTZ  NOT NULL,
    status                  VARCHAR(32)  NOT NULL,       -- 'accepted'|'verified'|'discovering'|'executing'|'completed'|'rejected'|'failed'
    next_action             VARCHAR(64),
    discovery_completed_at  TIMESTAMPTZ,
    execution_completed_at  TIMESTAMPTZ,
    response_bundle_path    TEXT,                         -- DBFS path to the bundle
    rejection_reason        TEXT,
    created_at              TIMESTAMPTZ  NOT NULL DEFAULT now()
);
```

## 7.3 · Identity verification (stub)

Real IDV is out of scope. We use a stub that issues tokens when the requester's email matches the principal's email on file. The stub flow:

1. Requester hits `POST /dsr/verify` with `{ "principal_identifier": "customer_04217", "email": "..." }`
2. Stub looks up the principal's email in the `customers` or `users` table
3. If match: returns a signed token (JWT with 1-hour expiry) `{ "verification_token": "eyJ..." }`
4. If no match: returns 400 with generic error (no hint whether principal exists)

Real implementation would add step-up authentication for erasure requests (SMS OTP, or similar). The POC demonstration proceeds past this stub without actually testing it, because the synthetic DSR principal's email is known.

Explicitly document this as a POC limitation in the Day 14 demo narrative — the GC will ask about identity verification and the correct answer is "stub for the POC; Phase 1 integrates with [IDV provider of choice]".

## 7.4 · Unified discovery query

Discovery uses Unity Catalog's system tables to find every column tagged as a principal identifier, then joins each identified table to the input principal. The goal is a single query that returns every row referencing this principal anywhere in the estate.

### 7.4.1 · Finding principal identifier columns

```sql
-- Identify all columns that are tagged as principal identifiers
WITH principal_id_columns AS (
    SELECT
        ct.catalog_name,
        ct.schema_name,
        ct.table_name,
        ct.column_name,
        f.pii_type
    FROM system.information_schema.column_tags ct
    JOIN compliance_pack.silver.pii_findings f
        ON f.catalog_name = ct.catalog_name
        AND f.schema_name = ct.schema_name
        AND f.table_name = ct.table_name
        AND f.column_name = ct.column_name
    WHERE ct.tag_name = 'pii_category'
      AND ct.tag_value LIKE 'direct_identifier%'
      AND f.pii_type IN ('external_id', 'email', 'aadhaar', 'pan')
)
SELECT * FROM principal_id_columns;
```

### 7.4.2 · Discovery per table

For each principal-identifying column found, run a discovery query against its table:

```python
def discover_for_table(catalog, schema, table, identifier_column, principal_identifier):
    query = f"""
        SELECT *
        FROM {catalog}.{schema}.{table}
        WHERE {identifier_column} = ?
    """
    return spark.sql(query, principal_identifier).collect()
```

Aggregate discovery results across all tables into a single `discovery_report`:

```python
{
    "principal_identifier": "customer_04217",
    "discovery_timestamp": "2026-04-27T10:32:00+05:30",
    "legal_hold_status": "none",  # hardcoded in POC
    "assets_found": [
        {
            "catalog": "compliance_pack",
            "schema": "silver",
            "table": "customers_tagged",
            "rows_matched": 1,
            "row_sample": { ... redacted ... },
            "action_decision": "erase_immediate",
            "retention_policy_applied": "no_retention"
        },
        {
            "catalog": "compliance_pack",
            "schema": "silver",
            "table": "transactions_tagged",
            "rows_matched": 14,
            "row_sample": { ... redacted ... },
            "action_decision": "schedule_residual_purge",
            "retention_policy_applied": "banking_7_year",
            "residual_purge_date": "2033-04-27"
        },
        ...
    ]
}
```

## 7.5 · Retention-aware execution logic

The decision tree per asset:

```
For each asset discovered:
  IF legal_hold_status != "none":
      action = "blocked_by_legal_hold"
  ELIF data type has retention obligation:
      action = "schedule_residual_purge"
      set residual_purge_date based on retention_policy
  ELIF data type has no retention obligation:
      action = "erase_immediate"
```

For the POC, retention policies are hardcoded simply:

| Table | Retention rule | Source |
|-------|----------------|--------|
| `customers` | none | No legal obligation |
| `users` | none | No legal obligation |
| `employees` | 7 years post-departure | Income Tax Act requirement |
| `patients` | 10 years from last visit | Medical records convention |
| `transactions` | 7 years | RBI / Banking Regulation Act |

In reality these are nuanced and belong in Module 0's retention catalog; the POC hardcodes them to demonstrate the mechanism.

## 7.6 · Delta DELETE + VACUUM execution

For each `erase_immediate` asset, execute:

```sql
DELETE FROM compliance_pack.silver.customers_tagged
WHERE customer_id = 'customer_04217';

DELETE FROM compliance_pack.silver.users_tagged
WHERE username = '<the user's username>';
```

After deletions:

```sql
-- Force physical removal by setting retention to 0 temporarily
-- IMPORTANT: this overrides the default 7-day VACUUM retention
-- It is ONLY appropriate in a POC sandbox; in production we'd wait out the retention
SET spark.databricks.delta.retentionDurationCheck.enabled = false;
VACUUM compliance_pack.silver.customers_tagged RETAIN 0 HOURS;
VACUUM compliance_pack.silver.users_tagged RETAIN 0 HOURS;
SET spark.databricks.delta.retentionDurationCheck.enabled = true;
```

The retention override is dangerous in production (it makes time travel impossible for the affected tables) and must be called out as a POC-only shortcut. In Phase 1, the VACUUM happens on schedule after the mandatory retention window.

## 7.7 · The response bundle

After execution, compose the four-part bundle at:

```
/Volumes/compliance_pack/compliance/dsr_bundles/<request_id>/
├── data_export.json
├── erasure_certificate.pdf
├── retention_schedule.pdf
└── audit_trail.json
```

### 7.7.1 · `data_export.json`

Pretty-printed JSON with the principal's data organized by table:

```json
{
    "principal_identifier": "customer_04217",
    "generated_at": "2026-04-27T14:15:00+05:30",
    "data_by_table": {
        "customers": [ { ... complete row ... } ],
        "users": [ { ... } ],
        "transactions": [
            { ... tx 1 ... },
            { ... tx 2 ... },
            ... 14 rows total ...
        ],
        "consent_events": [
            { ... event 1 ... },
            ... 4 events total ...
        ]
    }
}
```

### 7.7.2 · `erasure_certificate.pdf`

A signed PDF listing:
- Principal identifier (redacted to last 4 characters in the visible body, full in the QR code)
- Request ID and submission timestamp
- Execution timestamp
- Tables erased: list with row counts
- Tables masked: empty for POC (no Zone 1)
- Tables with scheduled residual: list with purge dates
- Signature block with the service principal identity
- QR code linking to the `audit_trail.json` path (for reproducibility)

Generate via ReportLab or WeasyPrint. Exact layout documented in `runbook/certificate_layout.md`.

### 7.7.3 · `retention_schedule.pdf`

For each scheduled residual:
- Asset description (table, row count, brief reason)
- Retention basis (legal reference where applicable)
- Scheduled purge date
- Commitment: "This data will be erased on or before [date]; a final erasure certificate will be issued at that time."

### 7.7.4 · `audit_trail.json`

Timestamped action sequence:

```json
{
    "request_id": "dsr_7f4a3c2b8e1d4f56",
    "events": [
        { "at": "2026-04-27T10:30:00", "action": "request_received", "actor": "api_gateway" },
        { "at": "2026-04-27T10:30:05", "action": "identity_verified", "actor": "stub_idv", "method": "email_match" },
        { "at": "2026-04-27T10:32:00", "action": "discovery_started", "actor": "compliance-pack-builder" },
        { "at": "2026-04-27T10:32:15", "action": "discovery_complete", "assets_found": 4 },
        { "at": "2026-04-27T14:12:00", "action": "erasure_executed", "target": "compliance_pack.silver.customers_tagged", "rows_deleted": 1 },
        { "at": "2026-04-27T14:12:30", "action": "erasure_executed", "target": "compliance_pack.silver.users_tagged", "rows_deleted": 1 },
        { "at": "2026-04-27T14:13:00", "action": "residual_scheduled", "target": "compliance_pack.silver.transactions_tagged", "purge_date": "2033-04-27" },
        { "at": "2026-04-27T14:15:00", "action": "bundle_generated", "bundle_path": "/Volumes/compliance_pack/compliance/dsr_bundles/dsr_7f4a3c2b8e1d4f56/" }
    ]
}
```

## 7.8 · Verification via time travel

The integration test confirms the erasure worked by running:

```sql
-- Before erasure (should return 1 row)
SELECT count(*) FROM compliance_pack.silver.customers_tagged VERSION AS OF <version_before>
WHERE customer_id = 'customer_04217';

-- After erasure (should return 0 rows)
SELECT count(*) FROM compliance_pack.silver.customers_tagged
WHERE customer_id = 'customer_04217';
```

The test passes only if both queries return the expected counts. This is the proof the GC asks for: "how do I know you actually deleted it?" — the answer is a reproducible time-travel comparison.

## 7.9 · Connection to Module 06 (residual retention register)

Assets with `schedule_residual_purge` write an entry to a simplified residual register:

```sql
CREATE TABLE IF NOT EXISTS compliance_pack.compliance.residual_retention_register (
    residual_id             STRING    NOT NULL,
    original_dsr_request_id STRING    NOT NULL,
    principal_identifier    STRING    NOT NULL,
    asset_description       STRING    NOT NULL,
    catalog_name            STRING    NOT NULL,
    schema_name             STRING    NOT NULL,
    table_name              STRING    NOT NULL,
    row_count               BIGINT    NOT NULL,
    retention_basis         STRING    NOT NULL,
    scheduled_purge_date    DATE      NOT NULL,
    status                  STRING    NOT NULL,  -- 'scheduled'|'purged'|'superseded'
    created_at              TIMESTAMP NOT NULL,
    purged_at               TIMESTAMP,
    final_certificate_path  STRING
) USING DELTA;
```

The actual scheduled purge does not execute during the POC (the dates are years in the future). The register entry is proof that the schedule exists and will be honored — exactly what the proposal's Module 06 would handle in production.

## 7.10 · What NOT to do

- **Do not** issue SQL `DELETE` without a preceding `VACUUM` when demonstrating erasure; the demo's "did you actually delete it" test depends on physical file removal
- **Do not** use `spark.databricks.delta.retentionDurationCheck.enabled = false` outside the POC demo path; it disables safety checks that exist for good reason
- **Do not** store raw principal identifiers in the audit trail; show last-4 only in bodies, full only in cryptographic QR codes
- **Do not** skip the legal hold check, even in the POC. Always show "legal_hold_status: none" explicitly so the absence is visible in the audit trail

Now proceed to `08_testing_strategy.md`.
