# §1 · Project context and success definition

> ⚠️ **Pre-build planning document.** Describes the 14-day sprint framing. The DPDP substance is still accurate; the Module 01/02 scoping and the Lakewatch/DSR-portal promises were narrowed on the free-trial deploy path. **For deploying today, follow [`docs/persona_deploy.md`](docs/persona_deploy.md).**

## 1.1 · The business problem

The Digital Personal Data Protection Act 2023 obligates every Indian data fiduciary to know what personal data they hold, capture specific consent for each processing purpose, honor data subject rights including erasure, notify the DPBI of breaches within 72 hours, and produce an annual DPIA if designated a Significant Data Fiduciary. Penalties reach ₹250 crore per contravention. Most Indian enterprises today cannot confidently produce their personal data inventory on demand, cannot evidence consent at the granularity the Act requires, and cannot reproduce historical processing decisions under regulator inspection.

This POC builds the foundation for closing that gap. It proves — against a synthetic source system in 14 days — that a Databricks-native platform can produce a living personal data register, an immutable consent log with real-time propagation, and fulfill a data subject rights request end-to-end with auditable evidence. If successful, it is the entry ticket to a Phase 1 build covering the complete six-module platform described in `reference/proposal.pdf`.

## 1.2 · The POC in the context of the six-module platform

The proposal document (`reference/proposal.pdf`) lays out a six-module platform that collectively covers the full DPDP operational envelope:

| Module | Scope | POC status |
|--------|-------|------------|
| **01 · PII inventory** | Living register of every personal data column, document, and field; classification and tagging | **In full POC scope** |
| **02 · Consent intelligence** | Every consent event captured, immutable log, real-time withdrawal propagation | **In full POC scope** |
| 03 · Rights hub | Access, correction, erasure end-to-end with Zone 1 split execution | Minimal stub only; proves the DSR pattern works |
| 04 · Breach detection | 72-hour alerting via Lakewatch; affected-principal lookup | Out of POC scope (Lakewatch is Private Preview) |
| 05 · Compliance audit | Live score, automated DPIA via Agent Bricks | Out of POC scope |
| 06 · Retention and transfers | Policy-driven purge, masking, cross-border monitoring | Out of POC scope; residual register pattern is present |

The POC is *not* a PII scanner. Earlier accelerators in this space (including the sinki.ai DPDP Discovery Accelerator that inspired parts of this spec) focused narrowly on finding PII gaps. That is a useful starting point but leaves the hard work — consent, rights, breach, audit, retention — unaddressed. This POC proves Modules 01 and 02 at sufficient depth that the extensibility into 03-06 is credible, not speculative.

The architectural commitments that make this extensibility real:

- **Unity Catalog as the single governance spine**: every table tagged and lineage-tracked, regardless of which module produces it. Module 03's discovery query (`§7.4`) already uses UC lineage; Modules 05 and 06 will read the same lineage.
- **Lakebase as the OLTP tier for every module that needs one**: consent events in this POC; DSR intake in the minimal stub; Module 04's breach register and Module 06's retention register in Phase 1. Same OLTP tier, same auth, same sync-to-Delta.
- **Databricks Asset Bundle (DAB) as the deployment contract**: every resource declared as code. Adding Modules 03-06 in Phase 1 is adding more `resources/*.yml` files and more pipelines, not re-architecting the deploy path.
- **Lakeflow Declarative Pipelines (DLT) for the medallion**: classification is a DLT table; Module 05's scoring engine will be another DLT table reading the same Silver layer.
- **Schemas designed for the full module set**: the `pii_findings` table has columns for human review (`human_reviewed`, `review_status`) that Module 01 alone doesn't need but Module 05 will use. The consent event schema has a `superseded_by_event_id` column that Module 03 (consent-aware DSR) will reference.

## 1.3 · The three demonstrable artifacts

### Artifact 1 — Personal data register (Module 01 output)

**What it is**: A queryable table in Unity Catalog showing every column across the source system that contains personal data, with its PII category, sensitivity tier, data owner, and lineage to its raw source.

**Where it lives**: `compliance_pack.compliance.personal_data_register` (view over the tagged Silver tables).

**How to verify**:

```sql
SELECT
    source_system,
    source_table,
    source_column,
    pii_category,
    sensitivity_tier,
    classifier_source,
    classification_confidence,
    data_owner,
    last_scanned_at
FROM compliance_pack.compliance.personal_data_register
ORDER BY sensitivity_tier DESC, source_table, source_column;
```

