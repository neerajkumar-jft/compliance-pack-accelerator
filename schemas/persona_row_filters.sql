-- Persona-aware row filter — extends the DPDP §16 residency-filter pattern
-- (schemas/residency_filter.sql) to per-persona row visibility.
--
-- Why this exists:
--   - Column masks hide VALUES but every persona still sees every ROW.
--   - UC row filters hide ROWS at query time, independent of UI path
--     (dashboard tile, Genie question, SQL editor, notebook, API).
--   - The existing residency_filter fences non-admins to India-resident
--     rows (DPDP §16). This filter adds a second dimension: persona
--     identity via current_user().
--
-- Policy on compliance_pack.compliance.consent_events_log:
--   admin (is_member('admins'))            → every row (~1000)
--   CCO persona (you+compliance-cco@...)         → every row (compliance oversight)
--   GC  persona (you+compliance-gc@...)          → every row (legal review)
--   CFO persona (you+compliance-cfo@...)         → every row (financial audit)
--   CMO persona (you+compliance-cmo@...)         → only marketing-relevant
--                                            purposes (~500): marketing_email,
--                                            marketing_sms,
--                                            product_personalization
--   everyone else                          → every row (grant-gated
--                                            separately by UC SELECT)
--
-- Why `purpose` (not notice_language or region):
--   The synthetic dataset was generated single-language (en-IN), so a
--   language-based filter would be a no-op. `purpose` has 6 values in
--   the POC data (analytics, core_service, marketing_email,
--   marketing_sms, product_personalization, third_party_sharing) and
--   splits ~50/50 along the marketing vs. non-marketing axis — a clean
--   demo of row-level scoping that maps to a real enterprise concern:
--   "the marketing team should not see rows about analytics or
--   third-party data-sharing consent."
--
-- Migration path to account-level groups:
--   When the workspace switches from plus-addressed emails to account
--   groups (e.g. `compliance-cmo`), replace
--     current_user() LIKE '%+compliance-cmo@%'
--   with
--     is_account_group_member('compliance-cmo')
--   on both matches below. Everything else stays the same.

CREATE OR REPLACE FUNCTION compliance_pack.compliance.persona_purpose_scope(purpose STRING)
RETURNS BOOLEAN
RETURN
  is_member('admins')
  OR current_user() NOT LIKE '%+compliance-cmo@%'
  OR purpose IN ('marketing_email', 'marketing_sms', 'product_personalization');

-- Apply the filter.
ALTER TABLE compliance_pack.compliance.consent_events_log
  SET ROW FILTER compliance_pack.compliance.persona_purpose_scope ON (purpose);

-- To drop the filter (e.g. for debugging or a schema migration):
--   ALTER TABLE compliance_pack.compliance.consent_events_log DROP ROW FILTER;
