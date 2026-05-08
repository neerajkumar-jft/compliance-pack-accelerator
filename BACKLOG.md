# Compliance Pack Accelerator — Backlog

Working list of items in flight. Originally seeded by the colleague's
DPDP-pack gap-analysis (2026-04-24) and the three-path ingestion demo
scope expansion; now also tracks ADR-0001 implementation milestones
(M1 Foundation → M2 UK GDPR pack → M3 UI surfaces → M4 Tests + docs).

> **For the multi-jurisdiction work**, the binding plan is
> [`docs/adr/0001-multi-jurisdiction-data-subject-routing.md`](docs/adr/0001-multi-jurisdiction-data-subject-routing.md).
> This backlog mirrors its M1–M4 milestones; if there's a conflict, the ADR wins.

Legend: `[ ]` open · `[x]` done · `[-]` dropped/subsumed

---

## P0 — Three-path native ingestion demo (centerpiece)

Goal: one ingestion from each Databricks-native path, proving governance is uniform.
Subsumes gaps 1.2, 2.2, 3.6, 5.1.

### Design decision (2026-04-24)
**Self-contained POC — no external services.** Three ingestion *patterns* demonstrated in code with synthetic data:
1. Landing zone (Auto Loader) — the 5 existing synthetic tables cover this
2. Lakeflow Connect — simulated via direct-write bronze tables with Salesforce-shaped schema
3. Lakehouse Federation — simulated via view over a `federation_mock` schema

Reason: POC reviewed internally next week on Free Edition workspace (no account admin). External-service dependencies (AWS, SF, Postgres) create demo logistics risk without strengthening the approach-validation goal. Industry convention for tier-3 demos (Databricks `dbdemos`, Snowflake quickstarts) is self-contained.

**Rolled back 2026-04-24:**
- External volume `dpdp_poc.bronze.landing_external` DROPPED
- External storage credential + external location in workspace left intact (pre-existing, not POC-specific)
- `pipelines/medallion.py` reverted to managed-volume-only code path

### Day 1 — Foundation (closes gap 1.2 + 2.2) — DONE 2026-04-27
- [x] `bronze.data_sources` extended with `silver_table_name` column; `schemas/bronze.sql` DDL spec aligned
- [x] `phase1_bootstrap.py` §2.5 added — idempotent MERGE seeds 10 canonical rows (5 Auto Loader + 3 SF + 2 federation) with `ingestion_pattern` enum (`auto_loader | direct_write | federation_view`)
- [x] `pipelines/classification_dlt.py` refactored — `_resolve_silver_tables()` reads `silver_table_name` from `data_sources` with a 5-table fallback for fresh deploys before phase1_bootstrap runs
- [x] Verified live: 10 rows seeded, classifier scans the 10 dynamically, smoke test 10/10 passing
- Replaces hardcoded `SILVER_TABLES` list. Adding a new ingestion path is now one row, not a code change.

### Day 2 — Landing zone pattern
- [x] Already covered by existing 5 Auto Loader sources (employees, customers, patients, transactions, users) on managed volume `bronze.landing`. Narrate as "file-arrival landing zone."
- [ ] Optional: add a 6th synthetic source to make the "new source onboarding" flow demonstrable live

### Day 3 — Lakeflow Connect pattern (simulated Salesforce) — DONE 2026-04-27 (dd88cf9)
- [x] `generate_salesforce_data.py` — Lead/Contact/Account generator, seed=43, 100/60/30 rows, India PII
- [x] `scripts/seed_salesforce_data.py` — idempotent CREATE OR REPLACE + batched INSERT (no Auto Loader)
- [x] Silver tables `sf_{leads,contacts,accounts}_tagged` populated end-to-end
- [x] Classifier picks them up — verified 13 SF findings (5 leads + 6 contacts + 2 accounts), all hybrid + confidence ≥0.91
- Follow-up tracked: GST + NAME pattern additions to `governance_core/pii_patterns/universal.py` would lift account findings; Phase-1 (no need to block review)

