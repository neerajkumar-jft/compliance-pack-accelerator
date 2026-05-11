#!/usr/bin/env python3
"""
DPDP POC synthetic data generator.

Generates deterministic synthetic Indian personal data for five source tables
plus consent events. All output is seeded for reproducibility.

Usage:
    python generate_synthetic_data.py --output-dir /path/to/landing --seed 42
"""

import argparse
import csv
import gzip
import json
import os
import random
import string
import uuid
from datetime import date, datetime, timedelta, timezone
from io import StringIO

from faker import Faker

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
SEED = 42
GENERATOR_DATE = date(2026, 4, 17)  # fixed to avoid non-determinism
IST = timezone(timedelta(hours=5, minutes=30))

TARGET_EMPLOYEES = 2000
TARGET_CUSTOMERS = 5000
TARGET_PATIENTS = 1500
TARGET_TRANSACTIONS = 10000
TARGET_USERS = 3000

DSR_PRINCIPAL_ID = "customer_04217"
DSR_PRINCIPAL_INDEX = 4217  # 0-indexed

IFSC_BANK_PREFIXES = [
    "SBIN", "HDFC", "ICIC", "AXIS", "KKBK",
    "YESB", "PUNB", "UTIB", "IOBA", "UNIN",
]

INDIAN_IP_PREFIXES = ["103", "49", "122", "157", "182", "117", "106", "14", "123"]

INDIAN_STATES = [
    "Andhra Pradesh", "Assam", "Bihar", "Chhattisgarh", "Goa", "Gujarat",
    "Haryana", "Himachal Pradesh", "Jharkhand", "Karnataka", "Kerala",
    "Madhya Pradesh", "Maharashtra", "Manipur", "Meghalaya", "Mizoram",
    "Nagaland", "Odisha", "Punjab", "Rajasthan", "Sikkim", "Tamil Nadu",
    "Telangana", "Tripura", "Uttar Pradesh", "Uttarakhand", "West Bengal",
    "Delhi",
]

INDIAN_CITIES = {
    "Karnataka": ["Bangalore", "Mysore", "Mangalore", "Hubli"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur", "Nashik"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai", "Salem"],
    "Telangana": ["Hyderabad", "Warangal", "Nizamabad"],
    "Delhi": ["New Delhi", "Delhi"],
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara", "Rajkot"],
    "West Bengal": ["Kolkata", "Howrah", "Siliguri"],
    "Uttar Pradesh": ["Lucknow", "Noida", "Agra", "Varanasi"],
    "Rajasthan": ["Jaipur", "Jodhpur", "Udaipur"],
    "Kerala": ["Kochi", "Thiruvananthapuram", "Kozhikode"],
    "Punjab": ["Chandigarh", "Ludhiana", "Amritsar"],
}

DEPARTMENTS = [
    "Engineering", "Marketing", "Sales", "Finance", "HR",
    "Operations", "Legal", "Customer Support", "Product", "Data Science",
]

DESIGNATIONS = [
    "Junior Engineer", "Senior Engineer", "Staff Engineer", "Lead Engineer",
    "Manager", "Senior Manager", "Director", "VP", "Analyst", "Associate",
]

LOYALTY_TIERS = ["bronze", "silver", "gold", "platinum"]
GENDERS = ["Male", "Female", "Other"]
BLOOD_GROUPS = ["A+", "A-", "B+", "B-", "O+", "O-", "AB+", "AB-"]
INSURANCE_PROVIDERS = [
    "Star Health", "HDFC ERGO", "ICICI Lombard",
    "Max Bupa", "Bajaj Allianz", "New India Assurance",
]
TRANSACTION_TYPES = ["purchase", "refund", "subscription", "transfer"]
TRANSACTION_STATUSES = ["completed", "pending", "failed", "reversed"]
PAYMENT_METHODS = ["credit_card", "debit_card", "upi", "net_banking", "wallet"]
ACCOUNT_STATUSES = ["active", "suspended", "deactivated", "pending_verification"]

CONSENT_PURPOSES = [
    "marketing_email", "marketing_sms", "analytics",
    "third_party_sharing", "product_personalization", "core_service",
]
CONSENT_CHANNELS = ["web", "mobile_app", "call_center", "partner_api"]
CONSENT_CAPTURE_METHODS = [
    "checkbox", "toggle", "ivr_digit", "signed_document", "implicit_continue",
]
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/122.0",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15",
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 Chrome/122.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3) AppleWebKit/605.1.15 Safari/17.2",
]

