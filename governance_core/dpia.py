"""DPIA Auto-Generator — pure-Python orchestration for the DPIA flow.

Used by:
  - notebooks/03_agent_bricks.py (Agent 1, demo path)
  - pipelines/dpia_generator.py  (scheduled job, production path)

Both call ``run_dpia_generation`` so the demo and the job produce
identical artifacts. The caller injects spark, the LLM-invoker callable,
and (optionally) mlflow + dbutils — this module is otherwise free of
Databricks runtime imports and unit-testable with stubs.

Phase 3 changes:
  - DPIA output is now structured (8 named sections via Pydantic), not
    one prose blob. Validated post-LLM with ``DPIASections.model_validate_json``;
    parse failures fall back to raw text in ``dpia_text`` + the
    Pydantic ``ValidationError`` in ``parse_error``.
  - Prompts are pack-aware via ``governance_core.pack_loader.DPIATemplate``
    (loaded from ``regulations/<pack>/dpia_template.yaml``).
  - ``compliance_rules`` is added to the context so the model can cite
    rule_id values + section numbers in its gap analysis.

Side effects of one run:
  1. JSON artifact written to ``/Volumes/<catalog>/compliance/dpia_artifacts/dpia_<run_id>.json``
  2. One row appended to ``<catalog>.compliance.dpia_runs`` (created on first call)
"""

from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from governance_core.agent_prompts import (
    dpia_prompt_version,
    render_dpia_system,
    render_dpia_user,
)


PROMPT_MODULE = "governance_core.agent_prompts:render_dpia_user"
PARSE_ERROR_MAX_CHARS = 1000


# ---------------------------------------------------------------------
# Structured-output schema (Phase 3)
# ---------------------------------------------------------------------


class DPIASections(BaseModel):
    """Structured output for the DPIA agent.

    The 8 section *keys* are regulation-agnostic — DPDP §10, GDPR Art. 35,
    and CCPA all expect roughly this structure, so dashboard tiles render
    consistently regardless of which regulation pack is active. The
    section *content* differs per pack via ``regulations/<pack>/dpia_template.yaml``.

    ``extra='forbid'`` rejects unknown keys so we catch the
    "model invented a 9th section" case rather than silently dropping it.

    ``min_length=50`` is a soft tripwire: if the model returns "TBD" or
    a one-line stub for any section, validation fails and the row lands
    with ``dpia_sections=NULL`` + ``parse_error`` populated, which the
    reviewer sees and can re-trigger.
    """

    model_config = ConfigDict(extra="forbid")

    executive_summary: str = Field(
        min_length=50,
        description=(
            "2-3 paragraph overview of the data fiduciary's personal-data "
            "processing footprint. Reference total PII column count, table "
            "count, and the most material findings from the metadata. "
            "Plain prose; avoid bullet lists in this section."
        ),
    )
    data_inventory: str = Field(
        min_length=50,
        description=(
            "What personal data is held, in which systems, at what sensitivity "
            "tier. Cite specific table and column names from the provided "
            "metadata. A markdown table works well here."
        ),
    )
    processing_activities: str = Field(
        min_length=50,
        description=(
            "What the data is used for, with the legal basis under the active "
            "regulation. Reference specific compliance rules from the provided "
            "compliance_rules metadata where the rule wording bears on the activity."
        ),
    )
    risk_assessment: str = Field(
        min_length=50,
        description=(
            "Critical and high-risk findings with severity ratings. Quote "
            "specific gap counts and per-rule numbers from the provided "
            "metadata. Group by severity (critical → high → medium)."
        ),
    )
    compliance_gap_analysis: str = Field(
        min_length=50,
        description=(
            "Gaps against the active regulation's obligations. Cite specific "
            "rule_id values from the provided compliance_rules metadata."
        ),
    )
    consent_status: str = Field(
        min_length=50,
        description=(
            "Coverage of consent across purposes, using the provided "
            "consent_coverage metadata. Note any purposes with low grant "
            "rates or high withdrawal rates."
        ),
    )
    remediation_plan: str = Field(
        min_length=50,
        description=(
            "Prioritised actions to close gaps. Each action references a "
            "specific rule_id and the table/column from the metadata where "
            "the gap manifests. Order critical → high → medium."
        ),
    )
    residual_risk: str = Field(
        min_length=50,
        description=(
            "What risk remains after the remediation plan executes. Honest "
            "and specific, tied to the metadata — not boilerplate."
        ),
    )