### Day 4 — Federation pattern (simulated Postgres marketing DB) — DONE 2026-04-27 (b596bb6)
- [x] `generate_federation_data.py` — lead_scoring (200) + campaign_response (100), seed=44; lead_ids cross-reference sf_leads
- [x] `scripts/seed_federation_data.py` — CREATE SCHEMA federation_mock + Delta tables + silver VIEWS (views, not tables — the federation code-shape signal)
- [x] Silver views `federation_{lead_scoring,campaign_response}_tagged` populated as passthroughs
- [x] Classifier reads through views via `spark.table()` with no special handling — verified 3 federation findings (2 on lead_scoring, 1 on campaign_response)
- Narrative: "in production this is a foreign catalog over Postgres; same governance applies at query time"

### Three-path P0 status — COMPLETE
**Total: 36 PII findings across 10 silver objects**
- 5 Auto Loader tables (file-arrival landing zone): 20 findings
- 3 Salesforce tables (Lakeflow Connect simulation): 13 findings
- 2 Federation views (Lakehouse Federation simulation): 3 findings

All 3 ingestion patterns deliver into the same governance layer (classifier → pii_findings → personal_data_register). Different code shapes (DLT @table + Auto Loader vs direct INSERT vs CREATE VIEW) make the pattern split visible to a code reviewer.

