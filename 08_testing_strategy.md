# §8 · Testing strategy

> ⚠️ **Pre-build planning document.** Integration tests referencing the DSR-portal app (INT-02, INT-03, INT-05 in their Lakebase form) don't apply on free-trial. **For the active test suite, see [`docs/how_to_test.html`](docs/how_to_test.html) (12 sections) and `tests/persona_boundary_test.py`.**

## 8.1 · The testing contract

Every task you complete must end with running the relevant tests from this specification. If tests fail, you must diagnose before fixing and before moving on. Tests are not a nice-to-have; they are the contract by which the POC is judged complete.

Four test categories operate at different levels:

- **Unit tests** — per-function correctness on pure logic (no Databricks calls)
- **Integration tests** — cross-module workflows against live Unity Catalog / Lakebase
- **Checkpoint tests** — milestone verification at Day 7 and Day 14
- **Performance spot-checks** — SLA verification for the one latency guarantee (withdrawal propagation)

All tests live in the `tests/` subdirectory. Failing tests block further work.

## 8.2 · Unit tests

Unit tests run locally (on your dev machine or in a Databricks notebook with no cluster-specific features) and exercise pure Python logic from the spec.

### 8.2.1 · Pattern library

```python
# tests/unit/test_pii_patterns.py

def test_aadhaar_regex_accepts_valid_format():
    from schemas.pii_patterns import AADHAAR_PATTERN
    assert AADHAAR_PATTERN.matches_value("2345 6789 0123") is True
    assert AADHAAR_PATTERN.matches_value("3456-7890-1234") is True
    assert AADHAAR_PATTERN.matches_value("234567890123") is True

def test_aadhaar_regex_rejects_invalid_first_digit():
    from schemas.pii_patterns import AADHAAR_PATTERN
    # Aadhaar must start with 2-9, not 0 or 1
    assert AADHAAR_PATTERN.matches_value("0345 6789 0123") is False
    assert AADHAAR_PATTERN.matches_value("1345 6789 0123") is False

def test_pan_regex_format():
    from schemas.pii_patterns import PAN_PATTERN
    assert PAN_PATTERN.matches_value("ABCDE1234F") is True
    assert PAN_PATTERN.matches_value("ABCDE12345") is False  # last must be letter
    assert PAN_PATTERN.matches_value("ABCD1234EF") is False  # wrong layout

def test_ifsc_regex_format():
    from schemas.pii_patterns import IFSC_PATTERN
    assert IFSC_PATTERN.matches_value("SBIN0001234") is True
    assert IFSC_PATTERN.matches_value("HDFC0002345") is True
    assert IFSC_PATTERN.matches_value("SBI0001234") is False  # only 3 letters
    assert IFSC_PATTERN.matches_value("SBINX001234") is False  # char at position 4 must be '0'

def test_column_hint_matching():
    from schemas.pii_patterns import EMAIL_PATTERN
    assert EMAIL_PATTERN.matches_column_name("email") is True
    assert EMAIL_PATTERN.matches_column_name("email_address") is True
    assert EMAIL_PATTERN.matches_column_name("user_mail") is True
    assert EMAIL_PATTERN.matches_column_name("phone") is False
```

### 8.2.2 · Confidence calculation

```python
def test_confidence_both_methods():
    # Column hint + regex match with high match rate
    assert 0.97 <= calculate_confidence(True, True, 0.9) <= 1.0

def test_confidence_column_only():
    # Column hint only, no regex (name column with no pattern)
    c = calculate_confidence(True, False, 0.0)
    assert 0.49 <= c <= 0.51  # exactly 0.5

def test_confidence_value_only():
    # Regex match but unusual column name
    c = calculate_confidence(False, True, 0.8)
    assert 0.63 <= c <= 0.65
```

### 8.2.3 · Sample redaction

```python
def test_aadhaar_redaction():
    from utils.redaction import redact_sample
    assert redact_sample("2345 6789 0123", pii_type="aadhaar") == "2345XXXXXX23"

def test_pan_redaction():
    from utils.redaction import redact_sample
    assert redact_sample("ABCDE1234F", pii_type="pan") == "ABXXXXXX4F"

def test_email_redaction():
    from utils.redaction import redact_sample
    assert redact_sample("rahul.sharma@company.com", pii_type="email") == "ra****@company.com"
```

