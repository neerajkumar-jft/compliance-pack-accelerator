# §4 · PII taxonomy and classification

> ⚠️ **Pre-build planning document.** Taxonomy and pattern-library design are accurate and match `schemas/pii_patterns.py`. Process notes referencing a separate `apply_uc_tags` job are superseded — classification + UC tag application run inside `pipelines/classification_dlt.py` + `pipelines/phase1_bootstrap.py`.

## 4.1 · Why this section is load-bearing

PII classification is the mechanism that turns raw data into a defensible personal data register. The register is only as trustworthy as the taxonomy underneath it. This section defines exactly what categories exist, what patterns match what, what confidence means, and how conflicts resolve. Every classification decision the platform makes references this specification.

The approach adopts the accelerator's `PIIDetector` pattern library (from `src/utils/pii_detector.py` in the inspected repo) — the 16 patterns are sound — but reorganizes them under the DPDP-aligned 9-category taxonomy and replaces the Python-loop execution with Databricks-native patterns.

## 4.2 · The nine categories

The DPDP Act speaks of "personal data" and "sensitive personal data" broadly. For operational use, the platform needs finer categorization so that retention policies, masking rules, and access controls can be differentiated. We use nine categories:

### 4.2.1 · `direct_identifier_government`

Government-issued identifiers that uniquely identify a person. Highest sensitivity in the DPDP context.

- **Included types**: `aadhaar`, `pan`, `passport`, `voter_id`, `driving_license`
- **Default sensitivity**: `critical`
- **Typical regulations**: `DPDP`

### 4.2.2 · `direct_identifier_contact`

Contact mechanisms that reach a specific person. Widely used and therefore widely leaked; medium sensitivity reflects volume, not criticality.

- **Included types**: `email`, `phone`, `address`, `ip_address`
- **Default sensitivity**: `medium` (except `ip_address` which is `low` in isolation, `medium` when combined with identity)
- **Typical regulations**: `DPDP`, `GDPR`, `CCPA`

### 4.2.3 · `direct_identifier_financial`

Financial instruments tied to an identified person. High to critical sensitivity depending on instrument.

- **Included types**: `credit_card`, `cvv`, `bank_account`, `ifsc_code`, `upi_id`
- **Default sensitivity**: `critical` for card/CVV, `high` for account/IFSC
- **Typical regulations**: `DPDP`, `PCI-DSS`

### 4.2.4 · `indirect_identifier`

Data that is not itself identifying but becomes identifying in combination.

- **Included types**: `date_of_birth`, `device_id`, `device_fingerprint`, `postal_code`, `geo_precise`
- **Default sensitivity**: `high` for DOB, `medium` for others
- **Typical regulations**: `DPDP`, `GDPR`

### 4.2.5 · `biometric`

Biometric identifiers or templates.

- **Included types**: `fingerprint_template`, `face_embedding`, `iris_scan`, `voice_print`
- **Default sensitivity**: `critical`
- **Typical regulations**: `DPDP`
- **Note**: none of these appear in the POC's synthetic data by default; the category exists for completeness

### 4.2.6 · `health`

Health and medical data.

- **Included types**: `medical_record_number`, `diagnosis`, `prescription`, `allergies`, `blood_group`, `insurance_id`, `insurance_provider`
- **Default sensitivity**: `critical` for diagnosis/prescription, `high` for IDs
- **Typical regulations**: `DPDP`, `HIPAA` (where applicable)

### 4.2.7 · `sensitive_demographic`

DPDP-designated sensitive data categories.

- **Included types**: `religion`, `caste`, `political_opinion`, `sexual_orientation`
- **Default sensitivity**: `critical`
- **Typical regulations**: `DPDP`
- **Note**: none in the POC's synthetic data; category included for specification completeness

### 4.2.8 · `financial_behavior`

Aggregated or behavioral financial data.

- **Included types**: `transaction_amount`, `transaction_pattern`, `salary`, `credit_score`
- **Default sensitivity**: `high`
- **Typical regulations**: `DPDP`

### 4.2.9 · `children_marker`

Indicators that a principal is or may be under 18. Triggers different consent mechanics.

