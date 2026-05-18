# CLAUDE.md — Context for AI Assistants

This file provides context for Claude Code or any AI assistant working on this repository.

> **Architectural decisions live in [`docs/adr/`](docs/adr/README.md).** Read
> the relevant ADR before changing the rule engine, the data model, the pack
> contract, or anything cross-cutting. ADRs are the single source of truth
> when code and docs disagree. ADR-0001 (multi-jurisdiction data-subject
> routing) is the foundational one — read it before you touch the loader.

## What This Project Is

**Compliance Pack Accelerator** — a Databricks-native compliance platform
that handles multiple data-protection regulations through pluggable
"regulation packs". Each pack (DPDP India 2023, UK GDPR, EU GDPR, CCPA,
…) is a directory of yaml files plus a small region-specific PII-pattern
module. Compliance applies *per data subject* — each principal's
jurisdiction routes to the pack that governs them — so an Indian SaaS
with UK clients applies DPDP rules to Indian principals and UK GDPR
rules to UK principals, simultaneously, in the same database. See
[ADR-0001](docs/adr/0001-multi-jurisdiction-data-subject-routing.md)
for why.

## Current State (as of May 2026)

### What's Built and Live (4-pack multi-jurisdiction deployment)

Everything below is deployed and queryable on the Databricks workspace.
DPDP / UK GDPR / EU GDPR are live in the workspace; CCPA is authored
locally and MERGEs into bronze on the next bundle deploy.

| Component | State | Key Detail |
|---|---|---|
| PII Discovery (Module 01) | COMPLETE | 36 findings across 10 silver objects · universal patterns + 5 IN-specific + 5 UK-specific + 8 EU-specific + 7 US-specific PII patterns active · AI free-text classifier (`pipelines/pii_ai_scan.py`, daily, per-row state in `compliance.pii_ai_scan_row_state`) bridged via `silver.pii_findings_all` |
| Consent Intelligence (Module 02) | COMPLETE | 1,000 events in Delta (no Lakebase — not available in trial) |
| Compliance Audit (Module 05) | COMPLETE | 51 multi-pack rules across 4 packs (9 DPDP + 12 UK GDPR + 14 EU GDPR + 16 CCPA), 818 gaps tagged by source pack, per-row routing, pack semver in DPIA prompts |
| Agent Bricks | COMPLETE | DPIA generator (pack-version-stamped prompts), Compliance Q&A, PII classifier |
| DPIA Generator (productionised) | COMPLETE | Quarterly cron + structured pydantic output + GC/CCO approval flow + Databricks Review App + multi-regulator citation merge across loaded packs · pipeline auto-derives applicable packs from `jurisdiction_breakdown` (no hardcoded pack) · `dpia_runs.regulation_packs ARRAY<STRING>` records contributors |
| Dashboard | COMPLETE | 10-page Lakeview dashboard + jurisdiction filter on Executive Overview + unmapped-principals counter tile (ADR-0001 Q3) |
| Data Source Onboarding | COMPLETE | Notebook with Federation/Lakeflow/Auto Loader patterns |
| Synthetic Data | COMPLETE | Seed=42, deterministic, mixed-jurisdiction 70/25/5 IN/GB/unmapped (3,503 IN + 1,258 GB + 239 NULL customers live) |
| Regulation-Pack Framework (ADR-0001) | COMPLETE | `governance_core/` + 4 packs in `regulations/`. Multi-pack loader, per-data-subject rule routing, pack semver (Q2), loader-side jurisdiction validation (Q3). M1–M4 + Q2/Q3/EU/CCPA all merged. |

### What's NOT Built (Phase 1 scope)

- Module 03 — DSR Hub (portal, erasure execution, certificates)
- Module 04 — Breach Detection (needs Lakewatch, Private Preview)
- Module 06 — Retention & Transfers (needs retention catalog)
- Lakebase OLTP tier (not available in free trial workspace)
- Real client data source connections (POC uses synthetic only)

## Workspace Details

The POC is workspace-portable; do not rely on a specific URL, warehouse ID,
notebook path, or dashboard ID being the same across deploys. To recover the
real values for the current deploy, ask Databricks at runtime:

- **URL**: read `targets.dev.workspace.host` in `databricks.yml` (set per-deployer by `scripts/configure_workspace_host.sh`).
- **Catalog**: `compliance_pack` (stable across deploys).
- **Schemas**: `bronze`, `silver`, `gold`, `compliance`, `federation_mock` (stable).
- **SQL Warehouse ID**: discover via `scripts/persona_config.py:get_warehouse_id()` (looks up the warehouse named `Serverless Starter Warehouse` in the current workspace, falls back to a STARTING/STOPPED instance if needed).
- **Notebooks**: deployed under `/Workspace/Users/${workspace.current_user.userName}/.bundle/compliance-pack/dev/files/notebooks/` by the bundle.
- **Dashboard ID**: written into `dashboards/personas/.dashboard_ids.json` by `scripts/slice_dashboards.py --upload`; read it from there rather than hardcoding.