### 8.2.4 · Generator determinism

```python
def test_generator_is_deterministic():
    """Running the generator twice with the same seed produces identical output."""
    run1 = generate_all(output_dir="/tmp/run1", seed=42)
    run2 = generate_all(output_dir="/tmp/run2", seed=42)
    assert filecmp.cmp("/tmp/run1/employees/...", "/tmp/run2/employees/...")

def test_dsr_principal_exists():
    """customer_04217 must exist in the generated customers table."""
    generate_all(output_dir="/tmp/test", seed=42)
    with open("/tmp/test/customers/...") as f:
        rows = list(csv.DictReader(f))
    assert any(r["customer_id"] == "customer_04217" for r in rows)
```

## 8.3 · Integration tests

Integration tests run against the live workspace. They are numbered for reference from the Day 7 and Day 14 checkpoints.

### Test INT-01 · Personal data register completeness

**Purpose**: verify Artifact 1 (the register) contains expected findings across all 5 tables.

```sql
-- After the classification job has run once
SELECT
    COUNT(DISTINCT source_table) AS tables_scanned,
    COUNT(*) AS total_findings,
    SUM(CASE WHEN sensitivity_tier = 'critical' THEN 1 ELSE 0 END) AS critical_findings,
    SUM(CASE WHEN classification_confidence >= 0.85 THEN 1 ELSE 0 END) AS auto_classified
FROM compliance_pack.compliance.personal_data_register;
```

**Pass criteria**:
- `tables_scanned` = 5
- `total_findings` ≥ 20
- `critical_findings` ≥ 8 (Aadhaar, PAN, passport, credit card, CVV, bank_account, medical_record, diagnosis)
- `auto_classified` ≥ 15 (most findings should be high-confidence)

### Test INT-02 · Withdrawal propagation latency

**Purpose**: verify withdrawal propagation completes within 5 minutes (Artifact 2 SLA).

```python
# Pseudocode
def test_withdrawal_propagation():
    # Pick a principal who currently has marketing_email=granted
    principal = "customer_04217"

    # Record start time
    t0 = now()

    # Insert withdrawal event into Lakebase
    lakebase_execute("""
        INSERT INTO public.consent_events (
            event_id, data_principal_id, event_timestamp, event_type,
            notice_version_id, notice_language, channel, purpose,
            purpose_grant_status, consent_capture_method,
            retention_clock_start, retention_duration_days, created_by
        ) VALUES (
            gen_random_uuid(), '<uuid>', now(), 'withdrawn',
            '<notice_uuid>', 'en-IN', 'web', 'marketing_email',
            'declined', 'toggle',
            now(), 0, 'compliance-pack-builder'
        )
    """)

    # Poll the Gold view until the principal is no longer eligible
    while now() - t0 < timedelta(minutes=5):
        result = spark.sql(f"""
            SELECT COUNT(*)
            FROM compliance_pack.gold.marketing_eligible_principals
            WHERE data_principal_id = '<customer_04217 uuid>'
              AND purpose = 'marketing_email'
        """).collect()[0][0]
        if result == 0:
            elapsed = now() - t0
            break
        sleep(15)

    assert result == 0, "Principal still marketing-eligible after 5 minutes"
    assert elapsed < timedelta(minutes=5), f"Propagation took {elapsed}"
```

**Pass criteria**: propagation completes in < 300 seconds.

### Test INT-03 · DSR end-to-end

**Purpose**: verify Artifact 3 (DSR bundle) matches the expected footprint for `customer_04217`.