This query must return at least 20 rows (based on the synthetic source schema having roughly that many PII-bearing columns) with non-null values for every column. Every entry must trace back through Unity Catalog lineage to a specific Bronze table and its originating source file.

**The demo story this artifact supports**: "Here is every place we hold personal data in this source system. This is the register the DPBI would ask to see if they asked us about our data inventory today. It updates automatically as new columns arrive."

### Artifact 2 — Consent log with withdrawal propagation (Module 02 output)

**What it is**: An immutable record of at least 1000 consent events in a Delta table, fed from a Lakebase OLTP store, with a demonstrated end-to-end withdrawal that propagates to downstream suppression within 5 minutes.

**Where it lives**:

- Lakebase: `compliance_pack_consent.public.consent_events` (OLTP store)
- Delta: `compliance_pack.compliance.consent_events_log` (immutable audit layer)
- Suppression surface: `compliance_pack.compliance.marketing_eligible_principals` (Gold view)

**How to verify**:

```sql
-- Check event volume and schema completeness
SELECT COUNT(*) AS event_count,
       COUNT(DISTINCT data_principal_id) AS distinct_principals,
       COUNT(DISTINCT purpose) AS distinct_purposes,
       COUNT(DISTINCT channel) AS distinct_channels
FROM compliance_pack.compliance.consent_events_log;
-- event_count >= 1000
-- distinct_principals >= 300 (from spec §6)
-- distinct_purposes = 6
-- distinct_channels >= 4
```

For the withdrawal demonstration, we pick a specific synthetic principal with active marketing consent, withdraw it via the DSR portal, and verify that within 5 minutes:

1. The Lakebase `consent_events` table has the new withdrawal event
2. The Delta `consent_events_log` has synced the event
3. The `marketing_eligible_principals` view no longer includes this principal

The test for this is in `tests/integration_tests.md` under "Test INT-02-withdrawal-propagation".

**The demo story this artifact supports**: "When a customer withdraws marketing consent at 10pm, this system ensures the campaign that fires at 8am tomorrow does not include them. The consent decision, the notice version they saw, the channel they used, and the timestamp are all immutably recorded for future regulator inspection."

### Artifact 3 — Synthetic DSR end-to-end (Module 03 minimum viable stub)

**What it is**: A complete data subject rights request fulfilled against a specific synthetic principal, producing a three-part response bundle: data export, erasure certificate, and retention schedule.

**Where it lives**: `compliance_pack_demo/dsr_bundles/<request_id>/` in DBFS, containing:

- `data_export.json` — all records associated with the principal, pretty-printed
- `erasure_certificate.pdf` — a signed certificate listing what was erased
- `retention_schedule.pdf` — what remains under retention obligation and when it will be purged
- `audit_trail.json` — the full sequence of actions taken with timestamps

**How to verify**:

The synthetic principal `customer_04217` (defined in `synthetic_data/dsr_principal_spec.md`) has a specific expected data footprint. The test in `tests/integration_tests.md` under "Test INT-03-dsr-end-to-end" verifies that:

1. A DSR request for this principal's data produces a bundle with all 4 expected assets
2. The data export contains exactly the rows predicted by the spec (no more, no fewer)
3. The erasure certificate lists the correct 3 tables marked for immediate erasure
4. The retention schedule shows the 1 asset scheduled for residual purge in 2032
5. Delta DELETE plus VACUUM applied to the correct tables
6. A time-travel query confirms the data was present before and is absent after
7. The audit trail references every action in Unity Catalog's audit log

**The demo story this artifact supports**: "This is a complete DPDP rights request handled end-to-end by the platform. The customer asked for her data and for erasure. She got her data. We erased what we could. We scheduled what we couldn't for its retention boundary. Every step is auditable. The GC can defend every decision."

## 1.4 · Out-of-scope items with rationale

Every item in this list has been deliberately excluded, with a specific reason. Do not add any of them to the POC regardless of how appealing they seem.

