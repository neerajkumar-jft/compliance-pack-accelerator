-- ============================================================================
-- HISTORICAL REFERENCE ONLY — do not run.
--
-- As of 2026-04-24 (branch refactor/regulation-packs-phase-0) the residency
-- filter is rendered at bootstrap time from the active regulation pack's
-- residency.yaml. See:
--
--   regulations/dpdp_2023/residency.yaml    (allowed_countries + apply_filter_to)
--   pipelines/phase1_bootstrap.py §8        (CREATE FUNCTION + ALTER TABLE)
--
-- On a UK GDPR / CCPA deployment, authoring a new pack's residency.yaml
-- (different allowed_countries list, same apply_filter_to shape) is all
-- that's needed — no SQL edits here. The body of this file is kept as a
-- reference for the shape of the generated UDF + ALTER statement.
-- ============================================================================

-- Cross-border residency filter — DPDP §16 data-flow controls.
--
-- Admins see every row; non-admins only see rows where the source
-- country is India. The filter is applied via ALTER TABLE ... SET
-- ROW FILTER — enforced at query time by Unity Catalog, regardless
-- of the UI path (dashboard tile, Genie question, SQL editor, API).

CREATE OR REPLACE FUNCTION compliance_pack.compliance.residency_filter(country STRING)
RETURNS BOOLEAN
RETURN is_member('admins') OR country IN ('India');

-- Apply to employees_tagged (has an explicit `country` column with
-- India + USA data in the POC).
ALTER TABLE compliance_pack.silver.employees_tagged
  SET ROW FILTER compliance_pack.compliance.residency_filter ON (country);
