# DPDP Compliance Platform POC — Specification

> ⚠️ **Pre-build planning document.** This spec describes what was planned before the POC was built. Some details (full Lakebase/DSR-portal app, Module 04 Lakewatch, 14-day sprint timeline) were deferred or dropped because they're unavailable on free-trial workspaces. **For deploying the POC today, follow [`docs/persona_deploy.md`](docs/persona_deploy.md) and [`README.md`](README.md) — not this file.**
>
> **Architectural pivot post-spec.** The spec was written for a single-regulation (DPDP-only) build. The platform has since evolved into a multi-regulation accelerator (Compliance Pack Accelerator) where DPDP is the seed pack and additional packs (UK GDPR, EU GDPR, CCPA, …) drop in as data-only authoring exercises. Compliance applies per data subject — see [`docs/adr/0001-multi-jurisdiction-data-subject-routing.md`](docs/adr/0001-multi-jurisdiction-data-subject-routing.md) for the binding architectural decision and 24 enumerated edge cases.

> **To Claude Code reading this for the first time.** This specification is designed for pair-programming execution. You are collaborating with a human engineer across a 14-day sprint to build a working DPDP compliance POC on Databricks, covering Modules 01 (PII inventory) and 02 (consent intelligence) of a six-module platform. The human is your primary reviewer; do not attempt to execute the full build autonomously without checkpoints. Read this file top to bottom, then walk the directory structure in §0.3 to understand where the detail lives.

## 0.1 · What this POC proves — and why the broader scope matters

At the end of 14 days, three demonstrable artifacts must exist in the Databricks workspace:

1. A **living personal data register** showing every PII column across the source system, classified by category and sensitivity tier, with lineage
2. An **immutable consent log** with at least 1000 synthetic consent events captured, one demonstrated withdrawal propagating to downstream suppression in under 5 minutes
3. A **synthetic DSR fulfilled end-to-end** — discovery, erasure execution on structured data, verified erasure certificate, and response bundle

The POC succeeds if all three are demonstrable in a single 45-minute session on Day 14 to four stakeholder personas (CCO, CMO, GC, CFO) without requiring you to narrate hidden steps. It fails if any of the three produces artifacts that don't hold up to a hostile question like "how do I know you actually deleted it?"

**This is not a PII scanner.** Earlier accelerators in this space focused narrowly on finding PII gaps. This POC is deliberately broader: it builds Modules 01 (PII inventory) and 02 (consent intelligence) of a six-module platform (see `reference/proposal.pdf` for the full architecture). Every schema, pattern, and resource here is designed to extend naturally into Modules 03-06 in Phase 1, not to be a throwaway proof.

## 0.2 · Scope and non-scope

**In scope for this POC**: Module 01 (PII inventory, structured only) and Module 02 (consent engine), running on a Databricks free trial workspace, against a single synthetic source system containing Indian personal data, deployed as a Databricks Asset Bundle with Lakeflow Declarative Pipelines (DLT) for the medallion, Lakebase as the consent OLTP tier, Unity Catalog as the governance spine, Delta Lake for the analytical medallion, a Databricks App for the DSR portal, and an AI/BI dashboard as the consumption layer.

**Explicitly out of scope for this POC** (see §1 for rationale per item):

- Zone 1 ingestion (SharePoint, email, collaboration tools)
- Any of Modules 03–06 beyond the minimal DSR stub required by deliverable 3
- Multi-source ingestion (one source only)
- Real customer data (synthetic only)
- Production operational readiness (HA, DR, scale testing)
- Agent Bricks for DPIA or DPBI notification drafting
- Cross-border transfer monitoring
- Lakewatch breach detection (Private Preview, unavailable in trial)
- External classifier integrations (Purview, Varonis, BigID)

If you find yourself wanting to build something in the non-scope list, stop and raise it with your human collaborator. Scope expansion during the 14 days is the primary failure mode for rapid-discovery POCs.

## 0.3 · Repository layout

```
dpdp_poc_spec/
├── SPEC.md                          ← you are here
├── README.md                        ← onboarding + quickstart for DAB deploy
├── databricks.yml                   ← DAB root — deploy entry point
├── 01_context.md                    ← success criteria, exit tests, out-of-scope
├── 02_runtime.md                    ← what DAB creates; manual setup details
├── 03_data_contracts.md             ← source CSV format, Bronze/Silver DDL, register schema
├── 04_pii_taxonomy.md               ← nine-category taxonomy, 16-pattern library, ai_classify
├── 05_consent_model.md              ← Lakebase consent schemas, purpose taxonomy
├── 06_synthetic_data.md             ← generator spec, 10k principals, DSR principal
├── 07_dsr_execution.md              ← DSR portal API, discovery, erasure, bundle
├── 08_testing_strategy.md           ← unit, integration, load, Day 7 checkpoint, Day 14 demo
├── 09_known_pitfalls.md             ← anti-patterns to avoid, trial workspace limits
├── 10_runbook.md                    ← rollback, re-run idempotency, error-to-cause map
├── 11_deployment.md                 ← DAB architecture: one-command deploy and teardown
├── resources/                       ← DAB resource declarations (YAML)
│   ├── catalog_and_storage.yml
│   ├── pipelines.yml
│   ├── jobs.yml
│   ├── apps.yml
│   └── dashboards.yml
├── pipelines/                       ← DLT pipelines and job notebooks
│   ├── medallion.py                 ← DLT Bronze + Silver
│   └── classification_dlt.py        ← DLT pii_findings
├── dashboards/
│   └── dpdp_compliance.lvdash.json  ← AI/BI dashboard
├── schemas/                         ← DDL and executable pattern library
│   ├── bronze.sql · silver.sql · register.sql
│   ├── consent_events.sql · notice_versions.sql · consent_events_delta.sql
│   └── pii_patterns.py              ← unit-tested Python pattern library
├── synthetic_data/
│   ├── generator_spec.md
│   └── dsr_principal_spec.md
├── tests/
│   ├── unit_tests.md · integration_tests.md
│   ├── day_07_checkpoint.md · day_14_demo_script.md
│   └── verify_environment.md
├── runbook/
│   ├── setup_day_00.md · troubleshooting.md
│   ├── rollback.md · certificate_layout.md
└── reference/
    ├── dpdp_glossary.md
    ├── databricks_trial_limits.md
    └── proposal.pdf                 ← the six-module platform context
```

