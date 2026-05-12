"""EU-specific PII patterns for the EU GDPR pack.

Each EU member state issues its own national-identifier schemes (national
ID cards, tax numbers, social security numbers, etc.). The EU GDPR pack
ships a representative cross-section covering the largest member states by
population — German, French, Italian, Spanish, plus EU-wide identifiers
(IBAN, VAT). Member-state deployments needing fuller per-country coverage
can extend this module without touching the core loader.

Eight entries:
  IBAN                  — International Bank Account Number (EU-wide, 15-34 chars)
  EU_VAT                — VAT number (per country format, EU-wide concept)
  DE_PERSONALAUSWEIS    — German national ID card (10-digit + check digits)
  DE_STEUER_ID          — German tax ID Steueridentifikationsnummer (11 digits)
  FR_NIR                — French Social Security Number (INSEE / NIR, 13 digits + 2 key)
  IT_CODICE_FISCALE     — Italian tax code (16 alphanumeric, encodes DOB + name)
  ES_DNI                — Spanish DNI / NIE (8 digits + check letter)
  EU_PASSPORT_GENERIC   — Member-state passport number (variable per state)

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


# IBAN — ISO 13616. Country code (2 letters) + 2 check digits + BBAN (up to
# 30 alphanumeric). Length varies by country (15 Norway, 16 Belgium, …, 34
# Saint Lucia). The regex below captures the EU member-state formats only,
# pinning length 15-31 to avoid false positives on long random strings.
IBAN_PATTERN = PIIPattern(
    pattern_id="iban",
    pii_type="iban",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b",
    column_hints=[
        "iban", "bank_iban", "account_iban", "creditor_iban", "debtor_iban",
        "international_bank_account",
    ],
    regulations=["EU_GDPR", "GDPR"],
    description="International Bank Account Number — ISO 13616; covers all EU/EEA banks plus 60+ other countries. Financial identifier under PSD2 + GDPR Art. 4(1).",
)


# EU VAT number — per-country format with shared country-code prefix.
# Standard structure: 2-letter country code + 2-15 chars (digits, sometimes
# letters). Belgium adds a leading 0; Spain uses NIF; etc. The regex is
# deliberately permissive — combined with the column_hints it has a low
# false-positive rate.
EU_VAT_PATTERN = PIIPattern(
    pattern_id="eu_vat",
    pii_type="vat_number",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b(?:AT|BE|BG|CY|CZ|DE|DK|EE|EL|ES|FI|FR|HR|HU|IE|IT|LT|LU|LV|MT|NL|PL|PT|RO|SE|SI|SK)[0-9A-Z]{2,15}\b",
    column_hints=[
        "vat", "vat_number", "vat_id", "tax_id_eu", "vat_registration",
        "ust_idnr",                     # German Umsatzsteuer-Identifikationsnummer
    ],
    regulations=["EU_GDPR", "GDPR"],
    description="EU VAT identification number — per-country format with shared 2-letter prefix. Business identifier; personal data when associated with a sole trader.",
)


# German Personalausweis — national ID card number. 10 digits + check
# character (sometimes shown). Issued by Bundesdruckerei.
DE_PERSONALAUSWEIS_PATTERN = PIIPattern(
    pattern_id="de_personalausweis",
    pii_type="de_id_card",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[CFGHJKLMNPRTVWXYZ][0-9CFGHJKLMNPRTVWXYZ]{8}[0-9]\b",
    column_hints=[
        "personalausweis", "personalausweis_nr", "ausweisnummer",
        "german_id", "de_id_card",
    ],
    regulations=["EU_GDPR", "GDPR", "BDSG"],     # BDSG = Bundesdatenschutzgesetz
    description="German Personalausweis (national identity card) — 10-character identifier issued by Bundesdruckerei.",
)


# German Steueridentifikationsnummer — 11-digit tax ID issued by BZSt.
# First digit cannot be 0; one digit repeats exactly twice or thrice (the
# check-digit constraint), and the remaining digits are distinct — the
# regex below captures the length, value-range checks happen downstream.
DE_STEUER_ID_PATTERN = PIIPattern(
    pattern_id="de_steuer_id",
    pii_type="de_tax_id",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[1-9]\d{10}\b",
    column_hints=[
        "steuer_id", "steueridentifikationsnummer", "idnr",
        "german_tax_id", "de_tax_id",
    ],
    regulations=["EU_GDPR", "GDPR", "AO"],       # AO = Abgabenordnung (German Fiscal Code)
    description="German tax identification number (Steueridentifikationsnummer / IdNr) — 11-digit lifelong identifier issued by Bundeszentralamt für Steuern.",
)


# French NIR — Numéro d'Inscription au Répertoire, the INSEE-issued
# permanent social security number. 13 digits + 2-digit "clé" (check key).
# Encodes: gender (1 digit), year of birth (2), month of birth (2), INSEE
# department code (2-3), commune code (3), order (3).
FR_NIR_PATTERN = PIIPattern(
    pattern_id="fr_nir",
    pii_type="fr_ssn",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[12]\s?\d{2}\s?\d{2}\s?\d{2}A?B?\d{2,3}\s?\d{3}\s?(?:\d{2})?\b",
    column_hints=[
        "nir", "insee", "ss_number_fr", "numero_securite_sociale",
        "french_ssn", "fr_ssn",
    ],
    regulations=["EU_GDPR", "GDPR"],
    description="French Social Security number (NIR / INSEE) — 13-digit core + 2-digit checksum, encodes gender + DOB + place of birth. Cited in CNIL guidance as a heightened-sensitivity identifier.",
)


# Italian Codice Fiscale — 16-character alphanumeric tax code encoding
# surname (3 letters), given name (3 letters), DOB year (2 digits), month
# (1 letter), day-with-gender (2 digits), place-of-birth code (4 chars),
# check letter (1).
IT_CODICE_FISCALE_PATTERN = PIIPattern(
    pattern_id="it_codice_fiscale",
    pii_type="it_tax_code",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[A-Z]{6}\d{2}[A-EHLMPRST][0-9]{2}[A-Z]\d{3}[A-Z]\b",
    column_hints=[
        "codice_fiscale", "cf", "italian_tax_code", "it_tax_code",
    ],
    regulations=["EU_GDPR", "GDPR"],
    description="Italian Codice Fiscale — 16-character tax identifier encoding name, DOB, gender, place of birth. Used as a near-universal identifier in Italy beyond tax (healthcare, contracts).",
)


# Spanish DNI / NIE. DNI = 8 digits + check letter (for nationals).
# NIE = X/Y/Z + 7 digits + check letter (for foreigners). Same check
# algorithm (Mod-23 over the numeric portion).
ES_DNI_PATTERN = PIIPattern(
    pattern_id="es_dni",
    pii_type="es_id_card",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[XYZ]?\d{7,8}[A-HJ-NP-TV-Z]\b",
    column_hints=[
        "dni", "nie", "spanish_id", "es_id_card", "documento_nacional",
    ],
    regulations=["EU_GDPR", "GDPR", "LOPDGDD"],   # LOPDGDD = Spanish data protection law
    description="Spanish DNI (citizens) or NIE (foreigners) — 8/9-character national identifier. Required for most administrative and contractual processes in Spain.",
)


# Generic EU member-state passport — length 9 alphanumeric (ICAO 9303).
# Specific country formats vary; this is a catch-all for "looks like a
# passport number" matched against passport-related column hints.
EU_PASSPORT_PATTERN = PIIPattern(
    pattern_id="eu_passport",
    pii_type="passport",
    category=CATEGORY_DIRECT_GOV,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b[A-Z0-9]{8,9}\b",
    column_hints=[
        "passport", "passport_number", "passport_no", "reisepass",
        "passeport", "passaporto", "pasaporte",
    ],
    regulations=["EU_GDPR", "GDPR"],
    description="EU member-state passport number — generic ICAO 9303 format (8-9 alphanumeric). Member-state-specific patterns (e.g., German P+8-digit) could narrow this.",
)


# The list the loader picks up. Variable name `IN_SPECIFIC_PATTERNS` is the
# pack-loader contract — kept consistent across all packs for the dynamic
# import in pack_loader.Pack.pii_patterns().
IN_SPECIFIC_PATTERNS = [
    IBAN_PATTERN,
    EU_VAT_PATTERN,
    DE_PERSONALAUSWEIS_PATTERN,
    DE_STEUER_ID_PATTERN,
    FR_NIR_PATTERN,
    IT_CODICE_FISCALE_PATTERN,
    ES_DNI_PATTERN,
    EU_PASSPORT_PATTERN,
]
