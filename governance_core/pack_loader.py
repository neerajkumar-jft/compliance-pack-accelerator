"""Regulation pack loader — reads active pack at bootstrap, exposes typed accessors.

The pack loader is the single entry point for regulation-specific values. Pipelines
and scripts import from this module rather than from individual pack files, so the
active pack is switchable via a single env var without code changes elsewhere.

## Usage

    from governance_core.pack_loader import active_pack

    pack = active_pack()
    for rule in pack.rules():
        spark.sql(f"INSERT ... VALUES ({rule['rule_id']}, ...)")

    retention_days = pack.retention_default(purpose="marketing_email")
    allowed = pack.residency_allowed_countries()
    langs = pack.languages()

## Activation

    export REGULATION_PACK=dpdp_2023    # default — current POC behavior
    export REGULATION_PACK=uk_gdpr      # Phase 1 target
    export REGULATION_PACK=ccpa

Pack directories live under `regulations/<code>/`.

## Status

**Phase 0.2:** yaml loader is live; consumed first by the compliance-rules migration
in `pipelines/phase1_bootstrap.py`. Additional accessors (notice templates, residency
SQL rendering, pattern-pack composition) wire in as each migration step lands.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    import yaml
except ImportError as _e:  # noqa: F841
    yaml = None  # Loader surfaces a clearer error below


class PackLoaderError(RuntimeError):
    """Raised when the active pack is missing, malformed, or incomplete."""


DEFAULT_PACK_CODE = "dpdp_2023"
REPO_ROOT = Path(__file__).resolve().parent.parent
PACKS_ROOT = REPO_ROOT / "regulations"


def active_pack_code() -> str:
    """Return the active regulation pack code. Defaults to DPDP-2023 (current POC)."""
    return os.environ.get("REGULATION_PACK") or DEFAULT_PACK_CODE


def active_pack_dir() -> Path:
    """Path to the active pack's directory. Raises if it doesn't exist."""
    code = active_pack_code()
    path = PACKS_ROOT / code
    if not path.exists():
        available = [p.name for p in PACKS_ROOT.iterdir() if p.is_dir()] if PACKS_ROOT.exists() else []
        raise PackLoaderError(
            f"Regulation pack '{code}' not found at {path}. Available: {available}"
        )
    return path


def _require_yaml() -> None:
    if yaml is None:
        raise PackLoaderError(
            "PyYAML is required to load regulation packs. Install: pip install pyyaml"
        )


def _read_yaml(path: Path) -> Any:
    _require_yaml()
    if not path.exists():
        raise PackLoaderError(f"Pack file not found: {path}")
    with path.open() as fh:
        return yaml.safe_load(fh) or {}


@dataclass
class DPIATemplate:
    """Typed wrapper around a pack's dpia_template.yaml.

    The 8 DPIA section *keys* are regulation-agnostic and defined by
    governance_core/dpia.py::DPIASections. This template controls the
    regulation-specific framing: which legal framework the model is
    told it's drafting under, the citation style for section
    references, the system prompt, and any per-section description
    overrides that get merged into the JSON schema fed to the LLM.
    """
    legal_framework_name: str
    section_citation_style: str
    system_prompt: str
    section_descriptions: dict = field(default_factory=dict)
    pack_version: str = "0.0.0"           # ADR-0001 Q2 — sourced from pack.yaml::version; feeds dpia_prompt_version() hash so MLflow segregates traces across pack bumps


@dataclass
class Pack:
    """Typed wrapper around a loaded regulation pack. Instantiate via load()."""
    code: str
    path: Path
    metadata: dict = field(default_factory=dict)
    _rules: list | None = field(default=None, repr=False)
    _rights: list | None = field(default=None, repr=False)
    _retention: dict | None = field(default=None, repr=False)
    _residency: dict | None = field(default=None, repr=False)
    _languages: list | None = field(default=None, repr=False)
    _breach_sla: dict | None = field(default=None, repr=False)
    _dpia_template: "DPIATemplate | None" = field(default=None, repr=False)

    @property
    def name(self) -> str:
        return self.metadata.get("name", self.code)

    @property
    def jurisdiction(self) -> str:
        return self.metadata.get("jurisdiction", "")

    @property
    def primary_locale(self) -> str:
        return self.metadata.get("primary_locale", "en-IN")

    @property
    def version(self) -> str:
        """Pack semver, e.g. '1.0.0'. Defaults to '0.0.0' for packs predating ADR-0001 Q2."""
        return str(self.metadata.get("version") or "0.0.0")

    def rules(self) -> list[dict]:
        """Return compliance rules as a list of dicts."""
        if self._rules is None:
            data = _read_yaml(self.path / "rules.yaml")
            self._rules = data.get("rules") or []
        return self._rules

    def rights(self) -> list[dict]:
        """Return activated data-subject rights from rights.yaml."""
        if self._rights is None:
            data = _read_yaml(self.path / "rights.yaml")
            self._rights = data.get("rights") or []
        return self._rights

    def retention_default(self, purpose: str) -> int:
        """Return retention days for a purpose; falls back to 730 if unset."""
        if self._retention is None:
            data = _read_yaml(self.path / "retention_defaults.yaml")
            self._retention = data.get("defaults") or {}
        val = self._retention.get(purpose)
        return int(val) if val is not None else 730

    def residency_allowed_countries(self) -> list[str]:
        """Countries whose rows non-admins may see."""
        if self._residency is None:
            self._residency = _read_yaml(self.path / "residency.yaml") or {}
        return list(self._residency.get("allowed_countries") or [])

    def residency_apply_targets(self) -> list[dict]:
        """[{table, column}] pairs where the residency filter should apply."""
        if self._residency is None:
            self._residency = _read_yaml(self.path / "residency.yaml") or {}
        return list(self._residency.get("apply_filter_to") or [])

    def languages(self) -> list[dict]:
        """Pack's language registry — one entry per locale."""
        if self._languages is None:
            data = _read_yaml(self.path / "languages.yaml")
            self._languages = data.get("languages") or []
        return self._languages

    def seeded_languages(self) -> list[dict]:
        """Languages with seeded_by_poc=true (hand-authored notice bodies)."""
        return [l for l in self.languages() if l.get("seeded_by_poc")]

    def breach_sla(self) -> dict:
        """Breach-notification SLA config."""
        if self._breach_sla is None:
            self._breach_sla = _read_yaml(self.path / "breach_sla.yaml") or {}
        return self._breach_sla

    def pii_patterns(self) -> list:
        """Return the pack's region-specific PII patterns.

        Dynamically imports `regulations.<code>.pii_patterns` and returns its
        IN_SPECIFIC_PATTERNS list. Returns [] if the pack has no
        pii_patterns.py or an empty IN_SPECIFIC_PATTERNS (valid for packs
        that only rely on universal patterns).
        """
        from importlib import import_module
        try:
            mod = import_module(f"regulations.{self.code}.pii_patterns")
        except ImportError:
            return []
        return list(getattr(mod, "IN_SPECIFIC_PATTERNS", []))

    def notices(self) -> list[dict]:
        """Return seeded consent notices from notices.yaml.

        Each entry is a dict with columns matching compliance.notice_versions.
        datetime strings are returned as ISO 8601; callers parse them with
        datetime.fromisoformat() when loading into Spark.
        """
        data = _read_yaml(self.path / "notices.yaml")
        return data.get("notices") or []

    def dpia_template(self) -> DPIATemplate:
        """Return the pack's DPIA prompt template (governance_core/dpia.py).

        Loaded lazily on first access. Required keys: legal_framework_name,
        section_citation_style, system_prompt. section_descriptions is
        optional and defaults to {}.
        """
        if self._dpia_template is None:
            path = self.path / "dpia_template.yaml"
            if not path.exists():
                raise PackLoaderError(
                    f"Pack '{self.code}' is missing dpia_template.yaml at {path}. "
                    f"Required for the DPIA Auto-Generator (Agent 1) — see "
                    f"regulations/README.md for the contract."
                )
            data = _read_yaml(path)
            for required in ("legal_framework_name", "section_citation_style", "system_prompt"):
                if not data.get(required):
                    raise PackLoaderError(
                        f"Pack '{self.code}' dpia_template.yaml is missing "
                        f"required key '{required}'."
                    )
            self._dpia_template = DPIATemplate(
                legal_framework_name=data["legal_framework_name"],
                section_citation_style=data["section_citation_style"],
                system_prompt=data["system_prompt"],
                section_descriptions=data.get("section_descriptions") or {},
                pack_version=self.version,
            )
        return self._dpia_template

    def default_purposes(self) -> list[str]:
        """Return the list of consent purposes the pack's notices cover.

        Used by phase1_bootstrap's consent-event generator so purposes stay
        consistent between notice templates and generated events.
        """
        data = _read_yaml(self.path / "notices.yaml")
        purposes = data.get("default_purposes")
        if purposes:
            return list(purposes)
        # Fallback: derive from the first notice's purposes_covered
        notices = data.get("notices") or []
        if notices and "purposes_covered" in notices[0]:
            return list(notices[0]["purposes_covered"])
        return []