# ---------------------------------------------------------------------------
# Multi-jurisdiction mix (ADR-0001 M2)
# ---------------------------------------------------------------------------
# Customer-level rows are seeded across jurisdictions in this ratio so the
# rule engine has principals for each loaded pack to route to. The mix is
# deterministic given the seed (pick_jurisdiction consumes one rng draw per
# row). Adjusting the mix changes the per-jurisdiction PII / consent /
# compliance-gap counts but not their relative ratios.
#
#   70% IN  → governed by regulations/dpdp_2023/ (730d retention, ₹250cr cap)
#   25% GB  → governed by regulations/uk_gdpr/  (90d retention, £17.5M cap)
#    5% NULL → "country uncaptured" — surfaces as high-severity gap UK-LAW-001
#             until backfilled (ADR-0001 §"Schema migration").
JURISDICTION_MIX: list[tuple[str | None, float]] = [
    ("IN", 0.70),
    ("GB", 0.25),
    (None, 0.05),                       # unmapped — country left blank
]

# UK cities + counties — used when jurisdiction is 'GB'.
UK_REGIONS = [
    "Greater London", "Greater Manchester", "West Midlands", "West Yorkshire",
    "Merseyside", "South Yorkshire", "Tyne and Wear", "Strathclyde",
    "Edinburgh", "Glasgow City", "Cardiff", "Belfast",
]
UK_CITIES = [
    "London", "Manchester", "Birmingham", "Leeds", "Liverpool", "Sheffield",
    "Bristol", "Newcastle", "Nottingham", "Edinburgh", "Glasgow", "Cardiff",
    "Belfast", "Reading", "Brighton", "Oxford", "Cambridge", "Southampton",
]


def pick_jurisdiction(rng: random.Random) -> str | None:
    """Return 'IN' / 'GB' / None according to JURISDICTION_MIX. One rng draw."""
    r = rng.random()
    cumulative = 0.0
    for code, weight in JURISDICTION_MIX:
        cumulative += weight
        if r < cumulative:
            return code
    return JURISDICTION_MIX[-1][0]  # safety, never hits


def country_for(jur: str | None) -> str:
    """Country string written into the row's `country` column."""
    if jur == "IN":
        return "India"
    if jur == "GB":
        return "United Kingdom"
    return ""                            # unmapped → empty string


# ---------------------------------------------------------------------------
# UK-specific PII generators
# ---------------------------------------------------------------------------
def fake_uk_mobile(rng: random.Random, with_prefix: bool = True) -> str:
    """UK mobile (E.164: +44 7xxx xxxxxx; leading 7 after country code)."""
    rest = "".join(rng.choices("0123456789", k=9))
    if with_prefix:
        return f"+44-7{rest}"
    return f"07{rest}"


def fake_uk_postcode(rng: random.Random) -> str:
    """UK postcode in canonical A9 9AA / AA9 9AA form."""
    area = rng.choice([
        "SW1A", "EC1A", "W1A", "WC1H", "NW1", "SE1", "E14", "N1", "M1",
        "B1", "L1", "G1", "EH1", "CF10", "BT1", "OX1", "CB2", "BS1",
    ])
    sector = rng.randint(0, 9)
    unit = "".join(rng.choices("ABDEFGHJLNPQRSTUWXYZ", k=2))
    return f"{area} {sector}{unit}"


def fake_nhs_number(rng: random.Random) -> str:
    """NHS Number — 10 digits, last is a Mod-11 check digit.

    NHS check-digit algorithm:
      1. multiply each of the first 9 digits by (10, 9, 8, ..., 2)
      2. sum the products, divide by 11, remainder = R
      3. check digit = 11 - R (or 0 if R == 0; INVALID if R == 10)

    Re-rolls invalid candidates so output always validates.
    """
    while True:
        digits = [rng.randint(0, 9) for _ in range(9)]
        weighted = sum(d * w for d, w in zip(digits, range(10, 1, -1)))
        rem = weighted % 11
        check = 11 - rem
        if check == 11:
            check = 0
        if check == 10:
            continue                     # invalid; re-roll
        digits.append(check)
        s = "".join(str(d) for d in digits)
        return f"{s[:3]} {s[3:6]} {s[6:]}"


def fake_nino(rng: random.Random) -> str:
    """UK National Insurance Number: 2 letters + 6 digits + suffix A-D."""
    # Valid first letters: A-Z except D, F, I, Q, U, V
    valid_first = "ABCEGHJKLMNOPRSTWXYZ"
    valid_second = "ABCEGHJLMNPRSTWXYZ"   # excludes D, F, I, O, Q, U, V
    prefix = rng.choice(valid_first) + rng.choice(valid_second)
    digits = "".join(rng.choices("0123456789", k=6))
    suffix = rng.choice("ABCD")
    return f"{prefix} {digits[:2]} {digits[2:4]} {digits[4:]} {suffix}"


def fake_utr(rng: random.Random) -> str:
    """HMRC Unique Taxpayer Reference: 10 digits, no checksum."""
    return "".join(rng.choices("0123456789", k=10))


