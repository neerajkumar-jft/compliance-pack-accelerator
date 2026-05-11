# Databricks notebook source
# MAGIC %md
# MAGIC # DPIA Auto-Generator — production pipeline
# MAGIC
# MAGIC Reads UC metadata (PII register, compliance gaps, consent coverage,
# MAGIC discovered tables, data sources), calls the foundation-model serving
# MAGIC endpoint, writes a JSON artifact to the
# MAGIC `compliance.dpia_artifacts` volume, and appends one row per run to
# MAGIC `compliance.dpia_runs`.
# MAGIC
# MAGIC The pure-Python orchestration lives in `governance_core/dpia.py` so
# MAGIC the demo notebook (`notebooks/03_agent_bricks.py`) and this
# MAGIC scheduled job produce identical artifacts. This notebook is the
# MAGIC thin Databricks wrapper that handles widgets, MLflow setup, and
# MAGIC the retry-aware HTTP client for the LLM call.
# MAGIC
# MAGIC Wire as a `notebook_task` in `resources/jobs.yml` (Phase 2).

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

dbutils.widgets.text("catalog", "compliance_pack", "Unity Catalog name")
dbutils.widgets.text("model_endpoint", "databricks-gpt-oss-120b",
                     "Foundation model endpoint")
dbutils.widgets.text("mlflow_experiment_path", "/Shared/dpdp_agent_bricks",
                     "MLflow experiment path")
dbutils.widgets.text("regulation_pack", "dpdp_2023", "Active regulation pack")

CATALOG = dbutils.widgets.get("catalog")
MODEL_ENDPOINT = dbutils.widgets.get("model_endpoint")
MLFLOW_EXPERIMENT_PATH = dbutils.widgets.get("mlflow_experiment_path")
REGULATION_PACK = dbutils.widgets.get("regulation_pack")

print(f"Catalog:          {CATALOG}")
print(f"Endpoint:         {MODEL_ENDPOINT}")
print(f"Experiment:       {MLFLOW_EXPERIMENT_PATH}")
print(f"Regulation pack:  {REGULATION_PACK}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Repo-root locator + imports
# MAGIC
# MAGIC Same pattern as `pipelines/phase1_bootstrap.py` and the agent-bricks
# MAGIC notebook — needed so the job can import from `governance_core/`,
# MAGIC which lives at the bundle-synced repo root.

# COMMAND ----------

import os as _os
import sys as _sys


def _locate_repo_root() -> str:
    candidates: list[str] = []
    try:
        ctx = dbutils.notebook.entry_point.getDbutils().notebook().getContext()  # noqa: F821
        nb_path = ctx.notebookPath().get()
        if nb_path:
            fs_path = "/Workspace" + nb_path if not nb_path.startswith("/Workspace") else nb_path
            # this notebook lives at <repo>/pipelines/dpia_generator.py
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

import mlflow  # noqa: E402
from governance_core.dpia import run_dpia_generation  # noqa: E402

# COMMAND ----------

# MAGIC %md
# MAGIC ## MLflow experiment

# COMMAND ----------

mlflow.set_experiment(MLFLOW_EXPERIMENT_PATH)
print(f"MLflow experiment: {MLFLOW_EXPERIMENT_PATH}")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Retry-aware LLM client
# MAGIC
# MAGIC 30-second per-attempt timeout, 3 retries on 429/5xx with exponential
# MAGIC backoff. Worst case ~100s; DPIA responses typically land in 20-30s.

# COMMAND ----------

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

_TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
_HOST = spark.conf.get("spark.databricks.workspaceUrl")
_ENDPOINT_URL = f"https://{_HOST}/serving-endpoints/{MODEL_ENDPOINT}/invocations"

_retry = Retry(
    total=3,
    backoff_factor=1.0,
    status_forcelist=[429, 500, 502, 503, 504],
    allowed_methods=["POST"],
    raise_on_status=False,
)
_LLM_SESSION = requests.Session()
_LLM_SESSION.mount("https://", HTTPAdapter(max_retries=_retry))
_LLM_TIMEOUT_SEC = 30


def _extract_assistant_text(content):
    """Pull the assistant's actual answer text out of an OpenAI-style
    chat-completions ``content`` field.

    Most chat models return ``content`` as a plain string. **Reasoning
    models like databricks-gpt-oss-120b return a list of typed blocks**
    instead, with the reasoning trace as the first block and the
    actual answer as a later "message"-typed block, e.g.

        [
          {"type": "reasoning", "summary": [{"type": "summary_text", "text": "..."}]},
          {"type": "message",   "content": "<the actual JSON output>"},
        ]

    Variants exist (``"output_text"`` / ``"text"`` keys, etc.); we walk
    the list from the END (the answer always comes last) and return the
    first non-empty string-valued ``content`` or ``text`` field on a
    non-reasoning block. Falls back to ``json.dumps`` when nothing
    matches so the parse_error column captures the raw response for
    forensic review.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for item in reversed(content):
            if not isinstance(item, dict):
                continue
            t = item.get("type", "")
            if t in ("reasoning", "reasoning_content", "thinking"):
                continue
            for key in ("content", "text"):
                v = item.get(key)
                if isinstance(v, str) and v.strip():
                    return v
        import json as _json
        return _json.dumps(content)
    return str(content)


def invoke_llm(messages, *, max_tokens, temperature):
    """POST to the foundation-model endpoint with retries + timeout.

    Handles both plain-string and reasoning-model list-of-blocks
    response shapes (see ``_extract_assistant_text``).
    """
    r = _LLM_SESSION.post(
        _ENDPOINT_URL,
        headers={"Authorization": f"Bearer {_TOKEN}", "Content-Type": "application/json"},
        json={"messages": messages, "max_tokens": max_tokens, "temperature": temperature},
        timeout=_LLM_TIMEOUT_SEC,
    )
    r.raise_for_status()
    raw_content = r.json()["choices"][0]["message"]["content"]
    return _extract_assistant_text(raw_content)


print("LLM client ready:")
print(f"  endpoint    = {MODEL_ENDPOINT}")
print(f"  timeout     = {_LLM_TIMEOUT_SEC}s per attempt")
print(f"  max_retries = 3 (on 429, 500, 502, 503, 504)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Run

# COMMAND ----------


@mlflow.trace(span_type="LLM", name="dpia_generator")
def _traced_run():
    return run_dpia_generation(
        spark=spark,
        catalog=CATALOG,
        invoke_llm=invoke_llm,
        model_endpoint=MODEL_ENDPOINT,
        regulation_pack=REGULATION_PACK,
        mlflow=mlflow,
        dbutils=dbutils,
    )


result = _traced_run()

print(f"\n✓ DPIA generated")
print(f"  run_id          = {result['run_id']}")
print(f"  artifact_path   = {result['artifact_path']}")
print(f"  latency_seconds = {result['latency_seconds']:.2f}")
print(f"\nAudit row appended to {CATALOG}.compliance.dpia_runs (status='draft')")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Recent runs

# COMMAND ----------

spark.sql(f"""
    SELECT run_id, generated_at, generated_by, status, latency_seconds
    FROM {CATALOG}.compliance.dpia_runs
    ORDER BY generated_at DESC
    LIMIT 10
""").show(truncate=False)

# COMMAND ----------

# MAGIC %md
# MAGIC ## DPIA text

# COMMAND ----------

print(result["dpia_text"])
