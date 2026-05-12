"""US-specific PII patterns for the CCPA/CPRA pack.

CCPA §1798.140(v) defines "personal information" broadly to include
any information that identifies, relates to, describes, is reasonably
capable of being associated with, or could reasonably be linked, directly
or indirectly, with a particular consumer or household. CPRA §1798.140(ae)
adds "sensitive personal information" — SSN, DL/state ID, financial
account, precise geolocation, racial/ethnic origin, religious beliefs,
union membership, communications content, genetic/biometric, health, sex
life or sexual orientation.

This module ships representative US identifiers that are universally
classified as PI (or SPI) under both statutes:

Seven entries:
  US_SSN              — Social Security Number (XXX-XX-XXXX), SPI under §1798.140(ae)(1)(A)
  US_ITIN             — Individual Taxpayer Identification Number (9XX-XX-XXXX)
  US_EIN              — Employer Identification Number (XX-XXXXXXX) (PI when associated with sole trader)
  US_DRIVERS_LICENSE  — State driver's license / state ID, SPI under §1798.140(ae)(1)(B)
  US_PASSPORT         — US passport (9 digits)
  US_BANK_ACCOUNT     — US bank routing+account, SPI under §1798.140(ae)(1)(C)
  US_ZIP_PLUS4        — ZIP+4 postal code (precise enough to be quasi-identifier per HHS de-id)

Variable name `IN_SPECIFIC_PATTERNS` is kept for loader compatibility (the
pack loader expects this attribute name on every pack's pii_patterns
module — naming is historical, not jurisdictional).
"""

from __future__ import annotations

from governance_core.pii_patterns.universal import (
    PIIPattern,
    CATEGORY_DIRECT_FIN,
    CATEGORY_DIRECT_GOV,
    CATEGORY_INDIRECT,
    SENSITIVITY_CRITICAL,
    SENSITIVITY_HIGH,
    SENSITIVITY_MEDIUM,
)


# US Social Security Number — XXX-XX-XXXX with optional separators (- or
# space) or none. SSA-issued, lifelong, the canonical US national identifier.
# Explicitly enumerated as "sensitive personal information" in CPRA
# §1798.140(ae)(1)(A). Carries strict §1798.81.5 reasonable-security duty.
US_SSN_PATTERN = PIIPattern(
    pattern_id="us_ssn",
    pii_type="ssn",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b(?!000|666|9\d{2})\d{3}[- ]?(?!00)\d{2}[- ]?(?!0000)\d{4}\b",
    column_hints=[
        "ssn", "social_security_number", "social_security", "ss_number",
        "tax_id_us", "us_ssn", "social_sec",
    ],
    regulations=["CCPA", "CPRA", "PRIVACY_ACT_1974"],
    description="US Social Security Number — 9-digit SSA-issued identifier (regex excludes invalid ranges 000-*-*, 666-*-*, 9XX-*-*, *-00-*, *-*-0000). Sensitive PI under CPRA §1798.140(ae)(1)(A).",
)


# US Individual Taxpayer Identification Number (ITIN) — issued by the IRS
# to taxpayers not eligible for an SSN. Format: 9XX-{70-88,90-92,94-99}-XXXX
# (the middle range constraint distinguishes ITINs from SSNs).
US_ITIN_PATTERN = PIIPattern(
    pattern_id="us_itin",
    pii_type="us_itin",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b9\d{2}[- ]?(?:7\d|8[0-8]|9[0-2]|9[4-9])[- ]?\d{4}\b",
    column_hints=[
        "itin", "individual_taxpayer_id", "us_itin", "tax_id_individual",
    ],
    regulations=["CCPA", "CPRA"],
    description="US Individual Taxpayer Identification Number — IRS-issued 9-digit identifier for non-SSN-eligible taxpayers (starts with 9, middle digits 70-88, 90-92, 94-99). Sensitive PI under CPRA §1798.140(ae)(1)(A).",
)


# US Employer Identification Number (EIN) — IRS-issued 9-digit business
# identifier (XX-XXXXXXX). Treated as PI here when associated with a sole
# proprietor (the EIN then identifies the individual). Schedule C filers
# commonly use an EIN in lieu of SSN.
US_EIN_PATTERN = PIIPattern(
    pattern_id="us_ein",
    pii_type="us_ein",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b\d{2}[- ]?\d{7}\b",
    column_hints=[
        "ein", "employer_id", "us_ein", "federal_tax_id", "fein",
        "business_tax_id",
    ],
    regulations=["CCPA", "CPRA"],
    description="US Employer Identification Number — IRS-issued 9-digit business identifier (XX-XXXXXXX). Personal data when associated with a sole proprietor.",
)