def fake_uk_address(rng: random.Random, fake: Faker) -> str:
    """UK-shaped address string."""
    return (
        f"{rng.randint(1, 199)} {fake.last_name()} "
        f"{rng.choice(['Road','Street','Lane','Avenue','Close','Place'])}, "
        f"{rng.choice(UK_CITIES)}"
    )


# ---------------------------------------------------------------------------
# PII generators
# ---------------------------------------------------------------------------
def fake_aadhaar(rng: random.Random) -> str:
    first = rng.choice("23456789")
    rest = "".join(rng.choices("0123456789", k=11))
    num = first + rest
    return f"{num[:4]} {num[4:8]} {num[8:]}"


def fake_pan(rng: random.Random) -> str:
    letters = string.ascii_uppercase
    return (
        "".join(rng.choices(letters, k=3))
        + rng.choice(letters)
        + "P"
        + "".join(rng.choices("0123456789", k=4))
        + rng.choice(letters)
    )


def fake_passport(rng: random.Random) -> str:
    return rng.choice(string.ascii_uppercase) + "".join(
        rng.choices("0123456789", k=7)
    )


def fake_ifsc(rng: random.Random) -> str:
    prefix = rng.choice(IFSC_BANK_PREFIXES)
    branch = "".join(
        rng.choices(string.ascii_uppercase + string.digits, k=6)
    )
    return f"{prefix}0{branch}"


def fake_indian_mobile(rng: random.Random, with_prefix: bool = True) -> str:
    leading = rng.choice("6789")
    rest = "".join(rng.choices("0123456789", k=9))
    if with_prefix:
        return f"+91-{leading}{rest}"
    return f"{leading}{rest}"


def fake_indian_ip(rng: random.Random) -> str:
    p = rng.choice(INDIAN_IP_PREFIXES)
    return f"{p}.{rng.randint(0,255)}.{rng.randint(0,255)}.{rng.randint(0,255)}"


def fake_ip(rng: random.Random, fake: Faker) -> str:
    if rng.random() < 0.9:
        return fake_indian_ip(rng)
    return fake.ipv4()


def fake_bank_account(rng: random.Random) -> str:
    length = rng.choice([11, 12, 14, 16])
    return "".join(rng.choices("0123456789", k=length))


def fake_address(rng: random.Random, fake: Faker) -> str:
    num = rng.randint(1, 999)
    street = fake.street_name()
    area = fake.city_suffix()
    return f"{num} {street}, {area}"


def fake_postal_code(rng: random.Random) -> str:
    first = rng.choice("123456789")
    rest = "".join(rng.choices("0123456789", k=5))
    return first + rest


def fake_dob(rng: random.Random, min_age: int = 18, max_age: int = 70) -> date:
    days_back = rng.randint(min_age * 365, max_age * 365)
    return GENERATOR_DATE - timedelta(days=days_back)


def fake_date_range(
    rng: random.Random, start: date, end: date
) -> date:
    delta = (end - start).days
    if delta <= 0:
        return start
    return start + timedelta(days=rng.randint(0, delta))


# ---------------------------------------------------------------------------
# Table generators
# ---------------------------------------------------------------------------
def generate_employees(rng: random.Random, fake: Faker, count: int) -> list[dict]:
    rows = []
    for i in range(count):
        emp_id = f"EMP{i+1:06d}"
        # ADR-0001 M2: jurisdiction mix drives country + region + PII shape.
        jur = pick_jurisdiction(rng)
        if jur == "GB":
            region = rng.choice(UK_REGIONS)
            city = rng.choice(UK_CITIES)
            phone = fake_uk_mobile(rng)
            address = fake_uk_address(rng, fake)
            postal = fake_uk_postcode(rng)
        else:
            # IN and unmapped both use Indian-shape data — unmapped is "country
            # uncaptured" not "non-Indian".
            region = rng.choice(INDIAN_STATES)
            cities = INDIAN_CITIES.get(region, [fake.city()])
            city = rng.choice(cities)
            phone = fake_indian_mobile(rng)
            address = fake_address(rng, fake)
            postal = fake_postal_code(rng)

        row = {
            "employee_id": emp_id,
            "first_name": fake.first_name(),
            "last_name": fake.last_name(),
            "email": f"{fake.user_name()}@company.com",
            "phone_number": phone,
            "date_of_birth": fake_dob(rng, 22, 60).isoformat(),
            # India-only PII fields are blank on GB rows; the per-data-subject
            # classifier under UK GDPR doesn't expect aadhaar/pan/ifsc.
            "aadhaar_number": "" if jur == "GB" else fake_aadhaar(rng),
            "pan_number": "" if jur == "GB" else fake_pan(rng),
            "passport_number": fake_passport(rng),
            "address": address,
            "city": city,
            "state": region,
            "country": country_for(jur),
            "postal_code": postal,
            "salary": round(rng.uniform(300000, 5000000), 2),
            "bank_account": fake_bank_account(rng),
            "ifsc_code": "" if jur == "GB" else fake_ifsc(rng),
            "department": rng.choice(DEPARTMENTS),
            "designation": rng.choice(DESIGNATIONS),
            "hire_date": fake_date_range(
                rng, date(2015, 1, 1), date(2026, 3, 1)
            ).isoformat(),
            "manager_employee_id": (
                f"EMP{rng.randint(1, max(1, i)):06d}" if i > 0 else ""
            ),
        }
        rows.append(row)
    return rows


