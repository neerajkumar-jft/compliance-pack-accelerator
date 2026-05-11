"""Check that compliance.notice_versions has every language the pack lists.

The pack's languages.yaml declares every language the POC is expected to
cover. After running `scripts/generate_multilang_notices.py`, each entry
should produce a row in compliance.notice_versions for the canonical
marketing_notice v1. Machine-translated rows must carry the watermark
preamble so consumers can distinguish legal-reviewed copy from demo copy.

Checks:
  1. Each language code in the pack's languages.yaml has a notice row
  2. Hand-authored (seeded_by_poc=true) rows DO NOT carry the watermark
  3. Generated (seeded_by_poc=false) rows DO carry the watermark
  4. Every body is non-empty (>100 chars — catches truncated generations)

Run:
    python3 tests/test_multilang_notices.py
    python3 tests/test_multilang_notices.py --verbose
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from _sql import rows_or_raise  # noqa: E402
from governance_core.pack_loader import active_pack  # noqa: E402

WATERMARK_PREFIX = "[MACHINE-TRANSLATED"
NOTICE_ID = "marketing_notice"
VERSION = 1
MIN_BODY_CHARS = 100

# Content-quality signals every notice body must carry, regardless of language.
# The numbered list (items 1-6) + the DPDP Act citation year must survive any
# translation — if they don't, something truncated or drifted.
REQUIRED_LIST_MARKERS = ["1.", "2.", "3.", "4.", "5.", "6."]
REQUIRED_CITATION_YEAR = "2023"


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--verbose", action="store_true")
    args = p.parse_args()

    pack = active_pack()
    expected = {l["code"]: l.get("seeded_by_poc", False) for l in pack.languages()}

    print(f"Multilang notices — pack {pack.code}, {NOTICE_ID} v{VERSION}")
    print("=" * 70)

    # Fetch all rows for this notice+version, including the full body so we
    # can assert content-quality signals per language.
    rows = rows_or_raise(
        f"SELECT language, SUBSTR(notice_text, 1, 30) AS preamble, "
        f"LENGTH(notice_text) AS chars, notice_text "
        f"FROM compliance_pack.compliance.notice_versions "
        f"WHERE notice_id = '{NOTICE_ID}' AND version_number = {VERSION}"
    )
    by_lang: dict[str, tuple[str, int, str]] = {
        r[0]: (r[1], int(r[2]), r[3]) for r in rows
    }

    if args.verbose:
        print(f"\nPack expects {len(expected)} languages: {sorted(expected.keys())}")
        print(f"DB has {len(by_lang)} rows for this notice:")
        for lang in sorted(by_lang):
            preamble, chars, _ = by_lang[lang]
            origin = "machine" if preamble.startswith(WATERMARK_PREFIX) else "human"
            print(f"  {lang:6s}  {origin:8s}  {chars} chars")
        print()

    checks: list[tuple[str, bool, str]] = []

    # 1. Every pack language has a row.
    missing = [lang for lang in expected if lang not in by_lang]
    checks.append((
        f"Every pack-declared language ({len(expected)}) has a notice row",
        not missing,
        f"missing: {missing}" if missing else "",
    ))

    # 2. Seeded languages don't carry the machine-translation preamble.
    seeded_with_wm = [
        lang for lang, seeded in expected.items()
        if seeded and lang in by_lang and by_lang[lang][0].startswith(WATERMARK_PREFIX)
    ]
    checks.append((
        "Seeded (human-authored) notices do NOT carry the machine-translation preamble",
        not seeded_with_wm,
        f"offenders: {seeded_with_wm}" if seeded_with_wm else "",
    ))

    # 3. Non-seeded languages DO carry the preamble.
    non_seeded_without_wm = [
        lang for lang, seeded in expected.items()
        if not seeded and lang in by_lang and not by_lang[lang][0].startswith(WATERMARK_PREFIX)
    ]
    checks.append((
        "Generated notices DO carry the machine-translation preamble",
        not non_seeded_without_wm,
        f"offenders: {non_seeded_without_wm}" if non_seeded_without_wm else "",
    ))

    # 4. Every body is non-trivially long (sanity check against truncated output).
    short = [lang for lang in by_lang if by_lang[lang][1] < MIN_BODY_CHARS]
    checks.append((
        f"Every notice body is at least {MIN_BODY_CHARS} characters",
        not short,
        f"short: {short}" if short else "",
    ))

    # 5. Numbered-list structure (1. through 6.) survives every translation.
    #    If a translation dropped a purpose, we want a loud signal.
    lang_missing_list = []
    for lang, (_, _, body) in by_lang.items():
        if not all(marker in body for marker in REQUIRED_LIST_MARKERS):
            missing_markers = [m for m in REQUIRED_LIST_MARKERS if m not in body]
            lang_missing_list.append(f"{lang}(missing={missing_markers})")
    checks.append((
        f"Every notice body contains the numbered list 1.-6.",
        not lang_missing_list,
        ", ".join(lang_missing_list) if lang_missing_list else "",
    ))

    # 6. DPDP Act citation year ('2023') must appear in every body — the
    #    one non-translatable anchor a regulator would look for as proof
    #    of statute reference. Script-transliterated forms in Devanagari /
    #    Bengali / Tamil / etc. still use Arabic digits for the year in
    #    practice. If the model localised the year into another numeral
    #    system this check would flag it for review.
    lang_missing_year = [
        lang for lang, (_, _, body) in by_lang.items()
        if REQUIRED_CITATION_YEAR not in body
    ]
    checks.append((
        f"Every notice body cites the DPDP year '{REQUIRED_CITATION_YEAR}'",
        not lang_missing_year,
        f"missing: {lang_missing_year}" if lang_missing_year else "",
    ))

    # Report
    print()
    passed = 0
    for name, ok, detail in checks:
        marker = "✓" if ok else "✗"
        print(f"  {marker} {name}")
        if not ok and detail:
            print(f"      {detail}")
        if ok:
            passed += 1

    print("\n" + "=" * 70)
    print(f"Summary: {passed}/{len(checks)} checks passed")
    return 0 if passed == len(checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