def _schema_with_pack_overrides(overrides: dict[str, str]) -> dict[str, Any]:
    """Build the JSON schema given to the LLM, merging in any pack-specific
    section description overrides from ``DPIATemplate.section_descriptions``.

    The default Field descriptions in DPIASections are the regulation-agnostic
    baseline; the pack can replace any of them with regulation-specific
    guidance (e.g. "cite DPDP section numbers"). The Pydantic model itself is
    not mutated — we only customise the schema dict that goes into the prompt.
    """
    schema = DPIASections.model_json_schema()
    properties = schema.get("properties", {})
    for section_key, override_desc in overrides.items():
        if section_key in properties:
            properties[section_key]["description"] = override_desc
    return schema


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def convert_decimals(obj: Any) -> Any:
    """Recursively coerce non-JSON-native Spark/pandas values for ``json.dumps``.

    Two types surface from Spark + ``.toPandas().to_dict('records')`` and
    blow up json.dumps:

      - ``decimal.Decimal`` from numeric columns → coerce to ``float``.
      - ``numpy.ndarray`` from ``array<...>`` columns (e.g. compliance_rules.regulations,
        applicable_categories) → coerce to a plain ``list`` and recurse.

    Public so the Compliance Q&A agent (which also serializes Spark
    output) can reuse it.
    """
    if isinstance(obj, list):
        return [convert_decimals(item) for item in obj]
    if isinstance(obj, dict):
        return {key: convert_decimals(value) for key, value in obj.items()}
    if isinstance(obj, Decimal):
        return float(obj)
    # Duck-type for numpy ndarrays without importing numpy at module top.
    # str/bytes also have a tolist-shaped misuse risk, so explicitly exclude.
    if hasattr(obj, "tolist") and not isinstance(obj, (str, bytes, bytearray)):
        return convert_decimals(obj.tolist())
    return obj


def gather_dpia_context(spark, catalog: str) -> dict[str, list]:
    """Run the SQL queries that feed the DPIA prompt.

    Returns a dict whose keys match the placeholder names in
    ``governance_core.agent_prompts._DPIA_USER_TEMPLATE``. Each value is
    a list of records (one per row) so the prompt template can json.dumps
    them in deterministic order.

    Phase 3 added ``compliance_rules`` so the model can cite rule_id +
    rule_text directly in its compliance_gap_analysis section instead of
    summarising gap counts only.
    """
    return {
        "pii_summary": spark.sql(f"""
            SELECT sensitivity_tier,
                   COUNT(*) AS columns,
                   COUNT(DISTINCT source_table) AS tables
            FROM {catalog}.compliance.personal_data_register
            GROUP BY sensitivity_tier
            ORDER BY CASE sensitivity_tier
                WHEN 'critical' THEN 1
                WHEN 'high'     THEN 2
                WHEN 'medium'   THEN 3
                ELSE 4
            END
        """).toPandas().to_dict("records"),
        "critical_pii": spark.sql(f"""
            SELECT source_table, source_column, pii_type, pii_category
            FROM {catalog}.compliance.personal_data_register
            WHERE sensitivity_tier = 'critical'
        """).toPandas().to_dict("records"),
        "gaps_summary": spark.sql(f"""
            SELECT rule_type, severity,
                   COUNT(*) AS gap_count,
                   COUNT(DISTINCT table_name) AS tables
            FROM {catalog}.silver.compliance_gaps
            GROUP BY rule_type, severity
            ORDER BY CASE severity
                WHEN 'critical' THEN 1
                WHEN 'high'     THEN 2
                ELSE 3
            END
        """).toPandas().to_dict("records"),
        # Phase 3: feed the rule definitions so the model can cite
        # specific rule_id, description, and remediation text in gap
        # analysis. Schema mirrors phase1_bootstrap's CREATE TABLE for
        # bronze.compliance_rules — the table has no separate `citation`
        # column; the regulation context comes from the `regulations`
        # array column instead.
        "compliance_rules": spark.sql(f"""
            SELECT rule_id, rule_type, severity, regulations,
                   description, remediation
            FROM {catalog}.bronze.compliance_rules
            WHERE is_active = true
            ORDER BY rule_id
        """).toPandas().to_dict("records"),
        "consent_coverage": spark.sql(f"""
            SELECT * FROM {catalog}.gold.consent_coverage_summary
        """).toPandas().to_dict("records"),
        "data_sources": spark.sql(f"""
            SELECT source_name, source_type, ingestion_pattern
            FROM {catalog}.bronze.data_sources
            WHERE is_active = true
        """).toPandas().to_dict("records"),
        # silver.discovered_tables has no pii_column_count column of its own —
        # join the pre-aggregated table_pii_column_count from the personal data
        # register so the DPIA can quote a per-table PII count.
        "tables_scanned": spark.sql(f"""
            SELECT
                d.table_name,
                d.row_count,
                d.column_count,
                COALESCE(p.table_pii_column_count, 0) AS pii_column_count
            FROM {catalog}.silver.discovered_tables d
            LEFT JOIN (
                SELECT DISTINCT source_table, table_pii_column_count
                FROM {catalog}.compliance.personal_data_register
            ) p ON d.table_name = p.source_table
        """).toPandas().to_dict("records"),
    }


