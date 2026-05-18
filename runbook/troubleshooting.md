# Troubleshooting guide

> ⚠️ **Pre-build planning document.** The Lakebase section and references to tables like `discovered_tables` / `residual_retention_register` (which were planned but not built) don't apply to the current free-trial POC. The Permission errors and Auto Loader sections are still accurate. **For current deploy issues, see [`docs/persona_deploy.md`](../docs/persona_deploy.md#troubleshooting).**

Symptom-first index of common problems. Each entry identifies the symptom, the root cause, and the fix. Complements `10_runbook.md` §10.6 (the same error-to-cause map in table form).

## How to use this guide

When you hit an error, search this file for a phrase from the error message first. If you find a match, follow the fix. If you don't find a match, check §10.6 of the spec; if that's also empty, stop and raise with the human collaborator per §10.8.

When you resolve a new failure mode not listed here, *add it to this file* before moving on. The spec gets better with every sprint.

## Permission errors

### `Permission denied: APPLY TAG on catalog 'compliance_pack'`

**Cause**: the service principal was granted table creation but not tagging.

**Fix**:
```sql
GRANT APPLY TAG ON CATALOG compliance_pack TO `compliance-pack-builder`;
```

If the error persists after grant, tags may need up to 60 seconds to propagate. Wait one minute, retry.

### `Permission denied: CREATE TABLE on schema compliance_pack.silver`

**Cause**: missing `CREATE TABLE` grant on the schema.

**Fix**: review §2.3 — apply the full grant block, not just individual statements.

### `PERMISSION_DENIED: User does not have USE SCHEMA permission`

**Cause**: grants were applied at catalog level but `USE SCHEMA` wasn't explicit.

**Fix**:
```sql
GRANT USE SCHEMA ON SCHEMA compliance_pack.bronze TO `compliance-pack-builder`;
-- Repeat for silver, gold, compliance
```

## Auto Loader issues

### `java.lang.IllegalStateException: Checkpoint location ... already exists`

**Cause**: a prior run left a checkpoint directory; Auto Loader refuses to reuse it with inconsistent metadata.

**Fix**: rollback per §10.2 — drop the Bronze table AND remove the checkpoint dir together.

### `AnalysisException: Unable to infer schema for CSV`

**Cause**: empty or malformed CSV file landed in the ingestion path.

**Fix**:
1. Check the file exists and is non-empty: `dbutils.fs.ls(path)`
2. If the file is truly malformed, the synthetic generator is buggy — re-run per §10.5
3. If the file looks fine, `cloudFiles.inferColumnTypes` may need to be explicitly set to `false` per §3.4

### Files not getting ingested despite being in landing zone

**Cause**: checkpoint state says the files have already been processed.

**Fix**: reset checkpoint per §10.2. Do NOT delete the landing zone files themselves.

### `rescuedDataColumn` contains data unexpectedly

**Cause**: a source column's values don't match the Bronze STRING column (e.g., embedded newlines in a quoted field).

**Fix**: this is usually a generator bug. Confirm the generator writes RFC 4180-compliant CSVs per §3.2. Run `zcat file.csv.gz | head -3` to spot-check.

## Classification issues

### `pii_findings` table has unexpected false positives

**Cause**: a regex is too liberal, or a column hint is catching unrelated columns.

**Fix**: for a specific false positive, check the entry in `pii_findings`:
- If `classifier_source = 'regex'` only (no column hint match), the regex is too broad → tighten the regex in `schemas/pii_patterns.py`
- If `classifier_source = 'column_hint'`, the hint is matching an unintended substring → refine the hint list
- After fixing the pattern library, re-run classification and overwrite the findings for that scan_job_id (see §10.3)

### Columns that should be PII are not flagged

**Cause**: either the pattern library doesn't cover the type, or confidence fell below the 0.65 review threshold.

**Fix**:
1. Check `compliance_pack.silver.pii_findings` for the column — if `confidence < 0.65` the row is there but below threshold; inspect and decide whether to manually override per §4.8
2. If the column is absent entirely, the pattern library needs an addition. Add a new `PIIPattern` instance in `schemas/pii_patterns.py`, re-run classification.

### `ai_classify` returning unexpected labels

**Cause**: the label set passed to `ai_classify` didn't include an appropriate category, or the input text is too short/too long.

**Fix**:
1. Review the label set in §4.5.2 — ensure it covers the realistic cases
2. For very long text, truncate to ~500 characters before calling `ai_classify`
3. Add an `other` or `non_applicable` category so the model has a safe default

### Classification job timing out

**Cause**: scan is iterating too many (column, pattern) pairs, or `ai_classify` is rate-limited.

**Fix**:
1. Confirm the Spark SQL pattern from §4.5.1 is being used (not the anti-pattern in §9.1.2)
2. For `ai_classify`, apply `LIMIT 100` on the samples passed per column
3. If still slow, split the classification into per-table jobs running sequentially

## Lakebase issues

### `connection refused` when writing to Lakebase

**Cause**: Lakebase instance not running, or network policy blocks access.

