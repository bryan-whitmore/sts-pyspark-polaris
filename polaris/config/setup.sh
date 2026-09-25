#!/bin/sh
set -e
apk add --no-cache jq >/dev/null

REALM="${POLARIS_REALM:-polaris_demo}"
CATALOG_NAME="${CATALOG_NAME:-sts_demo}"
BASE_LOCATION="s3://${VAST_BUCKET}/${VAST_BUCKET_PREFIX}"

fail() {
  echo "$1" >&2
  exit 1
}

echo "Obtaining root access token..."
TOKEN_RESPONSE=$(curl --fail-with-body -s -S -X POST http://polaris:8181/api/catalog/v1/oauth/tokens \
  -H 'Content-Type: application/x-www-form-urlencoded' \
  -d "grant_type=client_credentials&client_id=${ROOT_CLIENT_ID}&client_secret=${ROOT_CLIENT_SECRET}&scope=PRINCIPAL_ROLE:ALL") \
  || fail "Failed to obtain root access token"
TOKEN=$(echo "$TOKEN_RESPONSE" | jq -r '.access_token')
[ -n "$TOKEN" ] && [ "$TOKEN" != "null" ] || fail "Could not parse root access token: $TOKEN_RESPONSE"
echo "Obtained root access token"

echo "Creating catalog '${CATALOG_NAME}' with STS AssumeRole storage config..."
PAYLOAD=$(cat <<EOF
{
  "catalog": {
    "name": "${CATALOG_NAME}",
    "type": "INTERNAL",
    "readOnly": false,
    "properties": {
      "default-base-location": "${BASE_LOCATION}"
    },
    "storageConfigInfo": {
      "storageType": "S3",
      "roleArn": "${VAST_ROLE_ARN}",
      "region": "${VAST_S3_REGION}",
      "endpoint": "${VAST_S3_ENDPOINT}",
      "endpointInternal": "${VAST_S3_ENDPOINT}",
      "stsEndpoint": "${VAST_STS_ENDPOINT}",
      "pathStyleAccess": true,
      "allowedLocations": ["${BASE_LOCATION}"]
    }
  }
}
EOF
)
curl --fail-with-body -s -S -X POST http://polaris:8181/api/management/v1/catalogs \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -H "Polaris-Realm: ${REALM}" -d "${PAYLOAD}" \
  || fail "Failed to create catalog"
echo "Catalog created"

echo "Creating namespace 'demo'..."
curl --fail-with-body -s -S -X POST "http://polaris:8181/api/catalog/v1/${CATALOG_NAME}/namespaces" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -H "Polaris-Realm: ${REALM}" \
  -d '{"namespace": ["demo"], "properties": {}}' \
  || fail "Failed to create namespace demo"
echo "Namespace created"

# create_table <name> <iceberg-schema-json>
# Polaris requires a table to already exist before it can be the target of
# a table-scoped grant (see runbook.md) — so the admin (root) pre-creates
# empty tables here; Alice/Bob's notebooks only INSERT/SELECT into them.
create_table() {
  tname="$1"; schema="$2"
  curl --fail-with-body -s -S -X POST "http://polaris:8181/api/catalog/v1/${CATALOG_NAME}/namespaces/demo/tables" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -H "Polaris-Realm: ${REALM}" \
    -d "{\"name\": \"${tname}\", \"schema\": ${schema}}" \
    || fail "Failed to create table demo.${tname}"
}

echo "Pre-creating empty tables (customers, orders, order_items, products, events)..."
create_table customers '{"type":"struct","fields":[{"id":1,"name":"customer_id","required":false,"type":"int"},{"id":2,"name":"name","required":false,"type":"string"}]}'
create_table orders '{"type":"struct","fields":[{"id":1,"name":"order_id","required":false,"type":"int"},{"id":2,"name":"customer_id","required":false,"type":"int"},{"id":3,"name":"total","required":false,"type":"double"}]}'
create_table order_items '{"type":"struct","fields":[{"id":1,"name":"order_id","required":false,"type":"int"},{"id":2,"name":"item","required":false,"type":"string"},{"id":3,"name":"qty","required":false,"type":"int"},{"id":4,"name":"written_by","required":false,"type":"string"}]}'
create_table products '{"type":"struct","fields":[{"id":1,"name":"product_id","required":false,"type":"int"},{"id":2,"name":"name","required":false,"type":"string"},{"id":3,"name":"price","required":false,"type":"double"}]}'
create_table events '{"type":"struct","fields":[{"id":1,"name":"event_id","required":false,"type":"int"},{"id":2,"name":"description","required":false,"type":"string"}]}'
echo "Tables created"