- **Included types**: `dob_implies_minor`, `school_identifier`, `guardian_relationship`, `age_verification_method`
- **Default sensitivity**: `critical`
- **Typical regulations**: `DPDP` (special children-data provisions)

## 4.3 · The pattern library

The following patterns are adapted from the accelerator's `DEFAULT_PATTERNS` (pii_detector.py lines 78-269) and mapped to the 9-category taxonomy above. The dataclass model is preserved; what changes is the execution layer (§4.5).

### 4.3.1 · The `PIIPattern` dataclass (from accelerator, unchanged)

```python
from dataclasses import dataclass
from typing import Optional

@dataclass
class PIIPattern:
    pattern_id: str          # unique within the library
    pii_type: str            # e.g., 'aadhaar'
    category: str            # one of the 9 categories in §4.2
    sensitivity: str         # 'critical'|'high'|'medium'|'low'
    regex_pattern: Optional[str]  # None for column-hint-only types
    column_hints: list[str]  # substrings that match against column names
    regulations: list[str]   # always includes 'DPDP' for this POC
    description: str
    priority: int = 50       # higher wins in conflicts
```

### 4.3.2 · Pattern library contents (16 patterns, category-aligned)

The table below is the complete pattern library for the POC. The full dataclass instantiation appears in `schemas/pii_patterns.py`.

| pattern_id | pii_type | category | sensitivity | regex | column hints | priority |
|------------|----------|----------|-------------|-------|--------------|----------|
| `email` | email | direct_identifier_contact | medium | `\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b` | email, e_mail, email_address, mail | 90 |
| `phone_india` | phone | direct_identifier_contact | medium | `(\+91[-.\s]?)?[6-9]\d{9}` | phone, mobile, cell, contact_number, msisdn | 85 |
| `phone_intl` | phone | direct_identifier_contact | medium | `\+\d{1,3}[-.\s]?\d{3,14}` | phone, mobile, telephone | 80 |
| `aadhaar` | aadhaar | direct_identifier_government | critical | `\b[2-9]\d{3}[\s-]?\d{4}[\s-]?\d{4}\b` | aadhaar, aadhar, uid_number | 100 |
| `pan` | pan | direct_identifier_government | critical | `\b[A-Z]{5}\d{4}[A-Z]\b` | pan, pan_number, pan_card | 99 |
| `passport_india` | passport | direct_identifier_government | critical | `\b[A-PR-WY][1-9]\d{6}\b` | passport, passport_number, passport_no | 98 |
| `credit_card` | credit_card | direct_identifier_financial | critical | `\b(?:4\d{12}(?:\d{3})?\|5[1-5]\d{14}\|3[47]\d{13}\|6(?:011\|5\d{2})\d{12})\b` | credit_card, card_number, cc_number | 100 |
| `cvv` | cvv | direct_identifier_financial | critical | `^\d{3,4}$` (context-sensitive) | cvv, cvc, security_code, card_security | 100 |
| `bank_account` | bank_account | direct_identifier_financial | high | `\b\d{9,18}\b` (low precision, rely on hints) | bank_account, account_number, iban | 95 |
| `ifsc` | ifsc_code | direct_identifier_financial | high | `^[A-Z]{4}0[A-Z0-9]{6}$` | ifsc, ifsc_code, routing_code | 90 |
| `name` | name | direct_identifier_contact | medium | none (column-hint only) | name, first_name, last_name, full_name, patient_name, account_holder_name | 70 |
| `address` | address | direct_identifier_contact | medium | none (column-hint only) | address, street, billing_address, shipping_address | 75 |
| `dob` | date_of_birth | indirect_identifier | high | none (column-hint only) | dob, date_of_birth, birth_date, birthdate | 80 |
| `ip_address` | ip_address | direct_identifier_contact | medium | `\b(?:(?:25[0-5]\|2[0-4]\d\|[01]?\d\d?)\.){3}(?:25[0-5]\|2[0-4]\d\|[01]?\d\d?)\b` | ip_address, ip, client_ip, user_ip, last_login_ip | 60 |
| `medical_record` | medical_record | health | critical | none (column-hint only) | diagnosis, medical_record, prescription, treatment, allergies | 100 |
| `insurance_id` | insurance_id | health | high | none (column-hint only) | insurance_id, policy_number, insurance_number | 90 |