## Key Design Decisions

1. **No Lakebase in POC** — free trial doesn't include it. Consent events go directly to Delta. Same schema, same CDF. Lakebase is the production upgrade path for sub-second OLTP writes.

2. **No Python connectors** — Databricks SA explicitly asked us to replace all 28 Python connectors with Lakehouse Federation, Lakeflow Connect, and Auto Loader. The `src/connectors/` directory from the old accelerator is deliberately deleted.

3. **Classification via SQL API** — PII scanning runs column-by-column regex matching via SQL warehouse API calls (not Spark `.collect()` loops). Regex patterns need `\\\\` double-escaping for SQL RLIKE.

4. **Dashboard adapted from accelerator** — the 177K `dpdp_dashboard.lvdash.json` from the old accelerator was adapted with column name mappings: `job_id` → `scan_job_id`, `category` → `pii_category`, `sensitivity` → `sensitivity_tier`, `detection_method` → `classifier_source`.

5. **Agent Bricks uses `databricks-gpt-oss-120b`** — the open-source model endpoint. Other foundation models (`gpt-5-4`, `gpt-5-4-mini`) are rate-limited to 0 on the free tier. The `ai_classify` and `ai_extract` SQL functions work fine. Endpoint name is centralized in `scripts/persona_config.py:get_model_endpoint()` (overridable via `COMPLIANCE_MODEL_ENDPOINT` env) and in the `model_endpoint` widget of `notebooks/03_agent_bricks.py` — change both together if you swap endpoints.

6. **Decimal serialization fix** — `toPandas()` returns `Decimal` objects from SQL numeric columns. The `convert_decimals()` helper in `03_agent_bricks.py` recursively converts them to `float` before `json.dumps`.

## Schema Mappings (Our Schema vs Original Accelerator)

If you're reading old accelerator code or dashboard queries:

| Accelerator Column | Our Column | Table |
|---|---|---|
| `job_id` | `scan_job_id` | pii_findings, compliance_gaps |
| `category` | `pii_category` | pii_findings |
| `sensitivity` | `sensitivity_tier` | pii_findings |
| `detection_method` | `classifier_source` | pii_findings |
| `data_type` | `column_data_type` | pii_findings |
| `sample_pattern` | `sample_match_redacted` | pii_findings |

## DSR Test Principal

`customer_04217` (Oeshi Desai) is the designated test principal:
- 1 row in `customers_tagged`, 1 in `users_tagged` (USR001298, linked by email `odate@example.net`)
- 20 transactions in `transactions_tagged`
- 4 consent events: marketing_email granted then withdrawn, analytics granted, third_party_sharing declined
- Not in employees or patients

## How to Execute SQL Against the Workspace (from CLI)

The classification and setup scripts use this pattern to run SQL via the API:

```python
import subprocess, json
from scripts.persona_config import get_warehouse_id

WH = get_warehouse_id()  # never hardcode — varies per workspace

def run_sql(sql):
    payload = {"warehouse_id": WH, "statement": sql, "wait_timeout": "50s"}
    with open("/tmp/_sql_payload.json", "w") as f:
        json.dump(payload, f)
    result = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements", "--json", "@/tmp/_sql_payload.json"],
        capture_output=True, text=True
    )
    d = json.loads(result.stdout)
    state = d.get("status", {}).get("state", "UNKNOWN")
    if state != "SUCCEEDED":
        return {"error": d.get("status", {}).get("error", {}).get("message", "")[:500]}
    return {"data": d.get("result", {}).get("data_array", [])}
```

**Important**: `wait_timeout` must be between 5s and 50s. The `@` prefix for `--json` reads from file (avoids shell escaping issues).

**Local Python deps**: scripts that run on the deployer's machine (not on the workspace cluster) need a few packages installed locally — `pydantic`, `PyYAML`, `requests`, `faker`. They live in the top-level `requirements.txt`. Databricks runtime deps (pyspark, dlt, mlflow) are deliberately NOT in that file because the workspace provides them. Without `pydantic` installed locally, `setup_agent_bricks.py`'s prompts-loader check fails with `ModuleNotFoundError: pydantic`.

## How to Upload Notebooks

`notebooks/**` is in the bundle's `sync.include`, so `databricks bundle deploy`
pushes every notebook to the bundle deploy root automatically. The demo
notebooks land at:

```
/Workspace/Users/${ME}/.bundle/compliance-pack/dev/files/notebooks/03_agent_bricks
/Workspace/Users/${ME}/.bundle/compliance-pack/dev/files/notebooks/01_add_data_source
```

Reach for `databricks workspace import` only for one-off uploads to a
non-bundle path (e.g., to seed a teammate's home folder for a hand-driven
demo). The bundle path is the canonical demo location.

```bash
# One-off upload to a custom path — rarely needed:
ME="$(databricks current-user me | python3 -c 'import json,sys; print(json.load(sys.stdin)["userName"])')"
databricks workspace mkdirs "/Workspace/Users/${ME}/compliance_pack"
databricks workspace import \
  "/Workspace/Users/${ME}/compliance_pack/<name>" \
  --file notebooks/<name>.py --language PYTHON --format SOURCE --overwrite
```

## How to Deploy the Dashboard

```python
# Update via API
databricks api patch /api/2.0/lakeview/dashboards/<dashboard_id> --json @payload.json

# Publish (substitute the warehouse_id from get_warehouse_id() above)
databricks api post /api/2.0/lakeview/dashboards/<dashboard_id>/published \
  --json '{"warehouse_id": "<your-warehouse-id>", "embed_credentials": true}'
```

## Docs-sync checklist (mandatory after any substantive change)

Whenever you land code, SQL, or config that changes BEHAVIOR, TESTS, or the
DEPLOY PATH, audit these docs in one pass before committing. Miss any of
them and the next teammate hits a surprise. Invoke the skill via
`/dpdp:sync-docs` for a systematic walk-through, or run this checklist
manually:

| Doc | Update when... |
|---|---|
| `README.md` | Deploy sequence changes, new script added, new env var, top-level architecture shift |
| `docs/persona_deploy.md` | Same as README + any new prereq or ordering change |
| `docs/changelog_and_gaps.html` | Any LANDED/OPEN/DEFERRED item; add a `gap-card` + flip `§5.x` roadmap entry; include date |
| `docs/how_to_test.html` | New test exists, schema/threshold drifts, new UC object to verify |
| `docs/architecture.html` | Platform feature table (line ~390) and module cards if structure changed; illustrative-numbers caveat footer |
| `docs/persona_governance.md` | New UC grant/mask/filter, new layer in the 3-layer-defense narrative, enforcement matrix row |
| `docs/presentation.html` | Only for top-level capability slides; skip for internal tweaks |
| `docs/business_pitch.html` | Only for numbers that appear in the pitch (rare) |
| `CLAUDE.md` (this file) | New env var, new file under "don't edit", new workspace prereq |
| `.github/workflows/validate.yml` | New test file — add to `integration-tests` step |
| `tests/*.py` | Any ground-truth table/column/threshold changed |

After updating, run `databricks bundle validate --target dev` and
`python3 -c "import ast; ast.parse(open('<each_changed_py>').read())"`
to sanity-check before committing.

## Regulation-pack framework (Phase 0, landed 2026-04-24)

The POC is regulation-adaptive. Values that differ between regulations
(compliance rules, notices, retention defaults, residency filter, DSR defaults,
region-specific PII patterns) live in `regulations/<REGULATION_PACK>/`.
Regulation-agnostic code + universal PII patterns live in `governance_core/`.
Switching packs is one env var + `bundle run phase1_bootstrap` — no code edit.

- `REGULATION_PACK` env var (default `dpdp_2023`) selects the active pack
- `governance_core/pack_loader.py` is the single entry point (typed accessors)
- `schemas/pii_patterns.py` is a composition shim — still the one import for consumers
- Current pack under `regulations/dpdp_2023/` has 9 yaml files + `pii_patterns.py`
- To add a new pack (UK GDPR, CCPA, PIPEDA): copy `dpdp_2023/` as template,
  rewrite each yaml/py with regulation-specific values. Zero code edits needed
  outside `regulations/<new_code>/`. Contract in `regulations/README.md`.

Framework overview: `docs/modular_framework.html`.

## Files You Should NOT Edit Without Understanding Dependencies

- `schemas/pii_patterns.py` — the 16-pattern library is referenced by classification scripts
- `schemas/bronze.sql` and `schemas/silver.sql` — column names must match the generator output exactly
- `generate_synthetic_data.py` — changing this changes the DSR principal's footprint; update tests accordingly
- `dashboards/dpdp_compliance.lvdash.json` — 177K file, adapted from accelerator; edit via API, not by hand

## Origin

This project started as the `dpdp_accelerator-dev` (by Sinki.ai), which used 28 Python connectors. After Databricks SA review, it was rebuilt from scratch using Databricks-native features only. The PII pattern library, compliance rules, and dashboard SQL queries were ported; the connector code was deleted entirely.