**Fix**:
1. Check Lakebase instance status in the Databricks UI; restart if stopped
2. Ensure the cluster and Lakebase instance are in the same workspace
3. Verify secret `compliance-pack/lakebase-token` is valid if using token auth

### `duplicate key violates unique constraint "idx_notice_currently_live"`

**Cause**: attempt to insert a second "currently live" (retired_at IS NULL) notice for the same notice_id + language.

**Fix**: the old version must be retired first:
```sql
UPDATE public.notice_versions SET retired_at = now()
WHERE notice_id = 'marketing_notice' AND language = 'en-IN' AND retired_at IS NULL;
```
Then insert the new version.

### `consent_events` INSERT rejected by rule

**Cause**: you attempted an UPDATE or DELETE; the table is append-only per §5.10.

**Fix**: withdrawals are new events with `event_type='withdrawn'`, not UPDATEs to the granted row. Modifications are new events that reference `superseded_by_event_id`.

### Lakebase → Delta sync stuck or lagging

**Cause**: sync job paused, or high workspace load.

**Fix**:
1. Check sync status in the Databricks UI under Lakebase → your instance → Sync tables
2. Force a manual refresh if supported
3. If persistent lag > 5 min, raise with the Databricks account team

## Delta issues

### `DELTA_TABLE_NOT_FOUND: Table or view not found`

**Cause**: the table hasn't been created, or the catalog/schema name is wrong.

**Fix**: run the DDL from `schemas/` — verify the catalog is `compliance_pack` and the schema is one of bronze/silver/gold/compliance.

### `VACUUM` refuses to run with RETAIN 0 HOURS

**Cause**: safety check `spark.databricks.delta.retentionDurationCheck.enabled` is true.

**Fix**: temporarily disable per §7.6 (and re-enable after):
```python
spark.conf.set("spark.databricks.delta.retentionDurationCheck.enabled", "false")
spark.sql("VACUUM compliance_pack.silver.customers_tagged RETAIN 0 HOURS")
spark.conf.set("spark.databricks.delta.retentionDurationCheck.enabled", "true")
```

Only do this for the specific tables being erased as part of a DSR.

### `VERSION AS OF` query fails with "file not found"

**Cause**: the requested version's files were vacuumed.

**Fix**: you ran VACUUM before capturing the time-travel snapshot. The test must capture the "before" version *first*, then VACUUM, not the other way around. Re-run the synthetic data generator (§10.5) and redo the test in the correct order.

## Synthetic data issues

### `customer_04217` not found after generator run

**Cause**: generator is non-deterministic, OR the seed was changed, OR the number of customers is below 4,218.

**Fix**:
1. Verify `SEED = 42` in the generator
2. Verify `NUM_CUSTOMERS = 5000` in the generator
3. Re-run the generator twice and diff: `diff -r /tmp/run1 /tmp/run2` must produce no differences
4. If differences, the generator is using some non-deterministic input — check for `datetime.now()` or set iteration

### Manifest row counts don't match actual files

**Cause**: the generator wrote the manifest from its target counts rather than actual counts (bug).

**Fix**: the generator's final step must read back actual row counts from each written file and write the manifest based on those, per §6.8.

## Test failures

### INT-01 returns fewer than 20 findings

**Cause**: classification didn't run against all 5 tables, or confidence threshold is filtering too many.

**Fix**:
1. Check `compliance_pack.silver.discovered_tables` — should have 5 rows
2. Check each table has `pii_column_count > 0`
3. If a table was skipped, re-run classification for that specific table

### INT-02 times out (propagation > 5 min)

**Cause**: Lakebase → Delta sync lag, or the Gold view is caching.

**Fix**:
1. Check sync status in UI
2. Gold views are not cached by default, but the notebook kernel might be — restart the kernel and retry
3. If sync genuinely slow, fall back to Day 13 rehearsal — live demo may show > 5 min propagation; have slide fallback ready

### INT-03 bundle missing files

**Cause**: DSR execution failed partway through.

**Fix**: check `compliance_pack.compliance.dsr_requests` for the request_id — the `status` and `next_action` columns tell you where execution stopped. Review the `audit_trail.json` if the bundle path is populated. Common stop points: identity verification failure, legal hold check failure (shouldn't happen in POC), VACUUM failure (see §7.6 retention override).

### INT-05 shows UPDATE or DELETE operations on consent_events_log

**Cause**: something ran a non-append operation; this should be impossible if the Lakebase rules from `consent_events.sql` are applied.

**Fix**: this is a serious bug. Check:
1. Did the Lakebase rules actually apply? Re-run `schemas/consent_events.sql`.
2. Did something write directly to the Delta table bypassing the sync? The Delta table is sync-maintained and should not have direct writes.

If neither of those, stop and raise with the human collaborator immediately — this is a platform-integrity failure.

## When to stop and escalate

Per §10.8, stop immediately if:
- You hit an error not in this file AND not in §10.6
- You are about to install a Python database/SaaS connector
- You are about to skip a test
- You are about to work around a permission error by using a different service principal
- Credit burn exceeds 60% by Day 7

The cost of stopping and asking is minutes. The cost of proceeding on a misread is a day or more.
