# Databricks notebook source
# MAGIC %md
# MAGIC # Add Data Source — Databricks-Native Ingestion
# MAGIC
# MAGIC **DPDP Compliance Platform**
# MAGIC
# MAGIC Connect to external data sources using Databricks-native features only.
# MAGIC No Python connectors, no pip installs, no driver-side collection.
# MAGIC
# MAGIC **Three ingestion patterns:**
# MAGIC | Source Type | Databricks Feature | Example |
# MAGIC |---|---|---|
# MAGIC | Databases (Postgres, MySQL, SQL Server, Snowflake, BigQuery) | **Lakehouse Federation** | `CREATE FOREIGN CATALOG` |
# MAGIC | SaaS apps (Salesforce, Workday, ServiceNow, SharePoint) | **Lakeflow Connect** | Managed ingestion pipeline |
# MAGIC | File sources (S3, ADLS, GCS) | **Auto Loader** via DLT | `cloudFiles` format |
# MAGIC
# MAGIC ---

# COMMAND ----------

# MAGIC %md
# MAGIC ## Configuration

# COMMAND ----------

import re

def validate_identifier(name, label="identifier"):
    if not name or not re.match(r'^[a-zA-Z_][a-zA-Z0-9_]{0,127}$', name):
        raise ValueError(f"Invalid {label}: {name!r}")
    return name

dbutils.widgets.text("catalog", "compliance_pack", "Target Catalog")
dbutils.widgets.dropdown("ingestion_pattern", "lakehouse_federation",
    ["lakehouse_federation", "lakeflow_connect", "auto_loader", "unity_catalog"],
    "Ingestion Pattern")
dbutils.widgets.text("source_name", "", "Source Name (e.g., 'client_crm')")
dbutils.widgets.text("connection_name", "", "Connection Name (for Federation)")

CATALOG = validate_identifier(dbutils.widgets.get("catalog"), "catalog")
PATTERN = dbutils.widgets.get("ingestion_pattern")
SOURCE_NAME = dbutils.widgets.get("source_name").strip()
CONNECTION_NAME = dbutils.widgets.get("connection_name").strip()

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern 1: Lakehouse Federation (Databases)
# MAGIC
# MAGIC **Use for:** Postgres, MySQL, SQL Server, Snowflake, BigQuery, Redshift, Oracle
# MAGIC
# MAGIC **What it does:** Creates a foreign catalog in Unity Catalog that queries the source database in-place.
# MAGIC No data movement. No Python connectors. One SQL statement.
# MAGIC
# MAGIC ### Prerequisites
# MAGIC 1. Create a **Connection** in Unity Catalog (Catalog Explorer > External Data > Connections)
# MAGIC 2. Provide the connection name below

# COMMAND ----------

if PATTERN == "lakehouse_federation" and CONNECTION_NAME:
    foreign_catalog = validate_identifier(SOURCE_NAME or CONNECTION_NAME, "foreign_catalog")

    # Create the foreign catalog
    print(f"Creating foreign catalog: {foreign_catalog}")
    print(f"Using connection: {CONNECTION_NAME}")

    spark.sql(f"""
        CREATE FOREIGN CATALOG IF NOT EXISTS {foreign_catalog}
        USING CONNECTION `{CONNECTION_NAME}`
    """)

    # List schemas in the foreign catalog
    schemas = spark.sql(f"SHOW SCHEMAS IN {foreign_catalog}").collect()
    print(f"\nSchemas discovered: {len(schemas)}")
    for s in schemas[:20]:
        tables = spark.sql(f"SHOW TABLES IN {foreign_catalog}.{s.databaseName}").collect()
        print(f"  {s.databaseName}: {len(tables)} tables")

    # Register as a data source for PII scanning
    spark.sql(f"""
        MERGE INTO {CATALOG}.bronze.data_sources t
        USING (SELECT
            '{foreign_catalog}' AS source_id,
            '{SOURCE_NAME or foreign_catalog}' AS source_name,
            'lakehouse_federation' AS source_type,
            'auto_loader' AS ingestion_pattern,
            '{foreign_catalog}' AS catalog_name,
            '' AS schema_name,
            '' AS landing_volume_path,
            '' AS owner_email,
            true AS is_active,
            current_timestamp() AS created_at,
            current_timestamp() AS updated_at
        ) s ON t.source_id = s.source_id
        WHEN NOT MATCHED THEN INSERT *
        WHEN MATCHED THEN UPDATE SET
            updated_at = current_timestamp(),
            is_active = true
    """)
    print(f"\nRegistered {foreign_catalog} as data source for PII scanning")