def generate_customers(rng: random.Random, fake: Faker, count: int) -> list[dict]:
    rows = []
    for i in range(count):
        cust_id = f"customer_{i:05d}"
        # DSR test principal (customer_04217) must stay Indian — many tests
        # assert DPDP-specific behaviour against this row.
        if i == DSR_PRINCIPAL_INDEX:
            jur = "IN"
        else:
            jur = pick_jurisdiction(rng)

        if jur == "GB":
            region = rng.choice(UK_REGIONS)
            city = rng.choice(UK_CITIES)
            mobile = fake_uk_mobile(rng, with_prefix=rng.random() < 0.6)
            address = fake_uk_address(rng, fake)
            postal = fake_uk_postcode(rng)
            pref_lang = "en"
        else:
            region = rng.choice(INDIAN_STATES)
            cities = INDIAN_CITIES.get(region, [fake.city()])
            city = rng.choice(cities)
            mobile = fake_indian_mobile(rng, with_prefix=rng.random() < 0.6)
            address = fake_address(rng, fake)
            postal = fake_postal_code(rng)
            pref_lang = rng.choice(
                ["en", "hi", "ta", "te", "kn", "ml", "mr", "bn", "gu"]
            )

        reg_date = fake_date_range(rng, date(2020, 1, 1), date(2026, 3, 1))

        row = {
            "customer_id": cust_id,
            "full_name": f"{fake.first_name()} {fake.last_name()}",
            "email_address": fake.email(),
            "mobile": mobile,
            "date_of_birth": fake_dob(rng, 18, 75).isoformat(),
            # Aadhaar/PAN are India-only IDs; GB rows leave them blank so the
            # DPDP classifier doesn't surface false-positive matches on UK
            # principals.
            "aadhaar_number": "" if jur == "GB" else fake_aadhaar(rng),
            "pan_number": "" if jur == "GB" else fake_pan(rng),
            "credit_card_number": fake.credit_card_number(),
            "cvv": f"{rng.randint(100,999)}",
            "billing_address": address,
            "city": city,
            "state": region,
            "country": country_for(jur),       # ADR-0001 M2 routing key
            "postal_code": postal,
            "loyalty_tier": rng.choice(LOYALTY_TIERS),
            "loyalty_points": rng.randint(0, 50000),
            "preferred_language": pref_lang,
            "registration_date": reg_date.isoformat(),
            "last_activity_date": fake_date_range(
                rng, reg_date, GENERATOR_DATE
            ).isoformat(),
            "account_holder_name": "",  # filled below
            "ip_address": fake_ip(rng, fake),
        }
        row["account_holder_name"] = row["full_name"]
        rows.append(row)
    return rows


