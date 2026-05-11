-- Personal data register - the stakeholder-facing view (Artifact 1)
-- Referenced from §3.6
-- Run after schemas/silver.sql and after the classification job has produced findings

CREATE OR REPLACE VIEW compliance_pack.compliance.personal_data_register AS
WITH latest_scan AS (
    SELECT scan_job_id
    FROM compliance_pack.silver.pii_findings
    WHERE discovered_at = (SELECT MAX(discovered_at) FROM compliance_pack.silver.pii_findings)
    LIMIT 1
)
SELECT
    f.catalog_name || '.' || f.schema_name || '.' || f.table_name   AS fully_qualified_table,
    f.catalog_name                      AS source_catalog,
    f.schema_name                       AS source_schema,
    f.table_name                        AS source_table,
    f.column_name                       AS source_column,
    f.column_data_type                  AS data_type,
    f.pii_category                      AS pii_category,
    f.pii_type                          AS pii_type,
    f.sensitivity_tier                  AS sensitivity_tier,
    f.classifier_source                 AS classifier_source,
    f.confidence                        AS classification_confidence,
    f.match_rate                        AS match_rate,
    f.regulations                       AS applicable_regulations,
    f.sample_match_redacted             AS redacted_sample,
    COALESCE(t.comment, '(not assigned)') AS data_owner,
    dt.row_count                        AS table_row_count,
    dt.pii_column_count                 AS table_pii_column_count,
    f.human_reviewed                    AS human_reviewed,
    f.review_status                     AS review_status,
    f.review_notes                      AS review_notes,
    f.discovered_at                     AS last_scanned_at,
    f.reviewed_at                       AS last_reviewed_at
FROM compliance_pack.silver.pii_findings f
JOIN latest_scan ls
    ON f.scan_job_id = ls.scan_job_id
LEFT JOIN compliance_pack.silver.discovered_tables dt
    ON dt.scan_job_id = f.scan_job_id
    AND dt.catalog_name = f.catalog_name
    AND dt.schema_name = f.schema_name
    AND dt.table_name = f.table_name
LEFT JOIN system.information_schema.tables t
    ON t.table_catalog = f.catalog_name
    AND t.table_schema = f.schema_name
    AND t.table_name = f.table_name
ORDER BY
    CASE f.sensitivity_tier
        WHEN 'critical' THEN 1
        WHEN 'high'     THEN 2
        WHEN 'medium'   THEN 3
        WHEN 'low'      THEN 4
        ELSE 5
    END,
    f.table_name,
    f.column_name;

-- Grant read access to any principal needing to view the register
-- (Adjust grantee based on your workspace's permission model)
GRANT SELECT ON VIEW compliance_pack.compliance.personal_data_register TO `account users`;
