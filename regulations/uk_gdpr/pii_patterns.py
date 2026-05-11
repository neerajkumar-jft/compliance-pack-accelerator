"""UK-specific PII patterns for the UK GDPR pack.

Patterns that are meaningful under UK GDPR / DPA 2018 but not universal.
Universal patterns (email, phone, credit card, name, address, DOB, IP,
medical, insurance) live in `governance_core/pii_patterns/universal.py` and
compose with `IN_SPECIFIC_PATTERNS` below at load time by
`schemas/pii_patterns.py` once that loader becomes pack-list-aware (M3).

Five entries:
  NHS_NUMBER          — 10-digit NHS patient identifier (with checksum)
  NINO                — National Insurance Number (XX-NNNNNN-X)
  UK_POSTCODE         — UK postcode (AA9A 9AA / A9A 9AA / A9 9AA / A99 9AA)
  UTR                 — Unique Taxpayer Reference (10 digits, HMRC)
  UK_DRIVING_LICENCE  — DVLA driving licence (16 chars)

Each pattern uses the universal PIIPattern dataclass + category /
sensitivity constants. Variable name `IN_SPECIFIC_PATTERNS` is kept for
loader compatibility (the pack loader expects this attribute name on every
pack's pii_patterns module — naming is historical, not jurisdictional).
"""

from __future__ import annotations

from governance_core.pii_patterns.universal import (
    PIIPattern,
    CATEGORY_DIRECT_FIN,
    CATEGORY_DIRECT_GOV,
    CATEGORY_HEALTH,
    CATEGORY_INDIRECT,
    SENSITIVITY_CRITICAL,
    SENSITIVITY_HIGH,
    SENSITIVITY_MEDIUM,
)


# NHS Number — 10 digits, last digit is a Modulo-11 check digit.
# Example: 943 476 5919. Spaces or hyphens commonly used as separators.
# Source: NHS Data Dictionary, Information Standards Notice DAPB1077.
NHS_NUMBER_PATTERN = PIIPattern(
    pattern_id="nhs_number",
    pii_type="nhs_number",
    category=CATEGORY_HEALTH,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b\d{3}[\s-]?\d{3}[\s-]?\d{4}\b",
    column_hints=[
        "nhs", "nhs_number", "nhs_no", "patient_nhs_number",
        "patient_nhs", "nhs_id", "nhs_identifier",
    ],
    regulations=["UK_GDPR", "DPA_2018", "GDPR"],
    description="NHS Number — 10-digit unique patient identifier issued by the NHS. Special-category data under UK GDPR Art. 9.",
)


NINO_PATTERN = PIIPattern(
    pattern_id="uk_nino",
    pii_type="nino",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]\s?\d{2}\s?\d{2}\s?\d{2}\s?[A-D]\b",
    column_hints=[
        "nino", "national_insurance_number", "ni_number",
        "national_insurance", "ninumber",
    ],
    regulations=["UK_GDPR", "DPA_2018"],
    description="UK National Insurance Number — government-issued identifier used for tax, benefits, NHS records.",
)


UK_POSTCODE_PATTERN = PIIPattern(
    pattern_id="uk_postcode",
    pii_type="uk_postcode",
    category=CATEGORY_INDIRECT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b[A-Z]{1,2}\d[A-Z\d]?\s?\d[A-Z]{2}\b",
    column_hints=[
        "postcode", "post_code", "postal_code", "zip", "zipcode",
        "billing_postcode", "shipping_postcode",
    ],
    regulations=["UK_GDPR", "DPA_2018"],
    description="UK postcode — geographic identifier, indirect PII when combined with other fields per ICO guidance on anonymisation.",
)


UTR_PATTERN = PIIPattern(
    pattern_id="uk_utr",
    pii_type="utr",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=r"\b\d{10}\b",
    column_hints=[
        "utr", "tax_reference", "taxpayer_reference", "hmrc_utr",
        "self_assessment_utr",
    ],
    regulations=["UK_GDPR", "DPA_2018"],
    description="HMRC Unique Taxpayer Reference — 10-digit identifier for self-assessment, partnerships, corporation tax.",
)


UK_DRIVING_LICENCE_PATTERN = PIIPattern(
    pattern_id="uk_driving_licence",
    pii_type="uk_driving_licence",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=r"\b[A-Z9]{5}\d{6}[A-Z9]{2}\d[A-Z]{2}\b",
    column_hints=[
        "driving_licence", "driver_licence", "dvla_licence",
        "licence_number", "driver_id",
    ],
    regulations=["UK_GDPR", "DPA_2018"],
    description="DVLA Driving Licence Number — 16-char identifier encoding surname, DOB and initials.",
)


# The list the loader picks up. Variable name `IN_SPECIFIC_PATTERNS` is the
# pack-loader contract — kept consistent across all packs for the dynamic
# import in pack_loader.Pack.pii_patterns().
IN_SPECIFIC_PATTERNS = [
    NHS_NUMBER_PATTERN,
    NINO_PATTERN,
    UK_POSTCODE_PATTERN,
    UTR_PATTERN,
    UK_DRIVING_LICENCE_PATTERN,
]
