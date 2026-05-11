# Environment verification script

> ⚠️ **Pre-build planning document.** Lakebase / service-principal verification checks don't apply on the free-trial deploy path. **For today's environment check, run `databricks bundle validate --target dev` and then `databricks current-user me`.**

Run this before Day 1 begins. Referenced from §2.11. All checks must pass before build work starts.

## Purpose

A partially-configured workspace produces confusing failures that look like code bugs. This script isolates environment issues so you catch them at minute zero, not day two.

## How to run

Execute each block in order in a Databricks notebook on the POC cluster. Every block should return a green tick `✓`. Any `✗` means stop and fix before proceeding.

## Block 1 — Workspace access

```python
# Verify cluster is on expected runtime
import platform
spark_version = spark.version
print(f"Spark: {spark_version}")

runtime_version = spark.conf.get("spark.databricks.clusterUsageTags.sparkVersion", "unknown")
print(f"Databricks Runtime: {runtime_version}")
assert "15.4" in runtime_version or "15.5" in runtime_version or "16." in runtime_version, \
    "Expected Databricks Runtime 15.4 LTS or later"
print("✓ Runtime check passed")
```

## Block 2 — Catalog and schemas

```python
catalogs = [row.catalog for row in spark.sql("SHOW CATALOGS").collect()]
assert "compliance_pack" in catalogs, f"Catalog compliance_pack not found. Available: {catalogs}"

schemas = [row.databaseName for row in spark.sql("SHOW SCHEMAS IN compliance_pack").collect()]
for required in ["bronze", "silver", "gold", "compliance"]:
    assert required in schemas, f"Schema compliance_pack.{required} missing"

print("✓ Catalog compliance_pack and all 4 schemas present")
```

## Block 3 — Service principal grants

```python
# Verify we can CREATE TABLE in bronze (implies USE CATALOG, USE SCHEMA, CREATE TABLE)
try:
    spark.sql("CREATE TABLE IF NOT EXISTS compliance_pack.bronze._env_check (x INT) USING DELTA")
    spark.sql("DROP TABLE compliance_pack.bronze._env_check")
    print("✓ CREATE TABLE privilege on bronze")
except Exception as e:
    print(f"✗ FAILED CREATE TABLE in bronze: {e}")
    raise

# Verify APPLY TAG privilege
try:
    spark.sql("CREATE TABLE IF NOT EXISTS compliance_pack.silver._tag_check (x INT) USING DELTA")
    spark.sql("""
        ALTER TABLE compliance_pack.silver._tag_check
        ALTER COLUMN x SET TAGS ('_env_check' = 'ok')
    """)
    spark.sql("""
        ALTER TABLE compliance_pack.silver._tag_check
        ALTER COLUMN x UNSET TAGS ('_env_check')
    """)
    spark.sql("DROP TABLE compliance_pack.silver._tag_check")
    print("✓ APPLY TAG privilege")
except Exception as e:
    print(f"✗ FAILED APPLY TAG: see §2.3 for grant statement. Error: {e}")
    raise
```

## Block 4 — Landing zone volume

```python
# The volume must exist and be writable
try:
    result = dbutils.fs.ls("/Volumes/compliance_pack/bronze/landing/")
    print("✓ Landing zone volume exists")

    # Test write
    test_path = "/Volumes/compliance_pack/bronze/landing/_env_check.txt"
    dbutils.fs.put(test_path, "env check", overwrite=True)
    dbutils.fs.rm(test_path)
    print("✓ Landing zone is writable")
except Exception as e:
    print(f"✗ Landing zone check failed: {e}")
    print("Create the volume per §2.10: CREATE VOLUME IF NOT EXISTS compliance_pack.bronze.landing")
    raise
```

## Block 5 — Python dependencies

```python
required_packages = {
    "presidio_analyzer": "2.2.355",
    "presidio_anonymizer": "2.2.355",
    "faker": "33.3.1",
    "pandas": "2.2.3",
}

import importlib.metadata

for pkg, expected_version in required_packages.items():
    try:
        actual = importlib.metadata.version(pkg.replace("_", "-"))
        if actual == expected_version:
            print(f"✓ {pkg} == {actual}")
        else:
            print(f"⚠ {pkg} is {actual}, expected {expected_version}; may still work")
    except importlib.metadata.PackageNotFoundError:
        print(f"✗ {pkg} not installed; see §2.7 for cluster library config")
        raise
```

## Block 6 — Lakebase reachability

```python
# Replace <host> and <credentials> with actual connection per §2.4
# For native Databricks integration, use dbutils.secrets or Databricks-integrated auth

try:
    # Uses the Databricks SQL Connector or similar; exact call depends on setup
    from databricks.sql import connect as lakebase_connect  # placeholder
    with lakebase_connect(server_hostname="<lakebase-host>",
                          http_path="/databricks/lakebase/dpdp-poc-consent",
                          access_token=dbutils.secrets.get("dpdp-poc", "lakebase-token")) as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT 1")
        result = cursor.fetchone()
        assert result[0] == 1
    print("✓ Lakebase reachable")
except ImportError:
    print("⚠ Lakebase client not available in this cluster; may be fine if using SQL Warehouse")
except Exception as e:
    print(f"✗ Lakebase reachability check failed: {e}")
    raise
```

## Block 7 — AI functions available

```python
try:
    result = spark.sql("""
        SELECT ai_classify(
            'customer email support request',
            ARRAY('support', 'sales', 'other')
        ) AS classification
    """).collect()
    print(f"✓ ai_classify available; test result: {result[0].classification}")
except Exception as e:
    print(f"✗ ai_classify failed: {e}")
    print("Check workspace AI function enablement; may need region-specific setup")
    raise
```

## Block 8 — Credit budget visibility

```python
# Check that we can see workspace usage; exact mechanism varies
# This is a manual check for the human collaborator — no automated assertion
print("""
Manual check: visit Account Settings → Usage in Databricks UI.
Current trial credit balance: _____________
Used so far:                   _____________
Target burn rate by Day 7:     < 50% of budget
""")
```

## Summary

If all 8 blocks pass, the environment is ready. Proceed to Day 1 work per §0.5 of SPEC.md.

If any block fails:
- Blocks 1-5: configuration issue, follow §2 of the spec to remediate
- Block 6: Lakebase provisioning issue, follow §2.4
- Block 7: workspace feature issue, raise with Databricks account team
- Block 8: not blocking, but flag to the human collaborator

Do not start building until every check passes. A day spent getting the environment right saves three days of debugging downstream.