### 4.3.3 · Changes from the accelerator library

1. **`phone` split into `phone_india` and `phone_intl`**: the accelerator's single phone pattern was too liberal for DPDP context. Indian numbers follow a specific pattern (`+91` prefix, leading digit 6-9 for mobile). We keep the international fallback at lower priority.

2. **`ssn` removed**: not relevant for DPDP; US-specific.

3. **`passport` regex tightened**: the accelerator's `\b[A-Z][0-9]{7}\b` matches too many non-passport strings. We tighten to Indian passport format `[A-PR-WY][1-9]\d{6}`.

4. **`ifsc` added**: critical for Indian banking context; not in the accelerator library.

5. **`cvv` regex made context-sensitive**: matching `^\d{3,4}$` naively flags every short numeric column. We rely primarily on column hints for CVV.

6. **Categories remapped**: the accelerator's flat `identity`/`contact`/`financial`/`health`/`digital` becomes our 9-category taxonomy, with explicit DPDP alignment.

7. **`sample_match` becomes `sample_match_redacted`**: never store raw PII as a sample; always redact.

## 4.4 · Confidence calculation

Adopted from the accelerator with one modification. The base model is:

```python
BASE_CONFIDENCE_BOTH   = 0.7    # column hint matched AND regex matched
BOOST_FACTOR_BOTH      = 0.3
BASE_CONFIDENCE_COLUMN = 0.5    # column hint matched only
BOOST_FACTOR_COLUMN    = 0.2
BASE_CONFIDENCE_VALUE  = 0.4    # regex matched only (no column hint)
BOOST_FACTOR_VALUE     = 0.3

def calculate_confidence(column_match: bool, value_match: bool, match_rate: float) -> float:
    if column_match and value_match:
        return min(BASE_CONFIDENCE_BOTH + match_rate * BOOST_FACTOR_BOTH, 1.0)
    elif column_match:
        return min(BASE_CONFIDENCE_COLUMN + match_rate * BOOST_FACTOR_COLUMN, 1.0)
    elif value_match:
        return min(BASE_CONFIDENCE_VALUE + match_rate * BOOST_FACTOR_VALUE, 1.0)
    else:
        return 0.0
```

Where `match_rate` is the fraction of sampled non-null values that matched the regex.

### Default thresholds

- **`≥ 0.85`**: classify automatically, no review required, tag applied
- **`0.65 – 0.84`**: classify automatically, flag for CCO human review, tag applied
- **`0.50 – 0.64`**: do not classify, log as low-confidence candidate for manual triage
- **`< 0.50`**: ignore

### Modification from accelerator

The accelerator uses a fixed `0.6` minimum (line 315 of pii_detector.py, `min_confidence: float = 0.6`). We raise the floor to `0.65` for auto-classification because the POC's audience (GC, CCO) will test edge cases hostilely. A 60% confident "aadhaar" that turns out not to be aadhaar will undermine the demo. The 65% floor is empirically the point where false positives on this pattern library drop below 5%.

## 4.5 · Execution pattern (the Databricks-native translation)

This is the core architectural change from the accelerator. The accelerator's scan loop (`02_Silver_Discovery.py` lines 585-671) collects samples to the driver with `df.limit(SAMPLE_SIZE).collect()` and iterates columns in Python. The SA flagged this correctly. We replace it with two vectorized patterns:

### 4.5.1 · Pattern A: vectorized regex via Spark SQL

For every pattern with a regex, we use `regexp_extract` in a Spark SQL aggregation that computes match rate per column directly:

```python
from pyspark.sql import functions as F

def scan_column_regex(df, column_name, pii_pattern):
    """Compute match rate for a single (column, pattern) pair entirely on executors."""
    total = df.filter(F.col(column_name).isNotNull()).count()
    if total == 0:
        return 0, 0.0
    matched = df.filter(
        F.col(column_name).isNotNull() &
        (F.regexp_extract(F.col(column_name).cast("string"),
                          pii_pattern.regex_pattern, 0) != "")
    ).count()
    return matched, matched / total
```

This stays on the Spark side end to end. No driver-side sample collection, no Python loop over rows.

### 4.5.2 · Pattern B: pandas UDF for LLM classification

