"""Create one Genie space per persona (CCO, GC, CMO, CFO).

Each space is scoped to the tables that persona is allowed to query.
Table-scoping is the hard part (enforced at the space level) — custom
instructions and sample questions can also be set via API, but the
serialized_space schema is proto-defined and undocumented beyond the
data_sources section, so we:

  1. Create the space here with version + data_sources only.
  2. Emit a setup document (docs/persona_genie_instructions.md) with
     the custom-instructions text and sample questions each persona
     should have pasted into the Genie UI.

Usage:
    python scripts/setup_persona_genie_spaces.py

Writes dashboards/personas/.genie_space_ids.json and the setup doc.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = REPO_ROOT / "dashboards" / "personas"
IDS_FILE = OUTPUT_DIR / ".genie_space_ids.json"
DOC_FILE = REPO_ROOT / "docs" / "persona_genie_instructions.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_config import get_warehouse_id, get_workspace_url  # noqa: E402

WAREHOUSE_ID = get_warehouse_id()
WORKSPACE_URL = get_workspace_url()

PERSONA_DEFS = {
    "cco": {
        "title": "DPDP — CCO Agent",
        "description": "Chief Compliance Officer agent. Answers questions about PII inventory, compliance gaps, and remediation priorities across the DPDP data estate.",
        "tables": [
            "compliance_pack.compliance.personal_data_register",
            "compliance_pack.silver.pii_findings",
            # AI-PII surfaces (added when pii_ai_scan landed). The UNION view
            # `pii_findings_all` is the day-to-day query surface for combined
            # regex+AI inventory; underlying `pii_findings_ai` enables drill-
            # down on AI-specific fields (model_endpoint, label distribution).
            "compliance_pack.silver.pii_findings_all",
            "compliance_pack.silver.pii_findings_ai",
            "compliance_pack.silver.compliance_gaps",
            "compliance_pack.silver.discovered_tables",
            "compliance_pack.gold.consent_coverage_summary",
            # Scorecard + histogram views — CCO already has SELECT on these
            # via SHARED_OVERVIEW_TABLES in apply_persona_uc_grants.py. Adding
            # them to the Genie lets questions like "what's my risk score?"
            # answer from a single-row view instead of hand-aggregating the
            # raw silver tables.
            "compliance_pack.gold.persona_overview_metrics",
            "compliance_pack.gold.persona_sensitivity_histogram",
            # Lakeflow Connect simulation (Salesforce) — silver tables. CCO sees
            # PII columns masked (mask UDFs in compliance.mask_*).
            "compliance_pack.silver.sf_leads_tagged",
            "compliance_pack.silver.sf_contacts_tagged",
            "compliance_pack.silver.sf_accounts_tagged",
            # Lakehouse Federation simulation — silver views over federation_mock.
            # Same governance: masks live on the federation_mock backing tables
            # so reads through the views inherit them.
            "compliance_pack.silver.federation_lead_scoring_tagged",
            "compliance_pack.silver.federation_campaign_response_tagged",
        ],
        "instructions": (
            "You are the DPDP compliance assistant for the Chief Compliance Officer.\n\n"
            "**Scope**\n"
            "Answer questions about India's DPDP Act 2023, the organization's PII "
            "inventory, compliance gaps, and remediation priorities. Use only the "
            "data in the scoped tables.\n\n"
            "**Style**\n"
            "Be specific — always cite counts, severities, and table/column names. "
            "When users ask about 'risk' or 'critical' findings, combine critical "
            "PII (sensitivity_tier='critical') with critical gaps (severity='critical').\n\n"
            "**Out of scope**\n"
            "For legal/litigation questions → GC agent. For marketing audience → "
            "CMO agent. For ₹ penalty exposure → CFO agent."
        ),
        "sample_questions": [
            "What critical PII do we hold and in which tables?",
            "How many compliance gaps do we have by severity?",
            "Which tables have the most unresolved gaps?",
            "What is the PII coverage per source table?",
            "Which DPDP rules are we violating most often?",
        ],
    },
    "gc": {
        "title": "DPDP — GC Agent",
        "description": "General Counsel agent. Answers legal-exposure questions about DPDP obligations, consent withdrawals, notice-version history, and DPIA generation history.",
        "tables": [
            "compliance_pack.silver.compliance_gaps",
            "compliance_pack.compliance.consent_events_log",
            "compliance_pack.compliance.notice_versions",
            "compliance_pack.compliance.dsr_requests",
            # GC owns DPDP §10 sign-off — DPIA history is the artifact they
            # approve. Scoped to GC only; CCO sees status via the dashboard
            # tile, CMO/CFO have no business need.
            "compliance_pack.compliance.dpia_runs",
        ],
        "instructions": (
            "You are the DPDP legal assistant for the General Counsel.\n\n"
            "**Scope**\n"
            "Focus on legal exposure: DPDP Act sections and obligations, DSR "
            "procedures, consent-withdrawal evidence, breach-notification "
            "timelines, and notice-version history. Only use data from the "
            "scoped tables.\n\n"
            "**Style**\n"
            "Cite DPDP section numbers where relevant (e.g. §5, §8, §11). Focus "
            "on critical and high severity gaps — these are the exposure "
            "concerns. For consent, distinguish between 'granted', 'withdrawn', "
            "and 'declined'.\n\n"
            "**Out of scope**\n"
            "For overall compliance posture → CCO agent. For marketing audience "
            "→ CMO agent. For ₹ penalty math → CFO agent."
        ),
        "sample_questions": [
            "How many critical and high severity gaps do we have?",
            "Show me all consent withdrawals grouped by purpose.",
            "What is the latest notice_version and when was it published?",
            "Which gaps map to the most serious DPDP obligations?",
            "How many DSR requests are past SLA?",
            "Show me the latest approved DPIA and who approved it.",
            "List every DPIA generated this year with status and reviewer.",
        ],
    },
    "cmo": {
        "title": "DPDP — CMO Agent",
        "description": "Chief Marketing Officer agent. Answers questions about marketing-eligible audience, consent by purpose, and campaign-safe segmentation.",
        "tables": [
            "compliance_pack.gold.marketing_eligible_principals",
            "compliance_pack.compliance.consent_events_log",
        ],
        "instructions": (
            "You are the DPDP marketing assistant for the Chief Marketing Officer.\n\n"
            "**Scope**\n"
            "Answer questions about marketing-eligible audience size, consent "
            "status by purpose, and whether a specific principal can be contacted "
            "for a specific purpose. Use only the scoped tables.\n\n"
            "**Principal-level queries**\n"
            "When a user asks 'Can I email customer_XXXXX?', look up the most "
            "recent consent event for that principal_id + purpose='marketing_email' "
            "in consent_events_log. If the latest event has purpose_grant_status = "
            "'granted' the answer is YES; 'withdrawn' or 'declined' is NO. If no "
            "event exists, say you cannot confirm.\n\n"
            "**Out of scope**\n"
            "Never speculate about consent state that isn't in the data. For "
            "compliance gaps → CCO. For legal questions → GC. For penalty "
            "exposure → CFO."
        ),
        "sample_questions": [
            "How many principals are currently eligible for marketing email?",
            "Show the current consent status by purpose across all principals.",
            "Can I email customer_04217?",
            "How many principals withdrew marketing consent in the past 30 days?",
            "Break down marketing consent by channel.",
        ],
    },
    "cfo": {
        "title": "DPDP — CFO Agent",
        "description": "Chief Financial Officer agent. Answers questions about DPDP penalty exposure, gap counts weighted by penalty ceilings, and remediation cost estimates.",
        "tables": [
            "compliance_pack.silver.compliance_gaps",
            "compliance_pack.silver.discovered_tables",
        ],
        "instructions": (
            "You are the DPDP risk-quantification assistant for the Chief Financial Officer.\n\n"
            "**Scope**\n"
            "Focus on ₹-denominated penalty exposure, gap counts weighted by DPDP "
            "penalty ceilings, and remediation cost estimates.\n\n"
            "**Penalty model**\n"
            "DPDP Act penalty ceilings per open gap (₹ crore):\n"
            "  critical = 250\n"
            "  high     = 150\n"
            "  medium   = 50\n"
            "  low      = 5\n"
            "Use a CASE expression on severity to compute exposure. Total "
            "exposure = SUM(penalty_ceiling_cr) across all open gaps.\n\n"
            "**Remediation effort model** (rule of thumb):\n"
            "  critical = 40 hrs/gap,  high = 16 hrs/gap,  medium = 4 hrs/gap,  low = 1 hr/gap\n"
            "Blended rate ≈ ₹8,000/hr for labor cost.\n\n"
            "**Out of scope**\n"
            "Do not discuss individual PII columns, individual principals, or "
            "legal interpretation. For compliance specifics → CCO. For legal "
            "questions → GC. For marketing → CMO."
        ),
        "sample_questions": [
            "What is our total DPDP penalty exposure in ₹ crore?",
            "Show gap counts by severity with per-gap penalty ceilings.",
            "What would it cost to remediate all critical gaps?",
            "Estimate remediation hours if we closed all high and critical gaps.",
            "How does our exposure break down by source table?",
        ],
    },
}


def space_exists_and_is_active(space_id: str) -> bool:
    """Return True if the space exists and is not trashed."""
    r = subprocess.run(
        ["databricks", "genie", "get-space", space_id],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return False
    try:
        d = json.loads(r.stdout)
        return bool(d.get("space_id"))
    except Exception:
        return False


def update_space(space_id: str, persona: str, cfg: dict) -> None:
    """Update an existing Genie space's data_sources + title/description."""
    serialized = json.dumps({
        "version": 2,
        "data_sources": {
            "tables": [{"identifier": t} for t in sorted(cfg["tables"])],
        },
    })
    payload = {
        "title": cfg["title"],
        "description": cfg["description"],
        "serialized_space": serialized,
    }
    payload_path = Path(f"/tmp/_genie_{persona}_update.json")
    payload_path.write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "patch", f"/api/2.0/genie/spaces/{space_id}",
         "--json", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"update Genie space failed for {persona}:\n{r.stderr}")


