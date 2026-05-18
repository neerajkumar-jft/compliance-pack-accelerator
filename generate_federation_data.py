#!/usr/bin/env python3
"""Compliance Pack POC — synthetic Postgres-marketing-DB-shaped data generator.

Represents what Lakehouse Federation would expose if a marketing
analytics Postgres were registered as a foreign catalog. Self-contained:
this script writes nothing — it returns lists of dicts. The seeder in
``scripts/seed_federation_data.py`` is the consumer; it creates a
local ``federation_mock`` schema and silver views on top, mirroring
the foreign-catalog + view-projection pattern Federation produces.

Two tables, mirroring a typical CRM-marketing attribution database:

  lead_scoring        — per-lead enrichment with engagement signals
  campaign_response   — touchpoint events tied to campaigns

Both reference SF lead_ids (00Q00000000 … 00Q00000099) so that a
join across federation_mock and silver.sf_leads_tagged works in demos
— this is the "query without copy" headline.

Seed: 44 (independent of generate_synthetic_data's 42 and
generate_salesforce_data's 43).

Usage as library::

    from generate_federation_data import generate
    payload = generate(seed=44)        # → {"lead_scoring": [...], "campaign_response": [...]}

CLI for inspection::

    python3 generate_federation_data.py --counts
    python3 generate_federation_data.py --sample lead_scoring 3
"""

from __future__ import annotations

import argparse
import json
import random
from datetime import date, datetime, timedelta, timezone
from typing import Any

DEFAULT_SEED = 44
DEFAULT_LEAD_SCORING = 200
DEFAULT_CAMPAIGN_RESPONSE = 100
GENERATOR_DATE = date(2026, 4, 27)
IST = timezone(timedelta(hours=5, minutes=30))

# Reuse SF-side names so generated leads collide with sf_leads.lead_id
FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Arjun", "Ishaan", "Reyansh", "Krishna",
    "Sai", "Aryan", "Vihaan", "Aanya", "Diya", "Aadhya", "Ananya", "Pari",
    "Anika", "Navya", "Saanvi", "Riya", "Myra", "Karan", "Kabir", "Rohan",
    "Aditi", "Priya", "Tanvi", "Pooja", "Nisha", "Meera", "Shreya",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Reddy", "Nair", "Menon", "Khanna", "Kapoor",
    "Mehta", "Shah", "Patel", "Joshi", "Banerjee", "Bose", "Dutta",
]
COMPANIES = [
    "Saffron Technologies", "Trident Industries", "Bharat Solutions",
    "Nimbus Systems", "Granite Logistics", "Lotus Pharma", "Banyan Foods",
    "Indigo Bank", "Crimson Capital", "Helix Healthcare",
    "Polaris Telecom", "Quanta Networks", "Vertex Energy", "Zenith Holdings",
]
SCORE_BANDS = ["cold", "warm", "hot", "qualified"]
CAMPAIGNS = [
    ("CMP-2026-Q1-WEBINAR",   "Compliance Pack Webinar Q1",      "email"),
    ("CMP-2026-Q1-WHITEPAPER","Privacy Architecture Whitepaper", "email"),
    ("CMP-2026-Q2-EVENT",     "India Data Privacy Summit",        "paid_event"),
    ("CMP-2026-Q1-LINKEDIN",  "LinkedIn Sponsored — Compliance", "social"),
    ("CMP-2026-Q1-SMS",       "Reactivation SMS Drip",            "sms"),
    ("CMP-2026-Q1-RETARGET",  "Retargeting — Lead Qualification", "paid_search"),
]
RESPONSE_TYPES = ["clicked", "downloaded", "replied", "registered", "unsubscribed"]


def _phone_india(rng: random.Random) -> str:
    head = rng.choice("6789")
    rest = "".join(str(rng.randint(0, 9)) for _ in range(9))
    return f"+91 {head}{rest[:4]} {rest[4:]}"