Files prefixed with a two-digit number are the eleven core sections and should be read in order on first pass. The `resources/`, `pipelines/`, `apps/`, `dashboards/`, `schemas/`, `synthetic_data/`, `tests/`, `runbook/`, and `reference/` directories hold the executable artifacts referenced from the core sections.

## 0.4 · The eleven sections at a glance

| § | File | Purpose | When to read |
|---|------|---------|--------------|
| 1 | `01_context.md` | What success looks like and what's out of scope | First, before any coding |
| 2 | `02_runtime.md` | Workspace, UC, Lakebase, auth — what DAB creates | Before Day 1 kickoff |
| 3 | `03_data_contracts.md` | Exact schemas for source, Bronze, Silver, register | Before Day 1 ingestion work |
| 4 | `04_pii_taxonomy.md` | Classification categories, recognizers, prompts | Before Day 3 classification work |
| 5 | `05_consent_model.md` | Lakebase schemas and purpose enumeration | Before Day 4 consent work |
| 6 | `06_synthetic_data.md` | Data generator specification | Day 1 before any real work starts |
| 7 | `07_dsr_execution.md` | DSR portal API and execution contracts | Before Day 8 DSR work |
| 8 | `08_testing_strategy.md` | How you verify your own work | Continuously; run tests after every task |
| 9 | `09_known_pitfalls.md` | Problems already known to avoid | Consult on any blocker |
| 10 | `10_runbook.md` | Recovery procedures | When something breaks |
| **11** | `11_deployment.md` | **DAB deployment — the architectural path** | **Day 0 before setup, and whenever deploying** |

## 0.5 · The 14-day timeline

```
Day  1  | Environment setup, synthetic data generation (§2, §6)
Day  2  | Ingest source, Bronze layer live (§3, §6)
Day  3  | Presidio classification, Silver layer with PII tags (§4)
Day  4  | ai_classify for unstructured-like fields, conflict resolution (§4)
Day  5  | Personal data register view, UC lineage verified (§3)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Day  6  | Catch-up / buffer
Day  7  | INTERNAL CHECKPOINT with human collaborator (tests/day_07_checkpoint.md)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Day  8  | Lakebase setup, consent_events schema live, first event captured (§5)
Day  9  | Notice versioning, purpose taxonomy, 1000 synthetic events ingested (§5, §6)
Day 10  | Delta sync from Lakebase, immutable log observable, withdrawal propagation test (§5)
Day 11  | DSR intake stub, unified discovery query, erasure execution (§7)
Day 12  | DSR response bundle generation, erasure certificate (§7)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Day 13  | Day 14 demo script dry-run, rehearsal with human collaborator
Day 14  | DEMO to stakeholders (tests/day_14_demo_script.md)
```

Each day has specific deliverables that must be testable. Do not advance to the next day's work until the current day's tests pass. If a day's work takes longer than one day, consume the Day 6 buffer before consuming any other day's budget.

## 0.6 · Collaboration protocol with your human reviewer

This POC is pair-programmed. Follow this protocol for every task:

1. **Before starting a new task**, post a one-paragraph summary of what you are about to do, which spec sections you are consulting, and any assumptions you are making. Wait for acknowledgment.
2. **While executing a task**, if you hit an ambiguity the spec does not resolve, stop and ask. Do not guess. The spec has been built to resolve the expected ambiguities; any remaining one is a design decision that needs human input.
3. **After completing a task**, run the relevant tests from `tests/` and report the results before moving on. If tests fail, diagnose before attempting a fix.
4. **At the end of each day**, summarize what was done, what remains, and what risks you see for tomorrow's work. Your collaborator uses this to plan the next day.
5. **Never skip the Day 7 checkpoint**, even if Day 1-5 went faster than expected. The checkpoint is the primary risk-management mechanism.

## 0.7 · Verification contract

The POC is considered complete when:

- All unit tests in `tests/unit_tests.md` pass
- All integration tests in `tests/integration_tests.md` pass
- The Day 7 checkpoint script completes without errors
- The Day 14 demo script runs top to bottom in under 8 minutes with no manual intervention
- The three stakeholder artifacts (register, consent log, DSR bundle) are reachable via the URLs listed in §1

This is the complete definition of done. Features not covered by these tests are not in scope, regardless of how appealing they might seem.

## 0.8 · A note on the trial workspace

The Databricks free trial workspace has specific constraints that shape design choices throughout this spec. Before your first day of work, read `reference/databricks_trial_limits.md` fully and confirm you understand what is and is not available. In particular: compute budget is limited, certain Private Preview features are unavailable, and some networking configurations are restricted. The spec has been written to stay inside these constraints; if you find yourself needing something outside them, raise it as a design question before proceeding.

## 0.9 · One last thing

If anything in this specification reads as unclear or internally inconsistent, stop and flag it. The cost of asking a clarifying question is minutes; the cost of building against a misread spec is days. This is a collaborative build, not a contract handoff.

Now proceed to `01_context.md`.
