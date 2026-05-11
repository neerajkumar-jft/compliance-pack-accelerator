"""Local unit tests for the M3 composers (ADR-0001).

Validates that:
  - genie_instructions.compose() produces multi-pack guidance when 2+ packs
    are loaded; single-pack guidance when one pack is loaded.
  - compose_for_persona() honours `auto_compose: false` as a hand-authored
    override path.
  - dpia_template_merge.template_for_activity() picks the right pack(s)
    based on the activity's jurisdiction set; merges templates correctly
    when multiple packs apply; returns None for fully-unmapped activities.

Runs without Databricks. No serializer overhead. Stays under 1 second.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance_core.dpia_template_merge import template_for_activity  # noqa: E402
from governance_core.genie_instructions import compose, compose_for_persona  # noqa: E402
from governance_core.pack_loader import (  # noqa: E402
    Pack,
    loaded_packs,
    pack_for,
    reset_cache,
)


def setup_function(_fn) -> None:
    reset_cache()


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


# ---------------------------------------------------------------------------
# Genie composer
# ---------------------------------------------------------------------------

def test_compose_multi_pack_emits_routing_guidance() -> None:
    """Both packs loaded → multi-jurisdiction routing block + pack list."""
    packs = loaded_packs()
    assert len(packs) >= 2, f"need 2+ packs for multi-pack test, got {len(packs)}"
    out = compose("You are the CCO assistant.", packs=packs)
    assert "Multi-jurisdiction routing" in out
    assert "Loaded regulation packs" in out
    # Every loaded pack's name should appear in the block
    for p in packs:
        name = p.metadata.get("name", p.code)
        assert name in out, f"pack name {name!r} missing from composed text"
    # Base text preserved
    assert "You are the CCO assistant." in out
    print(f"  ✓ multi-pack compose: {len(packs)} packs enumerated, base text preserved")


def test_compose_single_pack_emits_single_pack_header() -> None:
    """Only one pack → single-pack header block, no routing guidance."""
    packs = loaded_packs()
    out = compose("Base CCO text.", packs=[packs[0]])
    assert "Active regulation pack" in out
    assert "Multi-jurisdiction routing" not in out  # don't pretend
    assert "Base CCO text." in out
    print(f"  ✓ single-pack compose: '{packs[0].code}' header emitted, routing block suppressed")


def test_compose_no_packs_returns_base_unchanged() -> None:
    """Edge: zero packs loaded → return base text unchanged."""
    out = compose("Just the base.", packs=[])
    assert out == "Just the base."
    print("  ✓ zero-pack compose: base text returned unchanged")


def test_compose_for_persona_honours_auto_compose_false() -> None:
    """auto_compose: false → hand-authored override path; return base verbatim."""
    cfg = {"text_instructions": "Override-only text.", "auto_compose": False}
    out = compose_for_persona(cfg)
    assert out == "Override-only text."
    assert "Loaded regulation packs" not in out
    print("  ✓ auto_compose: false yields verbatim base text (override path)")


def test_compose_for_persona_default_auto_compose_true() -> None:
    """No auto_compose flag → default to auto-compose path."""
    cfg = {"text_instructions": "Default text."}
    out = compose_for_persona(cfg)
    # With 2 packs loaded, expect multi-pack routing block
    if len(loaded_packs()) >= 2:
        assert "Multi-jurisdiction routing" in out
        print("  ✓ default auto-compose path engages with 2 packs loaded")
    else:
        assert "Active regulation pack" in out
        print("  ✓ default auto-compose path engages with 1 pack loaded")


# ---------------------------------------------------------------------------
# DPIA template merge
# ---------------------------------------------------------------------------

def test_template_for_activity_in_only_picks_dpdp() -> None:
    """Indian principals only → DPDP template unchanged."""
    tpl = template_for_activity(["IN", "IN", "IN"])
    assert tpl is not None
    # DPDP framework name is "Digital Personal Data Protection Act, 2023 (India)";
    # citation style is "DPDP §<section_number>" — assert against the
    # discriminating substrings, not the abbreviation alone.
    assert "Digital Personal Data Protection Act" in tpl.legal_framework_name, (
        f"unexpected framework name: {tpl.legal_framework_name!r}"
    )
    assert "DPDP §" in tpl.section_citation_style
    assert "Multi-regulation scope" not in tpl.system_prompt
    print(f"  ✓ IN-only activity → DPDP template, no multi-pack augmentation")


def test_template_for_activity_gb_only_picks_uk_gdpr() -> None:
    """UK principals only → UK GDPR template unchanged."""
    tpl = template_for_activity(["GB"])
    assert tpl is not None
    assert "UK" in tpl.legal_framework_name or "Data Protection Act" in tpl.legal_framework_name
    assert "UK GDPR" in tpl.section_citation_style
    assert "Multi-regulation scope" not in tpl.system_prompt
    print("  ✓ GB-only activity → UK GDPR template, no multi-pack augmentation")


def test_template_for_activity_in_and_gb_merges() -> None:
    """Both jurisdictions present → merged template citing both packs."""
    tpl = template_for_activity(["IN", "GB"])
    assert tpl is not None
    # Joined framework name "DPDP Act ... + UK GDPR ..."
    assert "Digital Personal Data Protection Act" in tpl.legal_framework_name
    assert "UK General Data Protection Regulation" in tpl.legal_framework_name or "Data Protection Act 2018" in tpl.legal_framework_name
    # Multi-regulation augmentation present in system_prompt
    assert "Multi-regulation scope" in tpl.system_prompt
    # Primary pack drives citation style (DPDP is hoisted first in loaded_packs)
    assert "DPDP §" in tpl.section_citation_style
    # At least one section description carries the cross-reference marker
    has_cross_ref = any(
        "Secondary regulation reference" in v
        for v in tpl.section_descriptions.values()
    )
    assert has_cross_ref, "merged template missing 'Secondary regulation reference' annotation"
    print("  ✓ IN+GB activity → merged template (DPDP primary, UK GDPR secondary cross-ref)")


def test_template_for_activity_null_only_returns_none() -> None:
    """Unmapped principals only → no template, surface as gap upstream."""
    assert template_for_activity([None, None]) is None
    assert template_for_activity([]) is None
    assert template_for_activity([""]) is None
    print("  ✓ unmapped/empty activity → template_for_activity returns None")


def test_template_for_activity_mixed_with_unmapped_ignores_null() -> None:
    """Mix of IN + None → drops None, returns DPDP template."""
    tpl = template_for_activity(["IN", None, "IN"])
    assert tpl is not None
    assert "Digital Personal Data Protection Act" in tpl.legal_framework_name
    assert "Multi-regulation scope" not in tpl.system_prompt
    print("  ✓ NULL jurisdictions are filtered out of the pack-resolution set")


def test_template_for_activity_unknown_jurisdiction_skipped() -> None:
    """Unknown code (e.g., 'ZZ') is skipped; remaining mapped jurisdictions win."""
    tpl = template_for_activity(["ZZ", "IN"])
    assert tpl is not None
    assert "Digital Personal Data Protection Act" in tpl.legal_framework_name
    print("  ✓ unknown jurisdiction codes are skipped without breaking the resolver")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def main() -> int:
    _section("M3 unit — Genie composer + DPIA template merge (ADR-0001)")

    tests = [
        test_compose_multi_pack_emits_routing_guidance,
        test_compose_single_pack_emits_single_pack_header,
        test_compose_no_packs_returns_base_unchanged,
        test_compose_for_persona_honours_auto_compose_false,
        test_compose_for_persona_default_auto_compose_true,
        test_template_for_activity_in_only_picks_dpdp,
        test_template_for_activity_gb_only_picks_uk_gdpr,
        test_template_for_activity_in_and_gb_merges,
        test_template_for_activity_null_only_returns_none,
        test_template_for_activity_mixed_with_unmapped_ignores_null,
        test_template_for_activity_unknown_jurisdiction_skipped,
    ]
    failures = 0
    for t in tests:
        setup_function(t)
        try:
            t()
        except AssertionError as e:
            print(f"  ✗ {t.__name__}: {e}")
            failures += 1
        except Exception as e:  # noqa: BLE001
            print(f"  ✗ {t.__name__}: unexpected {type(e).__name__}: {e}")
            failures += 1

    print()
    print("=" * 70)
    if failures:
        print(f"FAIL · {failures}/{len(tests)} tests failed")
        return 1
    print(f"OK · {len(tests)}/{len(tests)} tests passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
