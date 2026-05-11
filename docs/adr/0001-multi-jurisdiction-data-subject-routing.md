# ADR-0001 — Multi-jurisdiction data-subject routing

**Status:** Accepted
**Date:** 2026-05-08
**Implementation:** M1 + M2 complete (commits `58c4e7e` foundation, `ac866d4` UK GDPR pack + 70/25/5 synthetic mix + multi-pack rule/gap routing) · M3 in progress (UI surfaces) · M4 pending (live deploy + tests)

## Context

The platform began life as a single-jurisdiction (DPDP-only) accelerator with
a regulation-pack abstraction layered on after the fact (Phase 0 refactor,
2026-04-24). Activating a different regulation today is a single environment
variable: `REGULATION_PACK=uk_gdpr` swaps every rule, pattern, and template
deployment-wide. That works for a customer who operates under exactly one
regulator.

It does not work for the customer types that actually populate the market:

- **Indian SaaS, India only.** Subject to DPDP only. Today's behaviour is correct.
- **Indian SaaS with UK clients.** Subject to *both* DPDP (for Indian principals)
  and UK GDPR (for UK principals) at the same time, in the same database.
  The Indian principals' marketing data must be retained per DPDP defaults
  (~730 days). The UK principals' must be retained per UK GDPR (~90 days).
  These are incompatible per-row outcomes; they cannot be reconciled by
  picking a single deployment-wide rule.
- **UK SaaS expanding to EU.** UK GDPR plus EU GDPR. Largely overlapping but
  not identical (post-Brexit divergence on cookies, transfers, certain
  data-subject rights). Same per-row routing problem.
- **Future: global SaaS.** DPDP + UK GDPR + EU GDPR + CCPA, scaling
  arbitrarily as the customer expands.

The first instinct — load multiple packs and apply the **strictest rule
across all of them** to every row — was rejected during design discussion.
Forcing UK GDPR's 90-day retention onto Indian customers' marketing data
because the company also operates in the UK destroys the entire economic
case for the product: an Indian SaaS would be operationally worse off using
this platform than not using it. Compliance does not work like a strict-OR
filter. It is fundamentally per-data-subject.

A second observation surfaced during the same discussion: not every
regulatory rule is per-data-subject. Some rules apply to the company as a
whole regardless of which principals' data is involved — for example, an
incident affecting any UK principal triggers UK ICO breach notification
obligations on the company, in parallel with any DPDP §8(6) obligations
triggered by the same incident affecting Indian principals.

This ADR captures the per-row-versus-company split and the pack-loading
model that follows from it.

## Decision

**Compliance applies to the data subject, not to the deployment.**

1. Every customer-level row carries a `jurisdiction` column. Rules that
   bind data are evaluated per-row by routing to the pack that governs that
   principal.
2. The platform loads *all* packs found under `regulations/` simultaneously.
   The previous `REGULATION_PACK` env variable, which selected a single
   active pack, is retired. Customers control which packs are *available*
   by what's in their `regulations/` directory; they control which packs
   *apply* to a principal by setting that principal's jurisdiction.
3. Two distinct categories of rules, handled differently:
   - **Per-data-subject rules** — retention defaults, consent semantics
     (opt-in vs opt-out-of-sale), DSR SLA, lawful basis, data residency,
     notice language, minor-age threshold. **Routed by the principal's
     jurisdiction.** The pack governing that principal is the only pack
     consulted.
   - **Per-deployment / per-company rules** — breach notification
     obligations (the company must notify *every* applicable regulator
     in parallel), DPIA filing requirements, DPO requirement, registration
     as a Significant Data Fiduciary, audit-log retention. **Union across
     all packs whose principals are present in the deployment.** The
     company is independently subject to each regulator on these matters.
4. The DPIA generator output is per-processing-activity. A DPIA for a
   processing activity that touches both Indian and UK principals cites
   both DPDP and UK GDPR provisions in the same document.
5. Compliance gaps and PII findings are tagged with the regulation that
   surfaced them. A column flagged as PII under one regulation but not
   another is recorded against the regulation that flags it; the dashboard
   can show union-of-jurisdictions or per-jurisdiction breakdowns.