def generate_patients(rng: random.Random, fake: Faker, count: int) -> list[dict]:
    rows = []
    for i in range(count):
        patient_id = f"PAT{i+1:06d}"
        dob = fake_dob(rng, 1, 90)
        last_visit = fake_date_range(rng, date(2024, 1, 1), GENERATOR_DATE)

        diagnoses = [
            "Type 2 Diabetes Mellitus", "Essential Hypertension",
            "Acute Upper Respiratory Infection", "Allergic Rhinitis",
            "Iron Deficiency Anemia", "Hypothyroidism",
            "Gastroesophageal Reflux Disease", "Osteoarthritis",
            "Migraine without Aura", "Chronic Lower Back Pain",
            "Bronchial Asthma", "Urinary Tract Infection",
        ]
        prescriptions = [
            "Metformin 500mg BD", "Amlodipine 5mg OD", "Paracetamol 500mg TDS",
            "Cetirizine 10mg OD", "Ferrous Sulfate 200mg OD", "Levothyroxine 50mcg OD",
            "Pantoprazole 40mg OD", "Ibuprofen 400mg BD", "Sumatriptan 50mg PRN",
        ]

        jur = pick_jurisdiction(rng)
        if jur == "GB":
            phone = fake_uk_mobile(rng)
            emergency_phone = fake_uk_mobile(rng)
            aadhaar = ""
            nhs = fake_nhs_number(rng)         # GB rows carry NHS Number
        else:
            phone = fake_indian_mobile(rng)
            emergency_phone = fake_indian_mobile(rng)
            aadhaar = fake_aadhaar(rng)
            nhs = ""

        row = {
            "patient_id": patient_id,
            "medical_record_number": f"MRN-{rng.randint(100000,999999)}",
            "full_name": f"{fake.first_name()} {fake.last_name()}",
            "date_of_birth": dob.isoformat(),
            "gender": rng.choice(GENDERS),
            "aadhaar_number": aadhaar,
            "nhs_number": nhs,                # UK GDPR special-category PII
            "phone": phone,
            "email": fake.email(),
            "emergency_contact_name": f"{fake.first_name()} {fake.last_name()}",
            "emergency_contact_phone": emergency_phone,
            "blood_group": rng.choice(BLOOD_GROUPS),
            "primary_diagnosis": rng.choice(diagnoses),
            "current_prescription": rng.choice(prescriptions),
            "insurance_provider": rng.choice(INSURANCE_PROVIDERS),
            "insurance_id": f"INS-{rng.randint(10000000,99999999)}",
            "allergies": rng.choice(
                ["None", "Penicillin", "Sulfa drugs", "Aspirin", "Ibuprofen", "Latex"]
            ),
            "attending_physician": f"Dr. {fake.last_name()}",
            "country": country_for(jur),       # ADR-0001 M2 routing key
            "last_visit_date": last_visit.isoformat(),
            "next_appointment": (
                last_visit + timedelta(days=rng.randint(7, 180))
            ).isoformat(),
            "ward": rng.choice(["OPD", "Ward A", "Ward B", "ICU", "Emergency"]),
            "notes": rng.choice([
                "", "Follow-up required in 2 weeks",
                "Patient reports improvement", "Referred to specialist",
                "Lab results pending",
            ]),
        }
        rows.append(row)
    return rows


def generate_transactions(
    rng: random.Random, fake: Faker, customers: list[dict], count: int
) -> list[dict]:
    """Generate transactions with Zipf-like distribution over customers.

    Ensures DSR principal (customer_04217) gets 12-20 transactions.
    """
    rows = []
    n_customers = len(customers)

    # Ensure DSR principal gets 12-20 transactions first
    dsr_txn_count = rng.randint(12, 20)
    dsr_cust = customers[DSR_PRINCIPAL_INDEX]
    for _ in range(dsr_txn_count):
        txn_date = fake_date_range(rng, date(2024, 10, 1), GENERATOR_DATE)
        row = {
            "transaction_id": f"TXN{len(rows)+1:08d}",
            "customer_id": dsr_cust["customer_id"],
            "transaction_date": f"{txn_date.isoformat()}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}",
            "amount": round(rng.uniform(10, 250000), 2),
            "currency": "INR",
            "transaction_type": rng.choice(TRANSACTION_TYPES),
            "status": rng.choice(TRANSACTION_STATUSES),
            "payment_method": rng.choice(PAYMENT_METHODS),
            "card_last_four": dsr_cust["credit_card_number"][-4:],
            "merchant_name": fake.company(),
            "merchant_category": rng.choice([
                "retail", "groceries", "electronics", "dining",
                "travel", "healthcare", "utilities", "entertainment",
            ]),
            "ip_address": fake_ip(rng, fake),
            "device_id": f"DEV-{uuid.UUID(int=rng.getrandbits(128)).hex[:12]}",
            "account_holder_name": dsr_cust["account_holder_name"],
            "location": rng.choice(
                list(INDIAN_CITIES.get(dsr_cust["state"], [dsr_cust["city"]]))
            ),
        }
        rows.append(row)

    # Generate remaining transactions with Zipf distribution (excluding DSR principal)
    remaining = count - dsr_txn_count
    eligible_indices = [j for j in range(n_customers) if j != DSR_PRINCIPAL_INDEX]
    weights = [1.0 / (j + 1) ** 0.5 for j in eligible_indices]
    total_w = sum(weights)
    weights = [w / total_w for w in weights]

    for i in range(remaining):
        cust_idx = rng.choices(eligible_indices, weights=weights, k=1)[0]
        cust = customers[cust_idx]
        txn_date = fake_date_range(rng, date(2024, 10, 1), GENERATOR_DATE)

        row = {
            "transaction_id": f"TXN{i+1:08d}",
            "customer_id": cust["customer_id"],
            "transaction_date": f"{txn_date.isoformat()}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}",
            "amount": round(rng.uniform(10, 250000), 2),
            "currency": "INR",
            "transaction_type": rng.choice(TRANSACTION_TYPES),
            "status": rng.choice(TRANSACTION_STATUSES),
            "payment_method": rng.choice(PAYMENT_METHODS),
            "card_last_four": cust["credit_card_number"][-4:],
            "merchant_name": fake.company(),
            "merchant_category": rng.choice([
                "retail", "groceries", "electronics", "dining",
                "travel", "healthcare", "utilities", "entertainment",
            ]),
            "ip_address": fake_ip(rng, fake),
            "device_id": f"DEV-{uuid.UUID(int=rng.getrandbits(128)).hex[:12]}",
            "account_holder_name": cust["account_holder_name"],
            "location": rng.choice(
                list(INDIAN_CITIES.get(cust["state"], [cust["city"]]))
            ),
        }
        rows.append(row)
    return rows


