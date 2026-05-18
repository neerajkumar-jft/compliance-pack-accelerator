# Persona Governance — Unity Catalog + Workspace ACLs

This document explains exactly what's governed by Unity Catalog and what's
governed by Workspace ACLs for the four personas (CCO, GC, CMO, CFO).
Written for SA review — if a reviewer asks "what if user X tries to read
table Y?", this doc answers it.

The persona governance layer is **regulation-pack agnostic** — the same
four personas serve every loaded pack (DPDP, UK GDPR, EU GDPR, …).
Persona scoping composes with the per-data-subject jurisdiction routing
defined in [ADR-0001](adr/0001-multi-jurisdiction-data-subject-routing.md):
a CMO's UC grants restrict what tables they can read; the principal's
jurisdiction column then restricts which pack's rules apply to each row
within those tables. Both fences run at query time; neither replaces the
other.

**For deploying this setup in your own workspace**, see
`docs/persona_deploy.md`. That guide walks through the six-script
sequence and auto-detects the deployer's email to create plus-addressed
persona users (your email goes in, all four persona login links come
back to your inbox).

## TL;DR

| Boundary | Enforcement | What it protects |
|---|---|---|
| UC table grants | Unity Catalog (query-time) | Persona user cannot read tables outside their grants, regardless of UI path |
| Warehouse `CAN_USE` | Workspace | Persona user cannot run any SQL without this |
| Dashboard `CAN_READ` | Workspace | Persona user cannot open someone else's dashboard URL |
| Dashboard `embed_credentials=false` | Workspace + UC | Queries run as the viewer, so UC enforces — not as the dashboard owner |
| Genie space `CAN_RUN` | Workspace | Persona user cannot open someone else's Genie space |
| Genie space `data_sources` allowlist | Genie agent | Agent won't attempt queries outside its scoped tables |
| Databricks App `CAN_USE` | Workspace | Persona user cannot open the DPIA Review app at all |
| App SP UC grants (SELECT + UPDATE on `dpia_runs`) | Unity Catalog | Only the app's runtime SP can mutate the audit table — no persona user has direct UPDATE |

Two boundaries matter most for review:
1. **UC grants** — the authoritative table-level fence. Enforced at SQL
   execution time regardless of UI path.
2. **Dashboard `embed_credentials=false`** — the flag that makes UC
   actually kick in for dashboard viewers (queries run as viewer, not
   owner).

## Personas and identities

Because this trial workspace isn't identity-federated to its account
metastore, UC rejects workspace-group grantees (including the default
`users` group — tested and confirmed). We use **user emails** as UC
grantees instead. If the account admin later creates account-level
groups, the pattern is identical — swap the grantee from email to
group in `scripts/apply_persona_uc_grants.py`.

| Persona | Demo user email (pattern) | Display name |
|---|---|---|
| CCO | `<deployer-local>+compliance-cco@<deployer-domain>` | Compliance Pack POC — CCO Persona (Chief Compliance Officer) |
| GC | `<deployer-local>+compliance-gc@<deployer-domain>` | Compliance Pack POC — GC Persona |
| CMO | `<deployer-local>+compliance-cmo@<deployer-domain>` | Compliance Pack POC — CMO Persona |
| CFO | `<deployer-local>+compliance-cfo@<deployer-domain>` | Compliance Pack POC — CFO Persona |

The `<deployer-*>` placeholders come from `databricks current-user me`
(auto-detected by `scripts/setup_persona_users.py`). The actual four
emails for the current deployment are in
`dashboards/personas/.persona_emails.json` — read it, or run
`python3 scripts/persona_config.py` to echo the resolved context.

