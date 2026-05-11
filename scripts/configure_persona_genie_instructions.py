"""Apply YAML-defined knowledge-store instructions to persona Genie spaces.

Runs AFTER setup_persona_genie_spaces.py has created the spaces and
written ``dashboards/personas/.genie_space_ids.json``. For each
persona whose ``configs/genie/<persona>.yaml`` exists, this script
serializes the YAML into the Genie ``serialized_space`` proto and
PATCHes the space via ``/api/2.0/genie/spaces/{id}``.

What IS applied via API (scriptable on this workspace, verified by
probe in April 2026):

  * text_instructions         (Instructions → Text tab)
  * example_question_sqls     (Instructions → SQL Queries tab)
  * sql_snippets.filters      (Instructions → SQL Expressions, Filter)
  * sql_snippets.measures     (Instructions → SQL Expressions, Measure)
  * sql_snippets.expressions  (Instructions → SQL Expressions, Dimension)
  * sql_functions             (Instructions → SQL Queries, SQL Function)

What is NOT applied (still UI-only):

  * join_specs. The Genie export proto on this workspace rejects
    every shape for ``join_specs[].sql`` we could reverse-engineer.
    The YAML's ``manual_joins`` list is rendered into
    ``docs/persona_genie_instructions.md`` so a deployer can add
    them via the UI in ~30 seconds per persona.

Idempotent: the ``serialized_space`` is a full replacement, so every
run writes the complete desired state. IDs for snippets /
instructions are deterministically hashed from (persona + snippet
name) so re-runs do NOT create duplicates.

Usage:

    python3 scripts/configure_persona_genie_instructions.py
    python3 scripts/configure_persona_genie_instructions.py --persona cco
    python3 scripts/configure_persona_genie_instructions.py --dry-run
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError:
    print("error: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    raise SystemExit(1)

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIGS_DIR = REPO_ROOT / "configs" / "genie"
IDS_FILE = REPO_ROOT / "dashboards" / "personas" / ".genie_space_ids.json"
SETUP_DOC = REPO_ROOT / "docs" / "persona_genie_instructions.md"

sys.path.insert(0, str(Path(__file__).resolve().parent))
from persona_config import get_workspace_url  # noqa: E402
from setup_persona_genie_spaces import PERSONA_DEFS  # noqa: E402


def stable_id(persona: str, kind: str, name: str) -> str:
    """Deterministic 32-char lowercase hex id from (persona, kind, name).

    Genie's export proto requires every snippet / instruction to carry
    an id in that format. Deriving it from a stable key means re-runs
    produce the same ids, so PATCH does not create duplicate entries
    and users who pinned a snippet in the UI do not lose their pin."""
    h = hashlib.sha1(f"{persona}|{kind}|{name}".encode()).hexdigest()
    return h[:32]


def _snippet(persona: str, kind: str, item: dict[str, Any]) -> dict[str, Any]:
    """Normalize a filter / measure / dimension YAML entry to proto shape.

    Empty ``instruction`` is dropped — the proto rejects empty arrays
    for optional repeated fields in a few edge cases we hit during
    probing."""
    out: dict[str, Any] = {
        "id": stable_id(persona, kind, item["name"]),
        "display_name": item["name"],
        "sql": [item["sql"]],
    }
    if item.get("synonyms"):
        out["synonyms"] = list(item["synonyms"])
    if item.get("instruction"):
        out["instruction"] = [item["instruction"]]
    return out


def _by_id(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Sort snippets/instructions by their ``id``.

    Genie's proto validator rejects payloads where repeated message
    fields aren't ordered by id, with errors like
    ``instructions.example_question_sqls must be sorted by id``."""
    return sorted(items, key=lambda x: x["id"])