# US driver's license number — format varies per state (CA: 1 letter + 7
# digits; NY: 9 digits or 1 letter + 18 digits; TX: 8 digits; FL: 1 letter +
# 12 digits; ...). The regex below targets the most common formats; the
# column_hints catch state-specific column names. Sensitive PI under CPRA
# §1798.140(ae)(1)(B).
US_DRIVERS_LICENSE_PATTERN = PIIPattern(
    pattern_id="us_drivers_license",
    pii_type="us_dl",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[A-Z]\d{7,12}\b|\b\d{7,9}\b",
    column_hints=[
        "drivers_license", "dl_number", "driver_license", "dl",
        "state_id", "license_number", "ca_dl", "ny_dl", "tx_dl", "fl_dl",
    ],
    regulations=["CCPA", "CPRA"],
    description="US driver's license / state ID number — per-state format (California: 1 letter + 7 digits; others vary). Sensitive PI under CPRA §1798.140(ae)(1)(B).",
)


# US passport — 9 digits, sometimes prefixed by a letter for diplomatic /
# official passports. State Department issued; serves as the federal
# travel-document identifier.
US_PASSPORT_PATTERN = PIIPattern(
    pattern_id="us_passport",
    pii_type="us_passport",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[A-Z]?\d{9}\b",
    column_hints=[
        "passport", "passport_number", "us_passport", "passport_no",
    ],
    regulations=["CCPA", "CPRA"],
    description="US passport number — 9-digit State Department identifier (optional letter prefix for diplomatic/official). Sensitive PI when combined with the holder's name.",
)


# US bank account — routing + account number. Routing is 9 digits ABA;
# account length varies (typically 4-17 digits) and is captured by the
# column-hint strategy more than the regex. Sensitive PI under CPRA
# §1798.140(ae)(1)(C) (financial account in combination with access code /
# password / security question / answer).
US_BANK_ACCOUNT_PATTERN = PIIPattern(
    pattern_id="us_bank_account",
    pii_type="bank_account",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b\d{9}\b",
    column_hints=[
        "routing_number", "aba_routing", "us_routing", "rtn",
        "account_number", "bank_account", "us_bank_account",
        "checking_account", "savings_account",
    ],
    regulations=["CCPA", "CPRA", "GLBA"],
    description="US bank routing number (9-digit ABA) or account number. Sensitive PI under CPRA §1798.140(ae)(1)(C) when combined with access credentials.",
)


# US ZIP+4 — 5-digit ZIP optionally followed by a 4-digit extension. The
# 5-digit form is widely shared (rarely identifying alone, though the
# combination of ZIP + DOB + sex re-identifies ~87% of US residents per
# Sweeney). The +4 form is precise enough to be a quasi-identifier under
# HHS Safe Harbor de-identification (45 CFR §164.514(b)(2)).
US_ZIP_PLUS4_PATTERN = PIIPattern(
    pattern_id="us_zip_plus4",
    pii_type="us_zip",
    category=CATEGORY_INDIRECT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b\d{5}(?:-\d{4})?\b",
    column_hints=[
        "zip", "zipcode", "zip_code", "postal_code", "us_zip",
        "us_postal_code", "zip4", "zip_plus_4",
    ],
    regulations=["CCPA", "CPRA"],
    description="US ZIP code (5-digit) or ZIP+4 (5+4). The +4 form re-identifies in HHS Safe Harbor terms; the 5-digit form re-identifies in combination with DOB + sex (Sweeney 2000).",
)


# The list the loader picks up. Variable name `IN_SPECIFIC_PATTERNS` is the
# pack-loader contract — kept consistent across all packs for the dynamic
# import in pack_loader.Pack.pii_patterns().
IN_SPECIFIC_PATTERNS = [
    US_SSN_PATTERN,
    US_ITIN_PATTERN,
    US_EIN_PATTERN,
    US_DRIVERS_LICENSE_PATTERN,
    US_PASSPORT_PATTERN,
    US_BANK_ACCOUNT_PATTERN,
    US_ZIP_PLUS4_PATTERN,
]