_cache: dict[str, Pack] = {}
_all_packs_cache: list[Pack] | None = None


def load() -> Pack:
    """Load the legacy "active" regulation pack and return a Pack accessor (cached).

    Backward-compat shim from the single-pack-active era. New code should use
    ``loaded_packs()`` (all packs in the deployment) and ``pack_for(jurisdiction)``
    (per-data-subject routing) — see ADR-0001.

    The returned Pack is the *primary* pack for this deployment, defined as:
      1. Whatever ``REGULATION_PACK`` env var points at, if set.
      2. Otherwise the first pack in ``loaded_packs()`` order.
      3. Otherwise ``DEFAULT_PACK_CODE`` (``dpdp_2023``).

    Used by older call sites that haven't been migrated to multi-pack-aware
    queries. Safe to keep — ADR-0001 explicitly preserves it as a primary-pack
    accessor, not a "the only loaded pack" accessor.
    """
    code = active_pack_code()
    if code not in _cache:
        path = active_pack_dir()
        metadata = _read_yaml(path / "pack.yaml")
        _cache[code] = Pack(code=code, path=path, metadata=metadata)
    return _cache[code]


# Convenience alias for readability. Same backward-compat semantics as load().
active_pack = load


# ---------------------------------------------------------------------------
# Multi-pack accessors (ADR-0001)
#
# loaded_packs() — returns every pack found under regulations/ at deploy time.
# pack_for(jurisdiction) — routes a single principal to its governing pack.
# derive_jurisdiction(country) — translates a country signal into a jurisdiction
#   code that pack_for() can route on. Customers extend by overriding the
#   COUNTRY_TO_JURISDICTION mapping.
# ---------------------------------------------------------------------------