def build_serialized_space(persona: str, cfg: dict[str, Any], tables: list[str]) -> str:
    """Return the ``serialized_space`` JSON string for this persona."""
    # tables must be sorted — Genie proto validation rejects otherwise
    data_sources = {
        "tables": [{"identifier": t} for t in sorted(tables)],
    }

    instructions: dict[str, Any] = {}

    # ADR-0001 M3: pack-aware composition. compose_for_persona() returns the
    # hand-authored text_instructions augmented with a Loaded-regulations
    # block and multi-jurisdiction routing guidance whenever 2+ packs are
    # loaded; degrades to a single-pack header (or to the verbatim base
    # text if `auto_compose: false` is set in the persona YAML).
    from governance_core.genie_instructions import compose_for_persona  # noqa: E402
    composed_text = compose_for_persona(cfg)

    if composed_text:
        instructions["text_instructions"] = _by_id([{
            "id": stable_id(persona, "text", "general"),
            # Each paragraph becomes one element so Genie can present
            # long instruction blocks cleanly; splitting on blank lines
            # keeps authored markdown intact.
            "content": [p.strip() for p in composed_text.split("\n\n") if p.strip()],
        }])

    eqs = cfg.get("example_queries") or []
    if eqs:
        instructions["example_question_sqls"] = _by_id([
            {
                "id": stable_id(persona, "eq", eq["question"]),
                "question": [eq["question"]],
                "sql": [eq["sql"].strip()],
            }
            for eq in eqs
        ])

    snippets: dict[str, Any] = {}
    if cfg.get("filters"):
        snippets["filters"] = _by_id([_snippet(persona, "filter", f) for f in cfg["filters"]])
    if cfg.get("measures"):
        snippets["measures"] = _by_id([_snippet(persona, "measure", m) for m in cfg["measures"]])
    if cfg.get("dimensions"):
        # Proto field is `expressions` even though the UI tab is "Dimension".
        snippets["expressions"] = _by_id([_snippet(persona, "dim", d) for d in cfg["dimensions"]])
    if snippets:
        instructions["sql_snippets"] = snippets

    fns = cfg.get("sql_functions") or []
    if fns:
        instructions["sql_functions"] = _by_id([
            {"id": stable_id(persona, "fn", f["identifier"]), "identifier": f["identifier"]}
            for f in fns
        ])

    serialized: dict[str, Any] = {"version": 2, "data_sources": data_sources}
    if instructions:
        serialized["instructions"] = instructions
    return json.dumps(serialized)


