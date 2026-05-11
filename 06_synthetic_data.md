# §6 · Synthetic data generator

> ⚠️ **Pre-build planning document.** The generator design is accurate (`generate_synthetic_data.py` at the repo root implements it). References to `ingest_synthetic_data` / `generate_consent_events` as separate jobs are stale — on free-trial both are produced inline by `generate_synthetic_data.py` + `pipelines/phase1_bootstrap.py`.

## 6.1 · Why synthetic data

Real customer data requires data-use agreements that take weeks to negotiate. Synthetic data bypasses that entirely while preserving every technical proof point. The POC succeeds or fails on whether the platform works against realistic Indian personal data; it does not depend on whether the data is real.

The generator is deterministic (seeded) so that every run produces identical output. This matters because the Day 14 demo script references specific principals by ID (e.g., `customer_04217`), and those IDs must resolve the same way every time.

## 6.2 · Source of the generator

The generator builds on the schemas from the sinki.ai accelerator's `Demo_PII_Sample.py` (employees, customers, patients, transactions, users) expanded from 5 rows to realistic scale via Faker with the `en_IN` locale. The accelerator's hand-coded rows serve as a sanity reference — the generator's first few rows should look plausibly similar (Indian names, valid-format Aadhaar/PAN, realistic cities).

## 6.3 · Generator seed and determinism

```python
SEED = 42
import random
random.seed(SEED)
from faker import Faker
fake = Faker('en_IN')
Faker.seed(SEED)
```

Every downstream random choice must derive from this seed. If you call `random.random()` anywhere, you must do it after seeding. If a run produces different output than a prior run, the generator is non-deterministic and is a bug.

## 6.4 · Global distribution targets

Across all 5 tables, the 21,500 total rows reference a principal population of approximately **15,000 distinct principals** (many appear in multiple tables; employees may have user accounts, customers have transactions, etc.).

### Principal composition
- 2,000 employees (maps 1:1 to `employees`)
- 5,000 customers (maps 1:1 to `customers`)
- 1,500 patients (maps 1:1 to `patients`; roughly 40% overlap with customers)
- 3,000 users (overlaps ~50% with customers, ~30% with employees)
- Remainder: partners, prospects, leads not realized as customers

### Age distribution (critical for children-marker path)
- 12% under 25
- 45% 25-45
- 35% 45-65
- 8% over 65
- **3% of the under-25 population are under 18** (i.e., ~50 minors across the whole dataset)

### Geographic distribution (weighted to urban India)
- Karnataka: 18% (Bangalore-heavy)
- Maharashtra: 16% (Mumbai, Pune)
- Delhi NCR: 14%
- Tamil Nadu: 12% (Chennai)
- Telangana: 9% (Hyderabad)
- West Bengal: 7% (Kolkata)
- Gujarat: 6%
- Kerala: 4%
- Other states: 14% (distributed)

### Language preference distribution (for future multi-language notice testing)
- English: 45%
- Hindi: 18%
- Kannada: 8%
- Tamil: 7%
- Telugu: 6%
- Marathi: 5%
- Bengali: 4%
- Others: 7%

## 6.5 · Per-table generator rules

### 6.5.1 · `employees` (2,000 rows)

