# Erasure certificate PDF layout

> ⚠️ **Pre-build planning document — never implemented.** The free-trial POC uses `scripts/dsr_erasure.py` which emits a JSON audit bundle, not a PDF certificate. This file remains as a Phase 1 design reference. **For the current DSR flow, see [`docs/persona_deploy.md`](../docs/persona_deploy.md).**

The erasure certificate is the most audit-sensitive artifact the platform produces. A DPBI inspector will scrutinize it closely. This file documents the exact layout that §7.7.2 refers to.

## Page 1 — Certificate front

A single A4 page, portrait orientation. Sections from top to bottom:

### Header band
- Company logo (left) — placeholder for POC
- Certificate serial number (right) — same as `request_id` prefixed with `CERT-`
- Horizontal rule

### Title block
- Heading: "DATA ERASURE CERTIFICATE"
- Subtitle: "Issued under the Digital Personal Data Protection Act, 2023"

### Principal identification
Table with two columns:

| Field | Value |
|-------|-------|
| Principal identifier | `customer_04217` (last 4 shown; full hash in QR) |
| Request type | Combined (Access + Erasure) |
| Request submitted | `YYYY-MM-DD HH:MM IST` |
| Request completed | `YYYY-MM-DD HH:MM IST` |
| Identity verification method | Email match (POC stub — flag as limitation in text) |

### Erasure summary
Bold line: "The following records have been permanently erased from the organization's active systems as of the completion timestamp above."

Numbered list of erased tables:
1. `compliance_pack.silver.customers_tagged` — 1 row
2. `compliance_pack.silver.users_tagged` — 1 row
3. `compliance_pack.compliance.consent_events_log` — 4 events

For each entry:
- Table fully qualified name
- Rows erased
- Erasure method ("Delta DELETE + VACUUM RETAIN 0 HOURS")
- Delta version before erasure / Delta version after erasure

### Residual retention disclosure
Bold line: "The following records have NOT been erased and are retained under legal obligation."

Numbered list of scheduled residuals:
1. `compliance_pack.silver.transactions_tagged` — N rows
   - Retention basis: Banking Regulation Act, 1949
   - Retention period: 7 years from the most recent transaction
   - Scheduled purge date: `YYYY-MM-DD`

For each entry:
- Table fully qualified name
- Rows retained
- Retention basis (legal reference)
- Scheduled purge date

Commitment line: "A final erasure certificate will be issued on or before the scheduled purge date for each retained item."

### Verification block
- QR code (bottom left) — encodes the URL of the `audit_trail.json` plus a cryptographic hash of the certificate contents
- Signature block (bottom right):
  - Signed by: `dpdp-poc-builder` (service principal)
  - Signature timestamp: ISO 8601
  - Signature algorithm: SHA-256 digest of the page contents (POC stub — note as limitation)

### Footer
- Document ID (matches certificate serial number)
- Generation timestamp
- Disclaimer: "This is a synthetic POC artifact. Production certificates incorporate additional cryptographic signing and independent timestamp authority."

## Page 2 (optional) — Verification instructions

Brief instructions explaining how a recipient can verify the certificate:

1. Scan the QR code to retrieve the audit trail JSON
2. Compute the SHA-256 hash of the certificate page and compare against the signature block
3. Contact the DPO at `dpo@example.com` with questions

## Technical implementation notes

Generate via WeasyPrint (used for the proposal PDF already; HTML + CSS → PDF). Template structure:

```html
<!DOCTYPE html>
<html>
<head>
  <style>
    @page { size: A4; margin: 2cm; }
    body { font-family: Georgia, serif; font-size: 10pt; }
    .header-band { border-bottom: 1px solid #000; padding-bottom: 8px; }
    h1 { font-size: 16pt; text-transform: uppercase; letter-spacing: 2pt; }
    .qr-code { width: 100px; height: 100px; }
    .signature-block { border-top: 0.5pt solid #888; padding-top: 8px; }
  </style>
</head>
<body>
  <!-- Template sections as described above -->
</body>
</html>
```

QR code generation via `qrcode` Python package:
```python
import qrcode
import io
import base64

def generate_qr_base64(data: str) -> str:
    img = qrcode.make(data)
    buf = io.BytesIO()
    img.save(buf, format='PNG')
    return base64.b64encode(buf.getvalue()).decode()
```

Embed the QR image inline:
```html
<img class="qr-code" src="data:image/png;base64,{{ qr_base64 }}"/>
```

## POC limitations to disclose

The certificate itself must call these out:

- **Signature**: POC uses a SHA-256 digest, not a proper PKI signature. Phase 1 adds certificate-authority-backed signing.
- **Timestamp**: POC uses local system time. Phase 1 adds a trusted timestamp authority.
- **Identity verification**: POC stub uses email match. Phase 1 adds proper IDV provider.

These are not hidden — they are stated in the footer so that a hostile reviewer can see the platform is honest about its limitations. Production would remove these limitations, not hide them.

## Verification in INT-03

The test INT-03 opens the generated PDF and asserts:
- File exists at the expected path
- File size is between 20KB and 200KB (rough sanity check for a single page with QR)
- The QR code decodes to a URL containing the request_id

Full PDF content verification is not automated — visual inspection during the Day 14 demo is the human verification layer.