def generate_users(
    rng: random.Random, fake: Faker, customers: list[dict], count: int
) -> list[dict]:
    """Generate users, 50% overlap with customers."""
    rows = []
    overlap_count = count // 2
    # Pick overlap customers deterministically, ensuring DSR principal is included
    candidate_indices = [j for j in range(len(customers)) if j != DSR_PRINCIPAL_INDEX]
    overlap_indices = sorted(
        [DSR_PRINCIPAL_INDEX] + rng.sample(candidate_indices, k=overlap_count - 1)
    )

    for i in range(count):
        user_id = f"USR{i+1:06d}"
        is_overlap = i < overlap_count

        if is_overlap:
            cust = customers[overlap_indices[i]]
            name_parts = cust["full_name"].split(" ", 1)
            first_name = name_parts[0]
            last_name = name_parts[1] if len(name_parts) > 1 else ""
            email = cust["email_address"]
        else:
            first_name = fake.first_name()
            last_name = fake.last_name()
            email = fake.email()

        created = fake_date_range(rng, date(2021, 1, 1), date(2026, 3, 1))
        last_login = fake_date_range(rng, created, GENERATOR_DATE)

        # User jurisdiction: when the user overlaps with an existing customer,
        # inherit the customer's country to keep the linked-principal join
        # honest. For non-overlap users, draw from the mix independently.
        if is_overlap:
            jur = derive_user_jurisdiction_from_country(cust.get("country", ""))
        else:
            jur = pick_jurisdiction(rng)
        phone = fake_uk_mobile(rng) if jur == "GB" else fake_indian_mobile(rng)
        pref_lang = "en" if jur == "GB" else rng.choice(
            ["en", "hi", "ta", "te", "kn", "ml"]
        )

        row = {
            "user_id": user_id,
            "username": fake.user_name(),
            "email": email,
            "first_name": first_name,
            "last_name": last_name,
            "phone": phone,
            "date_of_birth": fake_dob(rng, 18, 70).isoformat(),
            "ip_address": fake_ip(rng, fake),
            "device_id": f"DEV-{uuid.UUID(int=rng.getrandbits(128)).hex[:12]}",
            "account_status": rng.choice(ACCOUNT_STATUSES),
            "mfa_enabled": rng.choice(["true", "false"]),
            "last_login": f"{last_login.isoformat()}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}",
            "created_at": f"{created.isoformat()}T{rng.randint(0,23):02d}:{rng.randint(0,59):02d}:{rng.randint(0,59):02d}",
            "preferred_language": pref_lang,
            "marketing_opt_in": rng.choice(["true", "false"]),
            "terms_accepted_version": f"v{rng.choice([1,2,3])}.{rng.randint(0,5)}",
            "referral_source": rng.choice([
                "organic", "google_ads", "social_media", "referral", "direct",
            ]),
            "country": country_for(jur),      # ADR-0001 M2 routing key
        }
        rows.append(row)
    return rows


def derive_user_jurisdiction_from_country(country: str) -> str | None:
    """Inverse of country_for() — used when a user inherits its parent
    customer's country and we need the jurisdiction code to render PII shape.
    Kept narrow on purpose; the canonical mapping lives in
    governance_core.pack_loader.derive_jurisdiction.
    """
    if country == "India":
        return "IN"
    if country == "United Kingdom":
        return "GB"
    return None