elif PATTERN == "lakehouse_federation":
    print("""
    To use Lakehouse Federation:

    1. Go to Catalog Explorer > External Data > Connections
    2. Click 'Create Connection'
    3. Select your database type (Postgres, MySQL, Snowflake, etc.)
    4. Provide connection details (host, port, credentials)
    5. Come back here and set:
       - connection_name = your connection name
       - source_name = what to call the foreign catalog

    Example for PostgreSQL:
      Connection Type: PostgreSQL
      Host: your-db.example.com
      Port: 5432
      Database: your_database
      User: read_only_user
      Password: (stored securely in UC)

    Then run this notebook with:
      ingestion_pattern = lakehouse_federation
      connection_name = your_connection_name
      source_name = client_crm
    """)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern 2: Lakeflow Connect (SaaS Apps)
# MAGIC
# MAGIC **Use for:** Salesforce, Workday, ServiceNow, SharePoint, HubSpot, Zendesk, SAP
# MAGIC
# MAGIC **What it does:** Creates a managed ingestion pipeline that continuously syncs data
# MAGIC from SaaS APIs into Delta tables. Handles auth, pagination, rate limiting, schema evolution.
# MAGIC
# MAGIC ### Setup via UI (recommended)
# MAGIC 1. Go to **Data Engineering > Lakeflow Connect**
# MAGIC 2. Select your SaaS source
# MAGIC 3. Configure auth (OAuth, API key, etc.)
# MAGIC 4. Select objects to sync
# MAGIC 5. Set target catalog/schema
# MAGIC
# MAGIC ### Setup via SQL

# COMMAND ----------

if PATTERN == "lakeflow_connect" and SOURCE_NAME:
    target_schema = validate_identifier(f"lfc_{SOURCE_NAME}", "schema")

    print(f"""
    Lakeflow Connect Setup for: {SOURCE_NAME}

    Run the following in your SQL editor after configuring the connection:

    -- Step 1: Create target schema
    CREATE SCHEMA IF NOT EXISTS {CATALOG}.{target_schema};

    -- Step 2: Create ingestion pipeline (Salesforce example)
    CREATE OR REFRESH STREAMING TABLE {CATALOG}.{target_schema}.accounts
    AS SELECT * FROM STREAM READ_SALESFORCE(
        connection => '{SOURCE_NAME}_connection',
        object => 'Account'
    );

    CREATE OR REFRESH STREAMING TABLE {CATALOG}.{target_schema}.contacts
    AS SELECT * FROM STREAM READ_SALESFORCE(
        connection => '{SOURCE_NAME}_connection',
        object => 'Contact'
    );

    -- Step 3: Register for PII scanning
    INSERT INTO {CATALOG}.bronze.data_sources VALUES (
        '{SOURCE_NAME}', '{SOURCE_NAME}', 'lakeflow_connect', 'lakeflow_connect',
        '{CATALOG}', '{target_schema}', '', '', true,
        current_timestamp(), current_timestamp()
    );
    """)

    # Register the source
    spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{target_schema}")
    spark.sql(f"""
        MERGE INTO {CATALOG}.bronze.data_sources t
        USING (SELECT
            '{SOURCE_NAME}' AS source_id,
            '{SOURCE_NAME}' AS source_name,
            'lakeflow_connect' AS source_type,
            'lakeflow_connect' AS ingestion_pattern,
            '{CATALOG}' AS catalog_name,
            '{target_schema}' AS schema_name,
            '' AS landing_volume_path,
            '' AS owner_email,
            true AS is_active,
            current_timestamp() AS created_at,
            current_timestamp() AS updated_at
        ) s ON t.source_id = s.source_id
        WHEN NOT MATCHED THEN INSERT *
        WHEN MATCHED THEN UPDATE SET updated_at = current_timestamp(), is_active = true
    """)
    print(f"Registered {SOURCE_NAME} for PII scanning (configure Lakeflow Connect pipeline separately)")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern 3: Auto Loader (File Sources)
# MAGIC
# MAGIC **Use for:** S3 buckets, ADLS containers, GCS buckets, HDFS, network shares
# MAGIC
# MAGIC **What it does:** Incrementally ingests new files as they arrive using Structured Streaming.
# MAGIC Handles schema inference, file tracking, exactly-once semantics.
# MAGIC
# MAGIC Already configured in the DLT medallion pipeline for the landing zone.
# MAGIC To add a new file source, drop files into a named subdirectory.

# COMMAND ----------

