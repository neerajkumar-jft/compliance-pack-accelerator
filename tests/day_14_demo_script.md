# Day 14 demo script

> ⚠️ **Pre-build planning document — 14-day sprint has concluded.** This describes the planned demo flow; several segments (DSR-portal app, Lakebase withdrawal sync) don't apply on the free-trial deploy. **For the current demo walkthrough, see [`docs/persona_deploy.md`](../docs/persona_deploy.md) and the persona login test at the end of it.**

The final deliverable. One 45-minute session with CCO, CMO, GC, CFO in the room. The demo itself is 8 minutes of running code; the remaining 37 minutes are stakeholder questions and scope discussion.

## Pre-demo setup — before stakeholders arrive

Complete this checklist 30 minutes before the demo:

- [ ] Workspace UI open to the demo notebook
- [ ] Lakebase connection tested (one quick SELECT)
- [ ] `marketing_eligible_principals` view currently shows `customer_04217` as eligible for `marketing_email`
- [ ] DSR portal UI loaded in a second browser tab (if using Databricks Apps)
- [ ] Unity Catalog Explorer open in a third tab, navigated to `compliance_pack.silver.employees_tagged`
- [ ] Stopwatch or timer ready for the 5-minute withdrawal propagation demo
- [ ] A fallback screenshot deck prepared in case of live issues — see "Fallback" below
- [ ] Credit budget reviewed: ensure enough remaining for the full demo
- [ ] The DSR principal `customer_04217` has NOT been erased yet (if you ran INT-03 in rehearsal, regenerate the data and restore the principal per §10.5 before the demo)

## [0:00 – 0:30] Opening — "Three things this platform proves today"

Say verbatim:

> "Thank you for joining. Over the next 45 minutes I'll demonstrate three artifacts this POC produced in 14 days: a personal data register, a consent log, and a completed data subject rights request. These are the three things that together establish the foundation for DPDP compliance on this platform. The demo itself is 8 minutes of live queries. The remaining time is for your questions and for us to align on the Phase 1 scope.
>
> One framing before we start: everything you'll see uses synthetic data. No real customer records. The platform works identically against real data; we used synthetic because that's the right discipline for a sprint."

## [0:30 – 3:00] Artifact 1 — Personal data register

Run query:
```sql
SELECT
    source_table,
    source_column,
    pii_category,
    pii_type,
    sensitivity_tier,
    classification_confidence,
    data_owner
FROM compliance_pack.compliance.personal_data_register
ORDER BY sensitivity_tier DESC, source_table, source_column;
```

Narrate while stakeholders view the output:

> "This is the register. Every column across 5 source tables, classified by PII category with a confidence score. This view queries Unity Catalog's metadata and our classification findings in real time — it doesn't get stale as the source evolves."

Switch to Unity Catalog Explorer tab. Navigate to `compliance_pack.silver.employees_tagged`. Point to the `aadhaar_number` column's tags.

> "The register is backed by column tags in Unity Catalog. Every downstream query, every masking rule, every access policy can reason about PII using these tags. The tagging is automatic when classification confidence exceeds 85%."

Click on Lineage for the employees_tagged table.

> "Full lineage from source file to Silver. If the GC asks 'where did this Aadhaar column come from?' — we answer in one click."

**Anticipated question** — CCO: "Does this update automatically as source systems change?"
**Answer**: "Yes. The classification job runs on a schedule — daily for POC, hourly in Phase 1. When a new column appears in the source, Auto Loader picks it up, classification tags it, and the register shows it on the next refresh. Phase 1 can add Delta CDC triggers for real-time schema-change detection."

## [3:00 – 5:30] Artifact 2 — Consent log with live withdrawal

Run query:
```sql
SELECT
    channel,
    purpose,
    event_type,
    purpose_grant_status,
    COUNT(*) AS event_count
FROM compliance_pack.compliance.consent_events_log
GROUP BY channel, purpose, event_type, purpose_grant_status
ORDER BY channel, purpose;
```

> "1000 consent events across 4 channels and 6 purposes. Every event carries its notice version, channel, timestamp, and grant status. This is the audit trail the DPBI expects a data fiduciary to maintain."

Run this to show the eligible audience right now:
```sql
SELECT COUNT(*) AS eligible_for_marketing_email
FROM compliance_pack.gold.marketing_eligible_principals
WHERE purpose = 'marketing_email';
```

Note the count. Then execute the live withdrawal. Show the audience the Lakebase INSERT:

```sql
INSERT INTO public.consent_events (
    data_principal_id,
    event_timestamp,
    event_type,
    notice_version_id,
    notice_language,
    channel,
    purpose,
    purpose_grant_status,
    consent_capture_method,
    retention_clock_start,
    retention_duration_days,
    created_by
) VALUES (
    (SELECT principal_id FROM public.data_principals WHERE external_identifier = '<DEMO_CUSTOMER>'),
    now(),
    'withdrawn',
    (SELECT notice_version_id FROM public.notice_versions
        WHERE notice_id = 'marketing_notice' AND language = 'en-IN' AND retired_at IS NULL),
    'en-IN',
    'web',
    'marketing_email',
    'declined',
    'toggle',
    now(),
    0,
    'demo-day-14'
);
```