```python
def test_dsr_end_to_end():
    # Submit the DSR
    response = requests.post(f"{POC_API_URL}/dsr/request", json={
        "principal_identifier": "customer_04217",
        "identifier_type": "external_id",
        "request_type": "combined",
        "verification_token": get_stub_token("customer_04217"),
        "requester_contact": {"email": "customer_04217@example.com", "preferred_language": "en-IN"}
    })
    assert response.status_code == 200
    request_id = response.json()["request_id"]

    # Poll until status = 'completed' (should be < 5 minutes)
    wait_for_completion(request_id, timeout_minutes=5)

    # Verify bundle contents
    bundle_path = f"/Volumes/compliance_pack/compliance/dsr_bundles/{request_id}/"
    assert exists(f"{bundle_path}/data_export.json")
    assert exists(f"{bundle_path}/erasure_certificate.pdf")
    assert exists(f"{bundle_path}/retention_schedule.pdf")
    assert exists(f"{bundle_path}/audit_trail.json")

    # Verify data export contents
    with open(f"{bundle_path}/data_export.json") as f:
        export = json.load(f)
    assert len(export["data_by_table"]["customers"]) == 1
    assert len(export["data_by_table"]["users"]) == 1
    # Transactions count is deterministic but computed from seed; use manifest
    expected_tx_count = read_manifest()["dsr_expected_transaction_count"]
    assert len(export["data_by_table"]["transactions"]) == expected_tx_count
    assert len(export["data_by_table"]["consent_events"]) == 4

    # Verify erasure actually happened via time travel
    version_before = get_version_before_dsr("compliance_pack.silver.customers_tagged")
    count_before = spark.sql(f"""
        SELECT COUNT(*) FROM compliance_pack.silver.customers_tagged VERSION AS OF {version_before}
        WHERE customer_id = 'customer_04217'
    """).collect()[0][0]
    count_after = spark.sql("""
        SELECT COUNT(*) FROM compliance_pack.silver.customers_tagged
        WHERE customer_id = 'customer_04217'
    """).collect()[0][0]
    assert count_before == 1
    assert count_after == 0

    # Verify residual retention entry exists for transactions
    residual = spark.sql(f"""
        SELECT scheduled_purge_date FROM compliance_pack.compliance.residual_retention_register
        WHERE original_dsr_request_id = '{request_id}'
          AND table_name = 'transactions_tagged'
    """).collect()
    assert len(residual) == 1
    assert residual[0]["scheduled_purge_date"].year == 2033
```

**Pass criteria**: all assertions pass.

### Test INT-04 · Unity Catalog lineage visibility

**Purpose**: verify every table in the medallion path has UC lineage.

```python
def test_lineage_bronze_to_silver():
    # For each Silver table, verify it has lineage from at least one Bronze table
    for silver_table in ["employees_tagged", "customers_tagged", "patients_tagged", "transactions_tagged", "users_tagged"]:
        lineage = query_uc_lineage(f"compliance_pack.silver.{silver_table}")
        assert len(lineage["upstream"]) >= 1
        assert any("bronze" in u for u in lineage["upstream"])

def test_lineage_silver_to_compliance():
    # personal_data_register view must have lineage from silver.pii_findings
    lineage = query_uc_lineage("compliance_pack.compliance.personal_data_register")
    assert "compliance_pack.silver.pii_findings" in lineage["upstream"]
```

**Pass criteria**: every lineage check passes.

### Test INT-05 · Immutability of consent log

**Purpose**: verify no UPDATE or DELETE has occurred on `consent_events_log`.

```python
def test_consent_log_append_only():
    history = spark.sql("""
        DESCRIBE HISTORY compliance_pack.compliance.consent_events_log
    """).collect()
    for entry in history:
        assert entry["operation"] in ("CREATE TABLE", "WRITE", "STREAMING UPDATE"), \
            f"Non-append operation found: {entry['operation']} at {entry['timestamp']}"
```

**Pass criteria**: no DELETE, UPDATE, MERGE operations ever appear in the Delta history.

### Test INT-06 · Tag application

**Purpose**: verify Unity Catalog column tags were applied for all high-confidence findings.

```python
def test_uc_tags_applied():
    tags = spark.sql("""
        SELECT table_name, column_name, tag_name, tag_value
        FROM system.information_schema.column_tags
        WHERE catalog_name = 'compliance_pack'
          AND schema_name = 'silver'
          AND tag_name = 'pii_type'
    """).collect()
    findings = spark.sql("""
        SELECT table_name, column_name
        FROM compliance_pack.silver.pii_findings
        WHERE confidence >= 0.85
    """).collect()
    tag_keys = {(t.table_name, t.column_name) for t in tags}
    finding_keys = {(f.table_name, f.column_name) for f in findings}
    # Every high-confidence finding should have a tag
    assert finding_keys.issubset(tag_keys), f"Missing tags for: {finding_keys - tag_keys}"
```

