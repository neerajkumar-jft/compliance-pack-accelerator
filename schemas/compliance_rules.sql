-- Compliance rules and gaps tables for DPDP POC
-- Ported from accelerator, adapted to DPDP 9-category taxonomy

-- ============================================================================
-- compliance_rules - what compliance gaps to detect
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.bronze.compliance_rules (
    rule_id                 STRING      NOT NULL,
    rule_type               STRING      NOT NULL,
    severity                STRING      NOT NULL,
    regulations             ARRAY<STRING> NOT NULL,
    applicable_categories   ARRAY<STRING> NOT NULL,
    description             STRING      NOT NULL,
    remediation             STRING      NOT NULL,
    is_active               BOOLEAN     NOT NULL
) USING DELTA
  COMMENT 'DPDP compliance gap detection rules';

-- ============================================================================
-- compliance_gaps - detected gaps per PII finding × rule
-- ============================================================================
CREATE TABLE IF NOT EXISTS compliance_pack.silver.compliance_gaps (
    gap_id          STRING      NOT NULL,
    scan_job_id     STRING      NOT NULL,
    table_name      STRING      NOT NULL,
    column_name     STRING      NOT NULL,
    pii_type        STRING      NOT NULL,
    pii_category    STRING      NOT NULL,
    rule_id         STRING      NOT NULL,
    rule_type       STRING      NOT NULL,
    severity        STRING      NOT NULL,
    regulation      STRING      NOT NULL,
    description     STRING      NOT NULL,
    remediation     STRING      NOT NULL,
    detected_at     TIMESTAMP   NOT NULL
) USING DELTA;

-- ============================================================================
-- Seed compliance rules (MERGE for idempotency)
-- ============================================================================

-- Encryption rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'ENC-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'ENC-001', 'encryption', 'high',
    ARRAY('DPDP', 'GDPR', 'HIPAA'),
    ARRAY('direct_identifier_government', 'direct_identifier_financial', 'health', 'biometric'),
    'Personal data must be encrypted at rest per DPDP Section 8',
    'Enable Delta table encryption and UC column masking for sensitive columns',
    true
);

MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'ENC-002' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'ENC-002', 'encryption', 'critical',
    ARRAY('PCI-DSS', 'DPDP'),
    ARRAY('direct_identifier_financial'),
    'Payment card data requires PCI-DSS compliant encryption',
    'Apply tokenization or format-preserving encryption to credit card and CVV columns',
    true
);

-- Consent rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'CNS-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'CNS-001', 'consent', 'critical',
    ARRAY('DPDP', 'GDPR'),
    ARRAY('direct_identifier_contact', 'direct_identifier_government', 'indirect_identifier'),
    'DPDP requires specific, informed consent before processing personal data',
    'Implement consent capture per purpose with notice version tracking (Module 02)',
    true
);

MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'CNS-002' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'CNS-002', 'consent', 'high',
    ARRAY('DPDP', 'GDPR'),
    ARRAY('direct_identifier_contact', 'direct_identifier_financial'),
    'DPDP mandates consent withdrawal mechanism with downstream propagation',
    'Deploy real-time withdrawal propagation via DLT CDC (Module 02)',
    true
);

-- Retention rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'RET-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'RET-001', 'retention', 'high',
    ARRAY('DPDP', 'GDPR'),
    ARRAY('direct_identifier_government', 'direct_identifier_financial', 'health'),
    'Data retention policy must be defined and enforced per DPDP Section 8(7)',
    'Define retention periods per data category and enforce via Module 06 workflows',
    true
);

-- Access control rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'ACC-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'ACC-001', 'access_control', 'high',
    ARRAY('DPDP', 'GDPR', 'HIPAA'),
    ARRAY('direct_identifier_government', 'health', 'biometric', 'sensitive_demographic'),
    'Role-based access control required for personal data per DPDP Section 8',
    'Implement UC row-level security and column masking for sensitive columns',
    true
);

MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'ACC-002' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'ACC-002', 'access_control', 'medium',
    ARRAY('DPDP', 'GDPR', 'HIPAA'),
    ARRAY('direct_identifier_government', 'health', 'direct_identifier_financial'),
    'Access audit logging required for all personal data queries',
    'Enable UC audit log monitoring and configure alerting for sensitive table access',
    true
);

-- DSR rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'DSR-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'DSR-001', 'data_subject_rights', 'critical',
    ARRAY('DPDP'),
    ARRAY('direct_identifier_government', 'direct_identifier_contact', 'direct_identifier_financial', 'indirect_identifier', 'health'),
    'Data principal rights (access, correction, erasure) must be fulfillable per DPDP Section 11-14',
    'Deploy DSR hub with UC lineage-based discovery and Delta DELETE + VACUUM (Module 03)',
    true
);

-- Breach notification rules
MERGE INTO compliance_pack.bronze.compliance_rules t
USING (SELECT 'BRN-001' AS rule_id) s ON t.rule_id = s.rule_id
WHEN NOT MATCHED THEN INSERT VALUES (
    'BRN-001', 'breach_notification', 'critical',
    ARRAY('DPDP'),
    ARRAY('direct_identifier_government', 'direct_identifier_financial', 'health', 'biometric'),
    'Breach notification to DPBI required within 72 hours per DPDP Section 8(6)',
    'Deploy breach detection with Lakewatch and automated DPBI notification drafting (Module 04)',
    true
);
