# Synthetic data generator · implementation notes

Detailed implementation notes extending §6 of the main spec. Read §6 first.

## Expected file output layout

After running `python generate_synthetic_data.py --output-dir /Volumes/compliance_pack/bronze/landing/ --seed 42`:

```
/Volumes/compliance_pack/bronze/landing/
├── employees/
│   └── employees_20260417.csv.gz       (~2000 rows + header)
├── customers/
│   └── customers_20260417.csv.gz       (~5000 rows + header)
├── patients/
│   └── patients_20260417.csv.gz        (~1500 rows + header)
├── transactions/
│   └── transactions_20260417.csv.gz    (~10000 rows + header)
├── users/
│   └── users_20260417.csv.gz           (~3000 rows + header)
├── _manifest.json                       (expected counts, DSR principal spec)
└── consent_events_seed.json             (1000 pre-generated events for Day 9)
```

## Implementation structure

Single-file module at the build site (not in the spec repo):

```
generate_synthetic_data.py
├── generate_all(output_dir, seed)      # entry point
├── generate_principal_population(seed) # creates the base 15k principals
├── generate_employees(principals)
├── generate_customers(principals)
├── generate_patients(principals)
├── generate_transactions(customers)
├── generate_users(principals)
├── generate_consent_events(principals)
├── write_csv(path, rows)               # gzipped RFC 4180 output per §3.2
└── write_manifest(path, counts)
```

## Critical: the DSR principal deterministic footprint

The test in §8.3 INT-03 asserts that `customer_04217` has:
- 1 row in customers
- 1 row in users (the principal is in the 50% overlap subset)
- Between 12 and 20 transactions (exact value determined by Zipf distribution with seed 42)
- 4 consent events across 3 purposes

The manifest file is the source of truth for the exact counts. The generator MUST write the manifest AFTER generating data, reading back the actual row counts:

```python
manifest = {
    "seed": 42,
    "generated_at": datetime.now(tz=timezone.utc).isoformat(),
    "dsr_principal_id": "customer_04217",
    "dsr_expected_transaction_count": <actual count from the transactions generator>,
    "dsr_expected_consent_event_count": 4,
    "row_counts": {
        "employees": <actual>,
        "customers": <actual>,
        "patients": <actual>,
        "transactions": <actual>,
        "users": <actual>,
        "consent_events": <actual>,
    }
}
```

If the actual counts differ from target counts in §6, that's fine — the manifest records what was actually generated. Tests assert against the manifest, not against hardcoded numbers.

## Dependency check

```python
import random
from datetime import datetime, timezone, timedelta
from faker import Faker        # pip install faker==33.3.1
# Optional: from stdnum.in_.aadhaar import format as format_aadhaar  # for Verhoeff check
```

Do not add other dependencies. If a generator decision needs a library (e.g., proper Verhoeff for Aadhaar), flag it with the human collaborator first — simpler alternatives usually work for POC synthetic data.

## Aadhaar generation — non-obvious detail

Real Aadhaar uses Verhoeff checksum. The accelerator's `pii_detector` regex accepts any 12-digit starting 2-9; it does NOT validate checksum. For the POC:

**Default**: generate 12 digits matching the regex pattern, do NOT compute Verhoeff.

**Rationale**: the classifier's regex doesn't check Verhoeff, so Verhoeff-invalid Aadhaar still gets classified correctly. Adding Verhoeff costs a library dependency and prevents easy casual inspection of the generated data.

**If needed later**: `python-stdnum` package has Verhoeff; add as explicit Phase 1 dependency.

## PAN generation

PAN format: AAAPA1234A where position 4 encodes holder type ('P' for individual). For POC, use 'P' at position 4 consistently; other letters would need more context than synthetic data justifies.

```python
def fake_pan():
    return (
        ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ', k=3))
        + random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')  # any letter
        + 'P'  # individual
        + ''.join(random.choices('0123456789', k=4))
        + random.choice('ABCDEFGHIJKLMNOPQRSTUVWXYZ')
    )
```

