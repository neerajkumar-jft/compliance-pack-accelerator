# Day 0 · pre-sprint setup checklist

> ⚠️ **Pre-build planning document.** This describes a 14-day sprint that has already concluded. The Lakebase provisioning and `compliance-pack-builder` service-principal steps don't apply on the free-trial deploy path the POC ended up using. **For deploying the POC today, follow [`docs/persona_deploy.md`](../docs/persona_deploy.md) and [`README.md`](../README.md).**

The 14-day timeline assumes certain things are already in place when Day 1 begins. If they are not, Day 1 gets consumed by setup work, the Day 7 checkpoint slips, and the whole sprint compresses. This checklist is what Day 0 looks like — the work that happens in the 2-3 weeks *before* Claude Code starts coding.

This file is primarily for the human collaborator coordinating the engagement, not for Claude Code. Claude Code's Day 1 assumes every item here is complete.

## Two to three weeks before Day 1

### Workspace procurement
- [ ] Databricks free trial workspace requested and provisioned
- [ ] Cloud chosen (AWS, Azure, or GCP) — the spec is cloud-agnostic but a choice must be made
- [ ] Region chosen with Lakebase availability confirmed
- [ ] Workspace admin identified on the customer side

### Stakeholder alignment
- [ ] CCO, CMO, GC, CFO have each confirmed they'll attend the Day 14 demo
- [ ] 45-minute Day 14 slot placed on all four calendars
- [ ] A single-threaded customer-side engagement owner named (typically a solution architect or compliance program manager)
- [ ] Day 7 checkpoint time reserved (90 min slot with the human collaborator, optionally with customer engagement owner)

## One week before Day 1

### Authentication setup
- [ ] Service principal `compliance-pack-builder` created in the workspace
- [ ] Service principal added to the workspace users group
- [ ] OAuth token-based auth configured (not personal access tokens)
- [ ] Secret scope `compliance-pack` created

### Unity Catalog scaffolding
- [ ] Catalog `compliance_pack` created
- [ ] Schemas `bronze`, `silver`, `gold`, `compliance` created
- [ ] Volume `compliance_pack.bronze.landing` created
- [ ] All grants from §2.3 applied to the service principal

Run the verify_environment notebook from `tests/verify_environment.md` at the end of this step. All checks must pass.

### Lakebase provisioning
- [ ] Lakebase instance `compliance-pack-consent` provisioned (smallest tier)
- [ ] Database `compliance_pack_consent` created
- [ ] Native auth integration configured (no separate JDBC password)
- [ ] Connection tested from the workspace

### Cluster setup
- [ ] Single shared cluster created per §2.9 (15.4 LTS, smallest node, 30-min auto-terminate)
- [ ] Required Python libraries configured as cluster libraries per §2.7
- [ ] Cluster attached to the workspace policies (if any) that the trial allows

## Two days before Day 1

### Repository setup
- [ ] This spec repo (`compliance_pack_spec/`) imported into the workspace or accessible via a shared location
- [ ] Claude Code has been granted workspace access through its service principal identity
- [ ] The human collaborator has reviewed SPEC.md top to bottom
- [ ] Any questions about the spec have been raised and resolved (or documented as open items)

### Day 1 handoff prep
- [ ] Day 1 kickoff meeting scheduled with Claude Code's human collaborator
- [ ] Day 7 checkpoint confirmed on calendars
- [ ] Day 14 demo slot confirmed with stakeholders
- [ ] Fallback contact named: who to escalate to if Claude Code and the collaborator hit a blocker neither can resolve

## Day 0 itself

### Final environment verification
Run `tests/verify_environment.md` end to end. Every block must return `✓`. If any block fails, fix before Day 1 starts; a partial environment produces confusing errors that cost hours.

### Credit budget baseline
- [ ] Record the starting credit balance
- [ ] Set a target burn rate (typically: 40% by Day 5, 60% by Day 10, 80% by Day 14)
- [ ] Configure any available usage alerts

### Spec completeness confirmation
- [ ] `SPEC.md` present
- [ ] All ten core sections `01_context.md` through `10_runbook.md` present
- [ ] `schemas/` contains all required DDL and pattern files
- [ ] `synthetic_data/` contains generator_spec.md and dsr_principal_spec.md
- [ ] `tests/` contains all test specs
- [ ] `runbook/` contains this file plus troubleshooting, rollback, certificate_layout
- [ ] `reference/` contains the glossary, trial limits doc, and proposal PDF

## What happens on Day 1

Claude Code begins with:

1. Read SPEC.md top to bottom (already done during Day 0 prep but re-read for freshness)
2. Read §1 (`01_context.md`) and §2 (`02_runtime.md`) in detail
3. Confirm the verify_environment run is clean and recent
4. Run the synthetic data generator per §6 and validate the manifest
5. Kick off Auto Loader ingestion per §3.4

Day 1's success criterion: Bronze tables populated with all five source tables, row counts matching the manifest, first synthetic data visible in the workspace. Everything downstream depends on Day 1 landing cleanly.