Start timer. Explain while waiting:

> "That was a single customer withdrawing marketing email consent. Now we wait for propagation. The SLA is under 5 minutes. In Phase 1 with a tuned sync, this will be under 30 seconds."

Poll the Gold view every 30 seconds:
```sql
SELECT COUNT(*) FROM compliance_pack.gold.marketing_eligible_principals
WHERE data_principal_id = '<DEMO_CUSTOMER_UUID>' AND purpose = 'marketing_email';
```

When it returns 0, note elapsed time.

> "Propagated in [N] seconds. The next marketing campaign run won't include this customer."

Show the immutability property:
```sql
DESCRIBE HISTORY compliance_pack.compliance.consent_events_log LIMIT 5;
```

> "Every operation on the consent log is append-only. No UPDATEs, no DELETEs. The Delta transaction log is content-addressed, so even post-hoc modification would be detectable. This is what makes the log regulator-grade."

**Anticipated question** — GC: "If a customer disputes three years from now, how do I prove what they agreed to?"
**Answer**: "Every event carries a `notice_version_id` foreign key. `notice_versions` preserves the exact text, hash, and language of every notice ever shown. Delta's time travel lets me query the consent log as of any date. Combined, I can reproduce the exact notice this person saw on the exact day in their exact language."

## [5:30 – 7:30] Artifact 3 — DSR end-to-end

Switch to the DSR portal tab (or run via API notebook). Submit:
```json
{
  "principal_identifier": "customer_04217",
  "identifier_type": "external_id",
  "request_type": "combined",
  "verification_token": "<pre-generated stub token>",
  "requester_contact": {"email": "<customer_04217's email from manifest>", "preferred_language": "en-IN"}
}
```

> "This is a combined access and erasure request from customer_04217. The stub IDV verified their email. The SLA timer starts now — target is under 30 days, we'll finish in minutes."

Wait for status: completed. Walk through the discovery:
```sql
SELECT * FROM compliance_pack.compliance.dsr_requests
WHERE request_id = '<returned request_id>';
```

Navigate to the bundle path and open each file:

1. `data_export.json` — show the full data footprint (1 customer row, 1 user row, N transactions, 4 consent events)
2. `erasure_certificate.pdf` — show the tables erased and the 1 scheduled residual
3. `retention_schedule.pdf` — show the 2033 purge date
4. `audit_trail.json` — show the timestamped action sequence

Then run the time-travel proof:
```sql
-- Before erasure
SELECT COUNT(*) FROM compliance_pack.silver.customers_tagged VERSION AS OF <version_before>
WHERE customer_id = 'customer_04217';
-- Expected: 1

-- After erasure
SELECT COUNT(*) FROM compliance_pack.silver.customers_tagged
WHERE customer_id = 'customer_04217';
-- Expected: 0
```

> "Before the erasure the customer had 1 row. After the erasure, 0. The time-travel query confirms the deletion is real, not just a flag. The VACUUM removed the underlying files. The customer's data is gone. The transaction records remain under retention obligation; the residual register tracks when they'll be purged."

**Anticipated question** — GC: "What if the customer comes back and says 'that wasn't me'?"
**Answer**: "The stub IDV is exactly that — a stub. Phase 1 integrates with a real identity verification provider. Step-up auth for erasure. For this POC the IDV limitation is explicit; the point here is to prove the discovery, execution, and audit trail work end-to-end."

## [7:30 – 8:00] Close — "What this POC does not do"

Say verbatim:

> "Three things this POC deliberately does not do. First, Zone 1 — we don't scan SharePoint or email. That requires a GC-signed exclusion policy for privileged content, which is Phase 1 work. Second, breach detection. Module 04 needs Lakewatch, which is Private Preview. Phase 5 territory. Third, the full DPIA generation. Module 05 needs multiple modules producing signal at once. Phase 3.
>
> What this POC DOES prove: the schemas hold up, the classification works, the consent log is append-only and fast to propagate withdrawals, the DSR flow produces regulator-grade evidence. The foundation is real.
>
> I'll hand back to [CCO/sponsor] to lead the Phase 1 scope discussion."

## Remaining 37 minutes — stakeholder Q&A

Expected questions per persona with pre-scripted answers in §8.6. Have that section open on a second screen.

## Fallback if live demo fails

Prepare a 6-slide backup deck with screenshots of each artifact query's output, taken during Day 13 rehearsal. If a query fails live, pivot to the slide calmly:

> "Let me show you the expected output from rehearsal while I diagnose the live issue."

Avoid the impulse to debug live in front of stakeholders; it eats the Q&A time. Diagnosis happens after the session.

## Post-demo

Within 24 hours:
- Capture the credit consumption summary
- Document any unresolved questions from stakeholders
- Produce the signed "POC complete" memo per §1.4 exit criteria
- Begin Phase 1 scope confirmation
