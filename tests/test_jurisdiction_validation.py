"""Unit tests for the jurisdiction-code validator (ADR-0001 Q3).

Pure-function tests; no Databricks dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from governance_core.pack_loader import (  # noqa: E402
    format_validation_report,
    loaded_packs,
    reset_cache,
    validate_jurisdictions,
)


def setup_function(_fn) -> None:
    reset_cache()


def _section(title: str) -> None:
    print()
    print("=" * 70)
    print(title)
    print("=" * 70)


def test_all_mapped_against_loaded_packs() -> None:
    """IN + GB + EU — all loaded packs. Every code lands in `mapped`."""
    report = validate_jurisdictions({"IN", "GB", "EU"})
    assert sorted(report["mapped"]) == ["EU", "GB", "IN"]
    assert report["null"] == []
    assert report["unmapped_known"] == []
    assert report["unmapped_unknown"] == []
    print("  ✓ all loaded jurisdictions classify as mapped")


def test_null_and_blank_go_to_null_bucket() -> None:
    """None and blank strings — surface as the unmapped-principal gap."""
    report = validate_jurisdictions({None, "", "  "})
    assert len(report["null"]) == 3
    assert report["mapped"] == []
    print("  ✓ NULL / empty / whitespace land in the null bucket")


def test_known_but_unmapped_jurisdiction() -> None:
    """'US' is in COUNTRY_TO_JURISDICTION values but no CCPA pack is loaded
    yet — classify as `unmapped_known`. Author the pack to resolve."""
    report = validate_jurisdictions({"US"})
    assert report["unmapped_known"] == ["US"]
    assert report["mapped"] == []
    print("  ✓ 'US' classifies as unmapped_known (no CCPA pack yet)")


def test_unknown_jurisdiction_unrecognised() -> None:
    """Garbage / typo / stale code — `unmapped_unknown`. Fix the data."""
    report = validate_jurisdictions({"ATLANTIS", "ZZ", "FOO"})
    assert sorted(report["unmapped_unknown"]) == ["ATLANTIS", "FOO", "ZZ"]
    assert report["mapped"] == []
    print("  ✓ garbage codes classify as unmapped_unknown")


def test_mixed_realistic_workspace_state() -> None:
    """A realistic mid-cut-over workspace: IN + GB principals, some NULL
    rows from old data, one stray US row from a hypothetical CCPA test."""
    report = validate_jurisdictions({"IN", "GB", None, "US"})
    assert sorted(report["mapped"]) == ["GB", "IN"]
    assert len(report["null"]) == 1
    assert report["unmapped_known"] == ["US"]
    assert report["unmapped_unknown"] == []
    print("  ✓ mixed-state classification correct (IN/GB mapped, NULL, US unmapped-known)")


def test_case_and_whitespace_insensitive() -> None:
    """Codes normalise to upper-case after trim."""
    report = validate_jurisdictions({"in", " gb ", "Eu"})
    assert sorted(report["mapped"]) == ["EU", "GB", "IN"]
    print("  ✓ case- and whitespace-insensitive lookup")


def test_format_validation_report_smoke() -> None:
    """Report formatter produces stable, scannable output (log-scraper friendly)."""
    report = validate_jurisdictions({"IN", "GB", None, "US", "ZZ"})
    text = format_validation_report(report, observed_count=5)
    assert "Jurisdiction validation (ADR-0001 Q3)" in text
    assert "observed distinct values: 5" in text
    assert "mapped:" in text
    assert "NULL/blank" in text
    assert "unmapped (known)" in text
    assert "unmapped (unknown)" in text
    print("  ✓ format_validation_report produces stable scannable output")


def test_explicit_packs_override_loaded_packs() -> None:
    """When a caller passes ``packs=``, that set wins over loaded_packs()."""
    # Pretend only DPDP is loaded — UK and EU should fall to unmapped_known
    dpdp_only = [p for p in loaded_packs() if p.code == "dpdp_2023"]
    report = validate_jurisdictions({"IN", "GB", "EU"}, packs=dpdp_only)
    assert report["mapped"] == ["IN"]
    assert sorted(report["unmapped_known"]) == ["EU", "GB"]
    print("  ✓ explicit packs=… overrides loaded_packs() for caller-controlled checks")


def main() -> int:
    _section("Q3 unit — jurisdiction validator (ADR-0001)")

    tests = [
        test_all_mapped_against_loaded_packs,
        test_null_and_blank_go_to_null_bucket,
        test_known_but_unmapped_jurisdiction,
        test_unknown_jurisdiction_unrecognised,
        test_mixed_realistic_workspace_state,
        test_case_and_whitespace_insensitive,
        test_format_validation_report_smoke,
        test_explicit_packs_override_loaded_packs,
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
