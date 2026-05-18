#!/usr/bin/env python3
"""Compliance Pack POC — synthetic Salesforce-shaped data generator.

Represents what Lakeflow Connect Salesforce ingestion would deliver to
Bronze in production. Self-contained: this script writes nothing — it
returns lists of dicts. The seeder in `scripts/seed_salesforce_data.py`
is the consumer; it pushes the rows into UC tables via SQL.

Three Salesforce standard objects, mirrored 1:1:

  Lead     — prospect, has personal PII (lead's identity)
  Contact  — known person at a customer account, FK → Account
  Account  — company/org, has business identifiers (PAN, GST)

All values are deterministic (seed=43 — separate from the medallion
generator's seed=42 to keep namespaces independent), and obviously fake
but format-matching so the classifier's pattern library catches them
end-to-end.

Usage as library::

    from generate_salesforce_data import generate
    payload = generate(seed=43)        # → {"leads": [...], "contacts": [...], "accounts": [...]}

CLI for inspection::

    python3 generate_salesforce_data.py --counts
    python3 generate_salesforce_data.py --sample lead 5

Counts default to 100 leads / 60 contacts / 30 accounts (per BACKLOG).
"""

from __future__ import annotations

import argparse
import json
import random
import string
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

# ---------------------------------------------------------------------------
# Defaults (matched to BACKLOG P0 / Day 3)
# ---------------------------------------------------------------------------
DEFAULT_SEED = 43
DEFAULT_LEADS = 100
DEFAULT_CONTACTS = 60
DEFAULT_ACCOUNTS = 30
GENERATOR_DATE = date(2026, 4, 27)
IST = timezone(timedelta(hours=5, minutes=30))

# ---------------------------------------------------------------------------
# India-specific reference data (subset — repeats are fine for a synthetic set)
# ---------------------------------------------------------------------------
FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Ishaan", "Reyansh", "Krishna",
    "Sai", "Aryan", "Vihaan", "Aanya", "Diya", "Aadhya", "Ananya", "Pari",
    "Anika", "Navya", "Saanvi", "Riya", "Myra", "Karan", "Kabir", "Rohan",
    "Aditi", "Priya", "Tanvi", "Pooja", "Nisha", "Meera", "Shreya",
    "Ravi", "Suresh", "Anjali", "Kavya", "Nikhil", "Vikram", "Manish",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Reddy", "Nair", "Menon", "Khanna", "Kapoor",
    "Mehta", "Shah", "Patel", "Joshi", "Banerjee", "Bose", "Dutta",
    "Pillai", "Krishnan", "Agarwal", "Gupta", "Rao", "Singh", "Choudhary",
    "Mishra", "Trivedi", "Bhat", "Pai", "Saxena", "Sinha", "Roy",
]
COMPANY_PREFIXES = [
    "Saffron", "Trident", "Bharat", "Nimbus", "Granite", "Lotus", "Banyan",
    "Indigo", "Crimson", "Helix", "Polaris", "Quanta", "Vertex", "Zenith",
    "Beacon", "Catalyst", "Oasis", "Fulcrum", "Mosaic", "Ridgeline",
]
COMPANY_SUFFIXES = [
    "Technologies", "Industries", "Solutions", "Systems", "Logistics",
    "Pharma", "Foods", "Bank", "Capital", "Healthcare", "Retail",
    "Energy", "Telecom", "Infrastructure", "Holdings", "Networks",
]
INDUSTRIES = [
    "Banking", "Insurance", "Healthcare", "Pharma", "Retail", "E-commerce",
    "Manufacturing", "Telecom", "Education", "Logistics", "FinTech",
    "Hospitality", "Real Estate",
]
JOB_TITLES = [
    "Chief Technology Officer", "VP Engineering", "Head of Data",
    "Director of Compliance", "Senior Software Engineer", "Product Manager",
    "Data Engineer", "Chief Privacy Officer", "Legal Counsel",
    "Marketing Manager", "Procurement Manager", "Operations Lead",
]
LEAD_STATUSES = ["new", "working", "qualified", "unqualified", "converted"]
LEAD_SOURCES = ["web", "referral", "event", "outbound", "partner", "linkedin"]
INDIAN_STATES = [
    "Karnataka", "Maharashtra", "Tamil Nadu", "Delhi", "Telangana",
    "Gujarat", "West Bengal", "Kerala", "Punjab", "Haryana", "Rajasthan",
    "Uttar Pradesh", "Madhya Pradesh", "Andhra Pradesh", "Odisha",
]
INDIAN_CITIES = {
    "Karnataka": ["Bangalore", "Mysore", "Mangalore"],
    "Maharashtra": ["Mumbai", "Pune", "Nagpur"],
    "Tamil Nadu": ["Chennai", "Coimbatore", "Madurai"],
    "Delhi": ["New Delhi", "Delhi"],
    "Telangana": ["Hyderabad", "Warangal"],
    "Gujarat": ["Ahmedabad", "Surat", "Vadodara"],
    "West Bengal": ["Kolkata", "Howrah"],
    "Kerala": ["Kochi", "Thiruvananthapuram"],
    "Punjab": ["Chandigarh", "Ludhiana"],
    "Haryana": ["Gurgaon", "Faridabad"],
    "Rajasthan": ["Jaipur", "Udaipur"],
    "Uttar Pradesh": ["Lucknow", "Noida", "Greater Noida"],
    "Madhya Pradesh": ["Indore", "Bhopal"],
    "Andhra Pradesh": ["Visakhapatnam", "Vijayawada"],
    "Odisha": ["Bhubaneswar", "Cuttack"],
}
PINCODES_BY_STATE = {  # one realistic pincode per state, repeated is fine
    "Karnataka": "560001", "Maharashtra": "400001", "Tamil Nadu": "600001",
    "Delhi": "110001", "Telangana": "500001", "Gujarat": "380001",
    "West Bengal": "700001", "Kerala": "682001", "Punjab": "160001",
    "Haryana": "122001", "Rajasthan": "302001", "Uttar Pradesh": "226001",
    "Madhya Pradesh": "452001", "Andhra Pradesh": "530001", "Odisha": "751001",
}
IFSC_BANK_PREFIXES = ["SBIN", "HDFC", "ICIC", "AXIS", "KKBK", "YESB", "PUNB"]