# --- VASTDB extension: register the whitmore VAST Database's tables (see
# vastdb-setup service) in Polaris as Generic Tables — metadata only.
# Apache Polaris 1.7.0's Generic Tables API does not implement credential
# vending server-side (checked upstream source), so this gives unified
# catalog discovery only; actual read/write auth for these tables is
# enforced by VAST's own Identity Policy on alice-db/bob-db's separate,
# static keys (see runbook.md), not by Polaris.
echo "Creating namespace 'vastdb_demo'..."
curl --fail-with-body -s -S -X POST "http://polaris:8181/api/catalog/v1/${CATALOG_NAME}/namespaces" \
  -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
  -H "Polaris-Realm: ${REALM}" \
  -d '{"namespace": ["vastdb_demo"], "properties": {}}' \
  || fail "Failed to create namespace vastdb_demo"
echo "Namespace created"

# create_generic_table <name>
create_generic_table() {
  tname="$1"
  curl --fail-with-body -s -S -X POST "http://polaris:8181/api/catalog/polaris/v1/${CATALOG_NAME}/namespaces/vastdb_demo/generic-tables" \
    -H "Authorization: Bearer ${TOKEN}" -H "Content-Type: application/json" \
    -H "Polaris-Realm: ${REALM}" \
    -d "{\"name\": \"${tname}\", \"format\": \"vastdb\", \"base-location\": \"vastdb://${VAST_BUCKET}/${VASTDB_SCHEMA}/${tname}\", \"doc\": \"Read/written via the VAST DB Spark connector, not Polaris-vended\", \"properties\": {\"vastdb.bucket\": \"${VAST_BUCKET}\", \"vastdb.schema\": \"${VASTDB_SCHEMA}\", \"vastdb.table\": \"${tname}\", \"vastdb.endpoint\": \"${VAST_S3_ENDPOINT}\"}}" \
    || fail "Failed to create generic table vastdb_demo.${tname}"
}

echo "Registering VASTDB tables as Generic Tables (shipments, reviews, inventory_events)..."
create_generic_table shipments
create_generic_table reviews
create_generic_table inventory_events
echo "Generic tables registered"

# grant_generic_table <catalog_role> <table_name>
# Generic Tables reuse the Iceberg TABLE_* privilege enum (confirmed in
# Polaris source — there is no separate GenericTablePrivilege). Only
# TABLE_FULL_METADATA is meaningful here since there's no delegation-gated
# op for generic tables at 1.7.0 (no vending to gate).
grant_generic_table() {
  crole="$1"; table="$2"
  curl --fail-with-body -s -S -X PUT "http://polaris:8181/api/management/v1/catalogs/${CATALOG_NAME}/catalog-roles/${crole}/grants" \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"type\": \"table\", \"privilege\": \"TABLE_FULL_METADATA\", \"namespace\": [\"vastdb_demo\"], \"tableName\": \"${table}\"}" \
    || fail "Failed to grant ${crole} access to vastdb_demo.${table}"
}