- `employee_id`: `EMP000001` through `EMP002000`, zero-padded
- `first_name`, `last_name`: `fake.first_name()`, `fake.last_name()` with en_IN locale
- `email`: `{first_name.lower()}.{last_name.lower()}@company.com`
- `phone_number`: `+91-{random 10-digit starting with 6-9}`
- `date_of_birth`: sampled from the age distribution above, skewed toward 25-55 for working-age population (minors excluded from employees)
- `aadhaar_number`: valid-format 12 digits starting 2-9, with Verhoeff checksum (use `python-stdnum` package if available, else generate with simple validity check). **95% populated**, 5% null for non-Indian staff.
- `pan_number`: valid-format `[A-Z]{5}\d{4}[A-Z]`. 95% populated.
- `passport_number`: valid-format Indian passport `[A-PR-WY][1-9]\d{6}`. 60% populated (not everyone has one).
- `address`, `city`, `state`, `country`: geographic distribution above; country always 'India' for those with Aadhaar
- `postal_code`: 6-digit Indian PIN code matching the city
- `salary`: log-normal distribution, median ₹800,000, tail to ₹5,000,000
- `bank_account`: 9-18 digit numeric string (accelerator's pattern)
- `ifsc_code`: `[A-Z]{4}0[A-Z0-9]{6}` from the set of real Indian bank IFSCs (SBIN, HDFC, ICIC, AXIS, KKBK, YESB)
- `department`: weighted choice from ['Engineering', 'Sales', 'Marketing', 'Finance', 'HR', 'Operations', 'Legal', 'Customer Service']
- `designation`: mapped to department
- `hire_date`: uniform between 5 years ago and today
- `manager_employee_id`: self-reference to another employee_id with hire_date earlier than own; null for 2% (C-suite)

### 6.5.2 · `customers` (5,000 rows)

- `customer_id`: `CUST00001` through `CUST05000`
- `full_name`: `fake.name()` en_IN
- `email_address`: `fake.email()` but swap to a variety of domains (gmail, yahoo, outlook, hotmail with 60/15/12/8% weighting, others 5%)
- `mobile`: 10-digit starting 6-9 (no country prefix, to test the phone_india pattern which accepts both)
- `date_of_birth`: full age distribution including the 3% under-18 subset
- `credit_card_number`: valid-Luhn 16-digit starting with 4 (Visa), 5 (MC), 6 (Discover) distributed 70/25/5
- `card_expiry`: `MM/YY` between 1 and 5 years in the future
- `cvv`: 3-digit random
- `billing_address`, `shipping_address`: usually same (80%); different for 20%
- `city`, `state`, `postal_code`: geographic distribution
- `ip_address`: realistic Indian ISP ranges (103.x.x.x, 49.x.x.x, 122.x.x.x, 157.x.x.x, 182.x.x.x weighted; 10% international)
- `device_id`: `DEV-{6 random uppercase alphanumeric}`
- `loyalty_tier`: bronze 60%, silver 25%, gold 12%, platinum 3%
- `loyalty_points`: log-normal tied to tier
- `registration_date`: uniform past 3 years
- `last_activity_date`: between registration_date and today, clustered toward recent

### 6.5.3 · `patients` (1,500 rows)

- `patient_id`: `PAT00001` through `PAT01500`
- `patient_name`: en_IN
- `dob`: full distribution; older skew (health care users are older on average)
- `gender`: Male 48%, Female 50%, Other 2%
- `blood_group`: realistic Indian distribution (O+ 37%, B+ 33%, A+ 21%, AB+ 8%, negatives rare)
- `contact_phone`, `emergency_contact`: two distinct 10-digit Indian mobiles
- `email`: 75% populated
- `address`, `city`, `state`, `postal_code`: geographic distribution
- `insurance_id`: `INS-{year}-{5 digits}` for ~85% of patients
- `insurance_provider`: weighted choice from ['Star Health', 'HDFC Ergo', 'ICICI Lombard', 'Max Bupa', 'Bajaj Allianz', 'Care Health', 'New India Assurance']
- `policy_type`: individual 60%, family 30%, corporate 10%
- `medical_record_number`: `MRN-{5 digits}`
- `primary_diagnosis`: free text — sampled from a list of 30 common conditions (Type 2 Diabetes, Hypertension, Asthma, etc.); this is the column that `ai_classify` runs against
- `prescription`: free text — sampled from a list of common medications
- `allergies`: free text — 60% "None", others from allergens list
- `last_visit`: past 3 years, skewed to past 6 months
- `next_appointment`: null for 40%, future date for 60%

### 6.5.4 · `transactions` (10,000 rows)

- `transaction_id`: `TXN00000001` through `TXN00010000`
- `customer_id`: FK to `customers` — each customer has between 1 and 50 transactions (Zipf distribution; most have few, some have many)
- `account_number`: 16-digit numeric tied to customer (stable per customer)
- `account_holder_name`: matches the customer's `full_name`
- `transaction_date`: past 18 months, with a retrospective-campaign spike at ~Day 30-45 before scan date
- `amount`: log-normal, median ₹2,500, with occasional high-value outliers up to ₹500,000
- `currency`: 97% INR, 2% USD, 1% other
- `transaction_type`: weighted (PURCHASE 65%, TRANSFER 20%, WITHDRAWAL 10%, REFUND 3%, DEPOSIT 2%)
- `merchant_name`: from list ['Amazon India', 'Flipkart', 'Myntra', 'Swiggy', 'Zomato', 'Uber', 'Ola', 'BookMyShow', 'Reliance Digital', 'IRCTC', ...]
- `merchant_category_code`: standard MCC codes tied to merchant
- `card_last_four`: the last 4 of the linked customer's credit card
- `ip_address`: realistic Indian ISP ranges
- `location_city`, `location_country`: matches customer's city 80% of the time; otherwise random Indian city
- `status`: SUCCESS 94%, FAILED 4%, PENDING 1%, REVERSED 1%

### 6.5.5 · `users` (3,000 rows)

- `user_id`: `USR00001` through `USR03000`
- `username`: `{first_name}_{dept_or_noun}` style
- `password_hash`: bcrypt hash of a random 20-char string (never the actual password)
- `email`, `phone`, `first_name`, `last_name`: consistent with the principal's identity in other tables for the ~50% overlap subset
- `date_of_birth`: full distribution
- `gender`: Male 48%, Female 50%, Other 2%
- `profile_picture_url`: realistic CDN URLs (not real images)
- `last_login_ip`: Indian ISP ranges
- `device_fingerprint`: `fp_{12 random lowercase alphanumeric}`
- `mfa_enabled`: true for 40%
- `mfa_method`: sms/totp/email weighted; null if mfa_enabled=false
- `created_at`: past 2 years
- `last_login`: past 30 days for active 85%; null for 15% (dormant)
- `account_status`: active 85%, suspended 5%, deleted 3%, pending 7%

## 6.6 · The 1,000 synthetic consent events

On Day 9, generate 1,000 consent events against the principal population. Distribution:

### Principal selection
- Choose 300 distinct principals from the ~15,000 population
- Weighted: 60% customers, 25% users, 10% employees, 5% patients
- Some principals get multiple consent events across different purposes

### Purpose distribution per event
Each event is a single (principal, purpose) grant or decline. Events distribute:
- `marketing_email`: 350 events (35% of total)
- `marketing_sms`: 250 events
- `analytics`: 180 events
- `third_party_sharing`: 100 events
- `product_personalization`: 90 events
- `core_service`: 30 events (fewest, because core service typically doesn't need explicit consent event)

### Channel distribution
- `web`: 40%
- `mobile_app`: 35%
- `call_center`: 15%
- `partner_api`: 10%

### Grant vs decline ratios (realistic rates, not uniform)
- `marketing_email`: 72% granted, 28% declined
- `marketing_sms`: 58% granted, 42% declined
- `analytics`: 82% granted, 18% declined
- `third_party_sharing`: 31% granted, 69% declined
- `product_personalization`: 64% granted, 36% declined
- `core_service`: 100% granted (implicit in service use)

### Temporal distribution
- Events span 90 days ending on the day before the POC demo
- 5x volume spike between Day -45 and Day -30 (simulating a retrospective consent campaign wave)
- Even distribution otherwise

### Event types
- 92% `granted` or `declined` (as above)
- 5% `withdrawn` (withdrawals of prior grants — pick principals who previously granted, write a later withdrawal event)
- 3% `modified` (purpose scope narrowing, e.g., withdrew marketing_sms while keeping marketing_email)

## 6.7 · The synthetic DSR principal

One specific principal is reserved for the Day 11-12 DSR end-to-end test. This is not randomly selected; it is deterministically the 4,217th customer generated:

**Principal**: `customer_04217`

### Expected data footprint (verified by test INT-03)

- **1 row in `customers`**: the principal's master record
- **1 row in `users`**: the principal has a user account (they are in the 50% overlap subset)
- **Transactions in `transactions`**: deterministically between 12 and 20 rows (Zipf distribution will land somewhere in this range with seed=42; exact count verified by test)
- **Multiple rows in `consent_events`**: the principal is seeded to have 4 consent events across 3 purposes with one prior withdrawal. Specifically:
  - `marketing_email` granted on Day -60, withdrawn on Day -5
  - `analytics` granted on Day -60, still active
  - `third_party_sharing` declined on Day -60
- **0 rows in `employees`**: customer_04217 is not an employee
- **0 rows in `patients`**: customer_04217 is not a patient

### Expected DSR bundle contents

The response bundle produced by the Day 11-12 DSR test must contain:
- **Data export** (`data_export.json`): all rows from `customers`, `users`, `transactions`, and `consent_events` that match this principal
- **Erasure certificate** (`erasure_certificate.pdf`): lists 3 tables marked for immediate erasure (the customer, user, and consent_events rows) plus the scheduled residual for the transactions rows (retained 7 years per banking regulation)
- **Retention schedule** (`retention_schedule.pdf`): shows transactions retention boundary ~2032
- **Audit trail** (`audit_trail.json`): timestamped action sequence

The test in `tests/integration_tests.md` verifies that the actual bundle matches this expectation to the row.

## 6.8 · Generator implementation sketch

The generator lives in a single Python module `generate_synthetic_data.py` with entry point:

```python
def generate_all(output_dir: str, seed: int = 42):
    """Generate all 5 tables plus consent events, write CSVs to output_dir."""
    random.seed(seed)
    Faker.seed(seed)

    # Order matters - principals generated first, then tables that reference them
    principals = generate_principal_population()
    employees = generate_employees(principals)
    customers = generate_customers(principals)
    patients = generate_patients(principals)
    users = generate_users(principals)
    transactions = generate_transactions(customers)
    consent_events = generate_consent_events(principals)

    # Write each as CSV matching §3.2 format exactly
    for name, data in [
        ("employees", employees),
        ("customers", customers),
        ("patients", patients),
        ("transactions", transactions),
        ("users", users),
    ]:
        path = f"{output_dir}/{name}/{name}_{today}.csv.gz"
        write_csv(path, data)

    write_consent_events(f"{output_dir}/consent_events.json", consent_events)

    # Verification manifest
    write_manifest(f"{output_dir}/_manifest.json", {
        "seed": seed,
        "generated_at": datetime.utcnow().isoformat(),
        "row_counts": {...},
        "dsr_principal_id": "customer_04217",
        "dsr_expected_asset_count": <computed>
    })
```

The manifest file is critical. It records the generator's expected outputs for this seed, which lets tests verify "did we get what we expected." If the manifest disagrees with actual output, the generator is non-deterministic and must be fixed.

## 6.9 · Running the generator

On Day 1:
```bash
python generate_synthetic_data.py \
    --output-dir /Volumes/compliance_pack/bronze/landing/ \
    --seed 42
```

Expected output:
- 5 gzipped CSVs written (one per table)
- 1 JSON file with 1,000 consent events
- 1 `_manifest.json` with the expected counts

The Day 1 integration test validates that the output matches the manifest, before any downstream work begins.

Now proceed to `07_dsr_execution.md`.