def generate_consent_events(
    rng: random.Random,
    customers: list[dict],
    count: int,
    dsr_principal_index: int,
) -> list[dict]:
    """Generate consent events as JSON for Day 9 Lakebase ingestion."""
    events = []
    n_customers = len(customers)

    # Target distribution by purpose
    purpose_targets = {
        "marketing_email": 350,
        "marketing_sms": 250,
        "analytics": 180,
        "third_party_sharing": 100,
        "product_personalization": 90,
        "core_service": 30,
    }

    # Grant rates by purpose
    grant_rates = {
        "marketing_email": 0.75,
        "marketing_sms": 0.60,
        "analytics": 0.85,
        "third_party_sharing": 0.40,
        "product_personalization": 0.70,
        "core_service": 0.98,
    }

    # First: insert the 4 DSR principal events deterministically
    dsr_cust = customers[dsr_principal_index]
    base_ts = datetime(2026, 2, 16, 14, 23, 10, tzinfo=IST)  # ~Day -60

    dsr_events = [
        {
            "data_principal_external_id": dsr_cust["customer_id"],
            "event_timestamp": base_ts.isoformat(),
            "event_type": "granted",
            "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
            "channel": "web",
            "purpose": "marketing_email",
            "purpose_grant_status": "granted",
            "ip_address": dsr_cust["ip_address"],
            "user_agent": rng.choice(USER_AGENTS),
            "consent_capture_method": "checkbox",
            "retention_clock_start": base_ts.isoformat(),
            "retention_duration_days": 365,
        },
        {
            "data_principal_external_id": dsr_cust["customer_id"],
            "event_timestamp": (base_ts + timedelta(seconds=5)).isoformat(),
            "event_type": "granted",
            "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
            "channel": "web",
            "purpose": "analytics",
            "purpose_grant_status": "granted",
            "ip_address": dsr_cust["ip_address"],
            "user_agent": rng.choice(USER_AGENTS),
            "consent_capture_method": "checkbox",
            "retention_clock_start": (base_ts + timedelta(seconds=5)).isoformat(),
            "retention_duration_days": 365,
        },
        {
            "data_principal_external_id": dsr_cust["customer_id"],
            "event_timestamp": (base_ts + timedelta(seconds=10)).isoformat(),
            "event_type": "granted",
            "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
            "channel": "web",
            "purpose": "third_party_sharing",
            "purpose_grant_status": "declined",
            "ip_address": dsr_cust["ip_address"],
            "user_agent": rng.choice(USER_AGENTS),
            "consent_capture_method": "checkbox",
            "retention_clock_start": (base_ts + timedelta(seconds=10)).isoformat(),
            "retention_duration_days": 365,
        },
        {
            "data_principal_external_id": dsr_cust["customer_id"],
            "event_timestamp": datetime(2026, 4, 12, 22, 5, 0, tzinfo=IST).isoformat(),  # Day -5
            "event_type": "withdrawn",
            "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
            "channel": "mobile_app",
            "purpose": "marketing_email",
            "purpose_grant_status": "declined",
            "ip_address": fake_indian_ip(rng),
            "user_agent": USER_AGENTS[1],  # iPhone
            "consent_capture_method": "toggle",
            "retention_clock_start": datetime(2026, 4, 12, 22, 5, 0, tzinfo=IST).isoformat(),
            "retention_duration_days": 0,
        },
    ]
    events.extend(dsr_events)

    # Generate remaining events (count - 4)
    remaining = count - 4
    # Pick ~300 distinct principals
    principal_pool_size = 300
    principal_indices = sorted(
        rng.sample(
            [j for j in range(n_customers) if j != dsr_principal_index],
            k=min(principal_pool_size - 1, n_customers - 1),
        )
    )

    # Build flat list of (purpose, target_count)
    purpose_pool = []
    for purpose, target in purpose_targets.items():
        purpose_pool.extend([purpose] * target)
    rng.shuffle(purpose_pool)

    for idx in range(remaining):
        purpose = purpose_pool[idx % len(purpose_pool)]
        cust_idx = rng.choice(principal_indices)
        cust = customers[cust_idx]

        # Decide event type: 92% granted/declined, 5% withdrawn, 3% modified
        roll = rng.random()
        if roll < 0.92:
            is_grant = rng.random() < grant_rates[purpose]
            event_type = "granted"
            grant_status = "granted" if is_grant else "declined"
        elif roll < 0.97:
            event_type = "withdrawn"
            grant_status = "declined"
        else:
            event_type = "modified"
            grant_status = rng.choice(["granted", "declined"])

        ts = datetime(
            2026,
            rng.randint(1, 4),
            rng.randint(1, 28),
            rng.randint(6, 23),
            rng.randint(0, 59),
            rng.randint(0, 59),
            tzinfo=IST,
        )

        channel = rng.choice(CONSENT_CHANNELS)
        event = {
            "data_principal_external_id": cust["customer_id"],
            "event_timestamp": ts.isoformat(),
            "event_type": event_type,
            "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
            "channel": channel,
            "purpose": purpose,
            "purpose_grant_status": grant_status,
            "ip_address": fake_ip(rng, Faker()),
            "user_agent": rng.choice(USER_AGENTS),
            "consent_capture_method": rng.choice(CONSENT_CAPTURE_METHODS),
            "retention_clock_start": ts.isoformat(),
            "retention_duration_days": rng.choice([90, 180, 365, 730]),
        }
        if channel == "partner_api":
            event["partner_source_id"] = f"PARTNER-{rng.randint(1,20):03d}"
        events.append(event)

    return events


