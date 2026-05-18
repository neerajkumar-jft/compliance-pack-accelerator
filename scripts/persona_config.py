"""Shared runtime configuration for the persona scripts.

Auto-detects workspace URL, SQL warehouse id, and catalog from the
Databricks CLI context so teammates can run the scripts against their
own workspace without editing any source file.

Detection order:
    workspace URL   : DATABRICKS_HOST env → `current-user me` response
                      host → CLI config profile
    warehouse id    : COMPLIANCE_WAREHOUSE_ID env → first RUNNING serverless
                      warehouse → fail with helpful error
    catalog         : COMPLIANCE_CATALOG env → default "compliance_pack"

All three values are memoized — expensive CLI calls happen once per
process.

Scripts should use the helpers below rather than hardcoding anything.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from functools import lru_cache


CATALOG_DEFAULT = "compliance_pack"


def _run(*cmd: str) -> str:
    r = subprocess.run(list(cmd), capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed: {r.stderr[:300]}")
    return r.stdout


@lru_cache(maxsize=1)
def get_deployer_email() -> str:
    """Return the currently-authenticated user's email (for admin ACLs)."""
    d = json.loads(_run("databricks", "current-user", "me"))
    email = d.get("userName") or (d.get("emails") or [{}])[0].get("value", "")
    if not email or "@" not in email:
        raise RuntimeError(f"could not read user email from current-user me: {d}")
    return email


@lru_cache(maxsize=1)
def get_workspace_url() -> str:
    """Return the workspace URL (no trailing slash) like
    'https://dbc-xxxx.cloud.databricks.com'.

    Order:
      1. DATABRICKS_HOST env var (set by the CLI when a profile is active)
      2. Parse out of `databricks current-user me`'s meta.location (if present)
      3. Inspect ~/.databrickscfg for the active profile
    """
    if env := os.environ.get("DATABRICKS_HOST"):
        return env.rstrip("/")

    # Most CLI versions include a 'location' or 'self_uri' that starts with
    # the workspace host — check meta.location if present.
    try:
        d = json.loads(_run("databricks", "current-user", "me"))
        loc = (d.get("meta") or {}).get("location") or d.get("self_uri")
        if loc and "://" in loc:
            scheme, rest = loc.split("://", 1)
            host = rest.split("/", 1)[0]
            return f"{scheme}://{host}"
    except Exception:
        pass

    # Fallback: parse ~/.databrickscfg
    cfg = os.path.expanduser("~/.databrickscfg")
    if os.path.exists(cfg):
        profile = os.environ.get("DATABRICKS_CONFIG_PROFILE", "DEFAULT")
        current = None
        with open(cfg) as fh:
            for line in fh:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1]
                    continue
                if current == profile and line.lower().startswith("host"):
                    _, _, val = line.partition("=")
                    return val.strip().rstrip("/")

    raise RuntimeError(
        "could not determine workspace URL — set DATABRICKS_HOST or "
        "configure the CLI with `databricks configure`"
    )


@lru_cache(maxsize=1)
def get_warehouse_id() -> str:
    """Return a serverless SQL warehouse id, warming it if stopped.

    Picking order:
      1. COMPLIANCE_WAREHOUSE_ID env var (no warmup — caller's responsibility)
      2. RUNNING serverless warehouse
      3. RUNNING classic warehouse
      4. STOPPED serverless warehouse  (warmed via SELECT 1 before return)
      5. Anything else                 (warmed)

    Why we accept STOPPED: serverless warehouses on this workspace
    auto-stop after 10 idle minutes. A long-running deploy_all.sh step
    (DLT pipeline ~4min + phase1_bootstrap ~3min) can easily push
    warehouse idleness past that threshold, so the next caller (e.g.
    apply_uc_tags.py) finds nothing RUNNING. Failing here forces the
    user to manually start the warehouse, which defeats the
    one-command deploy. Instead we fire a SELECT 1 with a generous
    wait_timeout — that auto-starts serverless warehouses inline, and
    the function returns only once the warehouse is ready.

    Raises only when there is no warehouse at all in the workspace.
    """
    if env := os.environ.get("COMPLIANCE_WAREHOUSE_ID"):
        return env

    out = _run("databricks", "warehouses", "list", "-o", "json")
    warehouses = json.loads(out)
    if not warehouses:
        raise RuntimeError(
            "no SQL warehouses found in this workspace. Create one (UI: "
            "Compute → SQL Warehouses → Create) or set COMPLIANCE_WAREHOUSE_ID."
        )

    running_serverless = [w for w in warehouses
                          if w.get("state") == "RUNNING" and w.get("enable_serverless_compute") is True]
    running_any        = [w for w in warehouses if w.get("state") == "RUNNING"]
    stopped_serverless = [w for w in warehouses if w.get("enable_serverless_compute") is True]
    pick = (running_serverless or running_any or stopped_serverless or warehouses)[0]

    if pick.get("state") != "RUNNING":
        _warm_warehouse(pick["id"])

    return pick["id"]


def _warm_warehouse(warehouse_id: str) -> None:
    """Fire a SELECT 1 to auto-start a stopped serverless warehouse.

    Uses a 50s wait_timeout — covers typical cold-start (~30-45s).
    Best-effort: failures are logged but not raised, since the caller
    will surface a clearer error on its first real query.
    """
    import json as _json
    import subprocess as _sub
    import tempfile as _tf
    payload = {"warehouse_id": warehouse_id, "statement": "SELECT 1", "wait_timeout": "50s"}
    with _tf.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        _json.dump(payload, f)
        path = f.name
    print(f"  ↻ warming warehouse {warehouse_id} (cold-start, ~30-45s)…", flush=True)
    r = _sub.run(
        ["databricks", "api", "post", "/api/2.0/sql/statements",
         "--json", f"@{path}"],
        capture_output=True, text=True,
    )
    if r.returncode == 0:
        try:
            state = _json.loads(r.stdout).get("status", {}).get("state", "?")
            print(f"  ✓ warehouse warmup: {state}", flush=True)
        except Exception:
            pass


def get_catalog() -> str:
    return os.environ.get("COMPLIANCE_CATALOG") or CATALOG_DEFAULT


# Foundation model endpoint for the persona agents and the DPIA /
# Compliance-Q&A notebooks. Centralized here so a future deprecation
# is a one-line change.
#
# On the Databricks trial free tier, `databricks-gpt-oss-120b` is the
# only endpoint that isn't rate-limited to 0. On a production
# workspace with Foundation Model APIs enabled, swap this for whatever
# Claude / Llama / OpenAI-style endpoint you actually want.
MODEL_ENDPOINT_DEFAULT = "databricks-gpt-oss-120b"


def get_model_endpoint() -> str:
    return os.environ.get("COMPLIANCE_MODEL_ENDPOINT") or MODEL_ENDPOINT_DEFAULT


def print_detected() -> None:
    """Print what was detected — called by the orchestrator on startup so
    teammates can sanity-check which workspace they're writing to."""
    print("Detected runtime context:")
    print(f"  workspace URL : {get_workspace_url()}")
    print(f"  deployer      : {get_deployer_email()}")
    print(f"  warehouse id  : {get_warehouse_id()}")
    print(f"  catalog       : {get_catalog()}")
    print(f"  model endpoint: {get_model_endpoint()}")


if __name__ == "__main__":
    # Allow `python3 scripts/persona_config.py` to print the detected
    # context — handy for debugging.
    try:
        print_detected()
    except Exception as e:
        print(f"error: {e}", file=sys.stderr)
        sys.exit(1)