# create_principal <name> -> prints "clientId clientSecret"
create_principal() {
  name="$1"
  resp=$(curl --fail-with-body -s -S -X POST http://polaris:8181/api/management/v1/principals \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"principal\": {\"name\": \"${name}\", \"properties\": {}}}") \
    || fail "Failed to create principal ${name}"
  cid=$(echo "$resp" | jq -r '.credentials.clientId')
  csec=$(echo "$resp" | jq -r '.credentials.clientSecret')
  [ -n "$cid" ] && [ "$cid" != "null" ] || fail "Failed to parse credentials for ${name}: $resp"
  echo "${cid} ${csec}"
}

create_role() {
  role="$1"
  curl --fail-with-body -s -S -X POST http://polaris:8181/api/management/v1/principal-roles \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"principalRole\": {\"name\": \"${role}\", \"properties\": {}}}" \
    || fail "Failed to create principal role ${role}"
}

create_catalog_role() {
  crole="$1"
  curl --fail-with-body -s -S -X POST "http://polaris:8181/api/management/v1/catalogs/${CATALOG_NAME}/catalog-roles" \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"catalogRole\": {\"name\": \"${crole}\", \"properties\": {}}}" \
    || fail "Failed to create catalog role ${crole}"
}

assign_principal_role() {
  principal="$1"; role="$2"
  curl --fail-with-body -s -S -X PUT "http://polaris:8181/api/management/v1/principals/${principal}/principal-roles" \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"principalRole\": {\"name\": \"${role}\"}}" \
    || fail "Failed to assign ${role} to ${principal}"
}

assign_catalog_role() {
  role="$1"; crole="$2"
  curl --fail-with-body -s -S -X PUT "http://polaris:8181/api/management/v1/principal-roles/${role}/catalog-roles/${CATALOG_NAME}" \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"catalogRole\": {\"name\": \"${crole}\"}}" \
    || fail "Failed to assign ${crole} to ${role}"
}

# grant_table <catalog_role> <table_name>
# TABLE_FULL_METADATA alone does NOT cover LOAD_TABLE_WITH_READ/WRITE_DELEGATION
# (the vended-credentials path Spark actually uses) — confirmed live via
# curl: a plain metadata-only load succeeds with just TABLE_FULL_METADATA,
# but requesting the table with the `X-Iceberg-Access-Delegation:
# vended-credentials` header 403s until TABLE_READ_DATA/TABLE_WRITE_DATA
# are also granted. All three together give full notebook functionality.
grant_table() {
  crole="$1"; table="$2"
  for privilege in TABLE_FULL_METADATA TABLE_READ_DATA TABLE_WRITE_DATA; do
    curl --fail-with-body -s -S -X PUT "http://polaris:8181/api/management/v1/catalogs/${CATALOG_NAME}/catalog-roles/${crole}/grants" \
      -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
      -d "{\"type\": \"table\", \"privilege\": \"${privilege}\", \"namespace\": [\"demo\"], \"tableName\": \"${table}\"}" \
      || fail "Failed to grant ${crole} ${privilege} on demo.${table}"
  done
}

# grant_catalog_wide <catalog_role> <privilege>
# NAMESPACE_LIST/TABLE_LIST have no per-namespace/per-table grant target
# (a namespace=[] grant is rejected as "must not be empty") — they're only
# grantable catalog-wide. Spark's SHOW NAMESPACES / SHOW TABLES need both,
# even though the actual per-table grants above still gate LOAD_TABLE.
# Tradeoff: this reveals *names* of tables a principal has no data access
# to (e.g. Alice can see "products" and "events" exist) — visibility only,
# not a data leak; confirmed LOAD_TABLE on a non-granted table still 403s.
grant_catalog_wide() {
  crole="$1"; privilege="$2"
  curl --fail-with-body -s -S -X PUT "http://polaris:8181/api/management/v1/catalogs/${CATALOG_NAME}/catalog-roles/${crole}/grants" \
    -H "Authorization: Bearer ${TOKEN}" -H "Polaris-Realm: ${REALM}" -H "Content-Type: application/json" \
    -d "{\"type\": \"catalog\", \"privilege\": \"${privilege}\"}" \
    || fail "Failed to grant ${crole} catalog-wide ${privilege}"
}

echo "Creating principals, roles, and table-scoped grants for alice/bob..."
create_role "alice_role"
create_role "bob_role"
create_catalog_role "alice_catalog_role"
create_catalog_role "bob_catalog_role"

ALICE_CREDS=$(create_principal alice)
ALICE_ID=$(echo "$ALICE_CREDS" | cut -d' ' -f1)
ALICE_SECRET=$(echo "$ALICE_CREDS" | cut -d' ' -f2)
BOB_CREDS=$(create_principal bob)
BOB_ID=$(echo "$BOB_CREDS" | cut -d' ' -f1)
BOB_SECRET=$(echo "$BOB_CREDS" | cut -d' ' -f2)

assign_principal_role alice alice_role
assign_principal_role bob bob_role
assign_catalog_role alice_role alice_catalog_role
assign_catalog_role bob_role bob_catalog_role

# Alice: customers, orders, order_items. Bob: order_items, products, events.
# demo.order_items is the deliberate overlap between the two.
for t in customers orders order_items; do grant_table alice_catalog_role "$t"; done
for t in order_items products events; do grant_table bob_catalog_role "$t"; done

# VASTDB generic tables, same overlap pattern: alice gets shipments +
# inventory_events, bob gets reviews + inventory_events.
for t in shipments inventory_events; do grant_generic_table alice_catalog_role "$t"; done
for t in reviews inventory_events; do grant_generic_table bob_catalog_role "$t"; done

# Required for Spark's SHOW NAMESPACES / SHOW TABLES to work at all — see
# grant_catalog_wide() comment above for the visibility tradeoff.
grant_catalog_wide alice_catalog_role NAMESPACE_LIST
grant_catalog_wide alice_catalog_role TABLE_LIST
grant_catalog_wide bob_catalog_role NAMESPACE_LIST
grant_catalog_wide bob_catalog_role TABLE_LIST

mkdir -p /creds
cat > /creds/alice.env <<EOF
POLARIS_CLIENT_ID=${ALICE_ID}
POLARIS_CLIENT_SECRET=${ALICE_SECRET}
POLARIS_CATALOG_NAME=${CATALOG_NAME}
EOF
cat > /creds/bob.env <<EOF
POLARIS_CLIENT_ID=${BOB_ID}
POLARIS_CLIENT_SECRET=${BOB_SECRET}
POLARIS_CATALOG_NAME=${CATALOG_NAME}
EOF

echo ""
echo "=========================================="
echo "Polaris STS demo setup complete"
echo "=========================================="
echo "Catalog: ${CATALOG_NAME} (base location ${BASE_LOCATION}, roleArn ${VAST_ROLE_ARN})"
echo "Wrote /creds/alice.env and /creds/bob.env"
echo "=========================================="

touch /tmp/polaris-setup-done
tail -f /dev/null