Why plus-addresses? Admin-settable passwords are disabled on most
modern Databricks workspaces (see screenshot-verified "Edit user"
page with no password action). The invite/reset flow sends a link to
the email address, so plus-addressing routes all four persona reset
links to a single real mailbox (the deployer's own) where one person
controls all four persona logins.

To do a live "log in as persona X" demo, first finish the one-time
password-set flow per user (trigger "Forgot password" on the workspace
login page, click the link in your real inbox, set a password), then
log in with the plus-addressed email in an incognito window.

## Two grant layers per persona

Each persona has two layers of allowlists that are *intentionally*
different:

### Layer 1 — Genie space `data_sources` (agent boundary)

The Genie agent will only generate SQL against these tables. This is
the narrow, domain-scoped list — what the persona's *agent* should be
thinking about. See `scripts/setup_persona_genie_spaces.py` constant
`PERSONA_DEFS[persona]["tables"]`.

```
cco  →  personal_data_register, pii_findings, compliance_gaps,
        discovered_tables, consent_coverage_summary,
        persona_overview_metrics, persona_sensitivity_histogram,
        sf_{leads,contacts,accounts}_tagged              (Lakeflow Connect sim)
        federation_{lead_scoring,campaign_response}_tagged (Federation sim)
gc   →  compliance_gaps, consent_events_log, notice_versions, dsr_requests
cmo  →  marketing_eligible_principals, consent_events_log
cfo  →  compliance_gaps, discovered_tables
```

CCO's data_sources expanded from 7 → 12 on 2026-04-27 to cover the new ingestion paths (Day 3 + Day 4). Other personas were left narrow on purpose — their domains haven't broadened. Any persona that needs SF/federation visibility later just gets the same two-layer extension (Genie list + UC grants).

### Layer 2 — UC grants (UI-render boundary)

Layer 1 **plus** two Gold views that carry the Executive Overview
aggregate tiles (risk score, compliance score, sensitivity histogram,
top-line counters):

```
SHARED_OVERVIEW_TABLES = [
    compliance_pack.gold.persona_overview_metrics,      # 1-row scorecard
    compliance_pack.gold.persona_sensitivity_histogram, # 4-row tier breakdown
]
```

So every persona's UC grants = domain-scoped tables ∪ shared overview
views. The views expose only pre-aggregated numbers — no per-column PII
metadata, no table-level findings rows. Non-CCO personas no longer have
SELECT on raw `pii_findings` or (for CMO) `compliance_gaps`.

> **Phase 0 framework note (2026-04-24):** the 3-layer defense-in-depth
> below is the *enforcement architecture* and is regulation-agnostic. In
> the parallel 4-layer *code architecture* ([`docs/modular_framework.html`](modular_framework.html)),
> Layers 1/2 (platform core + taxonomy) host the fences below; Layer 3
> (regulation pack) supplies the policy values (rules, residency countries,
> DSR SLAs) those fences enforce; Layer 4 (persona delivery) surfaces the
> enforced state as dashboards and Genie agents. Both layerings are valid
> lenses on the same system — governance ops folks read the
> defense-in-depth lens, regulation-pack authors read the code lens.

**Why this is SA-defensible:** Three independent fences, each enforced
by a different system with its own audit trail:
  - **Layer 1 (Genie agent):** agent can only generate SQL against its
    `data_sources` allowlist.
  - **Layer 2 (UC grants):** persona emails have SELECT only on their
    domain tables ∪ the two aggregate views — raw PII tables are
    inaccessible to non-CCO personas even via SQL editor or REST API.
    Grants survive phase1_bootstrap re-runs automatically: the job
    captures existing grants on the 5 re-created views before
    `CREATE OR REPLACE` and restores them in a finally block (see
    `_capture_grants_on` / `_restore_grants_on` in
    `pipelines/phase1_bootstrap.py`).
  - **Layer 3 (column masks + row filters):** even if a persona
    somehow reaches a PII-bearing column, UC rewrites the bytes at
    read time. Two row filters are in force: `residency_filter` on
    `silver.employees_tagged` (non-admins see only `country='India'`
    rows — DPDP §16) and `persona_purpose_scope` on
    `compliance.consent_events_log` (CMO sees only marketing-relevant
    purposes; admin + other personas see all rows). Bronze is
    unreachable because no persona has `USE_SCHEMA` on it.

## Enforcement matrix

For each "what if X tries to do Y" scenario:

| Scenario | Blocked by | Observable outcome |
|---|---|---|
| CMO user opens CCO dashboard URL directly | Dashboard ACL | "You don't have permission" page |
| CMO user opens CCO Genie space URL directly | Genie ACL | Permission denied banner |
| CCO user opens the Lakeview SQL editor and writes `SELECT * FROM compliance_pack.gold.marketing_eligible_principals` | UC grants | `PERMISSION_DENIED` on table |
| CMO user tries to run a query against `compliance.personal_data_register` via warehouse | UC grants | `PERMISSION_DENIED` on table |
| CMO Genie agent is asked "show me customer PII" | Genie space scoping (Layer 1) | Agent refuses — table not in allowlist |
| CMO user (hypothetically, if a dashboard query tried it) hits `personal_data_register` via Executive Overview | UC grants | Tile fails with permission error |
| CCO user asks the GC agent "how many DSRs are past SLA?" via API | Genie ACL | Can't invoke the agent at all |
| CCO user somehow obtains GC Genie space URL, opens it | Genie ACL | Can't open space |
| Anyone who is not the dashboard owner + not in the persona's ACL opens the dashboard | Dashboard ACL | Not visible in dashboard list, URL returns 403 |
| CMO user opens the DPIA Review app URL | App `CAN_USE` ACL | Permission denied — only CCO + GC + CFO have `CAN_USE` |
| CFO user opens the DPIA Review app and tries to click Approve | In-app role check | Approve button is hidden; CFO is view-only audience |
| CCO user opens the SQL editor and runs `UPDATE compliance.dpia_runs SET status='approved' …` directly | UC grants | `PERMISSION_DENIED` on UPDATE — no persona has direct UPDATE on the table; only the app's runtime SP does |

## What was actually configured

### Unity Catalog grants (per persona user)

Applied via `scripts/apply_persona_uc_grants.py` (reads
`.persona_emails.json` for the grantee mapping). Verify with (as admin):

```sql
-- Per-table view (SHOW GRANTS TO `email` has a known server-side
-- redaction issue with local-parts, use SHOW GRANTS ON instead):
SHOW GRANTS ON TABLE compliance_pack.compliance.personal_data_register;
SHOW GRANTS ON TABLE compliance_pack.gold.marketing_eligible_principals;
-- etc.
```

Each persona user has:
- `USE CATALOG` on `compliance_pack`
- `USE SCHEMA` on each schema they need
- `SELECT` on their layer-1 ∪ layer-2 tables

## Bronze is pipeline-only (not masked, not granted)

The bronze schema (`compliance_pack.bronze.*`) holds **raw source data** with
no column masks applied. It is never granted to any human user or
workspace group — only the DLT pipeline (running as the deployer or a
service principal) has the access path needed to read bronze and
materialize silver.

**Why no masks on bronze?** Masks on bronze would force the DLT
pipeline itself to see masked values, breaking the silver
materialization. The mask layer lives at silver because that's where
human queries happen.

**Why this is safe:** persona users have neither `USE_SCHEMA` nor any
table grant on bronze. `SHOW TABLES IN compliance_pack.bronze` returns an
empty list, and any `SELECT * FROM compliance_pack.bronze.*` attempt fails
with `PERMISSION_DENIED` at the UC layer — before the query even hits
the table.

Verify the bronze fence:

```sql
SHOW GRANTS ON SCHEMA compliance_pack.bronze;
-- Expected: zero rows for any compliance-*@ persona email. Only the catalog
-- owner (deployer / admin group / bundle service principal) should
-- have access.
```

**If an SA asks "what prevents a new workspace user from querying
bronze?":** the answer is UC grants — same mechanism as every other
UC-governed resource. No `USE_SCHEMA` on bronze → no visibility, no
query access. A future colleague added to the workspace inherits
nothing on bronze by default; explicit grant required.

**Production hardening (Phase 2):** when the `compliance-pack-builder`
service principal takes over pipeline ownership (see
`scripts/transfer_ownership_to_sp.py`), the SP becomes the sole
principal with `USE_SCHEMA` + `MODIFY` on bronze. Admin access
remains for break-glass only.

## Silver-layer data quality (warn-over-drop design)

The medallion DLT pipeline declares 16 data-quality expectations across the
5 silver tables (see `pipelines/medallion.py` — 5 `@dlt.expect_or_drop` +
11 `@dlt.expect`). DLT auto-emits pass/fail rates to its event log.

**Design decision — why most expectations warn rather than drop:** bad emails,
malformed phone numbers, and out-of-enum values flow through to silver with a
DLT warning rather than being dropped. This is intentional, not an oversight:

- **PII classifier coverage.** The 16-pattern library needs to see imperfect
  real-world data to correctly flag PII columns. If every malformed email were
  dropped, entire columns could end up looking clean to the classifier and
  never get tagged — creating a blind spot in the register.
- **Audit evidence fidelity.** When a DPO asks "what did we hold about principal
  X?", we need to show actual source state, not a sanitized subset.
- **Hard-drops reserved for identity.** `@dlt.expect_or_drop` fires only on
  NULL/empty primary keys (`employee_id`, `customer_id`, `patient_id`,
  `transaction_id`, `user_id`). A row without a primary key is unjoinable and
  untraceable; dropping it is correct.

**What this means for an SA's likely question** ("why aren't you enforcing
email format at ingestion?"): because the DPDP register should reflect what's
actually in source systems. Warning-level expectations surface the issue in
the DLT event log without distorting the PII inventory. If the customer's
production deployment wants hard enforcement, flip `@dlt.expect` →
`@dlt.expect_or_drop` on a per-column basis — but only after a policy
decision about the trade-off.

**What's NOT DQ-governed:** `consent_events_log` and `compliance_gaps` are
written by `pipelines/phase1_bootstrap.py` via plain Spark SQL, not DLT, so
they have no expectation layer. For the POC this is fine (bootstrap
generates deterministic correct-by-construction data). For a production
deployment with real consent ingestion, either route the consent sync
through a DLT table with expectations or add post-insert validation checks
in a separate quality job.

### Warehouse `CAN_USE`

A serverless SQL warehouse (auto-resolved via
`scripts/persona_config.py:get_warehouse_id()`) — `CAN_USE` granted
to each of the four persona users. Verify via:

```
WAREHOUSE_ID=$(python3 -c "from scripts.persona_config import get_warehouse_id; print(get_warehouse_id())")
databricks api get /api/2.0/permissions/warehouses/$WAREHOUSE_ID
```

### Dashboards — embed_credentials=false + ACL

All four persona dashboards republished with `embed_credentials=false`
so queries run as the **viewer**, activating UC enforcement. Each
dashboard has ACL:

- `CAN_MANAGE`: the deployer (from `databricks current-user me`)
- `CAN_READ`:   the matching persona user only

Verify via:
```
databricks api get /api/2.0/permissions/dashboards/<dashboard_id>
```

### Genie spaces — per-persona ACL

Each of the four Genie spaces has the matching persona user granted
`CAN_RUN` (view space + ask questions, but not edit). Admin `CAN_MANAGE`
is inherited from the parent directory. Verify via:

```
databricks api get /api/2.0/permissions/genie/<space_id>
```

## Resource IDs

Resource IDs are per-deployment. Read them from:

```
cat dashboards/personas/.dashboard_ids.json     # persona → Lakeview id
cat dashboards/personas/.genie_space_ids.json   # persona → Genie space id
cat dashboards/personas/.persona_emails.json    # persona → email
```

These three files are written by the orchestrator and are the
authoritative mapping for this workspace.

## How to verify end-to-end (no real user login needed)

Run each of these as workspace admin. They impersonate the UC boundary
without needing the persona user's password:

```sql
-- What CCO CAN see (should return rows)
SELECT COUNT(*) FROM compliance_pack.compliance.personal_data_register;
-- Grantee: compliance-cco@... → OK (in grants)

-- What CMO CAN'T see (should fail)
SELECT COUNT(*) FROM compliance_pack.compliance.personal_data_register;
-- Grantee: compliance-cmo@... → PERMISSION_DENIED (not in grants)
```

To actually run the queries as the persona user, use:

```bash
WAREHOUSE_ID=$(python3 -c "from scripts.persona_config import get_warehouse_id; print(get_warehouse_id())")
CMO_EMAIL=$(python3 -c "import json; print(json.load(open('dashboards/personas/.persona_emails.json'))['cmo'])")

databricks api post /api/2.0/sql/statements \
  --json "{\"warehouse_id\":\"$WAREHOUSE_ID\",
           \"statement\":\"SHOW GRANTS TO \`$CMO_EMAIL\`\",
           \"wait_timeout\":\"30s\"}"
```

…which will return the rows for the CMO persona. Cross-reference
with `PERSONA_TABLES["cmo"]` in `scripts/apply_persona_uc_grants.py`.

**Live walkthrough tips:** the plus-addressed emails route reset links
to the admin's real mailbox. One-time setup per user:

1. Log out / open incognito
2. Go to the workspace login page
3. Enter the persona email, click "Forgot password"
4. Check the deployer's inbox for the reset link (plus-addressing routes
   all four persona links there)