def patch_space(space_id: str, persona: str, serialized: str, cfg_title: str, cfg_desc: str) -> None:
    """PATCH the space. ``serialized_space`` is a full replacement."""
    payload = {
        "title": cfg_title,
        "description": cfg_desc,
        "serialized_space": serialized,
    }
    payload_path = Path(f"/tmp/_genie_cfg_{persona}.json")
    payload_path.write_text(json.dumps(payload))
    r = subprocess.run(
        ["databricks", "api", "patch", f"/api/2.0/genie/spaces/{space_id}",
         "--json", f"@{payload_path}"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(
            f"PATCH /api/2.0/genie/spaces/{space_id} failed for {persona}:\n"
            f"  stdout: {r.stdout[:2000]}\n"
            f"  stderr: {r.stderr[:2000]}"
        )


def render_manual_joins(persona: str, cfg: dict[str, Any], space_id: str, workspace_url: str) -> list[str]:
    """Return the markdown section documenting UI-only joins for this persona."""
    joins = cfg.get("manual_joins") or []
    if not joins:
        return []
    lines = [
        f"### Manual joins — {persona.upper()}",
        "",
        f"Open the space: `{workspace_url}/genie/rooms/{space_id}`",
        "Click **Instructions → Joins → + Add** once per row below.",
        "",
    ]
    for j in joins:
        lines.append(f"- **{j['name']}**")
        lines.append(f"  - Left table: `{j['left_table']}`")
        lines.append(f"  - Right table: `{j['right_table']}`")
        if j.get("second_left_column"):
            lines.append(
                f"  - Join condition: click **Use SQL expression** → paste "
                f"`` `{j['left_table'].split('.')[-1]}`.{j['left_column']} = "
                f"`{j['right_table'].split('.')[-1]}`.{j['right_column']} "
                f"AND `{j['left_table'].split('.')[-1]}`.{j['second_left_column']} = "
                f"`{j['right_table'].split('.')[-1]}`.{j['second_right_column']}` ``"
            )
        else:
            lines.append(
                f"  - Join condition: `{j['left_column']}` = `{j['right_column']}`"
            )
        lines.append(f"  - Relationship type: **{j['relationship']}**")
        lines.append(f"  - Instructions: {j['instruction']}")
        lines.append("")
    return lines


def append_manual_doc(sections: list[list[str]]) -> None:
    """Append/replace the Manual Joins section in SETUP_DOC.

    The doc already exists with the scope/sample-questions content.
    We replace everything after the marker so re-runs do not stack
    duplicate sections."""
    marker = "\n\n<!-- BEGIN: manual-joins (auto-generated — do not edit) -->\n"
    end_marker = "\n<!-- END: manual-joins -->\n"

    existing = SETUP_DOC.read_text() if SETUP_DOC.exists() else ""
    before = existing.split(marker, 1)[0] if marker in existing else existing

    payload = [marker, "## Knowledge-store joins (UI paste, ~30s per persona)\n\n",
               "The Genie API does not accept join_specs on this workspace. "
               "Apply these manually in the UI after running "
               "`scripts/configure_persona_genie_instructions.py`.\n\n"]
    for sec in sections:
        payload.append("\n".join(sec))
        payload.append("\n")
    payload.append(end_marker)

    SETUP_DOC.parent.mkdir(parents=True, exist_ok=True)
    SETUP_DOC.write_text(before + "".join(payload))


def apply_persona(persona: str, space_id: str, dry_run: bool, workspace_url: str) -> list[str] | None:
    cfg_path = CONFIGS_DIR / f"{persona}.yaml"
    if not cfg_path.exists():
        print(f"  ⟳ {persona}: no config at {cfg_path.relative_to(REPO_ROOT)}, skipping")
        return None
    try:
        cfg = yaml.safe_load(cfg_path.read_text()) or {}
    except yaml.YAMLError as e:
        raise SystemExit(
            f"✗ {persona}: {cfg_path.relative_to(REPO_ROOT)} is not valid YAML: {e}"
        )
    if not isinstance(cfg, dict):
        raise SystemExit(
            f"✗ {persona}: {cfg_path.relative_to(REPO_ROOT)} must be a mapping at the top level"
        )

    persona_def = PERSONA_DEFS.get(persona, {})
    tables = persona_def.get("tables") or []
    if not tables:
        raise RuntimeError(f"{persona}: no tables defined in PERSONA_DEFS")

    serialized = build_serialized_space(persona, cfg, tables)

    if dry_run:
        print(f"  (dry-run) would PATCH space {space_id} for {persona}")
        print(f"            serialized_space = {len(serialized)} chars, "
              f"{len(tables)} tables, "
              f"{len(cfg.get('filters') or [])} filters, "
              f"{len(cfg.get('measures') or [])} measures, "
              f"{len(cfg.get('dimensions') or [])} dimensions, "
              f"{len(cfg.get('example_queries') or [])} example queries, "
              f"{len(cfg.get('sql_functions') or [])} sql_functions")
        return render_manual_joins(persona, cfg, space_id, workspace_url)

    patch_space(space_id, persona, serialized,
                cfg_title=persona_def["title"], cfg_desc=persona_def["description"])
    print(f"  ✓ {persona}: patched space {space_id} "
          f"({len(cfg.get('filters') or [])}F / "
          f"{len(cfg.get('measures') or [])}M / "
          f"{len(cfg.get('dimensions') or [])}D / "
          f"{len(cfg.get('example_queries') or [])}EQ)")
    return render_manual_joins(persona, cfg, space_id, workspace_url)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--persona", choices=["cco", "gc", "cmo", "cfo"],
                        help="Only configure this persona (default: all that have YAML)")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not IDS_FILE.exists():
        print(f"error: {IDS_FILE} not found — run setup_persona_genie_spaces.py first",
              file=sys.stderr)
        return 1
    ids = json.loads(IDS_FILE.read_text())
    workspace_url = get_workspace_url()

    personas = [args.persona] if args.persona else list(ids.keys())
    manual_sections: list[list[str]] = []
    for persona in personas:
        if persona not in ids:
            print(f"  ⟳ {persona}: no space_id in {IDS_FILE.name}, skipping")
            continue
        section = apply_persona(persona, ids[persona], args.dry_run, workspace_url)
        if section:
            manual_sections.append(section)

    if manual_sections and not args.dry_run:
        append_manual_doc(manual_sections)
        print(f"\nUpdated manual-joins section in {SETUP_DOC.relative_to(REPO_ROOT)}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