## IFSC generation

Use real Indian bank prefixes to ensure the IFSC regex matches. Pick from:

```python
IFSC_BANK_PREFIXES = ['SBIN', 'HDFC', 'ICIC', 'AXIS', 'KKBK', 'YESB', 'PUNB', 'UTIB', 'IOBA', 'UNIN']

def fake_ifsc():
    prefix = random.choice(IFSC_BANK_PREFIXES)
    branch = ''.join(random.choices('ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=6))
    return f"{prefix}0{branch}"
```

## Phone number generation

Indian mobile only (no landline for this POC). Use format `+91-9XXXXXXXXX` with leading digit 6-9.

```python
def fake_indian_mobile(with_prefix: bool = True) -> str:
    leading = random.choice('6789')
    rest = ''.join(random.choices('0123456789', k=9))
    if with_prefix:
        return f"+91-{leading}{rest}"
    return f"{leading}{rest}"
```

Mix both formats across tables so the classifier sees both patterns.

## Credit card generation

Must pass the Luhn checksum for the credit_card regex to match. Use `faker.credit_card_number()` which generates Luhn-valid numbers by default.

```python
fake.credit_card_number(card_type='visa')
fake.credit_card_number(card_type='mastercard')
fake.credit_card_number(card_type='amex')
```

## IP address generation — Indian ISP bias

Real Indian IP ranges commonly seen: `103.x.x.x`, `49.x.x.x`, `122.x.x.x`, `157.x.x.x`, `182.x.x.x`. Generating only these ranges makes the IP address classification more believable.

```python
INDIAN_IP_PREFIXES = [
    "103", "49", "122", "157", "182", "117", "106", "14", "123"
]

def fake_indian_ip() -> str:
    p = random.choice(INDIAN_IP_PREFIXES)
    return f"{p}.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(0, 255)}"

def fake_ip():
    if random.random() < 0.9:
        return fake_indian_ip()
    return fake.ipv4()  # Fallback international
```

## Consent event generation

The 1000 events are generated to a JSON file (not CSV) because:
- They are consumed by the consent event ingester on Day 9, not by Auto Loader
- JSON preserves nested structure (e.g., the purposes array in the notice)
- The ingester converts JSON to Lakebase inserts

Format:
```json
[
  {
    "data_principal_external_id": "customer_00042",
    "event_timestamp": "2026-03-15T14:23:10+05:30",
    "event_type": "granted",
    "notice_version": {"notice_id": "marketing_notice", "version": 1, "language": "en-IN"},
    "channel": "web",
    "purpose": "marketing_email",
    "purpose_grant_status": "granted",
    "ip_address": "103.45.67.89",
    "user_agent": "Mozilla/5.0 ...",
    "consent_capture_method": "checkbox",
    "retention_clock_start": "2026-03-15T14:23:10+05:30",
    "retention_duration_days": 365
  },
  ...
]
```

The ingester on Day 9 maps each entry: resolves `data_principal_external_id` → `data_principals.principal_id` (creating the principal if it doesn't exist), resolves `notice_version` to `notice_version_id`, inserts into `public.consent_events`.

## Generator acceptance test

Run once before any downstream work:

```bash
python generate_synthetic_data.py --output-dir /tmp/test_gen --seed 42

# The generator should succeed and write the expected files
ls /tmp/test_gen/  # should show all 5 table dirs + manifest + consent_events_seed.json

# Run the generator twice and diff
python generate_synthetic_data.py --output-dir /tmp/test_gen_2 --seed 42
diff -r /tmp/test_gen /tmp/test_gen_2  # should be identical
```

If the diff shows any output differs between runs, the generator is non-deterministic and is a bug. Common causes: using `datetime.now()` somewhere in the generator body (use a fixed generator_date parameter instead), iterating over a dict without sorted keys, using set ordering.