5. Click, set a password, log in — you're now the persona user
6. Try opening a different persona's dashboard via URL → should 403

## DPIA Review app — three-layer permission model

The Databricks App at `apps/dpia_review/` is the human-review surface
for the DPIA Auto-Generator (replaces the Phase 2 self-asserted-reviewer
CLI). Three independent permission layers govern who can do what:

1. **Workspace `CAN_USE` on the app** — controls who can open the URL
   at all. Granted to CCO + GC + CFO; CMO has no business need.
2. **App's runtime SP holds SELECT + UPDATE on `compliance.dpia_runs`,
   plus SELECT on `/Volumes/compliance_pack/compliance/dpia_artifacts`** —
   the only identity in the workspace with UPDATE on the table. No
   persona user has UPDATE, so the only path to flipping `status` is
   through the app's enforce-the-rules code.
3. **In-app role check** — the app reads
   `dashboards/personas/.persona_emails.json` at startup and only
   shows the Approve button when the logged-in user matches the CCO
   or GC entry. CFO can open the app and view all rows but the button
   is hidden — they're audience, not approver.

Why this is stronger than the CLI: the CLI's `--reviewer` flag is
self-asserted (anyone who can type the command can type any email).
The app reads the verified Databricks user identity from
`X-Forwarded-Email` request headers, which Databricks Apps inject
on every request. The audit row's `reviewed_by` is therefore the
*verified* signer, not a self-claimed one.

