#!/usr/bin/env python3
"""One-shot: create the VASTDB schema + tables this demo's Generic Tables
point at.

Runs as polaris-db-admin (VASTDB_ADMIN_ACCESS_KEY_ID/
VASTDB_ADMIN_SECRET_ACCESS_KEY in polaris/.env.secrets), whose identity
policy carries tabular DDL over this one schema and nothing else. That is
deliberately not the STS signer identity: the signer may only call
AssumeRole, so its key vends nothing on its own. Neither is it the identity
Alice/Bob query with — theirs are separate, table-scoped keys. All of these
are created by vms/provision.py."""
import os

import pyarrow as pa
import vastdb

ENDPOINT = os.environ["VAST_S3_ENDPOINT"]
ACCESS_KEY = os.environ["VASTDB_ADMIN_ACCESS_KEY_ID"]
SECRET_KEY = os.environ["VASTDB_ADMIN_SECRET_ACCESS_KEY"]
BUCKET = os.environ["VAST_BUCKET"]
SCHEMA = os.environ.get("VASTDB_SCHEMA", "polaris_demo_db")

TABLES = {
    "shipments": pa.schema(
        [
            pa.field("shipment_id", pa.int32()),
            pa.field("order_id", pa.int32()),
            pa.field("carrier", pa.string()),
        ]
    ),
    "reviews": pa.schema(
        [
            pa.field("review_id", pa.int32()),
            pa.field("product_id", pa.int32()),
            pa.field("rating", pa.int32()),
            pa.field("comment", pa.string()),
        ]
    ),
    # Deliberate overlap with the Iceberg side's demo.order_items pattern:
    # both alice-db and bob-db get grants on this one.
    "inventory_events": pa.schema(
        [
            pa.field("event_id", pa.int32()),
            pa.field("sku", pa.string()),
            pa.field("delta", pa.int32()),
            pa.field("actor", pa.string()),
        ]
    ),
}


def main():
    session = vastdb.connect(endpoint=ENDPOINT, access=ACCESS_KEY, secret=SECRET_KEY)
    with session.transaction() as tx:
        bucket = tx.bucket(BUCKET)
        schema = bucket.create_schema(SCHEMA, fail_if_exists=False)
        # schema.tables() prints a benign "RPC failed: ... without elysium
        # enabled" warning per table while probing sorted-columns metadata
        # this cluster doesn't have enabled — harmless, table listing still
        # succeeds despite the noisy stderr line.
        existing = {t.name for t in schema.tables()}
        for name, arrow_schema in TABLES.items():
            if name in existing:
                print(f"table {SCHEMA}.{name} already exists, skipping")
                continue
            schema.create_table(name, arrow_schema)
            print(f"created table {SCHEMA}.{name}")
    print("VASTDB setup complete")


if __name__ == "__main__":
    main()