def _email(first: str, last: str, company: str, rng: random.Random) -> str:
    suffix = rng.randint(0, 99)
    slug = company.lower().split()[0]
    return f"{first.lower()}.{last.lower()}{suffix:02d}@{slug}.example.com"


def _lead_id(idx: int) -> str:
    """Match the SF generator's lead_id format so federation joins work."""
    return f"00Q{idx:08d}"


def _datetime_in_window(rng: random.Random, start: date, end: date) -> datetime:
    delta = (end - start).days
    day = start + timedelta(days=rng.randint(0, delta))
    return datetime(day.year, day.month, day.day,
                    rng.randint(0, 23), rng.randint(0, 59), rng.randint(0, 59),
                    tzinfo=IST)


def _generate_lead_scoring(rng: random.Random, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        # Cycle lead_id 0..99 so the 200 rows cover every SF lead twice with
        # different score events.
        lead_idx = i % 100
        first   = rng.choice(FIRST_NAMES)
        last    = rng.choice(LAST_NAMES)
        company = rng.choice(COMPANIES)
        score   = rng.randint(1, 100)
        if score >= 80:
            band = "hot"
        elif score >= 60:
            band = "warm"
        elif score >= 40:
            band = "qualified"
        else:
            band = "cold"
        out.append({
            "score_id":           f"LS{i:08d}",
            "lead_id":            _lead_id(lead_idx),
            "email":              _email(first, last, company, rng),
            "first_name":         first,
            "last_name":          last,
            "phone":              _phone_india(rng),
            "company":            company,
            "score":              score,
            "score_band":         band,
            "engagement_count":   rng.randint(0, 25),
            "last_activity_date": (GENERATOR_DATE - timedelta(days=rng.randint(0, 90))).isoformat(),
            "created_at":         _datetime_in_window(rng, date(2026, 1, 1), GENERATOR_DATE).isoformat(),
        })
    return out


def _generate_campaign_response(rng: random.Random, n: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for i in range(n):
        lead_idx = rng.randint(0, 99)
        campaign = rng.choice(CAMPAIGNS)
        first    = rng.choice(FIRST_NAMES)
        last     = rng.choice(LAST_NAMES)
        company  = rng.choice(COMPANIES)
        out.append({
            "response_id":         f"CR{i:08d}",
            "lead_id":             _lead_id(lead_idx),
            "campaign_id":         campaign[0],
            "campaign_name":       campaign[1],
            "channel":             campaign[2],
            "email":               _email(first, last, company, rng),
            "response_type":       rng.choice(RESPONSE_TYPES),
            "response_timestamp":  _datetime_in_window(rng, date(2026, 1, 1), GENERATOR_DATE).isoformat(),
        })
    return out


def generate(
    seed: int = DEFAULT_SEED,
    lead_scoring: int = DEFAULT_LEAD_SCORING,
    campaign_response: int = DEFAULT_CAMPAIGN_RESPONSE,
) -> dict[str, list[dict[str, Any]]]:
    """Return the two foreign-table-shaped lists."""
    rng = random.Random(seed)
    return {
        "lead_scoring":      _generate_lead_scoring(rng, lead_scoring),
        "campaign_response": _generate_campaign_response(rng, campaign_response),
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--counts", action="store_true")
    p.add_argument("--sample", nargs=2, metavar=("OBJECT", "N"))
    p.add_argument("--seed", type=int, default=DEFAULT_SEED)
    args = p.parse_args()

    payload = generate(seed=args.seed)

    if args.sample:
        obj, n = args.sample
        if obj not in payload:
            raise SystemExit(f"unknown object: {obj} (try {list(payload)})")
        print(json.dumps(payload[obj][: int(n)], indent=2, default=str))
        return 0

    print(f"Generated (seed={args.seed}):")
    for k, rows in payload.items():
        print(f"  {k:20s} {len(rows)} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