# ---------------------------------------------------------------------------
# India-PII format helpers (formats only — values are random/fake)
# ---------------------------------------------------------------------------

def _aadhaar(rng: random.Random) -> str:
    """12 digits, spaced as 'XXXX XXXX XXXX' — matches the pack pattern."""
    digits = "".join(str(rng.randint(0, 9)) for _ in range(12))
    return f"{digits[0:4]} {digits[4:8]} {digits[8:12]}"


def _pan_individual(rng: random.Random) -> str:
    """PAN: 5 letters + 4 digits + 1 letter; 4th letter = 'P' for individuals."""
    letters = string.ascii_uppercase
    return (
        "".join(rng.choices(letters, k=3))
        + "P"
        + rng.choice(letters)
        + "".join(str(rng.randint(0, 9)) for _ in range(4))
        + rng.choice(letters)
    )


def _pan_company(rng: random.Random) -> str:
    """PAN: same shape; 4th letter = 'C' for companies."""
    letters = string.ascii_uppercase
    return (
        "".join(rng.choices(letters, k=3))
        + "C"
        + rng.choice(letters)
        + "".join(str(rng.randint(0, 9)) for _ in range(4))
        + rng.choice(letters)
    )


def _gst_number(rng: random.Random, state_code: str = "29") -> str:
    """GSTIN: 2-digit state + 10-char PAN + 1 digit + 'Z' + 1 alphanumeric."""
    pan = _pan_company(rng)
    return f"{state_code}{pan}{rng.randint(1, 9)}Z{rng.choice(string.ascii_uppercase + string.digits)}"


def _phone_india(rng: random.Random) -> str:
    """+91 XXXXX XXXXX, mobile prefix 6-9."""
    head = rng.choice("6789")
    rest = "".join(str(rng.randint(0, 9)) for _ in range(9))
    return f"+91 {head}{rest[:4]} {rest[4:]}"


def _ifsc(rng: random.Random) -> str:
    bank = rng.choice(IFSC_BANK_PREFIXES)
    return f"{bank}0{rng.randint(100000, 999999):06d}"


def _email(rng: random.Random, first: str, last: str, company_slug: str) -> str:
    suffix = rng.randint(0, 99)
    return f"{first.lower()}.{last.lower()}{suffix:02d}@{company_slug}.example.com"


def _company_name(rng: random.Random) -> str:
    return f"{rng.choice(COMPANY_PREFIXES)} {rng.choice(COMPANY_SUFFIXES)}"


def _company_slug(name: str) -> str:
    return name.lower().split()[0]


def _state_city(rng: random.Random) -> tuple[str, str, str]:
    state = rng.choice(INDIAN_STATES)
    return state, rng.choice(INDIAN_CITIES[state]), PINCODES_BY_STATE[state]


def _date_in_window(rng: random.Random, start: date, end: date) -> date:
    delta = (end - start).days
    return start + timedelta(days=rng.randint(0, delta))


# ---------------------------------------------------------------------------
# Generators
# ---------------------------------------------------------------------------