# Default country → jurisdiction mapping. Case-insensitive lookup. Keys
# normalised to upper-case during lookup. Values are jurisdiction codes that
# match the `jurisdiction` field in each pack's `pack.yaml`.
#
# Override at deploy time by extending this dict before calling
# derive_jurisdiction(); the helper is small enough to monkey-patch in tests.
COUNTRY_TO_JURISDICTION: dict[str, str] = {
    # India / DPDP
    "IN": "IN", "IND": "IN", "INDIA": "IN",
    # United Kingdom / UK GDPR
    "GB": "GB", "UK": "GB", "GBR": "GB", "UNITED KINGDOM": "GB",
    "ENGLAND": "GB", "SCOTLAND": "GB", "WALES": "GB", "NORTHERN IRELAND": "GB",
    # United States / CCPA (and state-level laws)
    "US": "US", "USA": "US", "UNITED STATES": "US", "AMERICA": "US",
    # EU / EU GDPR — every EU/EEA member state routes to a single EU code
    # for now. A future ADR may split per-member-state if national-DPA
    # divergences (cookies, transfers) require pack-per-country.
    "AT": "EU", "AUSTRIA": "EU",
    "BE": "EU", "BELGIUM": "EU",
    "BG": "EU", "BULGARIA": "EU",
    "HR": "EU", "CROATIA": "EU",
    "CY": "EU", "CYPRUS": "EU",
    "CZ": "EU", "CZECH REPUBLIC": "EU", "CZECHIA": "EU",
    "DK": "EU", "DENMARK": "EU",
    "EE": "EU", "ESTONIA": "EU",
    "FI": "EU", "FINLAND": "EU",
    "FR": "EU", "FRANCE": "EU",
    "DE": "EU", "GERMANY": "EU",
    "GR": "EU", "GREECE": "EU",
    "HU": "EU", "HUNGARY": "EU",
    "IE": "EU", "IRELAND": "EU",
    "IT": "EU", "ITALY": "EU",
    "LV": "EU", "LATVIA": "EU",
    "LT": "EU", "LITHUANIA": "EU",
    "LU": "EU", "LUXEMBOURG": "EU",
    "MT": "EU", "MALTA": "EU",
    "NL": "EU", "NETHERLANDS": "EU",
    "PL": "EU", "POLAND": "EU",
    "PT": "EU", "PORTUGAL": "EU",
    "RO": "EU", "ROMANIA": "EU",
    "SK": "EU", "SLOVAKIA": "EU",
    "SI": "EU", "SLOVENIA": "EU",
    "ES": "EU", "SPAIN": "EU",
    "SE": "EU", "SWEDEN": "EU",
    # Iceland, Liechtenstein, Norway — EEA non-EU, GDPR also applies
    "IS": "EU", "ICELAND": "EU",
    "LI": "EU", "LIECHTENSTEIN": "EU",
    "NO": "EU", "NORWAY": "EU",
}


