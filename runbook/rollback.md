# Rollback procedures

> ⚠️ **Pre-build planning document.** Rollbacks 3 (Lakebase) and 4 (DSR principal restoration via synthetic-data regen) reference components not used in the free-trial POC. Rollback 5 (Full workspace reset via `DROP CATALOG compliance_pack CASCADE`) is still accurate. **For cleanup of the persona layer specifically, see [`docs/persona_deploy.md`](../docs/persona_deploy.md#cleaning-up--starting-over).**

Concrete recovery playbooks. Each procedure is self-contained — copy the commands, run them, verify. Cross-references §10.2 through §10.7 of the main spec.

## When to use this file

You are here because something went wrong and you need to get back to a known-good state. Before rolling back, first check `runbook/troubleshooting.md` — if your failure has a named fix there, use that. Rollback is a heavier intervention; use it when a targeted fix isn't obvious or when you've tried the targeted fix and it didn't stick.

## Rollback 1 — Bronze ingestion only

**Use when**: Bronze tables have wrong data, duplicate rows, schema mismatches from a bad source file.

**Preserves**: Silver, Gold, Lakebase, compliance views.

**Time cost**: ~10 minutes.

```sql
-- Drop all Bronze tables
DROP TABLE IF EXISTS compliance_pack.bronze.source_employees;
DROP TABLE IF EXISTS compliance_pack.bronze.source_customers;
DROP TABLE IF EXISTS compliance_pack.bronze.source_patients;
DROP TABLE IF EXISTS compliance_pack.bronze.source_transactions;
DROP TABLE IF EXISTS compliance_pack.bronze.source_users;
```

```python
# Remove Auto Loader state
dbutils.fs.rm("/Volumes/compliance_pack/bronze/_checkpoints/", recurse=True)
dbutils.fs.rm("/Volumes/compliance_pack/bronze/_schemas/", recurse=True)
```

```sql
-- Recreate Bronze DDL
-- (run schemas/bronze.sql contents)
```

Then rerun Auto Loader ingestion per §3.4. Verify:
```sql
SELECT COUNT(*) FROM compliance_pack.bronze.source_employees;
-- Expected: ~2000 (matches manifest)
```

## Rollback 2 — Classification results only

**Use when**: `pii_findings` has incorrect classifications, or UC column tags are wrong.

**Preserves**: Bronze, Silver table data, Lakebase, compliance infrastructure.

**Time cost**: ~5 minutes plus classification re-run.

```sql
-- Identify the bad scan_job_id
SELECT scan_job_id, discovered_at, COUNT(*) AS findings_count
FROM compliance_pack.silver.pii_findings
GROUP BY scan_job_id, discovered_at
ORDER BY discovered_at DESC;

-- Delete the bad scan's findings
DELETE FROM compliance_pack.silver.pii_findings
WHERE scan_job_id = '<bad job id>';

DELETE FROM compliance_pack.silver.discovered_tables
WHERE scan_job_id = '<bad job id>';
```

Remove UC tags applied by that scan. You need the list of (table, column) pairs that got tagged — either iterate findings or just clear all pii_type tags and re-apply:

```python
# Clear pii_type tags across all Silver tables
silver_tables = ['employees_tagged', 'customers_tagged', 'patients_tagged',
                 'transactions_tagged', 'users_tagged']
for tbl in silver_tables:
    cols = spark.sql(f"""
        SELECT column_name FROM system.information_schema.column_tags
        WHERE catalog_name = 'compliance_pack'
          AND schema_name = 'silver'
          AND table_name = '{tbl}'
          AND tag_name IN ('pii_type', 'pii_category', 'sensitivity',
                           'classifier_source', 'dpdp_applicable')
    """).collect()
    for col_row in cols:
        col = col_row.column_name
        spark.sql(f"""
            ALTER TABLE compliance_pack.silver.{tbl}
            ALTER COLUMN {col}
            UNSET TAGS ('pii_type', 'pii_category', 'sensitivity',
                        'classifier_source', 'dpdp_applicable')
        """)
```

Then re-run the classification job with the fixed pattern library or fixed logic. Verify per INT-01 and INT-06.

## Rollback 3 — Lakebase schema reset

**Use when**: Lakebase tables have wrong DDL, constraints misbehaving, or sync producing errors.

**Preserves**: Bronze, Silver data; all Delta tables on the Databricks side.

**Time cost**: ~15 minutes including regeneration of the 1,000 events.

```sql
-- In Lakebase (connect via the configured auth)
DROP TABLE IF EXISTS public.dsr_requests CASCADE;
DROP TABLE IF EXISTS public.consent_events CASCADE;
DROP TABLE IF EXISTS public.notice_versions CASCADE;
DROP TABLE IF EXISTS public.data_principals CASCADE;
```

Recreate from `schemas/consent_events.sql` and `schemas/notice_versions.sql`.

The Lakebase → Delta sync will recreate the Delta destination tables automatically, but the existing Delta tables on the Databricks side must also be dropped or they'll have stale schemas:

```sql
-- On Databricks side
DROP TABLE IF EXISTS compliance_pack.compliance.consent_events_log;
DROP TABLE IF EXISTS compliance_pack.compliance.dsr_requests;
```

Reconfigure the sync per §5.7.2. Re-seed the notice version. Re-run the consent event generator for 1,000 events per §6.6.

## Rollback 4 — DSR principal restoration

**Use when**: you ran INT-03 against `customer_04217` but need to demo the same principal again on Day 14.

**Preserves**: Everything except the erased records.

**Time cost**: ~20 minutes.

Since DSR erasure is (by design) irreversible once VACUUM has run, the only restoration path is to regenerate the whole synthetic dataset. Because the generator is deterministic, it will produce the same `customer_04217` footprint it produced originally.

```bash
# From outside the workspace or from a notebook
python generate_synthetic_data.py \
    --output-dir /Volumes/compliance_pack/bronze/landing/ \
    --seed 42
```

Then rollback 1 (Bronze) and rollback 2 (classification) to force re-ingest and re-classify.

If the residual_retention_register entry from the prior DSR run still exists, clear it:
```sql
DELETE FROM compliance_pack.compliance.residual_retention_register
WHERE principal_identifier = 'customer_04217';
```

## Rollback 5 — Full workspace reset

**Use when**: workspace state is hopelessly confused; multiple previous rollbacks haven't produced a clean state.

**Preserves**: Nothing except the workspace itself.

**Time cost**: ~45 minutes including regeneration and re-classification.

```sql
-- Nuclear option
DROP CATALOG IF EXISTS compliance_pack CASCADE;
```

```sql
-- In Lakebase
DROP TABLE IF EXISTS public.dsr_requests CASCADE;
DROP TABLE IF EXISTS public.consent_events CASCADE;
DROP TABLE IF EXISTS public.notice_versions CASCADE;
DROP TABLE IF EXISTS public.data_principals CASCADE;
```

Rebuild from Day 0 checklist in `setup_day_00.md`. Do not skip any setup step. Re-run verify_environment.

## Checkpoint after every rollback

After any rollback, run the tests appropriate to the scope of the rollback:

| Rollback | Tests to run |
|----------|--------------|
| 1 (Bronze) | INT-01, INT-04 |
| 2 (Classification) | INT-01, INT-04, INT-06 |
| 3 (Lakebase) | INT-02, INT-05 |
| 4 (DSR principal) | INT-03 + the DSR principal verification |
| 5 (Full reset) | All integration tests in sequence; Day 7 checkpoint |

Do not proceed with further build work until the relevant tests pass.

## What rollback does not fix

Rollback is mechanical state restoration. It does not fix:
- Bugs in the pattern library (fix `schemas/pii_patterns.py`, then rollback 2)
- Bugs in the synthetic data generator (fix the generator, then rollback 4)
- Missing grants on the service principal (fix the grants per §2.3, then retry)
- Lakebase instance-level problems (address at the infrastructure level, not via DDL)
- Trial credit exhaustion (no technical fix; escalate to Databricks account team)

If you find yourself repeatedly rolling back without the underlying issue resolving, stop and raise with the human collaborator per §10.8.