For columns that need `ai_classify` (free-text fields like `primary_diagnosis`), we use the built-in Databricks AI function directly in SQL:

```sql
SELECT
    medical_record_number,
    primary_diagnosis,
    ai_classify(
        primary_diagnosis,
        ARRAY('diagnosis', 'prescription', 'allergy_note', 'other_medical', 'non_medical')
    ) AS ai_classification
FROM compliance_pack.bronze.source_patients
WHERE primary_diagnosis IS NOT NULL
LIMIT 100;  -- sample for classification-cost control
```

The `ai_classify` call runs on executors via Databricks' model serving infrastructure. No Python loop required.

### 4.5.3 · The orchestration pattern

The scan job iterates (table, column) pairs at the **control plane** — a Python loop that issues Spark SQL queries, not a Spark DataFrame loop. For each column:

1. Get the column's data type and name from Unity Catalog metadata
2. Run column-hint matching against the pattern library (pure Python, no data touched)
3. For patterns with regex: run Pattern A (Spark SQL aggregation)
4. For long-text columns: run Pattern B (ai_classify via SQL)
5. Compute confidence from (column_match, value_match, match_rate)
6. Insert a row into `pii_findings` if confidence ≥ 0.65
7. Apply `ALTER TABLE ... SET TAGS` if confidence ≥ 0.85

This gives us roughly one Spark query per (column, regex-pattern) pair. For ~80 columns across 5 tables × 10 regex patterns, that's ~800 queries — well within the trial workspace's compute budget.

### 4.5.4 · What the code must NOT do

- **Never** `.collect()` more than 5 rows at a time from any DataFrame
- **Never** iterate a DataFrame with `.rdd.foreach` or similar row-wise patterns
- **Never** use `.toPandas()` on anything larger than the redacted-sample preview
- **Never** install Python database/SaaS connectors
- **Never** use JDBC + pandas for ingestion; use Auto Loader against landing-zone volumes

## 4.6 · Conflict resolution

When two patterns match the same column (e.g., a column named `id_number` with Aadhaar-format values matches both a generic `id` column hint and the `aadhaar` regex), the conflict resolution rule is:

1. The match with higher `priority` wins
2. If priorities are tied, the match with higher `confidence` wins
3. If both are tied, the match is flagged for human review (`review_status='pending'`)

The accelerator's `detect_pii` function (02_Silver_Discovery.py line 396) has `for p in patterns ... return` behavior that returns the first match, which is priority-sorted. We keep that pattern but make the tie-break explicit rather than implicit.

## 4.7 · Sample redaction

Every row in `pii_findings` stores a `sample_match_redacted` value for audit. The redaction rules:

- **Aadhaar** (12 digits): show first 4 and last 2 with `X` in between → `2345XXXXXX23`
- **PAN** (10 alphanumeric): show first 2 and last 2 → `ABXXXXXX4F`
- **Credit card** (13-19 digits): show first 4 and last 4 → `4532XXXXXXXX0366`
- **Phone** (10-14 digits with country code): show last 4 → `XXXXXX7890`
- **Email**: show first 2 of local part and the domain → `ra****@email.com`
- **Name**: first character + `X` padding → `RX`
- **Address**: first 5 characters + ellipsis → `123 M...`
- **Generic**: show length only → `[12 chars]`

The redaction is done in Python at finding-record time, not in SQL. Raw PII must never be written to `pii_findings`.

## 4.8 · Manual override mechanism

The CCO reviews low-confidence findings and can reclassify. The manual override pattern:

```sql
UPDATE compliance_pack.silver.pii_findings
SET
    pii_category = 'direct_identifier_government',
    pii_type = 'aadhaar',
    sensitivity_tier = 'critical',
    classifier_source = 'manual',
    confidence = 1.0,
    human_reviewed = true,
    review_status = 'reclassified',
    review_notes = 'CCO reclassified on 2026-04-22 after data-owner confirmation',
    reviewed_at = current_timestamp()
WHERE finding_id = '<uuid>';
```

The reclassification is logged with reviewer identity and timestamp. Once reviewed, the finding's status persists across subsequent scan runs — the classifier will not overwrite a human decision.

Now proceed to `05_consent_model.md`.
