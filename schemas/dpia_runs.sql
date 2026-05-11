-- ============================================================================
-- compliance.dpia_runs — audit trail for the DPIA Auto-Generator
-- ============================================================================
-- One row per generation run. Each row pairs the structured input snapshot
-- with the model output so the artifact is reproducible months later, even
-- if the underlying tables (pii_findings, compliance_gaps, consent log)
-- have moved on. Mirrors the shape of compliance.retention_audit, which is
-- the established pattern for "evidence about a regulatory action."
--
-- Created via CREATE TABLE IF NOT EXISTS inside
-- governance_core/dpia.py::_ensure_audit_table — kept here so the schema
-- is reviewable in PRs and the docs-sync checklist has a target.
--
-- Status lifecycle: draft → under_review → approved → superseded
--   draft        — generated, not yet reviewed (default on insert)
--   under_review — assigned to a reviewer; comments may follow elsewhere
--   approved     — signed off; this row is the regulator-ready artifact
--   superseded   — a newer run has replaced this one for the same period
-- The approval flow itself lands in Phase 2 (scripts/approve_dpia.py).
--
-- Phase 3 added:
--   dpia_sections — MAP<STRING,STRING> populated when DPIASections
--                   (governance_core/dpia.py) successfully parsed the
--                   model output as 8 named sections; NULL otherwise.
--                   Dashboard tile (Phase 4) will read from here.
--   parse_error   — first 1000 chars of pydantic.ValidationError or
--                   json.JSONDecodeError when validation failed.
--                   NULL on a clean parse.
-- Existing workspaces upgrading from Phase 1/2 need a one-time
--   ALTER TABLE compliance_pack.compliance.dpia_runs
--     ADD COLUMNS (dpia_sections MAP<STRING,STRING>, parse_error STRING);
-- New workspaces get the Phase 3 schema directly from this DDL.
-- ============================================================================

CREATE TABLE IF NOT EXISTS compliance_pack.compliance.dpia_runs (
    run_id              STRING    NOT NULL,
    generated_at        TIMESTAMP NOT NULL,
    generated_by        STRING    NOT NULL,
    catalog_name        STRING    NOT NULL,
    model_endpoint      STRING    NOT NULL,
    prompt_module       STRING    NOT NULL,
    prompt_version      STRING    NOT NULL,
    regulation_pack     STRING,
    context_snapshot    STRING    NOT NULL,
    dpia_text           STRING    NOT NULL,
    dpia_sections       MAP<STRING, STRING>,
    parse_error         STRING,
    artifact_path       STRING    NOT NULL,
    latency_seconds     DOUBLE,
    status              STRING    NOT NULL,
    reviewed_by         STRING,
    reviewed_at         TIMESTAMP,
    notes               STRING
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  );