# ---------------------------------------------------------------------------
# File writers
# ---------------------------------------------------------------------------
def write_csv_gz(filepath: str, rows: list[dict]) -> None:
    """Write rows as gzipped RFC 4180 CSV."""
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    if not rows:
        return
    fieldnames = list(rows[0].keys())
    buf = StringIO()
    writer = csv.DictWriter(
        buf, fieldnames=fieldnames, quoting=csv.QUOTE_MINIMAL, lineterminator="\n"
    )
    writer.writeheader()
    writer.writerows(rows)
    with gzip.open(filepath, "wt", encoding="utf-8") as f:
        f.write(buf.getvalue())


def write_json(filepath: str, data) -> None:
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False, default=str)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def generate_all(output_dir: str, seed: int = SEED) -> dict:
    rng = random.Random(seed)
    fake = Faker("en_IN")
    Faker.seed(seed)
    fake.seed_instance(seed)

    print(f"Generating synthetic data with seed={seed} to {output_dir}")

    # 1. Employees
    print(f"  Generating {TARGET_EMPLOYEES} employees...")
    employees = generate_employees(rng, fake, TARGET_EMPLOYEES)

    # 2. Customers
    print(f"  Generating {TARGET_CUSTOMERS} customers...")
    customers = generate_customers(rng, fake, TARGET_CUSTOMERS)

    # 3. Patients
    print(f"  Generating {TARGET_PATIENTS} patients...")
    patients = generate_patients(rng, fake, TARGET_PATIENTS)

    # 4. Transactions (depends on customers)
    print(f"  Generating {TARGET_TRANSACTIONS} transactions...")
    transactions = generate_transactions(rng, fake, customers, TARGET_TRANSACTIONS)

    # 5. Users (depends on customers for overlap)
    print(f"  Generating {TARGET_USERS} users...")
    users = generate_users(rng, fake, customers, TARGET_USERS)

    # 6. Consent events
    print("  Generating 1000 consent events...")
    consent_events = generate_consent_events(rng, customers, 1000, DSR_PRINCIPAL_INDEX)

    # Write CSVs
    datestamp = GENERATOR_DATE.strftime("%Y%m%d")
    write_csv_gz(
        os.path.join(output_dir, "employees", f"employees_{datestamp}.csv.gz"),
        employees,
    )
    write_csv_gz(
        os.path.join(output_dir, "customers", f"customers_{datestamp}.csv.gz"),
        customers,
    )
    write_csv_gz(
        os.path.join(output_dir, "patients", f"patients_{datestamp}.csv.gz"),
        patients,
    )
    write_csv_gz(
        os.path.join(output_dir, "transactions", f"transactions_{datestamp}.csv.gz"),
        transactions,
    )
    write_csv_gz(
        os.path.join(output_dir, "users", f"users_{datestamp}.csv.gz"),
        users,
    )

    # Write consent events JSON
    write_json(
        os.path.join(output_dir, "consent_events_seed.json"), consent_events
    )

    # Count DSR principal transactions
    dsr_txn_count = sum(
        1 for t in transactions if t["customer_id"] == DSR_PRINCIPAL_ID
    )

    # Build manifest
    manifest = {
        "seed": seed,
        "generated_at": datetime(
            2026, 4, 17, 10, 0, 0, tzinfo=IST
        ).isoformat(),
        "generator_date": GENERATOR_DATE.isoformat(),
        "dsr_principal_id": DSR_PRINCIPAL_ID,
        "dsr_principal_email": customers[DSR_PRINCIPAL_INDEX]["email_address"],
        "dsr_principal_name": customers[DSR_PRINCIPAL_INDEX]["full_name"],
        "dsr_expected_transaction_count": dsr_txn_count,
        "dsr_expected_consent_event_count": 4,
        "row_counts": {
            "employees": len(employees),
            "customers": len(customers),
            "patients": len(patients),
            "transactions": len(transactions),
            "users": len(users),
            "consent_events": len(consent_events),
        },
    }
    write_json(os.path.join(output_dir, "_manifest.json"), manifest)

    print(f"\nManifest:")
    print(f"  DSR principal: {DSR_PRINCIPAL_ID}")
    print(f"  DSR transactions: {dsr_txn_count}")
    print(f"  Row counts: {manifest['row_counts']}")
    print(f"\nDone. Files written to {output_dir}")

    return manifest


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="DPDP POC synthetic data generator")
    parser.add_argument(
        "--output-dir",
        default="/tmp/dpdp_landing",
        help="Output directory for generated files",
    )
    parser.add_argument("--seed", type=int, default=SEED, help="Random seed")
    args = parser.parse_args()

    generate_all(args.output_dir, args.seed)
