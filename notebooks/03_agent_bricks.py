# Databricks notebook source
# MAGIC %md
# MAGIC ## Setup — upgrade MLflow for tracing support
# MAGIC
# MAGIC `mlflow.trace` requires MLflow ≥ 2.14. Older cluster runtimes ship with
# MAGIC older MLflow, so upgrade in-notebook and restart Python before any
# MAGIC import of `mlflow`. Skip / comment out if your cluster already has
# MAGIC MLflow ≥ 2.14 installed.

# COMMAND ----------

# MAGIC %pip install --upgrade mlflow

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# MAGIC %md
# MAGIC # Agent Bricks — DPDP Compliance Agents
# MAGIC
# MAGIC **Three AI agents powered by Databricks foundation models:**
# MAGIC
# MAGIC | Agent | Purpose | Model |
# MAGIC |-------|---------|-------|
# MAGIC | **DPIA Generator** | Auto-generates Data Protection Impact Assessment from UC metadata | databricks-gpt-oss-120b |
# MAGIC | **Compliance Q&A** | Answers natural language questions about the data estate | databricks-gpt-oss-120b |
# MAGIC | **PII Classifier** | Uses ai_classify + ai_extract for unstructured content | Built-in AI Functions |
# MAGIC
# MAGIC ---

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

CATALOG = "compliance_pack"
# Keep in sync with scripts/persona_config.py:get_model_endpoint().
# Override via the 'model_endpoint' notebook widget (created below) or
# by editing both in one commit if you change the default.
dbutils.widgets.text("model_endpoint", "databricks-gpt-oss-120b",
                     "Foundation model endpoint")
MODEL_ENDPOINT = dbutils.widgets.get("model_endpoint")

# COMMAND ----------

# MAGIC %md
# MAGIC ## MLflow tracing + run tracking
# MAGIC
# MAGIC Every LLM call (DPIA generator, Compliance Q&A) is wrapped in
# MAGIC `@mlflow.trace` so the prompt, response, and latency are captured
# MAGIC as a span in the MLflow Experiment. The two SQL-based AI agents
# MAGIC (`ai_classify`, `ai_extract`) are wrapped in `mlflow.start_run`
# MAGIC blocks since Spark SQL can't be auto-traced.
# MAGIC
# MAGIC Set `MLFLOW_EXPERIMENT_PATH` widget to override the default.

# COMMAND ----------

import mlflow

dbutils.widgets.text("mlflow_experiment_path", "/Shared/dpdp_agent_bricks",
                     "MLflow experiment path")
MLFLOW_EXPERIMENT_PATH = dbutils.widgets.get("mlflow_experiment_path")

# Idempotent: creates the experiment if missing, otherwise no-op.
mlflow.set_experiment(MLFLOW_EXPERIMENT_PATH)
print(f"MLflow experiment: {MLFLOW_EXPERIMENT_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Shared LLM client + versioned prompts
# MAGIC
# MAGIC One place to configure retries, timeouts, and the path to the
# MAGIC prompt registry (`governance_core/agent_prompts.py`). Every
# MAGIC subsequent cell uses `_invoke_llm(messages, max_tokens, temperature)`
# MAGIC instead of a raw `requests.post`, so retry + timeout behavior is
# MAGIC consistent across all agents.

# COMMAND ----------

import json
import os as _os
import sys as _sys
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def _locate_repo_root() -> str:
    """Return the repo root on the Databricks filesystem.

    Same pattern used by pipelines/phase1_bootstrap.py. Needed so the
    notebook can import from governance_core/ — which lives at the
    bundle-synced repo root, not alongside the notebook."""
    candidates: list[str] = []
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
        nb_path = ctx.notebookPath().get()
        if nb_path:
            fs_path = "/Workspace" + nb_path if not nb_path.startswith("/Workspace") else nb_path
            # notebook sits in <repo>/notebooks/; repo root is parent of parent
            candidates.append(_os.path.dirname(_os.path.dirname(fs_path)))
    except Exception:
        pass
    candidates.extend([_os.getcwd(), _os.path.dirname(_os.getcwd()), "/Workspace", "."])
    for c in candidates:
        if c and _os.path.isdir(_os.path.join(c, "governance_core")):
            return c
    raise RuntimeError(
        "Cannot locate repo root with governance_core/ dir. "
        f"Checked: {candidates}. Make sure the bundle sync includes governance_core/**."
    )


_repo_root = _locate_repo_root()
if _repo_root not in _sys.path:
    _sys.path.insert(0, _repo_root)

from governance_core.agent_prompts import (  # noqa: E402
    COMPLIANCE_QA_SYSTEM,
    COMPLIANCE_QA_PROMPT_VERSION,
    render_compliance_qa_user,
)
from governance_core.dpia import (  # noqa: E402
    convert_decimals,
    run_dpia_generation,
)

# One-shot token + workspace host — reused by every LLM call below.
_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
_HOST = spark.conf.get("spark.databricks.workspaceUrl")
_ENDPOINT_URL = f"https://{_HOST}/serving-endpoints/{MODEL_ENDPOINT}/invocations"

# Session with retries on transient failures. 429 / 5xx → retry with
# exponential backoff up to 3 attempts; everything else propagates.
_retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
    raise_on_status=False,
)
_LLM_SESSION = requests.Session()
_LLM_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))

