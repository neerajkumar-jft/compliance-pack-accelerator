"""Pack-aware Genie text_instructions composer (ADR-0001 M3).

When multiple regulation packs are loaded, every persona's Genie space needs
to learn how to qualify answers by jurisdiction — a CMO asking "can I email
this customer?" must check the principal's jurisdiction, route to the right
pack's consent rule, and answer correctly. Hand-editing each persona's
text_instructions every time a pack is added scales badly.

This module composes the final `text_instructions` block from:

  1. The persona's hand-authored base text (regulator-specific guidance,
     redirects to other personas, conventions).
  2. An auto-generated "Loaded regulations" block enumerating every pack
     in regulations/ with its jurisdiction, supervising authority, and
     citation style.
  3. An auto-generated "Multi-jurisdiction routing" block reminding the
     agent that per-row routing through `<table>.jurisdiction` is the
     correct way to answer pack-sensitive questions.

Override: a persona YAML can set ``auto_compose: false`` to skip steps
2-3 entirely (the hand-authored text_instructions is used verbatim).
This is the "hand-authored override" path ADR-0001 reserves for
deployments where the persona's guidance is genuinely pack-agnostic
(e.g., a workspace running only one pack and wanting the original
DPDP-specific text intact).

Used by `scripts/configure_persona_genie_instructions.py` at deploy time.
"""

from __future__ import annotations

from .pack_loader import Pack, loaded_packs


_MULTI_PACK_HEADER = """
**Multi-jurisdiction routing (loaded automatically — do not edit this block)**

This Genie space operates across multiple regulation packs simultaneously.
Each data subject (customer, user, patient, employee) carries a
`jurisdiction` column on their silver-table row that routes rule
evaluation to the pack governing them. When answering questions:

- For any question about a specific principal, qualify the answer with
  their jurisdiction. Indian principals are governed by DPDP; UK
  principals by UK GDPR. Retention windows, consent semantics, lawful
  basis, and DSR SLAs differ between packs.
- For aggregate questions ("how many compliance gaps do we have?"),
  default to a per-jurisdiction breakdown unless the user explicitly
  asks for a union number.
- Cite the specific article / section the answer rests on. Use the
  pack's citation style — see the per-pack list below.
- If a principal's jurisdiction is NULL or unmapped, flag it as a
  compliance gap rather than guessing.
"""


_SINGLE_PACK_HEADER = """
**Active regulation pack: {pack_name}**

This Genie space is currently operating under a single regulation pack
({pack_code}, jurisdiction {jurisdiction}). All answers should cite
{citation_style} where applicable; the supervising authority is
{authority}.
"""


def _format_pack_summary(pack: Pack) -> str:
    """One-line description of a loaded pack."""
    cite_style = ""
    try:
        cite_style = pack.dpia_template().section_citation_style
    except Exception:  # noqa: BLE001 — pack may not have a DPIA template
        cite_style = f"{pack.code} citations"

    return (
        f"  - **{pack.metadata.get('name', pack.code)}** "
        f"(`{pack.code}`, jurisdiction `{pack.jurisdiction or '?'}`). "
        f"Supervising authority: {pack.metadata.get('supervising_authority', 'unknown')}. "
        f"Citation style: {cite_style}."
    )


def compose(base_text: str, packs: list[Pack] | None = None) -> str:
    """Return text_instructions with pack-aware sections appended.

    ``base_text`` is the persona's hand-authored guidance — passed
    through unchanged at the top. ``packs`` defaults to the current
    ``loaded_packs()`` if not supplied.
    """
    if packs is None:
        packs = loaded_packs()

    if not packs:
        # No packs loaded — degenerate case. Return the base text unchanged
        # so deployers don't get a misleading "all packs disabled" block.
        return base_text

    out = [base_text.rstrip()]

    if len(packs) == 1:
        # Single-pack mode — keep the block short and pack-specific so the
        # agent's instructions don't pretend to handle a multi-jurisdiction
        # case that doesn't exist in this deployment.
        p = packs[0]
        cite_style = ""
        try:
            cite_style = p.dpia_template().section_citation_style
        except Exception:  # noqa: BLE001
            cite_style = f"{p.code} citations"
        out.append(_SINGLE_PACK_HEADER.format(
            pack_name=p.metadata.get("name", p.code),
            pack_code=p.code,
            jurisdiction=p.jurisdiction or "?",
            citation_style=cite_style,
            authority=p.metadata.get("supervising_authority", "unknown"),
        ).rstrip())
    else:
        # Multi-pack mode — full routing guidance + pack enumeration.
        out.append(_MULTI_PACK_HEADER.rstrip())
        out.append("\n**Loaded regulation packs:**")
        for p in packs:
            out.append(_format_pack_summary(p))

    return "\n\n".join(out) + "\n"


def compose_for_persona(persona_cfg: dict, packs: list[Pack] | None = None) -> str:
    """Return composed text_instructions for a persona config dict.

    Honours the ``auto_compose`` flag in the persona YAML — if explicitly
    set to ``false``, the base text is returned unchanged (hand-authored
    override).
    """
    base = persona_cfg.get("text_instructions", "") or ""
    if persona_cfg.get("auto_compose") is False:
        return base
    return compose(base, packs=packs)