The app's SP id is only known after the first
`databricks bundle deploy` — the SELECT + UPDATE grants on
`dpia_runs` are documented as a manual post-deploy step in
`docs/persona_deploy.md` rather than auto-applied. Re-running the
deploy doesn't reset the grants (UC remembers).

## Migrating to account-level groups later

When the Databricks account admin creates account-level groups
(`compliance-cco`, `compliance-gc`, `compliance-cmo`, `compliance-cfo`) and assigns them to
this workspace:

1. Edit `scripts/apply_persona_uc_grants.py` — replace the
   `persona_emails[persona]` grantee with the group name (backticks
   around hyphens: `` `compliance-cco` ``).
2. Re-run the script. UC grants will re-apply to the groups. The old
   email grants will remain until explicitly `REVOKE`d.
3. Similarly update warehouse / dashboard / Genie ACLs to use
   `group_name` instead of `user_name`.
4. Delete the four demo users (`databricks users delete ...`) if
   they're no longer needed.

Nothing else changes — the Genie space allowlists, dashboard content,
and the attach-Genie-banner scripts are identity-agnostic.

## Known gaps

- **Owner sees everything.** The dashboard and Genie spaces are owned
  by the deployer, who has `CAN_MANAGE` everywhere. This is expected.
  For production, transfer ownership to a service principal or rotate
  ownership per persona.
- **Plus-addressed demo users.** All four persona emails route to a
  single real inbox (admin's). This is a POC workaround because
  admin-settable passwords are disabled on this workspace and the
  account isn't identity-federated. For production, replace with real
  colleague emails (or account-level groups once federation is on).
- **Entitlements must be set per user.** After SCIM creates the user,
  their default entitlements are `Workspace access: On, SQL: On,
  Consumer access: Off`. For the persona demo, toggle
  `Consumer access: On` (required for dashboards/Genie) and optionally
  `Workspace access: Off`. This step is UI-only, not yet scripted.
- **SHOW GRANTS may list `compliance_pack.silver.pii_findings` on CMO/GC/CFO**
  because of the shared-overview layer. This is intentional — see
  "Two grant layers per persona" above. If an SA flags this, point to
  the Layer 1 (Genie space) scoping, which is the agent boundary and
  remains narrowly scoped.
