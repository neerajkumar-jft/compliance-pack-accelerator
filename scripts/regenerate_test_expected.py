"""Regenerate the documented "expected" counts from the live workspace.

Why this exists
---------------
The deployed POC is deterministic — same git revision + same seeds (42 / 43
/ 44) + same regulation pack always produce the same pii_findings,
compliance_gaps, sensitivity tier breakdown, etc. But every time someone
edits ``regulations/dpdp_2023/rules.yaml`` or ``pii_patterns.py`` (or seeds,
or generators) the deterministic numbers shift. The numeric assertions in
``docs/how_to_test.html`` and the strict baseline test
(``tests/test_baseline_counts.py``) then go stale.

Workflow
--------
1. You change a rule / pattern / seed / generator.
2. You re-deploy and verify the new state is correct.
3. Run::

       python3 scripts/regenerate_test_expected.py --write

   That refreshes ``tests/_baseline.json`` with the new numbers and prints
   a copy-paste-ready snippet for every test card in ``how_to_test.html``
   that references those numbers.

4. CI / ``tests/test_baseline_counts.py`` now passes again, and you have
   the doc-update text on stdout.

Without ``--write`` the script just prints what it would write, plus a
diff against the existing baseline.

Usage
-----
    python3 scripts/regenerate_test_expected.py            # dry-run + diff
    python3 scripts/regenerate_test_expected.py --write    # actually update baseline.json
    python3 scripts/regenerate_test_expected.py --json     # machine-readable output
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "tests"))
from _sql import rows_or_raise  # noqa: E402

BASELINE_PATH = REPO_ROOT / "tests" / "_baseline.json"
CATALOG = "compliance_pack"


# ---------------------------------------------------------------------------
# Live-state queries — each returns a normalized dict matching the baseline
# ---------------------------------------------------------------------------

def _q(stmt: str) -> list:
    return rows_or_raise(stmt)


def _scalar(stmt: str) -> int:
    return int(_q(stmt)[0][0])


def _kv(stmt: str) -> dict[str, int]:
    return {str(r[0]): int(r[1]) for r in _q(stmt)}


def collect_live_state() -> dict:
    """Return one big dict with every deterministic count we care about."""
    return {
        "pii_findings": {
            "total":            _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.pii_findings"),
            "by_sensitivity":   _kv(f"SELECT sensitivity_tier, COUNT(*) FROM {CATALOG}.silver.pii_findings GROUP BY sensitivity_tier"),
            "by_table":         _kv(f"SELECT table_name, COUNT(*) FROM {CATALOG}.silver.pii_findings GROUP BY table_name"),
            "distinct_pii_types": _scalar(f"SELECT COUNT(DISTINCT pii_type) FROM {CATALOG}.silver.pii_findings"),
        },
        "compliance_gaps": {
            "total":         _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.compliance_gaps"),
            "by_severity":   _kv(f"SELECT severity, COUNT(*) FROM {CATALOG}.silver.compliance_gaps GROUP BY severity"),
            "by_rule_type":  _kv(f"SELECT rule_type, COUNT(*) FROM {CATALOG}.silver.compliance_gaps GROUP BY rule_type"),
        },
        "compliance_rules":  _scalar(f"SELECT COUNT(*) FROM {CATALOG}.bronze.compliance_rules WHERE is_active = true"),
        "personal_data_register": _scalar(f"SELECT COUNT(*) FROM {CATALOG}.compliance.personal_data_register"),
        "data_sources": {
            "total":         _scalar(f"SELECT COUNT(*) FROM {CATALOG}.bronze.data_sources WHERE is_active = true"),
            "by_pattern":    _kv(f"SELECT ingestion_pattern, COUNT(*) FROM {CATALOG}.bronze.data_sources WHERE is_active = true GROUP BY ingestion_pattern"),
        },
        "consent": {
            "events_total":      _scalar(f"SELECT COUNT(*) FROM {CATALOG}.compliance.consent_events_log"),
            "distinct_principals": _scalar(f"SELECT COUNT(DISTINCT data_principal_id) FROM {CATALOG}.compliance.consent_events_log"),
            "distinct_purposes":   _scalar(f"SELECT COUNT(DISTINCT purpose)             FROM {CATALOG}.compliance.consent_events_log"),
            "distinct_channels":   _scalar(f"SELECT COUNT(DISTINCT channel)             FROM {CATALOG}.compliance.consent_events_log"),
        },
        "notice_versions": {
            "total":          _scalar(f"SELECT COUNT(*) FROM {CATALOG}.compliance.notice_versions"),
            "by_language":    _kv(f"SELECT language, COUNT(*) FROM {CATALOG}.compliance.notice_versions GROUP BY language"),
            "human_authored": _scalar(
                f"SELECT COUNT(*) FROM {CATALOG}.compliance.notice_versions "
                f"WHERE NOT notice_text LIKE '[MACHINE-TRANSLATED%'"
            ),
            "machine_translated": _scalar(
                f"SELECT COUNT(*) FROM {CATALOG}.compliance.notice_versions "
                f"WHERE notice_text LIKE '[MACHINE-TRANSLATED%'"
            ),
        },
        "silver_tagged_objects": _scalar(
            f"SELECT COUNT(*) FROM {CATALOG}.information_schema.tables "
            f"WHERE table_schema = 'silver' AND table_name LIKE '%_tagged'"
        ),
        "column_masks": _scalar(
            f"SELECT COUNT(*) FROM system.information_schema.column_masks "
            f"WHERE table_catalog = '{CATALOG}'"
        ),
        "column_tags_distinct_columns": _scalar(
            f"SELECT COUNT(DISTINCT CONCAT(schema_name, '.', table_name, '.', column_name)) "
            f"FROM system.information_schema.column_tags WHERE catalog_name = '{CATALOG}'"
        ),
        "dsr_principal_customer_04217": {
            "customers":     _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.customers_tagged WHERE customer_id = 'customer_04217'"),
            "transactions":  _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.transactions_tagged WHERE customer_id = 'customer_04217'"),
            "consent_events": _scalar(
                f"SELECT COUNT(*) FROM {CATALOG}.compliance.consent_events_log WHERE data_principal_id = 'customer_04217'"
            ),
        },
        "synthetic_row_counts": {
            "employees":    _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.employees_tagged"),
            "customers":    _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.customers_tagged"),
            "patients":     _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.patients_tagged"),
            "transactions": _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.transactions_tagged"),
            "users":        _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.users_tagged"),
            "sf_leads":     _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.sf_leads_tagged"),
            "sf_contacts":  _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.sf_contacts_tagged"),
            "sf_accounts":  _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.sf_accounts_tagged"),
            "federation_lead_scoring":      _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.federation_lead_scoring_tagged"),
            "federation_campaign_response": _scalar(f"SELECT COUNT(*) FROM {CATALOG}.silver.federation_campaign_response_tagged"),
        },
    }


# ---------------------------------------------------------------------------
# Diff + reporting
# ---------------------------------------------------------------------------

def _flatten(d: dict, prefix: str = "") -> dict:
    """Flatten nested dict for easier diffing.  {a: {b: 1}} → {'a.b': 1}."""
    out = {}
    for k, v in d.items():
        key = f"{prefix}.{k}" if prefix else k
        if isinstance(v, dict):
            out.update(_flatten(v, key))
        else:
            out[key] = v
    return out


def diff(old: dict, new: dict) -> list[tuple[str, object, object]]:
    """Return list of (key, old_value, new_value) for everything that changed."""
    f_old, f_new = _flatten(old), _flatten(new)
    diffs = []
    for k in sorted(set(f_old) | set(f_new)):
        if f_old.get(k) != f_new.get(k):
            diffs.append((k, f_old.get(k, "<absent>"), f_new.get(k, "<absent>")))
    return diffs


# ---------------------------------------------------------------------------
# Doc-snippet renderer — copy-paste blocks for the test cards
# ---------------------------------------------------------------------------

def doc_snippets(state: dict) -> str:
    pf = state["pii_findings"]
    cg = state["compliance_gaps"]
    nv = state["notice_versions"]
    co = state["consent"]
    ds = state["data_sources"]
    sr = state["synthetic_row_counts"]
    dsr = state["dsr_principal_customer_04217"]

    sev = pf["by_sensitivity"]
    s_crit, s_high, s_med = sev.get("critical", 0), sev.get("high", 0), sev.get("medium", 0)

    g_sev = cg["by_severity"]
    g_crit, g_high, g_med = g_sev.get("critical", 0), g_sev.get("high", 0), g_sev.get("medium", 0)

    rt = cg["by_rule_type"]

    out = []
    out.append("Doc-snippet replacements for docs/how_to_test.html:")
    out.append("")
    out.append("  T1.1 Pass: total_findings >= " + str(pf["total"]))
    out.append(f"            tables_scanned = {len(pf['by_table'])}")
    out.append("")
    out.append(f"  T1.2 Expected: critical >= {s_crit}, high >= {s_high}, medium >= {s_med}")
    out.append("")
    out.append(f"  T2.1 Expected: events={co['events_total']}, principals~={co['distinct_principals']}, "
               f"purposes={co['distinct_purposes']}, channels={co['distinct_channels']}")
    out.append("")
    out.append(f"  T2.4 Expected: {nv['total']} rows — {nv['human_authored']} human + "
               f"{nv['machine_translated']} machine")
    out.append("")
    out.append(f"  T4.1 Expected: {state['compliance_rules']} active rules")
    out.append("")
    out.append(f"  T4.2 Expected: critical = {g_crit}, high = {g_high}, medium = {g_med}, total = {cg['total']}")
    out.append("")
    out.append("  T4.3 Expected: " + ", ".join(
        f"{k} ({v})" for k, v in sorted(rt.items(), key=lambda kv: -kv[1])
    ))
    out.append("")
    out.append(f"  T7.1 Row counts (silver): "
               f"employees={sr['employees']}, customers={sr['customers']}, "
               f"patients={sr['patients']}, transactions={sr['transactions']}, "
               f"users={sr['users']}, sf={sr['sf_leads']}/{sr['sf_contacts']}/{sr['sf_accounts']}, "
               f"fed={sr['federation_lead_scoring']}/{sr['federation_campaign_response']}")
    out.append("")
    out.append(f"  T3.x DSR principal customer_04217: "
               f"customers={dsr['customers']}, transactions={dsr['transactions']}, "
               f"consent_events={dsr['consent_events']}")
    out.append("")
    out.append("Quick Reference table totals:")
    out.append(f"  pii_findings              = {pf['total']}")
    out.append(f"  personal_data_register    = {state['personal_data_register']}")
    out.append(f"  compliance_gaps           = {cg['total']}")
    out.append(f"  data_sources              = {ds['total']}  (by pattern: {ds['by_pattern']})")
    out.append(f"  notice_versions           = {nv['total']}")
    out.append(f"  silver _tagged objects    = {state['silver_tagged_objects']}")
    out.append(f"  UC column masks           = {state['column_masks']}")
    out.append(f"  UC column-tagged columns  = {state['column_tags_distinct_columns']}")
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--write", action="store_true",
                   help=f"Write current live state to {BASELINE_PATH.relative_to(REPO_ROOT)}")
    p.add_argument("--json", action="store_true",
                   help="Print live state as JSON only (machine-readable)")
    args = p.parse_args()

    print(f"Querying live state of catalog `{CATALOG}`…", file=sys.stderr)
    live = collect_live_state()

    if args.json:
        print(json.dumps(live, indent=2, sort_keys=True))
        return 0

    print(f"Live state captured.\n")
    print(doc_snippets(live))
    print()

    if BASELINE_PATH.exists():
        old = json.loads(BASELINE_PATH.read_text())
        d = diff(old, live)
        print("=" * 70)
        if not d:
            print(f"✓ No drift vs {BASELINE_PATH.relative_to(REPO_ROOT)} — nothing to do.")
            return 0
        print(f"⚠ Drift detected vs {BASELINE_PATH.relative_to(REPO_ROOT)} ({len(d)} keys):")
        for key, old_v, new_v in d:
            print(f"    {key:50s}  {old_v}  →  {new_v}")
        print()

    if args.write:
        BASELINE_PATH.parent.mkdir(parents=True, exist_ok=True)
        BASELINE_PATH.write_text(json.dumps(live, indent=2, sort_keys=True) + "\n")
        print(f"✓ Wrote {BASELINE_PATH.relative_to(REPO_ROOT)}")
    else:
        print(f"(dry-run) re-run with --write to refresh {BASELINE_PATH.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