The pack contract (`regulations/README.md`) is unchanged — packs remain
pure data, and adding a new pack remains a 9-yaml-file exercise with no
core code change.

## Consequences

### Positive
- Each principal's data is governed by the rules that actually apply to
  them; the product preserves operational efficiency for single-jurisdiction
  customers and remains useful for multi-national customers.
- Adding a new regulation (EU GDPR, CCPA, PIPEDA) is data-only: drop nine
  yaml files in `regulations/<code>/`, redeploy. No change to the loader,
  the rule engine, dashboards, Genie, or DPIA logic.
- Single-jurisdiction customers see no behavioural change. If every row
  has `jurisdiction = 'IN'` and only `regulations/dpdp_2023/` is present,
  the system behaves identically to today's single-pack path.
- Compliance posture reporting becomes per-jurisdiction
  ("87% DPDP-compliant on 4,200 IN principals, 73% UK-GDPR-compliant on
  850 GB principals"), which is the report a CCO actually wants.

### Negative
- Customer-level silver tables (`customers_tagged`, `users_tagged`,
  `patients_tagged`, `employees_tagged`) gain a `jurisdiction` column;
  data generators and the DLT classifier must populate it.
- Rule evaluation queries grow a join (or a column lookup) against
  jurisdiction. Compiles down to one extra predicate; not a perf concern
  at POC scale.
- Genie persona instructions become jurisdiction-aware. The CCO answering
  "how many compliance gaps do we have?" has to learn to break down by
  jurisdiction or qualify with "across all loaded regulations".
- Dashboards gain a `jurisdiction` facet. Tile counts can be union or
  filtered per-pack.
- Test matrix grows: each suite must be run for at least one
  single-jurisdiction case (every row `IN`) and at least one mixed
  case (rows split `IN`/`GB`). The synthetic generators need to support
  controllable jurisdiction mix.
- The Phase 0 pack abstraction was originally deployed-pack-singleton
  shaped; the loader contract changes from `active_pack() -> Pack` to
  `pack_for(jurisdiction) -> Pack` plus `loaded_packs() -> list[Pack]`.
  Existing call sites that assume a single active pack must be audited
  and migrated.

### Neutral
- Customers who only operate in one jurisdiction don't have to learn
  the multi-pack mental model — they set every row's jurisdiction to
  their country code, only one pack is active in `regulations/`, and the
  system behaves like today's single-pack mode.

## Alternatives considered

- **Mode 1 — keep single-pack-per-deployment.** Simple, today's behaviour.
  Rejected because it forces multi-national customers to either pick the
  strictest regulator (operational hit) or run two parallel deployments
  (operational and audit complexity). Both options destroy the value
  proposition.

- **Mode 2 — load multiple packs, apply strictest rule across all.**
  Rejected because compliance is not the strict-OR of all applicable
  regulations. An Indian customer's marketing data is governed by DPDP,
  not by the strictest of DPDP-and-whatever-else-the-company-also-supports.
  Applying UK GDPR's 90-day retention to Indian customers because the
  company has UK customers somewhere else is operationally damaging and
  legally unnecessary.

- **Mode 3 (this ADR) — per-data-subject routing.** Each principal has a
  jurisdiction; rules apply per principal. Cross-cutting company-level
  obligations are union-of-applicable-regulators. Selected.

- **Mode 4 — runtime pack composition by user role.** Some products let
  a CCO pick a regulation lens at query time. Rejected because compliance
  is not a UI preference; the rule that applies to a given record is a
  property of the record (its principal's jurisdiction), not a property
  of who's looking at it. A CCO who wants to see only UK gaps applies a
  filter; the underlying rule that surfaced each gap is unchanged.

## Edge cases

The categories below are the cases I want this ADR to be the answer to
when they hit prod. Where a case has a chosen resolution, the resolution
is noted; where it's deferred to a later phase, the deferral is explicit.

### Identity and jurisdiction

- **Country of residence vs citizenship.** A UK national resident in
  India: which regulation applies? **Decision: country of residence wins
  for the POC.** Matches the territorial-scope language in DPDP §3
  ("data principal located in India") and aligns with how UK GDPR / EU
  GDPR Art. 3 frames residency. Real-world enterprise overrides via
  per-principal jurisdiction tagging.
- **Dual residency / global mobility.** A person who maintains residences
  in both UK and India. **Decision: the most-recent-known residency wins;
  the customer's onboarding flow is responsible for setting this
  correctly.** Edge handled at data-collection time, not at rule-eval time.
- **Tourist / temporary residents.** A UK national vacationing in India
  who signs up for an Indian SaaS. **Decision: country of residence (UK)
  wins, because the principal will return there and DPDP §3 doesn't
  bind the long-tail data on a tourist.** EU/UK GDPR's territorial scope
  catches this case automatically (Art. 3(1) — the controller offers
  goods or services to UK residents).
- **EU GDPR extraterritorial reach (Art. 3(2)).** A non-EU SaaS that
  offers services to EU residents must apply EU GDPR to those EU
  residents regardless of where the SaaS is incorporated. **Decision:
  jurisdiction is the principal's country of residence, not the
  company's place of incorporation. Extraterritorial reach falls out
  naturally.** A US SaaS with EU customers tags those customers with an
  EU-country jurisdiction and EU GDPR rules apply to them.
- **Children's data with cross-jurisdiction conflict.** A 14-year-old UK
  resident creates an account from India. DPDP requires parental consent
  <18; UK GDPR <13 (Information Society Services). **Decision: the
  principal's jurisdiction (UK) governs; UK GDPR's age-13 threshold
  applies.** Edge resolved by the per-data-subject routing.
- **Principal explicitly invokes a different regulation than their
  jurisdiction.** A UK national in India explicitly invokes UK GDPR
  rights. **Decision: defer to Phase 2** — for the POC, the per-row
  jurisdiction is authoritative and override is a hand-tagged flag.
  Real-world enterprises handle this via legal review.
- **Anonymous or pseudonymous principals (no jurisdiction).** Logs,
  unauthenticated traffic. **Decision: out of scope. The platform's
  surface is about identified data subjects; truly anonymous data falls
  outside any regulation.** Pseudonymous data with linkable hashing keys
  is treated as identified per GDPR Recital 26 / DPDP §2(t); jurisdiction
  follows the linked principal.
- **B2B contacts at a customer org.** A contact who is an employee of a
  customer-company; whose jurisdiction matters? **Decision: the contact's
  own country of residence, not their employer's place of incorporation.**
  GDPR / DPDP both bind to the natural person.
- **System / bot accounts.** No jurisdiction, no data subject.
  **Decision: excluded from compliance rule evaluation. The platform's
  PII discovery doesn't fire on rows where `jurisdiction IS NULL` and
  the schema's `data_subject_type = 'system'`.** Defer the schema column
  for `data_subject_type` to the implementation; for the POC, NULL
  jurisdiction implies system or unknown.
- **Stateless / aggregate data.** Marketing analytics rolled up to
  cohorts where the cohort can no longer be linked to an individual.
  **Decision: out of compliance scope for that table; jurisdiction column
  not required. Tagging discipline: aggregates explicitly carry a
  `pii_class = 'aggregate'` annotation.** Cleanly out of scope.

### Data flow and joining

- **Joins across jurisdictions.** A query like `JOIN customers ON
  transactions.customer_id` produces rows that mix jurisdictions. **Decision:
  the joined record inherits the *customer's* jurisdiction.** Transactions
  are facts about principals; the principal's jurisdiction governs the
  fact. If a downstream query needs "all transactions for IN principals",
  it filters on the joined column.
- **Cross-border transfers.** A UK principal's data must stay within
  EEA / adequacy-decision countries. The company runs Databricks on
  AWS us-east-1 (no UK adequacy decision today; Standard Contractual
  Clauses required). **Decision: out of platform scope for the POC.
  Documented via residency.yaml and the residency filter; the legal
  enforcement (SCCs / adequacy / DPF) is a customer responsibility,
  not a feature we ship.** A future ADR may revisit when we have a
  customer asking for region-pinning support.
- **Consent withdrawal.** A principal withdraws consent for marketing.
  The withdrawal is per-principal; the cascade of which rows must be
  acted upon depends on the principal's pack (DPDP §8 vs UK GDPR Art. 7).
  **Decision: existing CDF-based withdrawal propagation is unchanged
  except that the `marketing_eligible_principals` view filters per
  principal-jurisdiction.** A withdrawn principal disappears regardless
  of pack; the difference is *which* downstream actions are required
  (e.g., GDPR's right to erasure-on-withdrawal vs. DPDP's narrower scope).
- **Aggregate / derived data tagged at source level.** A view that
  computes "marketing-eligible audience" must be a per-jurisdiction
  union: GDPR-eligible UK customers ∪ DPDP-eligible Indian customers.
  **Decision: the view's WHERE clause references each principal's
  jurisdiction and applies the corresponding rule; the result is one
  table with rows from multiple jurisdictions.** Already how the per-row
  routing works.

### Rule evaluation and pack mechanics

- **Conflicting consent default.** DPDP defaults to opt-in; CCPA defaults
  to opt-out-of-sale. **Decision: each pack handles its own principals;
  no conflict surfaces at runtime.** The DPDP pack's consent-event
  generator only generates rows for IN principals; the CCPA pack only
  for US-CA principals.
- **Regulatory updates.** DPDP §X is amended after a pack is deployed.
  **Decision: pack files are versioned (filename or in-yaml `version`
  field). When a pack is updated, redeployment re-derives compliance
  gaps using the new rule set. The `compliance_gaps` table is
  TRUNCATE+INSERT on each re-deploy, so old-rule gaps don't linger.**
  Implementation note: `pack.yaml` should grow a `version: '1.2'`
  field for traceability; existing rows in `dpia_runs.regulation_pack`
  stay tagged with the version they ran under.
- **New regulation effective in the future.** A pack is shipped today
  for a regulation that becomes effective next quarter. **Decision:
  pack metadata includes `effective_date`; rules don't fire until the
  current date is on or after that date.** `pack_loader.py` exposes
  `is_active(today)` per pack; rule-evaluation queries respect it.
- **Sunsetting a regulation.** UK leaves its current GDPR for a
  hypothetical UK-DPA. **Decision: the old pack's status flips to
  `deprecated` (a field in `pack.yaml`). Existing data tagged under
  the old pack stays tagged for audit purposes; new data is tagged
  under the new pack.** Migration path is well-understood; not a POC
  concern.
- **Penalty calculation when one incident affects multiple jurisdictions.**
  A breach affecting both Indian and UK customers triggers DPBI fines
  plus ICO fines, independently. **Decision: per-incident penalty is
  the sum across applicable regulators. The CFO Genie / dashboard tile
  computes per-pack penalty exposure and a sum; no single "blended
  rate" exists.** Matches how regulators actually operate.
- **DSR scope when principal's data sits across borders.** A UK
  principal exercises right of erasure. Their data sits in an Indian
  data centre due to the company's hosting choice. **Decision: erasure
  proceeds per UK GDPR Art. 17; the data location does not change the
  obligation.** The DSR script's audit bundle cites UK GDPR Art. 17
  regardless of physical storage region.

### Schema migration

- **Backfilling jurisdiction on existing rows.** A real customer adopting
  this platform from a single-jurisdiction past has rows with no
  jurisdiction set. **Decision: derive jurisdiction from existing
  country / nationality / billing-country columns where they exist;
  surface unmapped rows as a `compliance_gaps` entry of severity 'high'
  with a remediation suggestion ("set jurisdiction explicitly for these
  N principals").** A `derive_jurisdiction()` helper ships in the pack
  loader; customers can override.
- **Adding a new pack to an existing deployment.** New pack → new rules
  potentially fire on existing data. **Decision: this is the desired
  behaviour. A customer who adds the EU GDPR pack expects their existing
  EU customers to get GDPR-correct treatment retroactively.** The
  `compliance_gaps` table is TRUNCATE+INSERT on re-deploy, so new-pack
  gaps appear immediately.
- **Removing a pack.** A customer drops `regulations/<pack>/` because
  they exited a market. **Decision: existing principals with that
  jurisdiction become "unmapped" — surfaced as a high-severity gap.
  Data is not auto-deleted; the customer decides whether to migrate
  those principals to a different jurisdiction or purge them.**

### Operational / dashboard / Genie

- **PII definition differs between jurisdictions.** Aadhaar is critical
  PII under DPDP; not under UK GDPR. UK postcodes are PII under UK GDPR;
  not under DPDP. **Decision: PII discovery runs all loaded packs'
  patterns against all rows; findings are tagged with the regulation that
  flagged them. A column with a finding under any active pack is "PII
  somewhere"; per-jurisdiction filtering is one click.** This is what
  the architecture already does (each pack ships its own
  `pii_patterns.py`); we just have to plumb the per-finding pack tag
  through.
- **Default jurisdiction filter on dashboards.** **Decision: "all
  jurisdictions" is the default. Tiles show union counts. Drill-down
  filters by jurisdiction.** Matches the CCO's headline-first reading.
- **NULL or unmapped jurisdiction surfaced where?** **Decision: a
  dedicated tile on the CCO dashboard ("Unmapped principals") and a
  high-severity `compliance_gaps` entry. Genie answers about unmapped
  principals route to the CCO.**
- **Genie agent for a multi-jurisdiction customer.** **Decision: one
  Genie space per persona, instructions composed from all loaded packs.
  The space's `text_instructions` enumerate every loaded regulation and
  the agent learns to qualify answers with jurisdiction. Single-pack
  deployments produce a Genie that doesn't mention multiple regimes
  because only one pack is loaded.** No persona explosion.
- **Persona scoping interaction with jurisdiction.** CMO sees marketing-
  eligible audiences, must be per-jurisdiction. **Decision: persona
  row filters and jurisdiction filters compose. CMO's view of
  marketing-eligible UK customers honours BOTH the persona scope AND
  the per-row jurisdiction routing of consent rules.** Defense-in-depth
  unchanged.

### Loader and pack mechanics

- **Pack loaded but incomplete (some yaml files missing).** **Decision:
  pack-loader refuses to register that pack with a clear error naming
  the missing file. Other packs continue to load.** Today's behaviour
  for a single pack; preserved.
- **Pack-to-pack dependencies.** A hypothetical UK-overlay pack that
  builds on an EU GDPR base. **Decision: out of POC scope. Each pack
  is standalone today. If a real customer surfaces this need, a future
  ADR addresses pack inheritance.** Acceptable not-yet-needed.
- **Multiple packs claim the same jurisdiction code.** Two packs both
  declare `jurisdiction: GB`. **Decision: pack-loader rejects the
  duplicate at load time with a clear error.** First-loaded wins is
  not safe — the customer needs to fix the configuration.

## Open questions

| # | Question | Owner | Revisit by |
|---|----------|-------|-----------|
| Q1 | Should principals carry an array of jurisdictions (for EU GDPR Art. 3(2) extraterritorial cases that genuinely apply two regulators to the same row)? Today the answer is "no, single jurisdiction wins"; revisit if a real customer scenario requires otherwise. | Architecture | When first non-residence-based jurisdiction case lands |
| Q2 | Pack versioning — is a `version` field in `pack.yaml` enough, or do we need historical pack snapshots (so a DPIA generated under DPDP v1.0 can be re-rendered later under v1.0 even after the pack ships v1.1)? | Architecture | Phase 1 |
| Q3 | Should the loader validate that all jurisdiction codes referenced in `customers_tagged.jurisdiction` correspond to a loaded pack, and surface mismatches as a compliance gap? | Architecture | Implementation phase of this ADR |
| Q4 | Cross-border transfer enforcement — should the residency filter actually block reads of UK principals' data when the workspace is in a non-adequacy region, or just flag it? | Compliance + Platform | Phase 2 |

## References

- DPDP India 2023, especially §3 (territorial scope), §8 (breach reporting), §11–14 (rights), §33 (penalties)
- UK GDPR (post-Brexit), especially Art. 3 (territorial scope), Art. 17 (right of erasure), Art. 33–34 (breach), Art. 35 (DPIA)
- EU GDPR Recital 23, 24 (extraterritorial scope under Art. 3(2))
- Phase 0 pack-refactor commit `7fce83f` (current single-pack baseline)
- `governance_core/pack_loader.py` (the loader contract this ADR redefines)
- `regulations/README.md` (the per-pack file contract — unchanged by this ADR)