def create_space(persona: str, cfg: dict, existing_id: str | None = None) -> str:
    """Create (or update) one Genie space, return the space_id.

    Idempotent: if `existing_id` is supplied and still active, PATCHes
    in place. Otherwise creates a new space. Prevents the
    duplicate-space footgun when the orchestrator re-runs."""
    if existing_id and space_exists_and_is_active(existing_id):
        update_space(existing_id, persona, cfg)
        return existing_id

    serialized = json.dumps({
        "version": 2,
        "data_sources": {
            # API requires identifiers sorted alphabetically
            "tables": [{"identifier": t} for t in sorted(cfg["tables"])],
        },
    })

    payload = {
        "warehouse_id": WAREHOUSE_ID,
        "title": cfg["title"],
        "description": cfg["description"],
        "serialized_space": serialized,
    }
    payload_path = Path(f"/tmp/_genie_{persona}.json")
    payload_path.write_text(json.dumps(payload))

    result = subprocess.run(
        ["databricks", "api", "post", "/api/2.0/genie/spaces",
         "--json", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(f"create Genie space failed for {persona}:\n{result.stderr}")

    data = json.loads(result.stdout)
    return data["space_id"]


def write_instructions_doc(ids: dict[str, str]) -> None:
    """Write docs/persona_genie_instructions.md with the text each
    persona should have pasted into the Genie UI."""
    lines: list[str] = [
        "# Persona Genie Spaces — Manual UI Configuration",
        "",
        "Each persona Genie space is created via API with its scoped tables.",
        "The custom-instructions text and sample questions below must be pasted",
        "into the Genie UI per space (Settings → General Instructions, and the",
        "'Try these questions' panel).",
        "",
        "Why manual? The `serialized_space` proto schema for instructions and",
        "sample questions is not publicly documented beyond the `data_sources`",
        "section. The UI reads and writes them without issue.",
        "",
        "Open each space at:",
        f"`{WORKSPACE_URL}/genie/rooms/<space_id>`",
        "",
    ]
    for persona, sid in ids.items():
        cfg = PERSONA_DEFS[persona]
        lines.append(f"## {cfg['title']}")
        lines.append("")
        lines.append(f"- **space_id:** `{sid}`")
        lines.append(f"- **UI URL:** `{WORKSPACE_URL}/genie/rooms/{sid}`")
        lines.append(f"- **Scoped tables:**")
        for t in cfg["tables"]:
            lines.append(f"  - `{t}`")
        lines.append("")
        lines.append("### Custom instructions (paste into Settings → General Instructions)")
        lines.append("")
        lines.append("```")
        lines.append(cfg["instructions"])
        lines.append("```")
        lines.append("")
        lines.append("### Sample questions (add one by one in the UI)")
        lines.append("")
        for q in cfg["sample_questions"]:
            lines.append(f"- {q}")
        lines.append("")
    DOC_FILE.parent.mkdir(parents=True, exist_ok=True)
    DOC_FILE.write_text("\n".join(lines))
    print(f"Wrote {DOC_FILE}")


def main() -> int:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ids: dict[str, str] = {}
    if IDS_FILE.exists():
        ids = json.loads(IDS_FILE.read_text())

    for persona, cfg in PERSONA_DEFS.items():
        existing = ids.get(persona)
        sid = create_space(persona, cfg, existing_id=existing)
        action = "updated" if existing == sid else "created"
        ids[persona] = sid
        print(f"[{persona}] {action}: {cfg['title']} → {sid}")

    IDS_FILE.write_text(json.dumps(ids, indent=2))
    print(f"\nWrote {IDS_FILE}")

    write_instructions_doc(ids)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