| Item | Rationale for exclusion |
|------|-------------------------|
| Zone 1 ingestion (SharePoint, email) | Requires GC-signed exclusion policy for privileged content; realistic sign-off is 4-6 weeks, bursting the 14-day window |
| Modules 04–06 beyond minimal DSR stub | Each depends on Module 0 organizational registries (legal hold, retention catalog) that don't exist; would force ungrounded judgment calls |
| Multi-source ingestion | Credential coordination across teams is the primary organizational bottleneck; POC isolates this by sticking to one source |
| Real customer data | Legal review of data use for POC purposes takes weeks; synthetic data sidesteps this entirely while preserving all technical proof points |
| Production operational readiness | HA, DR, scale testing belong to Phase 1 deployment, not validation; including them in the POC misrepresents the sprint's purpose |
| Agent Bricks DPIA or breach notifications | Both depend on other modules producing signal; running them in isolation produces placeholder outputs that erode stakeholder trust |
| Cross-border transfer monitoring | Requires vendor registry and residency metadata that aren't built |
| Lakewatch breach detection | Private Preview feature, unavailable in Databricks free trial workspace |
| External classifier integrations | Requires customer-side tool (Purview, etc.) to integrate with; adds a dependency we can't control |
| Identity verification provider integration | POC uses stub verification; real IDV provider integration adds vendor selection and contracting that the sprint can't accommodate |
| Production monitoring and alerting | Belongs to Phase 1; POC uses Databricks default monitoring only |
| Multi-language notice variants | Single notice version in English suffices to prove the schema; multi-language is a scale test, not a capability test |

## 1.5 · The exit criteria

The POC is complete when every one of these statements is true:

1. `compliance_pack.compliance.personal_data_register` returns the expected non-empty, schema-complete result set (Artifact 1 verification query)
2. `compliance_pack.compliance.consent_events_log` contains at least 1000 events with all required columns populated (Artifact 2 volume check)
3. The withdrawal propagation integration test passes within the 5-minute SLA (Artifact 2 latency check)
4. The synthetic DSR integration test passes, producing a bundle that matches the spec in `synthetic_data/dsr_principal_spec.md` (Artifact 3 completeness)
5. The Day 7 checkpoint script completes without errors
6. The Day 14 demo script runs end-to-end in under 8 minutes without manual intervention
7. Every schema in `schemas/` is instantiated in the workspace with the correct DDL
8. Unity Catalog lineage is visible for every table in the medallion path (Bronze → Silver → Gold)
9. The audit log shows every table-level operation performed during the 14-day build
10. All four stakeholder personas (CCO, CMO, GC, CFO) can ask a specific probing question (listed in `tests/day_14_demo_script.md`) and receive a defensible answer from the platform

If any single one of these ten statements is false on Day 14, the exit decision is Iterate (two-week extension), not Go.

## 1.6 · The design principles this POC encodes

Five principles shape every decision in the specification. When a choice is ambiguous, refer back to these:

**1. Evidence over features.** The platform's value is defensibility to the regulator, not user-facing functionality. Every artifact must be reproducible on demand, months or years after creation.

**2. Honesty over optics.** The platform does not claim more than it can deliver. Residual retention is disclosed explicitly rather than hidden under "fully erased"; synthetic data is labeled as synthetic; limitations are documented alongside capabilities.

**3. One platform, one audit trail.** Every action the POC performs logs to Unity Catalog. No out-of-band operations, no orphaned scripts, no undocumented manual steps.

**4. Schema discipline over feature velocity.** The consent event schema and the register schema are load-bearing invariants. A week spent getting them right is cheaper than a year spent refactoring later.

**5. Scope discipline over completeness.** Every feature not in §1.4's out-of-scope list is still off-limits unless explicitly added to the in-scope list with human review. The sprint's success depends on what is excluded as much as what is included.

## 1.7 · What "failure" looks like

It is useful to name specific failure modes so you can recognize them if they begin to occur:

**Silent scope creep**: the most common POC failure. Starts with "it would be easy to also add X." Prevention: the out-of-scope list in §1.4 is the authoritative boundary. Every suggested addition must pass through human review.

**Demo-driven implementation**: building shortcuts that work only for the specific demo path. Prevention: integration tests must exercise broader scenarios than the demo script; if tests fail outside the demo path, the implementation is incomplete.

**Test avoidance**: skipping tests because they are inconvenient after a code change. Prevention: every task ends with running the relevant tests; test failures block further work.

**Checkpoint erosion**: treating the Day 7 checkpoint as optional. Prevention: Day 7 is the primary risk-management mechanism; the human reviewer must confirm the checkpoint before you proceed to Day 8.

**Documentation drift**: the spec says one thing, the implementation does another, neither is updated. Prevention: when you diverge from the spec, update the spec in the same commit; never let divergence accumulate silently.

Now proceed to `02_runtime.md`.
