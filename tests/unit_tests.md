# Unit tests · executable layout

> ⚠️ **Pre-build planning document.** **For the active test suite, see [`docs/how_to_test.html`](../docs/how_to_test.html) and `tests/persona_boundary_test.py`.**

Unit tests exercise pure Python logic with no Databricks dependency. They run in any Python environment that has the spec repo on path. The tests complement §8.2 of the main spec.

## Test file layout

```
tests/
└── unit/
    ├── conftest.py
    ├── test_pii_patterns.py       ← covers §8.2.1 (regex + column hint tests)
    ├── test_confidence.py         ← covers §8.2.2 (calculate_confidence)
    ├── test_redaction.py          ← covers §8.2.3 (redact_sample)
    └── test_generator.py          ← covers §8.2.4 (determinism + DSR principal)
```

## Running the unit tests

```bash
cd /path/to/compliance_pack_spec
python -m pytest tests/unit/ -v
```

All tests must pass before any Databricks-side work begins. If a unit test fails, the bug is in the pattern library, confidence function, or redaction logic — diagnose and fix before touching the workspace.

## Coverage targets

| Test file | Covers spec section | Test count target |
|-----------|---------------------|-------------------|
| test_pii_patterns.py | §4.3.2 all 16 patterns | ≥ 30 cases (2 per pattern + column hint variants) |
| test_confidence.py | §4.4 | 6 cases (both/column-only/value-only × with/without match_rate) |
| test_redaction.py | §4.7 | 9 cases (one per pii_type with a redaction rule) |
| test_generator.py | §6 determinism | 3 cases (deterministic output, manifest matches, customer_04217 exists) |

## Example test file — test_pii_patterns.py

Reproduced in full to show the shape; other files follow the same pattern.

```python
"""Unit tests for the PII pattern library (§4.3.2, §8.2.1)."""

import pytest
from schemas.pii_patterns import (
    AADHAAR_PATTERN, PAN_PATTERN, PASSPORT_INDIA_PATTERN,
    IFSC_PATTERN, CREDIT_CARD_PATTERN, EMAIL_PATTERN,
    PHONE_INDIA_PATTERN, IP_ADDRESS_PATTERN, DOB_PATTERN,
    NAME_PATTERN, ADDRESS_PATTERN, MEDICAL_RECORD_PATTERN,
    PATTERN_LIBRARY, patterns_by_column_name,
)


class TestAadhaar:
    def test_accepts_valid_formats(self):
        assert AADHAAR_PATTERN.matches_value("2345 6789 0123")
        assert AADHAAR_PATTERN.matches_value("3456-7890-1234")
        assert AADHAAR_PATTERN.matches_value("234567890123")

    def test_rejects_invalid_first_digit(self):
        # First digit must be 2-9, not 0 or 1
        assert not AADHAAR_PATTERN.matches_value("0345 6789 0123")
        assert not AADHAAR_PATTERN.matches_value("1345 6789 0123")

    def test_rejects_wrong_length(self):
        assert not AADHAAR_PATTERN.matches_value("2345 6789 012")    # 11 digits
        assert not AADHAAR_PATTERN.matches_value("2345 6789 01234")  # 13 digits

    def test_column_hints(self):
        assert AADHAAR_PATTERN.matches_column_name("aadhaar_number")
        assert AADHAAR_PATTERN.matches_column_name("aadhar")
        assert AADHAAR_PATTERN.matches_column_name("uid_number")
        assert not AADHAAR_PATTERN.matches_column_name("customer_id")


class TestPAN:
    def test_accepts_valid_format(self):
        assert PAN_PATTERN.matches_value("ABCDE1234F")
        assert PAN_PATTERN.matches_value("PQRST5678G")

    def test_rejects_wrong_layout(self):
        assert not PAN_PATTERN.matches_value("ABCDE12345")  # last char must be letter
        assert not PAN_PATTERN.matches_value("ABCD1234EF")  # wrong positions


class TestIFSC:
    def test_accepts_real_bank_prefixes(self):
        assert IFSC_PATTERN.matches_value("SBIN0001234")
        assert IFSC_PATTERN.matches_value("HDFC0002345")
        assert IFSC_PATTERN.matches_value("ICIC0003456")

    def test_rejects_wrong_structure(self):
        assert not IFSC_PATTERN.matches_value("SBI0001234")    # only 3 letters
        assert not IFSC_PATTERN.matches_value("SBINX001234")   # position 4 must be '0'
        assert not IFSC_PATTERN.matches_value("SBIN00012345")  # too long


class TestPhoneIndia:
    def test_accepts_indian_mobile(self):
        assert PHONE_INDIA_PATTERN.matches_value("+91-9876543210")
        assert PHONE_INDIA_PATTERN.matches_value("9876543210")
        assert PHONE_INDIA_PATTERN.matches_value("+919876543210")

    def test_rejects_leading_digit_below_6(self):
        # Indian mobile: leading digit 6-9
        assert not PHONE_INDIA_PATTERN.matches_value("5876543210")
        assert not PHONE_INDIA_PATTERN.matches_value("2345678901")


class TestPatternLookup:
    def test_patterns_by_column_name_prioritizes(self):
        # When multiple patterns match a column, sorted by priority desc
        patterns = patterns_by_column_name("pan_number")
        assert len(patterns) >= 1
        assert patterns[0].pattern_id == "pan"
        assert patterns[0].priority == 99

    def test_patterns_by_column_name_no_match(self):
        # Generic column name should match name patterns (hint 'name') - may or may not match
        # but specifically ambiguous labels return consistent ordering
        patterns = patterns_by_column_name("xyz_abc_qqq")
        assert patterns == []


class TestLibraryCompleteness:
    def test_library_has_sixteen_patterns(self):
        # Spec §4.3.2 lists 16 patterns; a change here is a spec change
        assert len(PATTERN_LIBRARY) == 16

    def test_every_pattern_has_dpdp(self):
        # Every pattern must include DPDP in regulations for this POC
        for p in PATTERN_LIBRARY:
            assert "DPDP" in p.regulations, f"{p.pattern_id} missing DPDP"
```

## conftest.py

```python
"""Pytest configuration — adds spec repo root to path so imports work."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))
```

## What NOT to test here

Unit tests do not:
- Touch Databricks (use integration tests instead)
- Touch Lakebase
- Generate real synthetic data (the generator is unit-tested separately for determinism; volume tests belong to integration)
- Exercise `ai_classify` (requires workspace)

If a test needs any of the above, it belongs in `tests/integration_tests.md`, not here.