**Pass criteria**: every high-confidence finding has a corresponding UC column tag.

## 8.4 · Day 7 checkpoint

By Day 7, Modules 01 and part of 02 must be at demonstrable state. The checkpoint notebook `tests/day_07_checkpoint.md` walks through:

1. Run INT-01 (register completeness) — must pass
2. Run INT-04 (lineage visibility) — must pass
3. Run INT-06 (UC tags) — must pass
4. Manually inspect `compliance_pack.compliance.personal_data_register` with the human collaborator
5. Manually inspect Unity Catalog lineage graph for one Silver table
6. Confirm Lakebase is provisioned and `consent_events` table schema is deployed (but may be empty)

**If the checkpoint fails**: stop all forward work, diagnose with the human collaborator, use the Day 6 buffer to catch up. Do NOT proceed to Day 8 work with failing Day 7 tests.

## 8.5 · Day 14 demo script

The demo script is a single notebook `tests/day_14_demo_script.md` that walks the stakeholder group through the three artifacts in a specific sequence. It must:

- Run end to end in under 8 minutes
- Require no manual intervention
- Produce the three stakeholder-visible artifacts as live queries and file downloads
- End with a "questions the stakeholders might ask" appendix with pre-scripted answers

The exact demo sequence:

**[0:00 - 0:30] Opening: "Three things this platform proves today"**
- Brief scope statement; set expectations

**[0:30 - 3:00] Artifact 1: The personal data register**
- Run the register query, walk through the findings
- Show the Unity Catalog lineage graph for one Silver table
- Show the UC column tags on `employees_tagged.aadhaar_number`
- Answer the CCO's question: "if a new PII-bearing column appears in my source, what happens?"

**[3:00 - 5:30] Artifact 2: Consent log with live withdrawal**
- Show the consent_events_log with the 1,000 events
- Execute a withdrawal for a demo principal
- Watch propagation to the Gold view in real time (< 5 min SLA)
- Show the Delta history proving append-only

**[5:30 - 7:30] Artifact 3: DSR end-to-end**
- Submit a DSR for `customer_04217`
- Walk through the discovery output
- Show the erasure certificate, retention schedule, audit trail
- Run the time-travel proof: "here's the data before; here's the data after"

**[7:30 - 8:00] Close: "what's NOT in this POC, by design"**
- Recap the Phase 1 scope
- Invite stakeholder questions

## 8.6 · Pre-scripted stakeholder questions

Each persona is likely to ask a specific probing question. These are the answers:

**CCO**: "Does this register update automatically as source systems change?"
- Yes — when the classification job runs on a schedule (e.g., daily), new columns are auto-detected and the register updates. In Phase 1 we'd also add Delta CDC triggers for real-time detection on schema changes.

**GC**: "If a customer disputes their consent three years from now, how do I prove what they agreed to?"
- Delta time travel plus the `notice_version_id` FK on every consent event. I can reproduce the exact notice text they saw on the exact day in their exact language.

**CMO**: "What happens if marketing sends a campaign to someone who withdrew 10 minutes ago?"
- The `marketing_eligible_principals` view filters withdrawn principals in under 5 minutes. For a 10-minute-old withdrawal, the campaign would not include them. For sub-5-minute SLAs in Phase 1, we'd add a direct Lakebase read path for last-mile verification.

**CFO**: "What's the cost to operate this at our real scale?"
- The POC runs on ~₹X credits per day. Phase 1 scaling depends on source volume; we'd produce an estimated cost model after Day 14 based on the metrics this POC produced.

**CISO** (if present): "How do you know nobody tampered with the audit log?"
- Delta's transaction log is content-addressable. Every operation is cryptographically linked to the previous one. We can detect any post-hoc modification and Unity Catalog's audit layer records every access.

**CTO** (if present): "Why Lakebase instead of a traditional RDBMS?"
- Native Unity Catalog integration, sync-to-Delta baked in, zero separate-system auth to manage. Any other OLTP tier would require a parallel auth/audit/lineage integration.

Now proceed to `09_known_pitfalls.md`.
