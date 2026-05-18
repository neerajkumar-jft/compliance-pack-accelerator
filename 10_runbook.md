# §10 · Operational runbook

> ⚠️ **Pre-build planning document.** Procedures referencing Lakebase, the DSR-portal app, or jobs like `process_dsr_request`/`sync_consent_to_delta` don't apply on the free-trial deploy path. **For current ops, see [`docs/persona_deploy.md`](docs/persona_deploy.md) and `runbook/` (with its own pre-build banners).**

## 10.1 · What this runbook is for

This section is where you come when something breaks. Every procedure here is a specific recovery path you can execute without further spec interpretation — copy the commands, run them, verify. When a new failure mode is discovered during the sprint, add its recovery procedure here so that it is captured for the next run.

## 10.2 · Rollback: "my ingestion is corrupted"

**Symptom**: Bronze tables have wrong data, duplicate rows, or schema mismatches.

**Recovery**:
```sql
-- 1. Drop the Bronze tables
DROP TABLE IF EXISTS compliance_pack.bronze.source_employees;
DROP TABLE IF EXISTS compliance_pack.bronze.source_customers;
DROP TABLE IF EXISTS compliance_pack.bronze.source_patients;
DROP TABLE IF EXISTS compliance_pack.bronze.source_transactions;
DROP TABLE IF EXISTS compliance_pack.bronze.source_users;
```

```bash
# 2. Remove the Auto Loader checkpoints
dbutils.fs.rm("/Volumes/compliance_pack/bronze/_checkpoints/", recurse=True)

# 3. Remove the schema inference state
dbutils.fs.rm("/Volumes/compliance_pack/bronze/_schemas/", recurse=True)
```

```python
# 4. Re-run the ingestion from schemas/bronze.sql and the Auto Loader config in §3.4
# (The landing zone files are unchanged; Auto Loader will re-ingest them fresh.)
```

Verify:
```sql
SELECT COUNT(*) FROM compliance_pack.bronze.source_employees;
-- Expected: 2000
```

## 10.3 · Rollback: "my classification produced bad tags"

**Symptom**: UC column tags are wrong, or `pii_findings` has incorrect classifications.

**Recovery**:
```sql
-- 1. Clear the findings for the bad scan_job_id
DELETE FROM compliance_pack.silver.pii_findings
WHERE scan_job_id = '<the bad job id>';

-- 2. Remove UC tags applied by that job
-- Loop over the findings from the bad job and call ALTER TABLE ... UNSET TAGS
```

```python
# Example: remove pii_type tag from a column
spark.sql("""
    ALTER TABLE compliance_pack.silver.employees_tagged
    ALTER COLUMN aadhaar_number
    UNSET TAGS ('pii_type', 'pii_category', 'sensitivity', 'classifier_source', 'dpdp_applicable')
""")
```

```python
# 3. Re-run the classification job from §4.5 with corrected logic
```

Verify:
```sql
SELECT COUNT(*) FROM compliance_pack.compliance.personal_data_register;
-- Expected: ≥ 20 (post-re-scan)
```

## 10.4 · Rollback: "Lakebase schema is wrong"

**Symptom**: Lakebase tables have wrong columns or constraints; sync is producing errors.

**Recovery**:
```sql
-- Warning: this drops all consent events; only safe in POC
DROP TABLE IF EXISTS public.consent_events CASCADE;
DROP TABLE IF EXISTS public.notice_versions CASCADE;
DROP TABLE IF EXISTS public.data_principals CASCADE;
DROP TABLE IF EXISTS public.dsr_requests CASCADE;

-- Recreate from the correct DDL in schemas/
-- Re-seed notice_versions (Day 8)
-- Re-run the consent event generator (Day 9)
```

The Lakebase→Delta sync will recreate the Delta tables automatically.

## 10.5 · Rollback: "the DSR demo target has drifted"

**Symptom**: `customer_04217`'s data footprint doesn't match the expected manifest.

**Recovery**:
```bash
# 1. Regenerate synthetic data with same seed
python generate_synthetic_data.py --output-dir /Volumes/compliance_pack/bronze/landing/ --seed 42

# 2. Truncate Bronze and re-ingest (per §10.2)

# 3. Re-run classification (per §10.3)

# 4. Re-generate the 1000 consent events
```

If the manifest says `customer_04217` should have 14 transactions but your generated data has 12, the generator is non-deterministic. Fix the generator before anything else.