def derive_jurisdiction(country: str | None) -> str | None:
    """Translate a country string into a jurisdiction code, or None if unmapped.

    Returns ``None`` when the country is NULL, blank, or doesn't match a known
    code. Unmapped principals are surfaced as a high-severity compliance gap by
    downstream queries — see ADR-0001 §"Schema migration / Backfilling
    jurisdiction on existing rows".

    Examples
    --------
    >>> derive_jurisdiction("India")
    'IN'
    >>> derive_jurisdiction("uk")
    'GB'
    >>> derive_jurisdiction("Germany")
    'EU'
    >>> derive_jurisdiction(None) is None
    True
    >>> derive_jurisdiction("Atlantis") is None
    True
    """
    if country is None:
        return None
    key = country.strip().upper()
    if not key:
        return None
    return COUNTRY_TO_JURISDICTION.get(key)


def loaded_packs() -> list[Pack]:
    """Return every regulation pack found under ``regulations/``.

    Cached on first call. A pack is considered loadable if its directory
    contains a readable ``pack.yaml``; packs whose ``pack.yaml`` is missing
    or malformed are skipped with a warning rather than failing the whole
    load (per ADR-0001 §"Loader and pack mechanics").

    Order is the lexically-sorted directory order, with ``DEFAULT_PACK_CODE``
    (``dpdp_2023``) hoisted to position 0 if present — this defines the
    "primary" pack used by ``active_pack()`` and by the DPIA generator
    when an activity touches multiple jurisdictions and needs a primary
    framework for the narrative.
    """
    global _all_packs_cache
    if _all_packs_cache is not None:
        return _all_packs_cache
    if not PACKS_ROOT.exists():
        _all_packs_cache = []
        return _all_packs_cache

    packs: list[Pack] = []
    for sub in sorted(PACKS_ROOT.iterdir()):
        if not sub.is_dir():
            continue
        pack_yaml = sub / "pack.yaml"
        if not pack_yaml.exists():
            continue
        try:
            metadata = _read_yaml(pack_yaml)
        except PackLoaderError:
            continue
        packs.append(Pack(code=sub.name, path=sub, metadata=metadata))

    # Hoist DEFAULT_PACK_CODE to position 0 so the primary-pack contract holds.
    packs.sort(key=lambda p: (p.code != DEFAULT_PACK_CODE, p.code))
    _all_packs_cache = packs
    return _all_packs_cache


def pack_for(jurisdiction: str | None) -> Pack | None:
    """Return the regulation pack that governs principals with this jurisdiction.

    The match is on each pack's ``jurisdiction`` field in ``pack.yaml``
    (e.g., ``IN`` for DPDP, ``GB`` for UK GDPR, ``EU`` for EU GDPR).
    Returns ``None`` for an unmapped jurisdiction (caller decides whether
    to fall back, raise, or surface as a compliance gap).

    If two loaded packs claim the same jurisdiction, this function returns
    the first one in ``loaded_packs()`` order (which is alphabetical with
    ``DEFAULT_PACK_CODE`` hoisted) — but ADR-0001 §"Loader and pack mechanics"
    documents this as a misconfiguration that the loader should reject at
    load time. Future enforcement to be added when the second pack lands.
    """
    if not jurisdiction:
        return None
    target = jurisdiction.strip().upper()
    if not target:
        return None
    for p in loaded_packs():
        pack_jur = (p.jurisdiction or "").upper()
        if pack_jur == target:
            return p
    return None


