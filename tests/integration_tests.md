# Integration tests · executable layout

> ⚠️ **Pre-build planning document.** INT-02 / INT-03 / INT-05 in their Lakebase form don't apply on free-trial. **For the active test suite, see [`docs/how_to_test.html`](../docs/how_to_test.html) (12 sections incl. negative Genie tests, CDF withdrawal propagation) and `tests/persona_boundary_test.py`.**

Integration tests run against the live Databricks workspace, Lakebase instance, and deployed Gold views. They are the core contract of what "working" means for the POC. Full test narratives are in §8.3 of the main spec; this file gives the executable structure.

## Running the integration tests

```bash
# Requires workspace credentials configured (Databricks CLI)
databricks workspace import tests/integration/ ... --language PYTHON

# Or, run as a job
databricks jobs run-now --job-id <integration-tests-job>
```

Integration tests are NOT idempotent in the weak sense — they create data, run operations, and verify outcomes. Running them repeatedly works, but each run consumes workspace credits and leaves audit-log entries. During build, run them after each task; never run them on a schedule.

## Test-to-spec map

| Test ID | File | Spec section | Day run by |
|---------|------|--------------|------------|
| INT-01 | test_register_completeness.py | §8.3 INT-01 | Day 5 |
| INT-02 | test_withdrawal_propagation.py | §8.3 INT-02 | Day 10 |
| INT-03 | test_dsr_end_to_end.py | §8.3 INT-03 | Day 12 |
| INT-04 | test_uc_lineage.py | §8.3 INT-04 | Day 5 |
| INT-05 | test_consent_log_append_only.py | §8.3 INT-05 | Day 10 |
| INT-06 | test_uc_tags_applied.py | §8.3 INT-06 | Day 4 |

## Pre-flight check before any integration test

Before running any integration test, verify the workspace state:

```python
# Verify the catalog and schemas exist
spark.sql("SHOW CATALOGS").filter("catalog == 'compliance_pack'").count() == 1
spark.sql("SHOW SCHEMAS IN compliance_pack").collect()  # should include bronze, silver, gold, compliance

# Verify the service principal can apply tags
try:
    spark.sql("""
        ALTER TABLE compliance_pack.silver.employees_tagged
        ALTER COLUMN employee_id SET TAGS ('_smoke_test' = 'ok')
    """)
    spark.sql("""
        ALTER TABLE compliance_pack.silver.employees_tagged
        ALTER COLUMN employee_id UNSET TAGS ('_smoke_test')
    """)
except Exception as e:
    raise RuntimeError("Service principal lacks APPLY TAG privilege; see §2.3") from e
```

## Day 7 checkpoint sequence

Run in this order on Day 7. Halt at the first failure.

1. INT-01 (register completeness) — verifies Module 01 output
2. INT-04 (lineage) — verifies UC integration
3. INT-06 (UC tags) — verifies tagging worked

If all three pass, Module 01 is demo-ready. Proceed to Day 8 work.

If any fail, see `10_runbook.md` for recovery procedures.

## Day 14 demo sequence

Run in this order on Day 13 as the rehearsal, and the demo script runs these same tests as validation on Day 14. Each test must pass and also produce a visible artifact the stakeholder sees:

1. INT-01 — shows the live register query output (CCO artifact)
2. INT-05 — shows the consent log's `DESCRIBE HISTORY` (GC defense: "no tampering")
3. INT-02 — live withdrawal with propagation timer running (CMO artifact)
4. INT-03 — live DSR for customer_04217 with bundle output (GC artifact)

## What integration tests cannot replace

- **Human review of classifications** — a confident classifier can still be wrong; a CCO must spot-check
- **Notice text review** — the classifier doesn't read English; Legal must approve notice content
- **Performance at real scale** — POC volumes are deliberately modest
- **Cross-workspace behavior** — POC runs in one workspace

Document these caveats in the Day 14 close; do not try to cover them in integration tests.
