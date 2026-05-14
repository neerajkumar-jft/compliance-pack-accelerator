"""Universal PII patterns — present under every privacy regulation.

Direct-identifier patterns that are universally classified as personal data:
email, phone (international format), payment card, CVV, bank account, name,
address, date of birth, IP address, medical record, insurance ID.

Region-specific patterns (Aadhaar, PAN, NHS, SSN, SIN, ...) live in
`regulations/<pack>/pii_patterns.py` and are composed with these at load
time by `schemas/pii_patterns.py`.

## Design invariants

- Every universal pattern MUST apply under at least 3 regulations.
  Anything specific to one regime (India-PAN, UK-NINO, US-SSN) belongs in a pack.
- No locale or country code assumption in the regex. Phone formats with a
  country-code prefix (e.g. `+91`) go in region packs.

## Exports

    PIIPattern, UNIVERSAL_PATTERNS
    CATEGORY_*, SENSITIVITY_*
    AUTO_CLASSIFY_THRESHOLD, REVIEW_REQUIRED_THRESHOLD, CANDIDATE_THRESHOLD
    calculate_confidence()
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional


# =============================================================================
# Pattern dataclass — identical across regulations
# =============================================================================

@dataclass
class PIIPattern:
    """A single PII detection pattern.

    A pattern declares one or more detection mechanisms — column-name hints,
    regex over values, AI classification (via Databricks ``ai_classify``),
    or AI extraction (via Databricks ``ai_extract``). The production
    classifier in ``pipelines/classification_dlt.py`` dispatches per pattern
    based on which mechanism(s) are populated. Multiple mechanisms on one
    pattern are fine — they layer (e.g., column hint + regex → ``hybrid``
    classifier_source).

    matches_value() is a unit-testing helper; production classification uses
    Spark SQL `regexp_extract` per §4.5 of the spec, not per-value Python.
    """
    pattern_id: str
    pii_type: str
    category: str
    sensitivity: str              # critical | high | medium | low
    regex_pattern: Optional[str]  # None for column-hint-only or AI-only patterns
    column_hints: list[str]
    regulations: list[str]
    description: str
    priority: int = 50
    # AI mechanisms (Databricks ai_classify / ai_extract). Optional — leaving
    # both None preserves the regex+column-hint behavior. No runtime cost
    # until a consumer (DLT scanner, weekly AI scan job, onboarding helper)
    # actually reads these fields and dispatches an LLM call.
    ai_labels: Optional[list[str]] = None         # for ai_classify(value, ARRAY[...])
    ai_extract_fields: Optional[list[str]] = None  # for ai_extract(value, ARRAY[...])

    _compiled_regex: Optional[re.Pattern] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if self.regex_pattern:
            try:
                self._compiled_regex = re.compile(self.regex_pattern)
            except re.error:
                self._compiled_regex = None
        # ai_classify needs at least 2 labels — its API rejects single-label
        # arrays. Catch the misconfiguration here rather than at scan time.
        if self.ai_labels is not None and len(self.ai_labels) < 2:
            raise ValueError(
                f"PIIPattern {self.pattern_id!r}: ai_labels must contain at least "
                f"2 labels (got {len(self.ai_labels)}); ai_classify rejects "
                f"single-label arrays."
            )

    def matches_column_name(self, column_name: str) -> bool:
        col_lower = column_name.lower()
        return any(hint.lower() in col_lower for hint in self.column_hints)

    def matches_value(self, value: Optional[str]) -> bool:
        if not self._compiled_regex or not value:
            return False
        try:
            return bool(self._compiled_regex.search(str(value)[:10000]))
        except re.error:
            return False

    def is_ai_classifiable(self) -> bool:
        """True when this pattern declares ``ai_labels`` for ``ai_classify``."""
        return self.ai_labels is not None

    def is_ai_extractable(self) -> bool:
        """True when this pattern declares ``ai_extract_fields`` for ``ai_extract``."""
        return self.ai_extract_fields is not None


# =============================================================================
# PII category taxonomy — 9 categories, regulation-agnostic
# =============================================================================

CATEGORY_DIRECT_GOV     = "direct_identifier_government"
CATEGORY_DIRECT_CONTACT = "direct_identifier_contact"
CATEGORY_DIRECT_FIN     = "direct_identifier_financial"
CATEGORY_INDIRECT       = "indirect_identifier"
CATEGORY_BIOMETRIC      = "biometric"
CATEGORY_HEALTH         = "health"
CATEGORY_SENSITIVE_DEMO = "sensitive_demographic"
CATEGORY_FIN_BEHAVIOR   = "financial_behavior"
CATEGORY_CHILDREN       = "children_marker"

SENSITIVITY_CRITICAL = "critical"
SENSITIVITY_HIGH     = "high"
SENSITIVITY_MEDIUM   = "medium"
SENSITIVITY_LOW      = "low"


# =============================================================================
# Confidence calculation — tuning constants from §4.4
# =============================================================================

BASE_CONFIDENCE_BOTH   = 0.7
BOOST_FACTOR_BOTH      = 0.3
BASE_CONFIDENCE_COLUMN = 0.5
BOOST_FACTOR_COLUMN    = 0.2
BASE_CONFIDENCE_VALUE  = 0.4
BOOST_FACTOR_VALUE     = 0.3

AUTO_CLASSIFY_THRESHOLD   = 0.85
REVIEW_REQUIRED_THRESHOLD = 0.65
CANDIDATE_THRESHOLD       = 0.50


def calculate_confidence(
    column_match: bool,
    value_match: bool,
    match_rate: float = 0.0,
) -> float:
    """Confidence score in [0.0, 1.0] from (column_hint, regex, match_rate) signals."""
    if column_match and value_match:
        return min(BASE_CONFIDENCE_BOTH + match_rate * BOOST_FACTOR_BOTH, 1.0)
    elif column_match:
        return min(BASE_CONFIDENCE_COLUMN + match_rate * BOOST_FACTOR_COLUMN, 1.0)
    elif value_match:
        return min(BASE_CONFIDENCE_VALUE + match_rate * BOOST_FACTOR_VALUE, 1.0)
    return 0.0


# =============================================================================
# Universal patterns — 11 entries
# =============================================================================

EMAIL_PATTERN = PIIPattern(
    pattern_id="email",
    pii_type="email",
    category=CATEGORY_DIRECT_CONTACT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b",
    column_hints=["email", "e_mail", "email_address", "mail"],
    regulations=["DPDP", "GDPR", "CCPA"],
    description="Email Address",
    priority=90,
)

PHONE_INTL_PATTERN = PIIPattern(
    pattern_id="phone_intl",
    pii_type="phone",
    category=CATEGORY_DIRECT_CONTACT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\+\d{1,3}[-.\s]?\d{3,14}",
    column_hints=["phone", "mobile", "telephone"],
    regulations=["DPDP", "GDPR", "CCPA"],
    description="International Phone Number",
    priority=80,
)

CREDIT_CARD_PATTERN = PIIPattern(
    pattern_id="credit_card",
    pii_type="credit_card",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=r"\b(?:4\d{12}(?:\d{3})?|5[1-5]\d{14}|3[47]\d{13}|6(?:011|5\d{2})\d{12})\b",
    column_hints=["credit_card", "card_number", "cc_number", "creditcard"],
    regulations=["DPDP", "PCI-DSS", "GDPR", "CCPA"],
    description="Credit Card Number",
    priority=100,
)

CVV_PATTERN = PIIPattern(
    pattern_id="cvv",
    pii_type="cvv",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=None,
    column_hints=["cvv", "cvc", "security_code", "card_security"],
    regulations=["DPDP", "PCI-DSS"],
    description="Card Security Code",
    priority=100,
)

BANK_ACCOUNT_PATTERN = PIIPattern(
    pattern_id="bank_account",
    pii_type="bank_account",
    category=CATEGORY_DIRECT_FIN,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=None,
    column_hints=["bank_account", "account_number", "iban", "routing_number"],
    regulations=["DPDP", "PCI-DSS"],
    description="Bank Account Number",
    priority=95,
)

NAME_PATTERN = PIIPattern(
    pattern_id="name",
    pii_type="name",
    category=CATEGORY_DIRECT_CONTACT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=None,
    column_hints=[
        "name", "first_name", "last_name", "full_name",
        "firstname", "lastname", "patient_name", "account_holder_name",
    ],
    regulations=["DPDP", "GDPR", "CCPA"],
    description="Person Name",
    priority=70,
)

ADDRESS_PATTERN = PIIPattern(
    pattern_id="address",
    pii_type="address",
    category=CATEGORY_DIRECT_CONTACT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=None,
    column_hints=["address", "street", "billing_address", "shipping_address"],
    regulations=["DPDP", "GDPR", "CCPA"],
    description="Physical Address",
    priority=75,
)

DOB_PATTERN = PIIPattern(
    pattern_id="dob",
    pii_type="date_of_birth",
    category=CATEGORY_INDIRECT,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=None,
    column_hints=["dob", "date_of_birth", "birth_date", "birthdate"],
    regulations=["DPDP", "GDPR", "HIPAA"],
    description="Date of Birth",
    priority=80,
)

IP_ADDRESS_PATTERN = PIIPattern(
    pattern_id="ip_address",
    pii_type="ip_address",
    category=CATEGORY_DIRECT_CONTACT,
    sensitivity=SENSITIVITY_MEDIUM,
    regex_pattern=r"\b(?:(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\.){3}(?:25[0-5]|2[0-4]\d|[01]?\d\d?)\b",
    column_hints=["ip_address", "ip", "client_ip", "user_ip", "last_login_ip"],
    regulations=["DPDP", "GDPR", "CCPA"],
    description="IP Address",
    priority=60,
)

MEDICAL_RECORD_PATTERN = PIIPattern(
    pattern_id="medical_record",
    pii_type="medical_record",
    category=CATEGORY_HEALTH,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=None,
    # NOTE: `medical_record` was removed from column_hints — substring match
    # caused false positives on `medical_record_number` (a numeric MRN ID,
    # not free-text). The remaining hints catch the actually-free-text
    # clinical columns. To detect free-text clinical observations on a
    # column NOT named with these hints (e.g., `notes`), see
    # CLINICAL_NOTES_PATTERN below.
    column_hints=[
        "diagnosis", "prescription",
        "treatment", "health_condition", "allergies",
    ],
    regulations=["DPDP", "HIPAA"],
    description="Medical Record",
    priority=100,
    # AI-classifiable: clinical free-text fields can't be regex'd, but
    # ai_classify can sort each value into a medical category. Negative
    # label MUST be last (the pii_ai_scan job uses ai_labels[-1] as the
    # "not PII" sentinel when computing match_rate).
    ai_labels=["diagnosis", "prescription", "allergy_note", "non_medical"],
)

# Free-text clinical notes — observations, follow-ups, treatment plans,
# patient communications. The `notes` column on patients_tagged is the
# canonical example: values like "Patient reports improvement",
# "Follow-up required in 2 weeks", "Lab results pending". Pure natural
# language — regex cannot extract PII signal; ai_classify can categorize
# whether the value is clinically meaningful.
#
# Column hint is intentionally just `notes` (currently only matches
# patients_tagged.notes in the schema). If a future schema adds free-text
# `notes` columns to non-clinical tables (e.g., support_tickets.notes),
# this pattern would also fire on those — which is acceptable because the
# `non_clinical` label catches them and the DELETE-on-below-threshold
# logic in pii_ai_scan suppresses any false-positive finding.
CLINICAL_NOTES_PATTERN = PIIPattern(
    pattern_id="clinical_notes_freetext",
    pii_type="clinical_notes",
    category=CATEGORY_HEALTH,
    sensitivity=SENSITIVITY_CRITICAL,
    regex_pattern=None,
    column_hints=["notes"],
    regulations=["DPDP", "HIPAA"],
    description="Free-text clinical notes (observations, follow-ups, treatment plans)",
    priority=100,
    ai_labels=["clinical_observation", "treatment_plan",
               "patient_communication", "non_clinical"],
)

INSURANCE_ID_PATTERN = PIIPattern(
    pattern_id="insurance_id",
    pii_type="insurance_id",
    category=CATEGORY_HEALTH,
    sensitivity=SENSITIVITY_HIGH,
    regex_pattern=None,
    column_hints=[
        "insurance_id", "policy_number", "insurance_number", "health_insurance",
    ],
    regulations=["DPDP", "HIPAA"],
    description="Insurance ID",
    priority=90,
)


UNIVERSAL_PATTERNS: list[PIIPattern] = [
    EMAIL_PATTERN,
    PHONE_INTL_PATTERN,
    CREDIT_CARD_PATTERN,
    CVV_PATTERN,
    BANK_ACCOUNT_PATTERN,
    NAME_PATTERN,
    ADDRESS_PATTERN,
    DOB_PATTERN,
    IP_ADDRESS_PATTERN,
    MEDICAL_RECORD_PATTERN,
    CLINICAL_NOTES_PATTERN,
    INSURANCE_ID_PATTERN,
]