## 10.6 · Error-to-cause map

| Error message | Likely root cause | Fix |
|---------------|-------------------|-----|
| `Permission denied: 'APPLY TAG' on catalog 'compliance_pack'` | Service principal missing `APPLY TAG` | `GRANT APPLY TAG ON CATALOG compliance_pack TO '<sp>'` |
| `Table or view not found: compliance_pack.silver.pii_findings` | Silver DDL not applied | Run `schemas/silver.sql` |
| `Cannot find Lakebase instance 'compliance-pack-consent'` | Instance not provisioned | Create per §2.4 |
| `ai_classify is not available in this workspace` | Feature not enabled or wrong region | Check workspace settings; region must support AI functions |
| `Auto Loader: file already processed` | Stale checkpoint | Delete checkpoint dir and retry (per §10.2) |
| `Delta VACUUM: retention too short` | Safety check enabled | Disable per §9.5.1 temporarily |
| `Cannot DELETE from streaming source` | Trying to DELETE from a table being streamed into | Stop the stream, DELETE, restart |
| `Column tag already exists` | Running classification twice without clearing | Rollback per §10.3 |
| `No rows found: customer_04217` | Ingestion didn't complete or principal ID mismatch | Check manifest; rerun generator |
| `Lakebase sync lag > 5 minutes` | Workspace load or misconfigured refresh interval | Check sync config; if persistent, raise with human collaborator |
| `regexp_extract produced unexpected match` | Regex too liberal | Tighten pattern per §9.7 |
| `Delta time travel fails: files not found` | VACUUM ran with RETAIN 0 before snapshot captured | Capture time-travel snapshot BEFORE VACUUM |

## 10.7 · Complete data reset

If the workspace state is hopelessly confused, reset everything:

```sql
-- Nuclear reset — drops all POC state
DROP CATALOG IF EXISTS compliance_pack CASCADE;
```

```bash
# Remove all Lakebase tables (must drop from Lakebase console or via SQL)

# Recreate from scratch per §2.3, §2.4
```

Time cost of a full reset: ~30 minutes including re-running generator and re-classifying. Only do this as a last resort.

## 10.8 · When to stop and raise with human collaborator

Stop immediately and raise with the human reviewer if:

- An error message contains `PERMISSION_DENIED` from Unity Catalog and the grants in §2.3 appear to be in place
- Lakebase sync consistently lags more than 5 minutes after 3 test cycles
- Any test in `tests/` fails with a message you cannot locate in the error-to-cause map above
- You find yourself wanting to install a Python database/SaaS connector (per §9.1.1)
- You discover a feature listed in §9.2.2 is unexpectedly unavailable
- Credit burn exceeds 60% of trial budget by Day 7
- Any operation produces an artifact that looks like real customer PII rather than synthetic

**Do not work around** issues in these categories silently. They indicate either a spec gap or a workspace configuration problem that must be understood before proceeding.

## 10.9 · Daily end-of-day checklist

At the end of each build day, run this checklist and post the summary to the human collaborator:

- [ ] What did I build today, referencing specific spec sections?
- [ ] What tests did I run? Which passed? Which failed?
- [ ] Current credit consumption (% of trial budget)
- [ ] Any deviations from the spec? If so, what, why, and did I update the spec?
- [ ] Any items from the non-scope list (§1.4) that I almost built? How did I prevent it?
- [ ] What is the Day N+1 plan? Which spec sections am I consulting?
- [ ] Any risks I see for tomorrow's work?

A rolling day-end summary lets the human collaborator catch drift early and lets you start the next day with a clear plan.

## 10.10 · Post-demo cleanup

After the Day 14 demo:

```sql
-- Optional: export the final state for reference
CREATE TABLE compliance_pack.compliance.demo_snapshot_register
AS SELECT * FROM compliance_pack.compliance.personal_data_register;

CREATE TABLE compliance_pack.compliance.demo_snapshot_consent
AS SELECT * FROM compliance_pack.compliance.consent_events_log;
```

Do NOT delete the workspace state immediately — the Phase 1 team may want to reference the built artifacts for a day or two after the demo. Coordinate with the human reviewer on the cleanup timing.

End of the ten core sections. Supporting material in `schemas/`, `synthetic_data/`, `tests/`, `runbook/`, and `reference/` subdirectories.
