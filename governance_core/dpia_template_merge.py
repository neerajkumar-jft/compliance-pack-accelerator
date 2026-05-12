"""Per-activity DPIA template selection / merging (ADR-0001 M3).

A DPIA documents one *processing activity*. When that activity touches
principals from more than one jurisdiction, ADR-0001 mandates that the
DPIA cite every applicable regulation in a single document — not one DPIA
per regulation. This module produces the merged DPIATemplate from the set
of jurisdictions present in the activity's data scope.

ADR-0001 §"Per-activity DPIA scope" rule: cite **only** the packs whose
principals appear in the activity. An activity touching only Indian
principals produces a DPDP-only DPIA, even in a deployment that also has
UK GDPR loaded.

Primary pack: when 2+ packs apply, the first-listed pack in
``loaded_packs()`` order drives the narrative (system_prompt voice,
section_citation_style); secondary packs are listed in a "Cross-regulation
citations" appendix-style section_description that gets merged into the
section schema.
"""

from __future__ import annotations

from typing import Iterable

from .pack_loader import DPIATemplate, Pack, loaded_packs, pack_for


def template_for_activity(
    jurisdictions: Iterable[str | None],
    packs: list[Pack] | None = None,
) -> DPIATemplate | None:
    """Return the DPIATemplate appropriate for an activity's jurisdiction set.

    ``jurisdictions`` is the set of unique jurisdiction codes present in the
    activity's data scope (e.g., the distinct values of
    ``customers_tagged.jurisdiction`` for rows the activity reads).
    NULL / unmapped jurisdictions are filtered out — they're handled by the
    "unmapped principals" compliance gap, not by the DPIA narrative.

    Returns ``None`` when no loaded pack applies (i.e., the activity only
    touches unmapped principals or jurisdictions without packs).
    """
    jurs = sorted({j.upper() for j in jurisdictions if j})
    if not jurs:
        return None

    applicable_packs: list[Pack] = []
    for j in jurs:
        p = pack_for(j)
        if p is not None and p not in applicable_packs:
            applicable_packs.append(p)

    if not applicable_packs:
        return None

    # Single-pack case — return its template unchanged.
    if len(applicable_packs) == 1:
        return applicable_packs[0].dpia_template()

    # Multi-pack case — pick primary pack from loaded_packs() order so the
    # narrative voice stays consistent across deploys, then merge the
    # secondary packs' citations into a per-section appendix.
    all_loaded = packs if packs is not None else loaded_packs()
    load_order = {p.code: i for i, p in enumerate(all_loaded)}
    applicable_packs.sort(key=lambda p: load_order.get(p.code, 9999))
    primary, *secondaries = applicable_packs
    return _merge_templates(primary, secondaries)


def _merge_templates(primary: Pack, secondaries: list[Pack]) -> DPIATemplate:
    """Merge primary + secondary pack templates into a single DPIATemplate.

    Strategy:
      - legal_framework_name: union with " + " separator
      - section_citation_style: primary's style is the default; system_prompt
        is augmented to instruct the model to additionally cite secondary
        packs inline where their obligations apply.
      - system_prompt: primary's prompt + a multi-regulation paragraph that
        names each secondary pack and instructs cross-citation.
      - section_descriptions: per-section, primary's override wins; for
        sections present in secondaries but not primary, secondary text is
        appended with a "Secondary regulation reference" prefix so the
        model knows it's a cross-citation.
    """
    primary_tpl = primary.dpia_template()

    framework_names = [primary_tpl.legal_framework_name] + [
        s.dpia_template().legal_framework_name for s in secondaries
    ]
    merged_framework = " + ".join(framework_names)

    secondary_summary = "; ".join(
        f"{s.metadata.get('name', s.code)} ({s.dpia_template().section_citation_style})"
        for s in secondaries
    )
    augmented_prompt = (
        primary_tpl.system_prompt.rstrip()
        + "\n\n"
        + "**Multi-regulation scope (ADR-0001).** This processing activity "
        + f"also touches principals governed by: {secondary_summary}. Where "
        + "obligations from these regulations apply to the activity, cite "
        + "them inline alongside the primary framework's citations. Do not "
        + "duplicate the same obligation twice if both regulations express "
        + "it equivalently; cite both bases once."
    )

    merged_section_descriptions = dict(primary_tpl.section_descriptions)
    for s in secondaries:
        s_tpl = s.dpia_template()
        for section_key, secondary_desc in s_tpl.section_descriptions.items():
            if section_key in merged_section_descriptions:
                # Primary already has guidance for this section. Append
                # the secondary's framing as a cross-reference so the
                # model includes citations from both packs.
                merged_section_descriptions[section_key] = (
                    merged_section_descriptions[section_key].rstrip()
                    + "\n\n"
                    + f"Secondary regulation reference "
                    + f"({s.metadata.get('name', s.code)}): "
                    + secondary_desc.strip()
                )
            else:
                # Primary has no override for this section; promote the
                # secondary's override but mark it cross-pack.
                merged_section_descriptions[section_key] = (
                    f"Secondary regulation reference "
                    f"({s.metadata.get('name', s.code)}): "
                    + secondary_desc.strip()
                )

    # Pack version stamp for the merged template — primary first, then
    # secondaries in load order. Surfaces in dpia_prompt_version() so MLflow
    # traces fork whenever any participating pack bumps its semver.
    merged_pack_version = "+".join(
        [f"{primary.code}@{primary.version}"]
        + [f"{s.code}@{s.version}" for s in secondaries]
    )

    return DPIATemplate(
        legal_framework_name=merged_framework,
        section_citation_style=primary_tpl.section_citation_style,
        system_prompt=augmented_prompt,
        section_descriptions=merged_section_descriptions,
        pack_version=merged_pack_version,
    )