def _generate_accounts(rng: random.Random, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        name = _company_name(rng)
        state, city, pincode = _state_city(rng)
        out.append({
            "account_id":         f"001{i:08d}",
            "name":                name,
            "industry":            rng.choice(INDUSTRIES),
            "annual_revenue":      float(rng.randint(50, 50_000)) * 10_00_000,  # ₹ in lakhs of rupees
            "num_employees":       rng.randint(20, 25_000),
            "billing_city":        city,
            "billing_state":       state,
            "billing_country":     "India",
            "billing_postal_code": pincode,
            "company_pan":         _pan_company(rng),
            "gst_number":          _gst_number(rng),
            "primary_phone":       _phone_india(rng),
            "website":             f"https://www.{_company_slug(name)}.example.com",
            "created_date":        _date_in_window(rng, date(2023, 1, 1), GENERATOR_DATE).isoformat(),
        })
    return out


def _generate_contacts(rng: random.Random, n: int, accounts: list[dict]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        first = rng.choice(FIRST_NAMES)
        last  = rng.choice(LAST_NAMES)
        acct  = rng.choice(accounts)
        slug  = _company_slug(acct["name"])
        out.append({
            "contact_id":         f"003{i:08d}",
            "account_id":          acct["account_id"],
            "first_name":          first,
            "last_name":           last,
            "email":               _email(rng, first, last, slug),
            "phone":               _phone_india(rng),
            "mobile":              _phone_india(rng),
            "title":               rng.choice(JOB_TITLES),
            "mailing_city":        acct["billing_city"],
            "mailing_state":       acct["billing_state"],
            "mailing_country":     "India",
            "mailing_postal_code": acct["billing_postal_code"],
            "aadhaar":             _aadhaar(rng),
            "pan":                 _pan_individual(rng),
            "date_of_birth":       _date_in_window(rng, date(1965, 1, 1), date(2002, 12, 31)).isoformat(),
            "ifsc":                _ifsc(rng),
            "created_date":        _date_in_window(rng, date(2024, 1, 1), GENERATOR_DATE).isoformat(),
        })
    return out


def _generate_leads(rng: random.Random, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        first = rng.choice(FIRST_NAMES)
        last  = rng.choice(LAST_NAMES)
        company = _company_name(rng)
        slug = _company_slug(company)
        state, city, pincode = _state_city(rng)
        out.append({
            "lead_id":         f"00Q{i:08d}",
            "first_name":      first,
            "last_name":       last,
            "email":           _email(rng, first, last, slug),
            "phone":           _phone_india(rng),
            "mobile":          _phone_india(rng),
            "company":         company,
            "industry":        rng.choice(INDUSTRIES),
            "title":           rng.choice(JOB_TITLES),
            "lead_status":     rng.choice(LEAD_STATUSES),
            "lead_source":     rng.choice(LEAD_SOURCES),
            "lead_score":      rng.randint(1, 100),
            "annual_revenue":  float(rng.randint(50, 50_000)) * 10_00_000,
            "num_employees":   rng.randint(10, 10_000),
            "city":            city,
            "state":           state,
            "country":         "India",
            "postal_code":     pincode,
            "aadhaar":         _aadhaar(rng),
            "pan":             _pan_individual(rng),
            "created_date":    _date_in_window(rng, date(2025, 1, 1), GENERATOR_DATE).isoformat(),
        })
    return out


def generate(
    seed: int = DEFAULT_SEED,
    leads: int = DEFAULT_LEADS,
    contacts: int = DEFAULT_CONTACTS,
    accounts: int = DEFAULT_ACCOUNTS,
) -> dict[str, list[dict[str, Any]]]:
    """Return the three SF object lists in dependency order."""
    rng = random.Random(seed)
    acct_rows = _generate_accounts(rng, accounts)
    contact_rows = _generate_contacts(rng, contacts, acct_rows)
    lead_rows = _generate_leads(rng, leads)
    return {"accounts": acct_rows, "contacts": contact_rows, "leads": lead_rows}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--counts", action="store_true", help="just print row counts")
    p.add_argument("--sample", nargs=2, metavar=("OBJECT", "N"),
                   help="dump first N rows of OBJECT (lead|contact|account) as JSON")
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args()

    payload = generate(seed=args.seed)

    if args.sample:
        obj, n = args.sample
        key = {"lead": "leads", "contact": "contacts", "account": "accounts"}.get(obj, obj)
        if key not in payload:
            raise SystemExit(f"unknown object: {obj}")
        print(json.dumps(payload[key][: int(n)], indent=2, default=str))
        return 0

    print(f"Generated (seed={args.seed}):")
    for k, rows in payload.items():
        print(f"  {k:10s} {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