def reset_cache() -> None:
    """Clear all module-level caches. Test-only helper."""
    global _all_packs_cache
    _cache.clear()
    _all_packs_cache = None


# ---------------------------------------------------------------------------
# Jurisdiction-code validation (ADR-0001 Q3)
# ---------------------------------------------------------------------------
#
# When a customer-level silver row's ``jurisdiction`` column doesn't match any
# loaded pack's declared ``jurisdiction``, that row's compliance rules can't
# be routed and the principal is operationally "ungoverned". This is the
# "unmapped principals" failure mode ADR-0001 §"Identity and jurisdiction"
# anticipates. The validator below scans the live silver layer, classifies
# each distinct jurisdiction value into one of four buckets, and returns a
# report that callers (typically phase1_bootstrap or a CI guard) consume to
# fail-or-warn.

def validate_jurisdictions(
    observed: set[str | None],
    packs: list[Pack] | None = None,
) -> dict:
    """Classify each observed jurisdiction code against the loaded pack set.

    ``observed`` is the distinct set of values from a customer-level
    ``jurisdiction`` column (e.g.,
    ``{j for (j,) in spark.sql("SELECT DISTINCT jurisdiction FROM
    silver.customers_tagged").collect()}``).

    Returns a dict with four keys, each mapping to a list of jurisdiction
    codes (or to ``None`` for the NULL bucket):

      - ``mapped`` — code matches a loaded pack's ``jurisdiction`` declared
        in pack.yaml. Routable; nothing to do.
      - ``null`` — NULL/None present. Indicates rows with no jurisdiction
        captured at all (the "unmapped principals" gap).
      - ``unmapped_known`` — code is in ``COUNTRY_TO_JURISDICTION``'s value
        set (e.g., ``'US'``) but no loaded pack declares that jurisdiction.
        Indicates a missing pack — author it or remove those principals.
      - ``unmapped_unknown`` — code is not in either ``COUNTRY_TO_JURISDICTION``
        values or any loaded pack. Indicates bad data — the row's
        ``jurisdiction`` value was set to something the platform doesn't
        recognise (typo, stale code, untranslated country string).

    Pure function — no Spark / Databricks dependency. The caller is
    responsible for collecting the ``observed`` set.
    """
    if packs is None:
        packs = loaded_packs()
    declared_jurisdictions = {(p.jurisdiction or "").upper() for p in packs if p.jurisdiction}
    known_codes = set(COUNTRY_TO_JURISDICTION.values())

    mapped: list[str] = []
    null_bucket: list[None] = []
    unmapped_known: list[str] = []
    unmapped_unknown: list[str] = []

    for raw in observed:
        if raw is None or (isinstance(raw, str) and not raw.strip()):
            null_bucket.append(None)
            continue
        code = raw.strip().upper()
        if code in declared_jurisdictions:
            mapped.append(code)
        elif code in known_codes:
            unmapped_known.append(code)
        else:
            unmapped_unknown.append(code)

    return {
        "mapped": sorted(set(mapped)),
        "null": null_bucket,
        "unmapped_known": sorted(set(unmapped_known)),
        "unmapped_unknown": sorted(set(unmapped_unknown)),
    }


def format_validation_report(report: dict, observed_count: int | None = None) -> str:
    """Return a human-readable summary of a validate_jurisdictions() result.

    Suitable for phase1_bootstrap stdout or a CI guard's failure message.
    The format is deliberately stable so log-scrapers can rely on it.
    """
    parts: list[str] = []
    parts.append("Jurisdiction validation (ADR-0001 Q3):")
    if observed_count is not None:
        parts.append(f"  observed distinct values: {observed_count}")
    parts.append(f"  ✓ mapped:           {report['mapped'] or '(none)'}")
    parts.append(f"  ⚠ NULL/blank:       {len(report['null'])} bucket(s)")
    parts.append(f"  ⚠ unmapped (known): {report['unmapped_known'] or '(none)'}")
    parts.append(f"  ✗ unmapped (unknown): {report['unmapped_unknown'] or '(none)'}")
    return "\n".join(parts)