# ---------------------------------------------------------------------
# Audit table
# ---------------------------------------------------------------------


_AUDIT_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS {catalog}.compliance.dpia_runs (
    run_id              STRING    NOT NULL,
    generated_at        TIMESTAMP NOT NULL,
    generated_by        STRING    NOT NULL,
    catalog_name        STRING    NOT NULL,
    model_endpoint      STRING    NOT NULL,
    prompt_module       STRING    NOT NULL,
    prompt_version      STRING    NOT NULL,
    regulation_pack     STRING,
    context_snapshot    STRING    NOT NULL,
    dpia_text           STRING    NOT NULL,
    dpia_sections       MAP<STRING, STRING>,
    parse_error         STRING,
    artifact_path       STRING    NOT NULL,
    latency_seconds     DOUBLE,
    status              STRING    NOT NULL,
    reviewed_by         STRING,
    reviewed_at         TIMESTAMP,
    notes               STRING
) USING DELTA
  TBLPROPERTIES (
    'delta.enableChangeDataFeed' = 'true',
    'delta.logRetentionDuration' = 'interval 730 days'
  )
"""

_AUDIT_ROW_SCHEMA = (
    "run_id string, generated_at timestamp, generated_by string, "
    "catalog_name string, model_endpoint string, prompt_module string, "
    "prompt_version string, regulation_pack string, context_snapshot string, "
    "dpia_text string, dpia_sections map<string,string>, parse_error string, "
    "artifact_path string, latency_seconds double, "
    "status string, reviewed_by string, reviewed_at timestamp, notes string"
)


def _ensure_audit_table(spark, catalog: str) -> None:
    """Idempotently create the audit table.

    Note for upgrades: the Phase 3 DDL adds ``dpia_sections`` and
    ``parse_error`` columns. ``CREATE TABLE IF NOT EXISTS`` is a no-op
    on an existing table, so workspaces that already have a Phase 1/2
    table need a one-time ``ALTER TABLE ... ADD COLUMNS`` — see
    Phase 5's docs-sync notes when that lands. New workspaces get the
    Phase 3 schema directly from this DDL.
    """
    spark.sql(_AUDIT_TABLE_DDL.format(catalog=catalog))


# ---------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------


def _parse_dpia_output(raw: str) -> tuple[dict[str, str] | None, str | None]:
    """Parse + validate the LLM's JSON response against ``DPIASections``.

    Returns ``(sections_dict, parse_error)``. On success, ``sections_dict``
    is populated and ``parse_error`` is None. On failure, ``sections_dict``
    is None and ``parse_error`` is the validation/decode error message
    capped at PARSE_ERROR_MAX_CHARS so it fits in the audit column.

    Two failure modes are caught:
      - ValidationError: shape didn't match (missing field, too short, extra key)
      - JSONDecodeError / ValueError: response wasn't valid JSON at all
    """
    try:
        sections = DPIASections.model_validate_json(raw)
        return sections.model_dump(), None
    except ValidationError as e:
        return None, f"ValidationError: {e}"[:PARSE_ERROR_MAX_CHARS]
    except (json.JSONDecodeError, ValueError) as e:
        return None, f"JSONDecodeError: {e}"[:PARSE_ERROR_MAX_CHARS]


def run_dpia_generation(
    *,
    spark,
    catalog: str,
    invoke_llm: Callable[..., str],
    model_endpoint: str,
    artifact_volume: str | None = None,
    regulation_pack: str | None = None,
    pack=None,
    mlflow=None,
    dbutils=None,
) -> dict:
    """Generate one DPIA artifact and persist evidence.

    Steps:
      1. Gather metadata from UC (SQL queries → ``dpia_context``).
      2. Build prompt + JSON schema from the active regulation pack's
         DPIA template, then call the LLM.
      3. Parse + validate the LLM response with ``DPIASections``;
         fall back gracefully if parsing fails.
      4. Write JSON artifact to ``<artifact_volume>/dpia_<run_id>.json``.
      5. Append one row to ``<catalog>.compliance.dpia_runs``.

    Args:
      spark: SparkSession.
      catalog: Unity Catalog name (e.g. ``compliance_pack``).
      invoke_llm: Callable accepting ``(messages, *, max_tokens, temperature)``
        and returning the assistant's text. Caller owns retries, timeouts,
        and auth.
      model_endpoint: Endpoint name. Logged into the audit row.
      artifact_volume: Volume path for the JSON output. Defaults to
        ``/Volumes/<catalog>/compliance/dpia_artifacts``.
      regulation_pack: Active regulation pack code (e.g. ``dpdp_2023``).
        Logged into the audit row + drives template lookup if ``pack``
        is not passed in directly.
      pack: Optional pre-loaded ``governance_core.pack_loader.Pack``. When
        omitted, the active pack is loaded from the ``REGULATION_PACK``
        env var (default ``dpdp_2023``). Tests can pass a stubbed pack.
      mlflow: Optional ``mlflow`` module. When provided, prompt module,
        prompt version, model endpoint, and latency are logged.
      dbutils: Optional ``dbutils``. Used for ``dbutils.fs.put`` so
        ``/Volumes/`` paths resolve. Falls back to plain filesystem
        writes when not provided (unit tests).

    Returns:
      dict with keys: run_id, artifact_path, dpia_text, dpia_sections
      (parsed dict or None), parse_error (str or None), latency_seconds,
      context_snapshot.
    """
    # Resolve the regulation pack template
    if pack is None:
        from governance_core.pack_loader import load as load_pack
        pack = load_pack()
    template = pack.dpia_template()
    prompt_version = dpia_prompt_version(template)

    # 1. Gather context
    context = gather_dpia_context(spark, catalog)
    context = convert_decimals(context)

    # 2. Build prompt + schema, call LLM
    json_schema = _schema_with_pack_overrides(template.section_descriptions)
    system_prompt = render_dpia_system(template)
    user_prompt = render_dpia_user(context, template, json_schema)

    if mlflow is not None:
        # Per-call metadata goes on the trace as tags (mutable across calls).
        # log_param keys are immutable across calls and would clash with
        # compliance_qa's params on the same active run.
        tags = {
            "model_endpoint": model_endpoint,
            "prompt_module": PROMPT_MODULE,
            "prompt_version": prompt_version,
            "catalog": catalog,
        }
        if regulation_pack:
            tags["regulation_pack"] = regulation_pack
        mlflow.update_current_trace(tags=tags)

    t0 = time.monotonic()
    dpia_text = invoke_llm(
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        max_tokens=4000,
        temperature=0.3,
    )
    latency_seconds = time.monotonic() - t0
    if mlflow is not None:
        mlflow.log_metric("latency_seconds", round(latency_seconds, 3))

    # 3. Parse + validate against DPIASections
    sections_dict, parse_error = _parse_dpia_output(dpia_text)
    if mlflow is not None:
        mlflow.log_metric("parse_succeeded", 0 if parse_error else 1)

    # 4. Write artifact (uuid filename — never overwrite previous runs)
    run_id = uuid.uuid4().hex[:12]
    generated_at = datetime.now(timezone.utc)
    if artifact_volume is None:
        artifact_volume = f"/Volumes/{catalog}/compliance/dpia_artifacts"
    artifact_path = f"{artifact_volume}/dpia_{run_id}.json"

    artifact = {
        "run_id": run_id,
        "generated_at": generated_at.isoformat(),
        "model_endpoint": model_endpoint,
        "prompt_module": PROMPT_MODULE,
        "prompt_version": prompt_version,
        "regulation_pack": regulation_pack,
        "catalog": catalog,
        "context_snapshot": context,
        "dpia_text": dpia_text,
        "dpia_sections": sections_dict,
        "parse_error": parse_error,
    }
    artifact_json = json.dumps(artifact, indent=2)
    if dbutils is not None:
        dbutils.fs.put(artifact_path, artifact_json, overwrite=True)
    else:
        # Local/test fallback — works on plain filesystem paths.
        from pathlib import Path
        Path(artifact_path).parent.mkdir(parents=True, exist_ok=True)
        Path(artifact_path).write_text(artifact_json)

    # 5. Append audit row
    _ensure_audit_table(spark, catalog)
    generated_by = spark.sql("SELECT current_user()").first()[0]
    audit_row = spark.createDataFrame(
        [(
            run_id,
            generated_at,
            generated_by,
            catalog,
            model_endpoint,
            PROMPT_MODULE,
            prompt_version,
            regulation_pack,
            json.dumps(context),
            dpia_text,
            sections_dict,
            parse_error,
            artifact_path,
            float(latency_seconds),
            "draft",
            None,
            None,
            None,
        )],
        schema=_AUDIT_ROW_SCHEMA,
    )
    audit_row.write.mode("append").saveAsTable(f"{catalog}.compliance.dpia_runs")

    return {
        "run_id": run_id,
        "artifact_path": artifact_path,
        "dpia_text": dpia_text,
        "dpia_sections": sections_dict,
        "parse_error": parse_error,
        "latency_seconds": latency_seconds,
        "context_snapshot": context,
    }