# 30-second per-attempt timeout. With 3 attempts + backoff, worst case
# is ~100s — DPIA responses land in 20–30s typically.
_LLM_TIMEOUT_SEC = 30


def _invoke_llm(messages: list[dict], *, max_tokens: int, temperature: float) -> str:
    """POST to the foundation-model endpoint with retries + timeout.

    Returns the assistant's text content. Raises on non-2xx responses
    after retries are exhausted, so callers see real failures instead
    of parsing a Databricks error JSON as if it were a completion."""
    r = _LLM_SESSION.post(
        _ENDPOINT_URL,
        headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
        json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
        timeout=_LLM_TIMEOUT_SEC,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


print("LLM client ready:")
print(f"  endpoint   = {MODEL_ENDPOINT}")
print(f"  timeout    = {_LLM_TIMEOUT_SEC}s per attempt")
print(f"  max_retries= 3 (on 429, 500, 502, 503, 504)")
print(f"  prompts    = {_repo_root}/governance_core/agent_prompts.py")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Agent 1: DPIA Auto-Generator
# MAGIC
# MAGIC Reads UC metadata + pii_findings + compliance_gaps + consent_events_log
# MAGIC and generates a DPDP-ready DPIA draft. Each run lands two artifacts:
# MAGIC
# MAGIC   1. JSON file at `/Volumes/{catalog}/compliance/dpia_artifacts/dpia_<run_id>.json`
# MAGIC   2. Audit row in `compliance.dpia_runs` (status = 'draft')
# MAGIC
# MAGIC The orchestration lives in `governance_core/dpia.py` so this demo
# MAGIC notebook and the scheduled job (`pipelines/dpia_generator.py`)
# MAGIC produce identical artifacts.
# MAGIC
# MAGIC **What this replaces:** 3-month manual DPIA exercise across Legal, Compliance, and IT teams.
# MAGIC
# MAGIC **What it produces:** Structured DPIA draft + queryable audit row in ~30 seconds.

# COMMAND ----------


@mlflow.trace(span_type="LLM", name="dpia_generator")
def generate_dpia():
    """Run the full DPIA flow against the live catalog.

    @mlflow.trace captures the prompt, response, and latency in the
    active experiment. The lifted function in governance_core/dpia.py
    handles the SQL queries, the LLM call, the volume write, and the
    audit-row insert — keeping the notebook focused on demo orchestration.
    """
    return run_dpia_generation(
        spark=spark,
        catalog=CATALOG,
        invoke_llm=_invoke_llm,
        model_endpoint=MODEL_ENDPOINT,
        regulation_pack="dpdp_2023",
        mlflow=mlflow,
        dbutils=dbutils,
    )


_dpia_result = generate_dpia()

print(f"\n✓ DPIA generated")
print(f"  run_id          = {_dpia_result['run_id']}")
print(f"  artifact_path   = {_dpia_result['artifact_path']}")
print(f"  latency_seconds = {_dpia_result['latency_seconds']:.2f}")
print(f"\nAudit row appended to {CATALOG}.compliance.dpia_runs (status='draft')")
print()
print(_dpia_result["dpia_text"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Agent 2: Compliance Q&A
# MAGIC
# MAGIC Natural language interface to the compliance data estate.
# MAGIC Ask questions like:
# MAGIC - "What critical PII do we hold?"
# MAGIC - "Which tables have the most compliance gaps?"
# MAGIC - "Does customer_04217 have active marketing consent?"
# MAGIC - "What is our overall risk score?"

# COMMAND ----------

# DBTITLE 1,Cell 9
@mlflow.trace(span_type="LLM", name="compliance_qa")
def compliance_qa(question: str) -> str:
    """Ask a natural language question about the DPDP compliance data estate.

    @mlflow.trace captures the question, the assembled context, the
    final prompt, and the model response as one span per call. Each
    invocation of this function from a notebook cell becomes its own
    trace in the MLflow Experiment UI.

    .. warning::
       The ``question`` argument is logged verbatim into MLflow traces
       by ``@mlflow.trace``. Likewise, the assembled SQL context can
       contain individual rows (e.g., a specific principal_id's consent
       events). On this POC the data is synthetic so this is fine, but
       BEFORE EVER CALLING THIS WITH REAL CUSTOMER DATA: redact PII
       from inputs, or move trace logging to an experiment with
       restricted ACLs and a documented retention policy.
    """
    mlflow.update_current_trace(tags={
        "model_endpoint": MODEL_ENDPOINT,
        "prompt_module": "governance_core.agent_prompts:render_compliance_qa_user",
        "prompt_version": COMPLIANCE_QA_PROMPT_VERSION,
    })

    # Gather relevant context based on the question
    context_parts = []

    # Always include summary stats
    summary = spark.sql(f"""
        SELECT
            (SELECT COUNT(*) FROM {CATALOG}.compliance.personal_data_register) AS pii_columns,
            (SELECT COUNT(*) FROM {CATALOG}.silver.compliance_gaps) AS total_gaps,
            (SELECT COUNT(*) FROM {CATALOG}.silver.compliance_gaps WHERE severity='critical') AS critical_gaps,
            (SELECT COUNT(*) FROM {CATALOG}.compliance.consent_events_log) AS consent_events,
            (SELECT COUNT(DISTINCT data_principal_id) FROM {CATALOG}.compliance.consent_events_log) AS consent_principals
    """).first()
    context_parts.append(f"Summary: {summary.pii_columns} PII columns, {summary.total_gaps} compliance gaps ({summary.critical_gaps} critical), {summary.consent_events} consent events across {summary.consent_principals} principals")

    # Add relevant details based on keywords
    q_lower = question.lower()

    if any(w in q_lower for w in ["critical", "risk", "sensitive", "high"]):
        critical = spark.sql(f"SELECT source_table, source_column, pii_type FROM {CATALOG}.compliance.personal_data_register WHERE sensitivity_tier = 'critical'").toPandas().to_dict('records')
        context_parts.append(f"Critical PII: {json.dumps(convert_decimals(critical))}")

    if any(w in q_lower for w in ["gap", "compliance", "remediat", "rule"]):
        gaps = spark.sql(f"SELECT rule_type, severity, COUNT(*) AS cnt FROM {CATALOG}.silver.compliance_gaps GROUP BY rule_type, severity").toPandas().to_dict('records')
        context_parts.append(f"Compliance gaps: {json.dumps(convert_decimals(gaps))}")

    if any(w in q_lower for w in ["consent", "marketing", "withdraw", "opt"]):
        coverage = spark.sql(f"SELECT * FROM {CATALOG}.gold.consent_coverage_summary").toPandas().to_dict('records')
        context_parts.append(f"Consent coverage: {json.dumps(convert_decimals(coverage))}")

    if "customer_04217" in q_lower or "dsr" in q_lower or "priya" in q_lower:
        dsr_data = spark.sql(f"""
            SELECT event_type, purpose, purpose_grant_status
            FROM {CATALOG}.compliance.consent_events_log
            WHERE data_principal_id = 'customer_04217'
        """).toPandas().to_dict('records')
        context_parts.append(f"customer_04217 consent: {json.dumps(convert_decimals(dsr_data))}")

    if any(w in q_lower for w in ["table", "source", "inventory", "register"]):
        tables = spark.sql(f"""
            SELECT
                d.table_name,
                d.row_count,
                COALESCE(p.table_pii_column_count, 0) AS pii_column_count
            FROM {CATALOG}.silver.discovered_tables d
            LEFT JOIN (
                SELECT DISTINCT source_table, table_pii_column_count
                FROM {CATALOG}.compliance.personal_data_register
            ) p ON d.table_name = p.source_table
        """).toPandas().to_dict('records')
        context_parts.append(f"Tables: {json.dumps(convert_decimals(tables))}")

    context = "\n".join(context_parts)

    return _invoke_llm(
        messages=[
            {"role": "system", "content": COMPLIANCE_QA_SYSTEM},
            {"role": "user", "content": render_compliance_qa_user(context, question)},
        ],
        max_tokens=1000,
        temperature=0.2,
    )

# COMMAND ----------

# Example queries
print("=" * 60)
print("Q: What critical PII do we hold?")
print("=" * 60)
print(compliance_qa("What critical PII do we hold and in which tables?"))

# COMMAND ----------

print("=" * 60)
print("Q: What are our top compliance gaps?")
print("=" * 60)
print(compliance_qa("What are the most urgent compliance gaps we need to address?"))

# COMMAND ----------

print("=" * 60)
print("Q: Does customer_04217 have active marketing consent?")
print("=" * 60)
print(compliance_qa("Does customer_04217 have active marketing consent? What is their consent status?"))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Agent 3: AI-Powered PII Classification
# MAGIC
# MAGIC Uses `ai_classify` and `ai_extract` for unstructured/semi-structured fields
# MAGIC that regex patterns cannot handle (diagnosis, prescriptions, free-text notes).

# COMMAND ----------

# Classify unstructured health data using ai_classify.
# Wrapped in an MLflow run so the SQL text, row count, and timing are
# captured — @mlflow.trace does not apply to Spark SQL calls.
import time

with mlflow.start_run(run_name="ai_classify_patients", nested=True) as run:
    mlflow.log_param("agent", "ai_classify")
    mlflow.log_param("source_table", f"{CATALOG}.silver.patients_tagged")
    mlflow.log_param("limit", 20)
    mlflow.log_param("diagnosis_labels",
                     "diagnosis,prescription,allergy_note,other_medical,non_medical")
    mlflow.log_param("prescription_labels",
                     "prescription,dosage_instruction,drug_name,non_medical")

    _t0 = time.time()
    ai_classification_results = spark.sql(f"""
        SELECT
            patient_id,
            primary_diagnosis,
            ai_classify(primary_diagnosis,
                ARRAY('diagnosis', 'prescription', 'allergy_note', 'other_medical', 'non_medical')
            ) AS ai_pii_class,
            current_prescription,
            ai_classify(current_prescription,
                ARRAY('prescription', 'dosage_instruction', 'drug_name', 'non_medical')
            ) AS ai_rx_class
        FROM {CATALOG}.silver.patients_tagged
        WHERE primary_diagnosis IS NOT NULL
        LIMIT 20
    """)
    # Materialize so the elapsed time reflects the actual SQL work.
    _row_count = ai_classification_results.count()
    _elapsed_s = time.time() - _t0
    mlflow.log_metric("row_count", _row_count)
    mlflow.log_metric("elapsed_seconds", round(_elapsed_s, 3))

display(ai_classification_results)

# COMMAND ----------

# Extract structured PII from unstructured text using ai_extract.
# Same rationale as the ai_classify cell above — MLflow run tracks
# params + metrics since Spark SQL isn't auto-traced.
with mlflow.start_run(run_name="ai_extract_patients", nested=True) as run:
    mlflow.log_param("agent", "ai_extract")
    mlflow.log_param("source_table", f"{CATALOG}.silver.patients_tagged")
    mlflow.log_param("limit", 10)
    mlflow.log_param("extraction_fields",
                     "patient_name,diagnosis,medication,dosage")

    _t0 = time.time()
    ai_extraction_results = spark.sql(f"""
        SELECT
            patient_id,
            primary_diagnosis,
            ai_extract(
                CONCAT('Patient: ', full_name, '. Diagnosis: ', primary_diagnosis, '. Prescription: ', current_prescription),
                ARRAY('patient_name', 'diagnosis', 'medication', 'dosage')
            ) AS extracted_pii
        FROM {CATALOG}.silver.patients_tagged
        WHERE primary_diagnosis IS NOT NULL
        LIMIT 10
    """)
    _row_count = ai_extraction_results.count()
    _elapsed_s = time.time() - _t0
    mlflow.log_metric("row_count", _row_count)
    mlflow.log_metric("elapsed_seconds", round(_elapsed_s, 3))

display(ai_extraction_results)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Summary
# MAGIC
# MAGIC ### What Agent Bricks delivers for DPDP compliance:
# MAGIC
# MAGIC | Agent | Manual Effort | With Agent Bricks | Impact |
# MAGIC |-------|--------------|-------------------|--------|
# MAGIC | **DPIA Generator** | 3 months across Legal + IT + Compliance | 30 seconds from UC metadata | 99% time reduction |
# MAGIC | **Compliance Q&A** | SQL expertise + domain knowledge needed | Natural language questions | Democratizes compliance access |
# MAGIC | **PII Classifier** | Manual review of unstructured fields | ai_classify + ai_extract | Automated health/diagnosis classification |
# MAGIC
# MAGIC ### Databricks features used:
# MAGIC - **Foundation Model Serving** — databricks-gpt-oss-120b endpoint
# MAGIC - **AI Functions** — ai_classify, ai_extract (built into SQL)
# MAGIC - **Unity Catalog** — metadata, lineage, and tags as agent context
# MAGIC - **Volumes** — DPIA output storage
# MAGIC
# MAGIC ### Phase 1 extensions:
# MAGIC - Deploy as **Mosaic AI Agent** with tool-calling for live SQL queries
# MAGIC - Add **DPBI notification drafter** triggered by Lakewatch breach alerts
# MAGIC - Add **multi-language consent notice generator** (22 languages)
# MAGIC - Register agents in **Unity Catalog** for governance and versioning