if PATTERN == "auto_loader" and SOURCE_NAME:
    landing_path = f"/Volumes/{CATALOG}/bronze/landing/{SOURCE_NAME}"
    safe_name = validate_identifier(SOURCE_NAME, "source_name")

    print(f"""
    Auto Loader Setup for: {SOURCE_NAME}

    1. Create the landing directory:
       databricks fs mkdirs dbfs:{landing_path}

    2. Drop CSV/JSON/Parquet files into:
       {landing_path}/

    3. The DLT medallion pipeline will auto-discover and ingest them.

    4. To add a dedicated Bronze table, add to pipelines/medallion.py:

       @dlt.table(name="source_{safe_name}")
       def source_{safe_name}():
           return _auto_loader_stream("{SOURCE_NAME}")
    """)

    # Register the source
    spark.sql(f"""
        MERGE INTO {CATALOG}.bronze.data_sources t
        USING (SELECT
            '{SOURCE_NAME}' AS source_id,
            '{SOURCE_NAME}' AS source_name,
            'csv_snapshot' AS source_type,
            'auto_loader' AS ingestion_pattern,
            '{CATALOG}' AS catalog_name,
            'bronze' AS schema_name,
            '{landing_path}' AS landing_volume_path,
            '' AS owner_email,
            true AS is_active,
            current_timestamp() AS created_at,
            current_timestamp() AS updated_at
        ) s ON t.source_id = s.source_id
        WHEN NOT MATCHED THEN INSERT *
        WHEN MATCHED THEN UPDATE SET updated_at = current_timestamp(), is_active = true
    """)
    print(f"Registered {SOURCE_NAME} for PII scanning")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Pattern 4: Unity Catalog (Already in Databricks)
# MAGIC
# MAGIC **Use for:** Tables already in Unity Catalog from other teams/workspaces
# MAGIC
# MAGIC **What it does:** Simply registers an existing catalog.schema for PII scanning.
# MAGIC No data movement needed — the scanner reads directly from UC.

# COMMAND ----------

if PATTERN == "unity_catalog" and SOURCE_NAME:
    dbutils.widgets.text("uc_catalog", "", "Source UC Catalog")
    dbutils.widgets.text("uc_schema", "", "Source UC Schema")

    uc_cat = dbutils.widgets.get("uc_catalog").strip()
    uc_sch = dbutils.widgets.get("uc_schema").strip()

    if uc_cat and uc_sch:
        uc_cat = validate_identifier(uc_cat, "uc_catalog")
        uc_sch = validate_identifier(uc_sch, "uc_schema")

        # Verify access
        tables = spark.sql(f"SHOW TABLES IN {uc_cat}.{uc_sch}").collect()
        print(f"Found {len(tables)} tables in {uc_cat}.{uc_sch}")
        for t in tables[:20]:
            print(f"  {t.tableName}")

        # Register
        spark.sql(f"""
            MERGE INTO {CATALOG}.bronze.data_sources t
            USING (SELECT
                '{SOURCE_NAME}' AS source_id,
                '{SOURCE_NAME}' AS source_name,
                'unity_catalog' AS source_type,
                'unity_catalog' AS ingestion_pattern,
                '{uc_cat}' AS catalog_name,
                '{uc_sch}' AS schema_name,
                '' AS landing_volume_path,
                '' AS owner_email,
                true AS is_active,
                current_timestamp() AS created_at,
                current_timestamp() AS updated_at
            ) s ON t.source_id = s.source_id
            WHEN NOT MATCHED THEN INSERT *
            WHEN MATCHED THEN UPDATE SET updated_at = current_timestamp(), is_active = true
        """)
        print(f"Registered {uc_cat}.{uc_sch} for PII scanning")

# COMMAND ----------

# MAGIC %md
# MAGIC ## View Registered Sources

# COMMAND ----------

# MAGIC %sql
# MAGIC SELECT source_id, source_name, source_type, ingestion_pattern,
# MAGIC        catalog_name, schema_name, is_active, updated_at
# MAGIC FROM compliance_pack.bronze.data_sources
# MAGIC ORDER BY updated_at DESC

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## What happens next
# MAGIC
# MAGIC After registering a source:
# MAGIC 1. Run **02_Silver_Discovery** to scan for PII across all registered sources
# MAGIC 2. The scanner auto-discovers tables from each registered source
# MAGIC 3. PII findings populate `silver.pii_findings` with UC tags
# MAGIC 4. Compliance gaps populate `silver.compliance_gaps`
# MAGIC 5. View results in the **DPDP Compliance Dashboard**
# MAGIC
# MAGIC ### Comparison with old approach
# MAGIC
# MAGIC | Aspect | Old (Python connectors) | New (Databricks-native) |
# MAGIC |--------|------------------------|------------------------|
# MAGIC | Code | 400K lines across 28 connectors | 3 SQL patterns |
# MAGIC | Dependencies | Runtime pip install | None |
# MAGIC | Auth | Custom per-connector | Unity Catalog connections |
# MAGIC | Schema evolution | Manual | Auto (Federation/Lakeflow) |
# MAGIC | Lineage | None | Built-in UC lineage |
# MAGIC | Governance | Separate | UC tags, masking, audit |
