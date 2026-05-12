"""Unit tests for ADR-0001 Q2 — pack versioning (semver field on pack.yaml).

Pure-function tests; no Databricks dependency.

Verifies:
  - Pack.version reads pack.yaml::version (default '0.0.0' when missing)
  - DPIATemplate.pack_version is threaded through Pack.dpia_template()
  - render_dpia_system() prepends the version stamp (skipped at '0.0.0')
  - dpia_prompt_version() hash flips when pack_version bumps
  - _merge_templates() composes a 'code@version+...' compound stamp
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance_core.pack_loader import (  # noqa: E402
    DPIATemplate,
    loaded_packs,
    reset_cache,
)
from governance_core.agent_prompts import (  # noqa: E402
    dpia_prompt_version,
    render_dpia_system,
)
from governance_core.dpia_template_merge import _merge_templates  # noqa: E402


def setup_function(_fn) -> None:
    reset_cache()


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def test_every_loaded_pack_declares_a_version() -> None:
    """Every pack shipped with the platform must declare pack.yaml::version."""
    packs = loaded_packs()
    assert len(packs) >= 4, f"expected >=4 packs, got {len(packs)}"
    for p in packs:
        assert p.version, f"pack {p.code} missing version"
        assert p.version != "0.0.0", f"pack {p.code} version is the fallback '0.0.0'"
    print(f"  ✓ all {len(packs)} packs declare version: {[(p.code, p.version) for p in packs]}")


def test_dpia_template_carries_pack_version() -> None:
    """Pack.dpia_template() must populate DPIATemplate.pack_version."""
    for p in loaded_packs():
        tpl = p.dpia_template()
        assert tpl.pack_version == p.version, (
            f"pack {p.code}: template.pack_version={tpl.pack_version} != pack.version={p.version}"
        )
    print("  ✓ DPIATemplate.pack_version matches Pack.version for every loaded pack")


def test_render_dpia_system_prepends_version_stamp() -> None:
    """render_dpia_system() prefixes '[regulation pack v<sem>]' when version is non-default."""
    tpl = DPIATemplate(
        legal_framework_name="Test",
        section_citation_style="Test §<n>",
        system_prompt="Test prompt.",
        pack_version="1.2.3",
    )
    out = render_dpia_system(tpl)
    assert out.startswith("[regulation pack v1.2.3]"), out[:40]
    assert "Test prompt." in out
    print("  ✓ version stamp prepended to system prompt")


def test_render_dpia_system_skips_default_version() -> None:
    """At the fallback version '0.0.0', no stamp is emitted (back-compat)."""
    tpl = DPIATemplate(
        legal_framework_name="Test",
        section_citation_style="Test §<n>",
        system_prompt="Test prompt.",
        # pack_version defaults to "0.0.0"
    )
    out = render_dpia_system(tpl)
    assert out == "Test prompt."
    print("  ✓ no stamp at default version '0.0.0' (back-compat)")


def test_prompt_version_hash_flips_with_pack_version() -> None:
    """dpia_prompt_version() must produce different hashes for different pack versions."""
    base_kwargs = dict(
        legal_framework_name="Test",
        section_citation_style="Test §<n>",
        system_prompt="Test prompt.",
    )
    h_v1 = dpia_prompt_version(DPIATemplate(**base_kwargs, pack_version="1.0.0"))
    h_v2 = dpia_prompt_version(DPIATemplate(**base_kwargs, pack_version="1.1.0"))
    h_v3 = dpia_prompt_version(DPIATemplate(**base_kwargs, pack_version="2.0.0"))
    assert h_v1 != h_v2, "1.0.0 vs 1.1.0 produced the same hash"
    assert h_v2 != h_v3, "1.1.0 vs 2.0.0 produced the same hash"
    print(f"  ✓ prompt hashes flip across versions: 1.0.0={h_v1} 1.1.0={h_v2} 2.0.0={h_v3}")


def test_merge_templates_composes_version_stamp() -> None:
    """Multi-pack merge yields 'primary@v+secondary@v+...' compound stamp."""
    packs = loaded_packs()
    primary = next(p for p in packs if p.code == "dpdp_2023")
    secondaries = [p for p in packs if p.code in ("uk_gdpr", "eu_gdpr")]
    merged = _merge_templates(primary, secondaries)
    expected_parts = [f"{primary.code}@{primary.version}"] + [
        f"{s.code}@{s.version}" for s in secondaries
    ]
    expected = "+".join(expected_parts)
    assert merged.pack_version == expected, (
        f"merged.pack_version={merged.pack_version}, expected={expected}"
    )
    print(f"  ✓ merged stamp: {merged.pack_version}")


def main() -> int:
    _section("Q2 unit — pack versioning (ADR-0001)")

    tests = [
        test_every_loaded_pack_declares_a_version,
        test_dpia_template_carries_pack_version,
        test_render_dpia_system_prepends_version_stamp,
        test_render_dpia_system_skips_default_version,
        test_prompt_version_hash_flips_with_pack_version,
        test_merge_templates_composes_version_stamp,
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
