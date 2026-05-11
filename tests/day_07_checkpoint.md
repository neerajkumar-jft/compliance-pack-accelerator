# Day 7 internal checkpoint

> ⚠️ **Pre-build planning document — 14-day sprint has concluded.** Retained as historical reference; not an active checklist.

This checkpoint is the primary risk-management point of the 14-day sprint. It is not optional. The human collaborator and Claude Code sit together and walk through the Module 01 deliverables in a live session.

## Goal of the checkpoint

Confirm that Module 01 (PII discovery) is demo-ready, and that the Module 02 scaffolding is in place for Days 8-12 work. If any checkpoint item fails, Days 8-12 must not proceed until the failure is remediated.

## Agenda — 90 minutes

### [0:00 – 0:15] Progress summary (Claude Code presents)
- What was built Days 1-5
- What tests passed, which failed (and how resolved)
- Credit consumption vs budget
- Any spec deviations, with rationale

### [0:15 – 0:30] Live run of INT-01 (register completeness)
- Execute the test with human observing
- Walk through the output: 5 tables scanned, ≥ 20 findings, ≥ 8 critical
- Sample 3 random findings; human collaborator confirms classification is correct
- Discuss any false positives in the low-confidence bucket

### [0:30 – 0:45] Live run of INT-06 (UC tags applied)
- Verify tags visible in Unity Catalog UI for 2-3 representative columns
- Confirm the tags render in the lineage graph
- Review any tagging failures logged by the classification job

### [0:45 – 1:00] Live run of INT-04 (lineage)
- Navigate the UC lineage graph from a Silver table back to Bronze and the source file
- Confirm the `personal_data_register` view shows up as a downstream consumer

### [1:00 – 1:15] Module 02 readiness review
- Verify Lakebase instance is provisioned (per §2.4)
- Verify `consent_events`, `notice_versions`, `data_principals`, `dsr_requests` DDL is applied
- Verify the seed notice `marketing_notice v1 en-IN` is inserted
- Confirm Lakebase → Delta sync is configured (even if no events flowing yet)
- Confirm the `marketing_eligible_principals` view definition is deployed (empty result OK at this point)

### [1:15 – 1:30] Day 8-12 planning
- Review the Day 8-12 plan per §0.5 in SPEC.md
- Identify risks or known blockers
- Confirm the Day 12 demo dry-run is scheduled for Day 13

## Pass criteria

All of the following must be true to pass the checkpoint:

- [ ] `SELECT COUNT(*) FROM compliance_pack.compliance.personal_data_register` returns ≥ 20
- [ ] At least 8 rows in the register have `sensitivity_tier = 'critical'`
- [ ] At least 15 rows have `classification_confidence >= 0.85`
- [ ] Every row in the register has a non-null `data_type`, `pii_category`, `pii_type`, `sensitivity_tier`
- [ ] Unity Catalog shows tags on at least 3 manually-spot-checked columns
- [ ] Unity Catalog lineage graph shows Bronze → Silver → `personal_data_register` for at least one table
- [ ] INT-01 passes
- [ ] INT-04 passes
- [ ] INT-06 passes
- [ ] Lakebase instance is reachable from the workspace
- [ ] All four Lakebase tables exist and have their DDL applied
- [ ] The notice version seed row is present
- [ ] Credit consumption is < 50% of trial budget
- [ ] No spec deviations have been made silently (any deviations are documented)

## If the checkpoint fails

The Day 6 buffer was explicitly reserved for this case. Use it.

### Low-risk failures (catch up in Day 6)
- 1-2 tests failing due to misconfiguration (e.g., missing grant)
- Classification produced unexpected false positives (tighten pattern hints)
- A Silver table has a column-type mismatch with Bronze cast

These are fixable in hours. Resolve, re-run the failing tests, pass the checkpoint by end of Day 6 or early Day 7.

### Medium-risk failures (compress Days 8-12)
- Lakebase provisioning delays
- `ai_classify` unavailable or slow
- Auto Loader checkpoint corruption requiring reset

Adjust the Day 8-12 plan to compress non-demo-path work. Raise with the extended team if the 14-day deadline becomes infeasible.

### High-risk failures (escalate)
- Architectural blocker discovered (e.g., UC privilege model doesn't allow a required operation)
- Trial workspace hits an unexpected limit
- Synthetic data generator non-deterministic

Escalate to the Databricks SA and the customer's executive sponsor. Extend the sprint if needed; do not ship a broken POC.

## Artifact captured from the checkpoint

At the end of the checkpoint, capture a signed summary with:
- Pass/fail decision
- Any open items carried forward to Days 8-12
- Any spec deviations agreed to
- Signatures (or Slack acknowledgments) from Claude Code's human collaborator and the customer lead

This artifact goes into `runbook/checkpoint_log.md` (created on first use) and is referenced during the Day 14 demo close.