### B-pass: downstream governance for SF + federation — DONE 2026-04-27
- [x] Column masks (`schemas/pii_column_masks.sql`) — 16 new ALTER TABLE: 13 on SF silver tables + 3 on federation_mock backing tables (views inherit via SELECT *). Applied + verified via `system.information_schema.column_masks`.
- [x] CCO Genie data_sources extended to 12 tables (was 7): adds 3 SF silver + 2 federation silver views. Updated in `setup_persona_genie_spaces.py` and pushed to live Genie space.
- [x] CCO UC grants synced — `apply_persona_uc_grants.py` PERSONA_TABLES extended to match Genie data_sources (incl. federation_mock backing tables for view-passthrough query support).
- [x] UC tags auto-flow confirmed — `apply_uc_tags.py` reads pii_findings dynamically; no code change needed.
- Deferred (Phase-1 backlog): `silver.discovered_tables` (hardcoded in medallion DLT — would need extraction OR information_schema view); `compliance_gaps` rules (don't fire for new sources — separate generator audit needed); CMO/GC/CFO Genie extension (would need matching UC grants).

---

## P1 — Before-review polish — DONE 2026-04-27

- [x] **3.2** — README "Step 0" + `scripts/configure_workspace_host.sh` (idempotent sed-rewrite of `databricks.yml`'s literal host)
- [x] **3.1** — `scripts/deploy_all.sh` wrapping the full 12-step sequence (bundle → synthetic → medallion → SF + federation → refresh → bootstrap → tags + masks + filters → multilang → smoke). `--from <step>` for partial reruns, `--smoke-only` for verification, `--skip-multilang` for cost savings. Idempotent end-to-end.
- [x] **4.2** — `tests/test_post_deploy_smoke.py` — 10 checks (schemas + silver-object count + findings count + per-pattern coverage + register + masks + consent + notices). Caught real gap on transactions_tagged.ip_address (now masked via `mask_full`). Currently 10/10 passing.
- [x] **5.2** — `docs/poc_scope.md` — module-by-module in-scope/out-of-scope writeup with decision log. Sets reviewer framing.

---

## P2 — Nice-to-have before review

- [x] **4.3** — `tests/test_dsr_e2e.py` (2026-04-27) chains `discover_principal()` → `count_rows()` → audit-bundle assertions. 11 checks: bundle shape, JSON-serializability, per-table coverage for `customer_04217`, total parity, erasure-vs-discovery drift, `compliance.dsr_requests` writability. Non-destructive (count-only erasure); the actual `--confirm` is a manual demo step.
- [x] **3.4** — `scripts/setup_agent_bricks.py` (2026-04-27) — headless infra setup + health-check: serving-endpoint READY state, MLflow experiment idempotent create, `governance_core.agent_prompts` loads + version hashes printed. `--smoke` flag does one real LLM call to verify payload shape. Wired into `deploy_all.sh` as the `agents` step before the smoke test. 3/3 checks pass without --smoke, 4/4 with.

---

## Deferred to Phase 1 (acknowledge in scope doc, do not attempt before review)

- **2.1** — Dynamic column masks driven from `pii_findings` (new-source masking)
- **2.4** — Extend persona row filters beyond `consent_events_log`
- **3.3** — Paid-tier Lakebase + DSR portal uncomment path + test
- **4.1** — CI workflow for `tests/` with workspace auth
- **4.4** — CDF consent-withdrawal propagation test
- **4.5** — Automated persona-boundary runtime test (requires live persona users)
- **4.6** — Agent Bricks DPIA roundtrip validation
- **5.3** — Full externalization of workspace-specific literals (warehouse IDs, catalog names)
- **GENIE-CFG** — Move regulation-specific values out of `configs/genie/*.yaml` into the regulation pack. Today these YAMLs hardcode DPDP penalty ceilings (₹250cr/150cr/50cr/5cr), labor rate (₹8000/hr), section citations (BRN-001 → §8(6)), and consent purpose names. When UK GDPR pack is added, each will need templating (Jinja over active_pack() values) or per-regulation YAMLs. Phase-0-acceptable tech debt.
- **AI-PROMPT-PACK** — Move regulation-specific text in `governance_core/agent_prompts.py` (DPDP Act 2023, India, "DPDP section references") into the active regulation pack. Either: (a) per-regulation prompt files (`regulations/dpdp_2023/agent_prompts.py`), or (b) keep templates generic and inject regulation metadata via `active_pack()` at render time. Same shape of decision as GENIE-CFG.
- **AI-MLFLOW-TESTS** — Add unit tests for `governance_core/agent_prompts.py` (render_dpia_user / render_compliance_qa_user with required-key check + JSON-encoding sanity) and a mocked `_invoke_llm()` retry test (assert 500-then-200 retries, 4xx fails fast). Both pure-local, no Databricks needed.
- **AI-MLFLOW-PII-GUARD** — Add a runtime check or env-flag-gated assertion that `compliance_qa(question)` rejects raw customer identifiers when called outside the synthetic-data POC. Pairs with the docstring warning added 2026-04-27.
- **PII-NAME-GST** — Universal PII pattern library (`governance_core/pii_patterns/universal.py`) doesn't catch generic names or India GST numbers. Names are intrinsically hard (false-positive prone); GST (`^\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z][A-Z\d]$`) is a clean addition. Adding GST would surface another finding on `sf_accounts_tagged.gst_number`.

---

## Dropped (already addressed — no action)

- [-] **1.1** — Classifier `.collect()` issue — already fixed; `pipelines/classification_dlt.py:10-12` documents no driver-side collects
- [-] **1.3** — `discovered_tables` "drift" — the 5-table list matches actual scanner coverage; no drift
- [-] **2.5** — Phase-0 pack integration — verified: `phase1_bootstrap.py:392-396`, `dsr_erasure.py:46,56-65` both load from pack
- [-] **3.5** — Retention job unscheduled — declared at `resources/jobs.yml:52-70` in dry-run mode by default
- [-] **3.6** — Fivetran typed-silver mirror — replaced by Lakeflow Connect in P0
- [-] **5.1** — Fivetran vs native narrative — resolved by P0 three-path demo

---

## Decisions resolved

- **SF schema depth** — 3 standard objects (Lead/Contact/Account). Opportunity/Case deferred — they don't add a *new* ingestion-pattern story.
- **Federation source** — local `federation_mock` schema with passthrough silver views. Not `samples` catalog (no India PII).
- **Persona row filter on SF/federation** — deferred to Phase-1 (gap 2.4). CCO has UC SELECT + Genie scope on the new tables; no row filter needed for demo.
