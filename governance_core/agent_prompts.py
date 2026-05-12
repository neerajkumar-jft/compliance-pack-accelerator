"""Versioned prompt templates for the Agent Bricks notebook.

Keeping prompts in a Python module (rather than inline in the notebook)
means prompt changes show up cleanly in git diffs, they can be imported
and tested from other callers, and the notebook stays focused on
orchestration rather than prose.

The DPIA prompts are pack-aware: the legal framework name, system
prompt, and per-section descriptions come from
``regulations/<pack>/dpia_template.yaml`` via
``governance_core.pack_loader.DPIATemplate``. The Pydantic schema for the
8 DPIA sections lives in ``governance_core.dpia.DPIASections`` and is
regulation-agnostic — section keys stay stable across packs so the
dashboard tile (Phase 4) renders consistently.

The Compliance Q&A prompts are not yet pack-aware (DPDP-leaning
language); pack-aware refactor for Q&A is a Phase 4+ concern.

Consumers:
  - notebooks/03_agent_bricks.py → generate_dpia()     uses render_dpia_system + render_dpia_user
  - pipelines/dpia_generator.py  → run_dpia_generation uses the same
  - notebooks/03_agent_bricks.py → compliance_qa()     uses COMPLIANCE_QA_SYSTEM + render_compliance_qa_user
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from governance_core.pack_loader import DPIATemplate


def _short_hash(s: str) -> str:
    """8-char content hash — used as a prompt version identifier."""
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:8]


# ---------------------------------------------------------------------
# DPIA generator — pack-aware
# ---------------------------------------------------------------------

# User-template skeleton. Pack-specific values (legal framework name,
# citation style) and the JSON schema are injected at render time.
_DPIA_USER_TEMPLATE: str = """You are a Data Protection Impact Assessment (DPIA) generator for {legal_framework_name}.

Based on the following data estate metadata, generate a structured DPIA as a single JSON object conforming to the schema below.

**Required output shape:** valid JSON only, no markdown fences, no preamble. The keys and types must match the schema exactly.

```json
{json_schema}
```

**Citation style for this regulation:** {section_citation_style}

---

### PII Inventory Summary
{pii_summary}

### Critical PII Columns
{critical_pii}

### Compliance Gaps
{gaps_summary}

### Compliance Rules (rule definitions to cite by rule_id)
{compliance_rules}

### Consent Coverage
{consent_coverage}

### Data Sources
{data_sources}

### Tables Scanned
{tables_scanned}

---

Generate the DPIA grounded strictly in the metadata above. Reference specific table names, column names, gap counts, and rule_id values. Do not invent statistics. Output exactly one JSON object matching the schema."""


def render_dpia_system(template: "DPIATemplate") -> str:
    """Build the DPIA system prompt from a regulation pack's template.

    The system prompt frames the model's role and citation expectations.
    Sourced from ``regulations/<pack>/dpia_template.yaml::system_prompt``;
    prepended with a pack-version stamp (ADR-0001 Q2) so the model — and
    a reviewer reading the prompt — can see which authored version of the
    pack is in force.
    """
    if template.pack_version and template.pack_version != "0.0.0":
        return f"[regulation pack v{template.pack_version}]\n\n{template.system_prompt}"
    return template.system_prompt


def render_dpia_user(
    context: dict[str, Any],
    template: "DPIATemplate",
    json_schema: dict[str, Any],
) -> str:
    """Fill the DPIA user prompt with metadata + JSON schema.

    Args:
      context: result of ``governance_core.dpia.gather_dpia_context``.
        Required keys: pii_summary, critical_pii, gaps_summary,
        consent_coverage, data_sources, tables_scanned. Optional:
        compliance_rules (added in Phase 3 — empty list if missing).
      template: pack-specific framing (legal name, citation style,
        section description overrides).
      json_schema: the ``DPIASections.model_json_schema()`` dict, with
        any pack-specific section description overrides already merged
        in by the caller.

    Decimal values must already be converted to float (see
    ``governance_core.dpia.convert_decimals``); ``json.dumps`` will
    fail otherwise.
    """
    return _DPIA_USER_TEMPLATE.format(
        legal_framework_name=template.legal_framework_name,
        section_citation_style=template.section_citation_style,
        json_schema=json.dumps(json_schema, indent=2),
        pii_summary=json.dumps(context["pii_summary"], indent=2),
        critical_pii=json.dumps(context["critical_pii"], indent=2),
        gaps_summary=json.dumps(context["gaps_summary"], indent=2),
        compliance_rules=json.dumps(context.get("compliance_rules", []), indent=2),
        consent_coverage=json.dumps(context["consent_coverage"], indent=2),
        data_sources=json.dumps(context["data_sources"], indent=2),
        tables_scanned=json.dumps(context["tables_scanned"], indent=2),
    )


def dpia_prompt_version(template: "DPIATemplate") -> str:
    """Hash of (system + user template + pack-specific section descriptions).

    Flips when ANY of these change — the user template lives in this
    file, the system prompt + section description overrides live in the
    pack. MLflow traces stay groupable per prompt configuration.
    """
    parts = [
        template.system_prompt,
        _DPIA_USER_TEMPLATE,
        json.dumps(template.section_descriptions, sort_keys=True),
        template.pack_version,                 # ADR-0001 Q2 — flip the hash on pack bumps
    ]
    return _short_hash("|".join(parts))


# ---------------------------------------------------------------------
# Compliance Q&A — not yet pack-aware (DPDP-specific language)
# ---------------------------------------------------------------------

COMPLIANCE_QA_SYSTEM: str = (
    "You are a DPDP compliance assistant. Answer questions using ONLY "
    "the provided data context. Be specific with numbers and table "
    "names. If you cannot answer from the context, say so."
)

_COMPLIANCE_QA_USER_TEMPLATE: str = "Context:\n{context}\n\nQuestion: {question}"


def render_compliance_qa_user(context: str, question: str) -> str:
    """Fill the compliance-Q&A user prompt template."""
    return _COMPLIANCE_QA_USER_TEMPLATE.format(context=context, question=question)


COMPLIANCE_QA_PROMPT_VERSION: str = _short_hash(COMPLIANCE_QA_SYSTEM + _COMPLIANCE_QA_USER_TEMPLATE)
