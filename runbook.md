# Polaris + STS AssumeRole Alice/Bob Demo — Runbook

**Validated end to end on 2026-10-22** against `selab-var-203`
(VAST 5.4.6.0, `main` VIPs `172.200.203.1`–`.6`): the VAST-side identities,
the Polaris bootstrap/catalog/grants, the STS AssumeRole path with
prefix-scoped vended credentials, the `ndb` VASTDB path, both overlaps, and
every expected-fail gate in both notebooks.

A lab cluster rebuild invalidates the VAST side of this demo wholesale —
users, access keys, AIM roles, identity policies, the bucket's contents, the
VASTDB schema, and the STS TLS certificate all go at once, and which of them
you notice first depends only on which layer you hit. **Re-probe before
debugging**: compare the cluster's uptime and `main` VIP range against
`polaris/.env`, then re-run `vms/provision.py` and `vms/fetch_sts_cert.sh`
rather than chasing the 403.

Directory: `~/bw/sts/` — `postgres/`, `polaris/`, `jupyter/`, each with its own
`docker-compose.yml`, plus `vms/`, which provisions and verifies the VAST side.
Run the steps below in order. Tags: `[VMS]` = against the VMS API (scripted
under `vms/`), `[HOST]` = shell/docker on this host, `[NOTEBOOK]` = inside a
Jupyter notebook cell.

See [README.md's "Appendix: Design rationale"](README.md#appendix-design-rationale)
for the full design rationale (why STS AssumeRole, why Postgres, why
table-scoped grants).

## 0. Prerequisites — cluster-specific values

Every real bucket name, endpoint/IP, role ARN, credential, and cert this
demo needs lives in a gitignored file, each with a tracked `*.example`
template to copy. Fill these in before step 1:

| What | Value (this lab) | Lives in |
|---|---|---|
| VAST S3 bucket | `whitmore` | `polaris/.env` → `VAST_BUCKET` |
| S3 data-plane endpoint | `http://172.200.203.1` | `polaris/.env` → `VAST_S3_ENDPOINT` |
| STS endpoint (hostname + VIP) | `https://sts.vastdata.com:443` → `172.200.203.1` | `polaris/.env` → `VAST_STS_ENDPOINT` / `VAST_STS_IP` |
| AIM Role ARN | `arn:vast:iam::default:role/polaris-demo` | `polaris/.env` → `VAST_ROLE_ARN` |
| VAST DB (ndb) endpoint(s) | `172.200.203.1`–`.6` | `polaris/.env` → `VASTDB_DATA_ENDPOINTS` (step 6 copies these into each Generic Table's properties; the notebooks read them back from Polaris rather than from a file of their own) |
| Postgres password | — | `postgres/.env.secrets` + `polaris/.env.secrets` (must match) |
| Polaris root client secret | — | `polaris/.env.secrets` → `ROOT_CLIENT_ID`/`ROOT_CLIENT_SECRET` |
| VMS admin token | — | `vms/.vms.env` → `VMS_ADDRESS`/`VMS_TOKEN` (needed only by `vms/provision.py`) |
| STS signer key (`polaris-sts`) | — | `polaris/.env.secrets` → `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` (written by step 2) |
| VASTDB DDL key (`polaris-db-admin`) | — | `polaris/.env.secrets` → `VASTDB_ADMIN_ACCESS_KEY_ID`/`VASTDB_ADMIN_SECRET_ACCESS_KEY` (written by step 2) |
| Alice/Bob Polaris client creds | — | `jupyter/creds/alice.env`, `bob.env` (written by step 6) |
| Alice/Bob VASTDB keys | — | `jupyter/creds/alice_db.env`, `bob_db.env` (written by step 2) |
| VAST STS leaf cert | — | `polaris/docker/vast-sts.pem` (fetched by `vms/fetch_sts_cert.sh`) |

None of the files in the left column of the table above are committed —
see `.gitignore`. The rest of this runbook states the real values inline
for narrative clarity (this is a lab-only demo), but the files above are
the actual source of truth and what a fresh checkout needs filled in.

## 1. `[VMS]` Confirm the whitmore view's policy flavor

STS AssumeRole and AIM Roles require the **S3 Native** policy flavor, so
confirm the View backing the `whitmore` bucket has it and has the S3
protocol enabled (see kb.vastdata.com "Security Token Service (STS) and IAM
Roles"; VAST's own object type is what this runbook calls an **AIM Role**,
to stay distinct from AWS IAM):

```bash
# view policy id, then its flavor
vastpy-cli get views/ --json | jq '.[] | select(.bucket=="whitmore") | {policy_id, protocols}'
vastpy-cli get viewpolicies/<policy_id>/ --json | jq '.[0].flavor'   # must be S3_NATIVE
```

The DATABASE protocol must also be on the view for the VASTDB half of the
demo (step 6b); `protocols` above should read `["S3","DATABASE"]`.

## 2. `[VMS]` Provision the VAST identities

```bash
cd ~/bw/sts
cp vms/.vms.env.example vms/.vms.env   # fill in VMS_ADDRESS + VMS_TOKEN, chmod 600
./vms/provision.py --dry-run           # report what would change
./vms/provision.py
```

`vms/provision.py` is idempotent and creates four VAST identities, each
holding only what its one job needs:

| Identity | Kind | May do |
|---|---|---|
| `polaris-sts` | User | Nothing but `sts:AssumeRole` — it has **no** identity policy at all. Its permanent key is what Polaris signs `AssumeRole` with, written to `polaris/.env.secrets`. |
| `polaris-demo` | AIM Role | Object read/write under `whitmore/polaris_demo/*` plus `ListBucket`/`GetBucketLocation` on the bucket, via identity policy `polaris-sts-demo`. This is what Polaris vends. |
| `polaris-db-admin` | User | Tabular DDL over `whitmore/polaris_demo_db` only. The `vastdb-setup` sidecar runs as this to create the schema and its three tables. |
| `alice-db`, `bob-db` | Users | Read/write on their own VASTDB tables and the shared `inventory_events`, nothing else. Keys written to `jupyter/creds/{alice,bob}_db.env`. |

The split matters: a leak of the signer's key vends nothing by itself, and
the role it assumes cannot reach the rest of the bucket. Verify both halves
of that claim in step 3 before configuring Polaris.

Four things about the VMS API this script exists to encode:

1. **The role's identity-policy attachment writes as `s3_policies` and reads
   back as `s3_policies_ids`.** Sending `s3_policies_ids` on `POST` or
   `PATCH iamroles/` is accepted with a 200 and silently ignored, leaving a
   role with no identity policy — which fails later, as an `AccessDenied` on
   every S3 call made with the vended credentials, nowhere near the cause.
   Check `s3_policies_ids` on a `GET` after creating a role.
2. **Identity-policy `Resource` entries are bare bucket and prefix strings**
   (`whitmore/polaris_demo/*`), not full ARNs.
3. **`s3:TabularCreateSchema` is checked against the schema being created**,
   not against the bucket — the 403 names `@s3_object_name=<schema>`. It
   belongs on a statement whose `Resource` is `bucket/schema`. The same shape
   applies to `s3:TabularListTables` (step 6b).
4. **The Trust Policy can name the principal**:
   `{"Principal": {"User": ["polaris-sts"]}, "Action": "sts:AssumeRole"}` is
   enough, and the assuming user needs no identity policy of its own. VAST's
   documented example uses `Principal: "*"`, which is far broader than this
   demo needs.

Two things the VMS UI would also have you do, which are **not** required:
changing the bucket owner to the role (`--bucket-owner-type ROLE`) — the
vended credentials work against a bucket still owned by a user, and leaving
it alone avoids disturbing other work on a shared bucket — and granting the
role `s3:*` on `*`.

Then refresh the STS certificate, which a cluster rebuild replaces:

```bash
./vms/fetch_sts_cert.sh
```

Polaris's Java AWS SDK validates the chain itself, so this cert is baked
into the image's JVM truststore with `keytool` (`polaris/docker/Dockerfile`)
— `update-ca-trust extract` silently drops it, since it is not a CA cert.
The hostname in `VAST_STS_ENDPOINT` has to match one of the cert's SAN
entries; see the README on why that is a made-up `sts.vastdata.com`.

## 3. `[HOST]` Pass/fail: raw AssumeRole + S3 check

Prove the role works standalone before touching Polaris — there is no value
in debugging Polaris storage config against a broken AIM Role:

```bash
./vms/verify_sts.sh
```

It assumes the role with the signer key, then with the returned temporary
credentials lists, puts, gets and deletes under `whitmore/polaris_demo/`,
and checks that the same credentials are **refused** outside that prefix.
All six lines must read `PASS`.

STS requires **HTTPS on port 443** here: plain `http://` on port 80, which
is fine for ordinary S3 calls, is rejected for `AssumeRole` with a generic
`AccessDenied` however the policies read. The cert is self-signed, hence
`--no-verify-ssl` on the CLI path.

VAST accepts both the newer 5-field ARN (`arn:vast::<tenant>:role/<name>`)
and the older 6-field form (`arn:vast:iam::<tenant>:role/<name>`).
**Use the 6-field form everywhere**, as `polaris/.env` does: Apache Polaris
1.7.0 hardcodes a 6-field ARN regex and rejects the 5-field VAST form with
`Invalid role ARN format` (apache/polaris#5099, open upstream).

If `AssumeRole` itself fails, check that the signer user named in
`polaris/.env.secrets` is the one the role's Trust Policy names, and that
the role exists on the tenant in the ARN. If `AssumeRole` passes but the S3
calls do not, the role almost certainly has no identity policy attached —
see point 1 in step 2.

## 4. `[HOST]` Create the shared docker network

```bash
docker network create sts_net
```

(One-time; skip if it already exists.)

## 5. `[HOST]` Start Postgres

```bash
cd ~/bw/sts/postgres
docker compose up -d
```

**Pass/fail:**
```bash
docker compose exec postgres pg_isready -U polaris -d polaris
```

## 6. `[HOST]` Start Polaris (bootstrap → server → catalog/principal setup)

```bash
cd ~/bw/sts/polaris
docker compose build polaris vastdb-setup   # picks up a refreshed STS cert
docker compose up
```

This runs `polaris-bootstrap` (creates the Postgres schema + root
principal), then `polaris` (the server) and `vastdb-setup` (creates the
VASTDB schema/tables for the VASTDB extension below) in parallel, then
`polaris-setup` (creates the catalog with the STS storage config, the
`demo` namespace, the 5 Iceberg tables, the `vastdb_demo` namespace +
Generic Table registrations, and the `alice`/`bob` principals + grants for
both — see `polaris/config/setup.sh`). It also writes
`../jupyter/creds/alice.env`/`bob.env`.

Three bugs found and fixed while getting this step to pass (all baked into
the files now, documented here so we don't lose why):
1. **Tables must exist before they can be table-scoped-granted.** Polaris
   rejects a grant on a nonexistent table with 404. `setup.sh` now has the
   admin (root) pre-create all 5 empty tables (schema only) right after the
   namespace, *before* any grants — Alice/Bob's notebooks only
   insert/select into pre-existing tables, they don't create them.
2. **Polaris's Java AWS SDK client needs to actually trust and hostname-
   verify VAST's STS cert**, not just have the CLI's `--no-verify-ssl`
   available. `polaris/docker/Dockerfile` builds a custom Polaris image
   that imports VAST's STS cert (`polaris/docker/vast-sts.pem`, refreshed by
   `vms/fetch_sts_cert.sh`) into the JVM truststore with `keytool` —
   `update-ca-trust extract` silently drops it since it's not a CA cert, so
   that path doesn't work. **The cert is per cluster build**, so a rebuilt
   cluster serves a different one and every Polaris STS call fails chain
   validation until the pem is re-fetched and the image rebuilt. Separately, the
   cert's SAN doesn't cover the raw VIP, only `*.vastdata.com` and other
   IPs — `polaris/docker-compose.yml` adds an `extra_hosts` entry mapping a
   made-up `sts.vastdata.com` (matches the wildcard SAN) to the real VIP,
   and `VAST_STS_ENDPOINT` in `.env` uses that hostname. Polaris has no
   config-level way to skip TLS verification (checked upstream source —
   apache/polaris#5099-adjacent gap), so this was the only fix that didn't
   require patching Polaris itself.
3. **Table-scoped grants alone aren't enough for Spark to browse the
   catalog.** `SHOW NAMESPACES`/`SHOW TABLES` need `NAMESPACE_LIST` and
   `TABLE_LIST` — but those privileges have no per-namespace/per-table
   grant target (Polaris rejects `"namespace": []` as empty), only
   catalog-wide. `setup.sh` now grants both, catalog-wide, to both
   `alice_catalog_role` and `bob_catalog_role`. Tradeoff: this lets Alice
   see table *names* she has no data access to (e.g. `products`,
   `events`) — confirmed `LOAD_TABLE` on those still 403s, so it's a
   visibility leak only, not a data leak. Acceptable for this demo.
4. **`TABLE_FULL_METADATA` doesn't cover the vended-credentials data path.**
   A plain metadata-only table load works with just that privilege, but
   Spark's actual read/write path sends `X-Iceberg-Access-Delegation:
   vended-credentials`, which triggers a separate
   `LOAD_TABLE_WITH_READ_DELEGATION`/`..._WRITE_DELEGATION` check —
   confirmed via curl to 403 until `TABLE_READ_DATA` and `TABLE_WRITE_DATA`
   are *also* granted per table. `grant_table()` in `setup.sh` now grants
   all three privileges per table.

**Pass/fail:**
```bash
curl --fail http://localhost:8182/q/health
curl -s -X POST http://localhost:8181/api/catalog/v1/oauth/tokens \
  -d 'grant_type=client_credentials&client_id=root&client_secret=s3cr3t&scope=PRINCIPAL_ROLE:ALL'
# then, with that token:
curl -s http://localhost:8181/api/management/v1/catalogs \
  -H "Authorization: Bearer <token>" -H "Polaris-Realm: polaris_demo" | jq .
```
Confirm the catalog exists and `jupyter/creds/alice.env` / `bob.env` no
longer say `CHANGE_ME`.

Persistence check (do this once, then move on): `docker compose restart
polaris` and re-run the catalogs GET above — the catalog must still be there,
proving it's really on Postgres and not falling back to in-memory.

## 6b. VASTDB extension — `whitmore` is also a VAST Database

`whitmore` isn't just an S3 bucket — it's also backed by a VAST Database
(confirmed: `shipments`/`reviews`/`inventory_events` created successfully
via the `vastdb` Python SDK against the same `whitmore` bucket, schema
`polaris_demo_db`, endpoint `172.200.203.1`, alongside pre-existing schemas
`kafka_topics`/`s1` from other work in `~/bw`).

**Why this isn't STS-vended like the Iceberg tables:** checked both sides
directly against `apache-polaris-1.7.0` source and VAST's own connector
docs/SDK:
- Polaris's Generic Tables API has the *wire format* for credential
  vending (`storage-access-configs` with `s3.session-token` etc.) but the
  server handler never populates it — confirmed live, a real
  `POST .../generic-tables` call returns `"storage-access-configs":[]`.
  Polaris's own docs also list this explicitly: "Generic tables APIs do
  not support credential vending." Metadata registration only.
- Even if it did: VAST's Spark connector (`ndb`/`VastCatalog`) and the
  `vastdb` Python SDK only accept a static access/secret key pair — no
  `session_token` field exists anywhere in their config surface, so a
  temporary STS credential has nowhere to go even by hand.

**Bug found and fixed getting the Generic Tables endpoint path right:**
the OpenAPI spec and the quickstart's own printed banner both suggest
`/api/polaris/v1/...`, and that 404s. The real, confirmed-working mount is
under the *catalog* REST prefix, not a separate one:
`POST /api/catalog/polaris/v1/{catalog}/namespaces/{namespace}/generic-tables`
— found by calling the catalog's own `/api/catalog/v1/config` discovery
endpoint and reading the literal `endpoints` list it returns, rather than
trusting the spec/docs. `setup.sh` uses this path now.

So the design here is: Polaris gives unified **catalog discovery**
(`vastdb_demo` namespace, Generic Tables `shipments`/`reviews`/
`inventory_events`), but **actual read/write authorization is enforced by
VAST directly**, via two new, separate, narrowly-scoped VAST access keys —
not by Polaris.

Discovery here does *not* mean `SHOW TABLES`. Generic Tables are a
different entity subtype from Iceberg tables (confirmed in
`polaris_schema.entities`: both `type_code` 7, but `sub_type_code` 4 for
generic vs 2 for Iceberg), and the Iceberg REST `listTables` endpoint
returns only the Iceberg subtype. Generic Tables have their own
list/load endpoints, which the `/api/catalog/v1/config` `endpoints` list
confirms:

```
GET polaris/v1/{prefix}/namespaces/{namespace}/generic-tables
GET polaris/v1/{prefix}/namespaces/{namespace}/generic-tables/{generic-table}
```

Reaching them from a notebook is an HTTP call, not a Spark catalog
operation — Iceberg's `SparkCatalog` speaks only the Iceberg endpoints,
and would try to load a `vastdb://` location as Iceberg anyway. Measured
behavior of those two endpoints as `alice`/`bob` (2026-09-28):

| | `shipments` | `reviews` | `inventory_events` |
|---|---|---|---|
| list (both principals) | visible | visible | visible |
| load as `alice` | 200 | **403** | 200 |
| load as `bob` | **403** | 200 | 200 |

So the per-table `TABLE_FULL_METADATA` grants *are* load-bearing — the
403s name `op LOAD_TABLE` — while listing is catalog-wide `TABLE_LIST` and
leaks table names only, exactly the same tradeoff as the Iceberg side in
step 6. A load returns `storage-access-configs: []` plus all five
`vastdb.*` properties, which is enough to build the `ndb` identifier
without hardcoding bucket or schema anywhere in the notebook.

**This is what the notebooks now do.** Both call these two endpoints in
the cell before the `SparkSession` is built — before, because the session
needs `spark.ndb.endpoint` / `data_endpoints`, which come from the
registration. The cell produces:

- `registered` — `{name: properties}` for every table the principal can
  load, and an `ndb(name)` helper that builds the fully-qualified
  identifier from `vastdb.bucket`/`vastdb.schema`/`vastdb.table`;
- `denied` — names Polaris listed but refused to load, used by the last
  cell's pass/fail check;
- `DB`, `VASTDB_ENDPOINT`, `VASTDB_DATA_ENDPOINTS`, read off the first
  loadable registration.

Nothing about the VAST DB layout is written in the notebooks any more, and
a registration that drifts from reality now fails visibly instead of being
silently overridden by a hardcoded value. `vastdb.data_endpoints` is our
own property name, not a VAST DB convention; registrations made before it
was added fall back to the single control endpoint, so the notebook prints
`1 data endpoint(s)` until `polaris-setup` is re-run.

The final VASTDB cell checks *both* layers refusing the same table: the
name appearing in `denied` is Polaris enforcing the grant, and the write
attempt that follows is VAST enforcing the Identity Policy. The identifier
for that write has to be built by hand, since `ndb()` can only produce
identifiers for tables Polaris was willing to describe.

**The two scoped VASTDB identities** (`alice-db`, `bob-db`) are created by
`vms/provision.py` in step 2, along with their identity policies and access
keys. Each gets read/write on its own table plus the shared
`inventory_events`, and nothing else:

| | `shipments` | `reviews` | `inventory_events` |
|---|---|---|---|
| `alice-db` | allow | — | allow |
| `bob-db` | — | allow | allow |

Verify the grants against the raw `vastdb` SDK before involving Spark:

```bash
.bw/bin/python vms/verify_vastdb.py
```

Each principal must load, select and insert on its two tables and be
refused on the third. The inserts are real rows, as the notebooks' are —
reset the VASTDB tables if a demo needs clean counts.

Action names and the `bucket/schema/table` resource format are from VAST's
"Managing permissions for accessing VAST tabular databases" KB doc.
Table-level scoping (not just schema-level) is explicitly supported, which
is what makes the same overlap pattern as the Iceberg side possible here.

Four actions these policies need that the KB's example policy does not
show, each found as a separate 403 and each isolated against the `vastdb`
SDK directly rather than by rebuilding a notebook:

1. **Plain `s3:ListBucket` on the bucket.** With only `s3:Tabular*`
   actions, a raw `aws s3api head-bucket` 403s identically to the Tabular
   calls — the gap is not Tabular-specific: VAST's
   `HeadBucket`/tabular-session-open path needs `s3:ListBucket` first.
2. **The schema object itself as a `Resource`.** "List tables in schema X"
   checks permission on the schema object, not on a `schema/*` "children
   of" pattern — the 403 names `@s3_object_name=polaris_demo_db` with no
   trailing wildcard. Grant both `whitmore/polaris_demo_db` and
   `whitmore/polaris_demo_db/*`.
3. **`s3:TabularGetTableStats`.** Loading a table object — which the SDK
   does internally before any select or insert — issues
   `GET .../<table>?stats`, a distinct check the table-scoped statement
   would otherwise not grant.
4. **`s3:TabularListColumns`.** Past `?stats`, loading hits
   `GET .../<table>?column` for the table's schema, a fourth action.

The pattern worth carrying forward: merely *loading* a table chains through
`TabularGetTableStats` and `TabularListColumns` as separate checks before
`TabularQueryData` is ever reached.

**A table-scoped principal cannot list the whole schema.** `schema.tables()`
in `vastdb` 2.0.14.1 eagerly loads per-table stats while listing, so it
raises `Forbidden` on the first table the principal has no grant on —
table-scoped grants and a bare schema listing are mutually exclusive in
this SDK. Nothing in the demo needs it: the notebooks discover tables
through Polaris's Generic Tables API and address them by name, and
`vms/verify_vastdb.py` does the same. Only `polaris-db-admin`, which holds
schema-wide DDL, can list.


## 7. `[HOST]` Start Jupyter

```bash
cd ~/bw/sts/jupyter
docker compose up -d --build
```

**This step needed real rework — four more bugs found and fixed getting
the `ndb` catalog to actually load, on top of everything in step 6b:**

5. **Wrong base image entirely.** The original `quay.io/jupyter/
   pyspark-notebook:spark-3.5.3` image is pinned to **Scala 2.12** — but
   every VAST Spark-3.5.x connector release is **Scala 2.13-only**
   (checked all 20 `spark3-vast-3.5.x` release tags on GitHub, no `_2.12`
   variant exists anywhere). Confirmed live:
   `NoSuchMethodError: scala.PartialFunction.fromFunction`. Fix: switched
   the base image to `quay.io/jupyter/scipy-notebook` (no bundled Spark)
   and install Spark 3.5.3 + Scala 2.13 ourselves via `setup_spark.py`
   (copied from jupyter/docker-stacks, same script
   `~/vast-docker-compose-examples/jupyter-pyspark` already uses for
   exactly this reason) — and switched the Iceberg jars to their `_2.13`
   builds to match.
6. **`spark.driver.extraClassPath` from `SparkSession.builder.config()`
   silently does nothing in client/local mode** — the driver JVM has
   already started by the time Python runs; Spark's own docs say this
   explicitly. Confirmed live: `Cannot find catalog plugin class for
   catalog 'ndb'` even though the jar and class were genuinely present.
   Fix: moved this to a static `jupyter/docker/spark-defaults.conf`
   (read before JVM launch) instead of the notebook's `.config()` calls.
7. **Classpath order matters for jar collisions.** Spark bundles its own
   ancient `guava-14.0.1.jar`; the connector needs `guava-31.0.1-jre.jar`
   (has `Splitter.splitToStream`, added in Guava 28+). Listing
   `/usr/local/spark/jars/*` before the connector's subdirectory let the
   old one win. Confirmed live: `NoSuchMethodError:
   ...Splitter.splitToStream`. Fix: connector directory listed **first**
   in `spark-defaults.conf`.
8. **The connector's zip bundles a few jars that partially, incompatibly
   overlap Spark's own.** `netty-buffer`/`netty-common-4.1.114.Final.jar`
   (only 2 of the ~18 netty jars Spark needs at 4.1.96.Final — breaks
   Arrow's netty-based allocator: confirmed live, `NoSuchFieldError:
   chunkSize` inside `PooledByteBufAllocatorL`) and
   `arrow-compression-18.0.0.jar` (newer Arrow release than Spark's
   bundled 12.0.1, not needed for basic insert/select). Fix: `rm` those
   three jars from the connector's unzipped directory right after
   unzipping, in the Dockerfile.

After all four fixes, `SHOW TABLES IN ndb.\`whitmore\`.\`polaris_demo_db\``
gets **past every classpath/dependency error** and reaches VAST for real.
The Identity Policy itself then needed four more iterative fixes (missing
plain `s3:ListBucket`, schema-object-itself resource form,
`TabularGetTableStats`, `TabularListColumns` — see step 6b, all confirmed
fixed live via the `vastdb` SDK directly). Both notebooks now run
correctly end-to-end, including the VASTDB cells.

9. **Default Spark logging is very noisy for this demo** —
   `rootLogger.level=info` (Spark's own template default, and there was no
   active `log4j2.properties` at all before this, only the `.template`)
   means pages of Spark/Hadoop/connector INFO lines per cell. Fixed with
   a `jupyter/docker/log4j2.properties` (based on Spark's own template,
   root level turned down to `warn`) copied into
   `/usr/local/spark/conf/log4j2.properties` at build time. Confirmed
   live: a trivial `SparkSession` + query went from dozens of INFO lines
   to 4.
10. **`spark.task.maxFailures` was never set, so Spark's local-mode
    default of 4 applied.** The VAST connector's `newWriteBuilder` rejects
    any write outright once it sees `maxFailures != 1`
    (`SparkConfValidator`, "Unsafe Spark configuration blocks write") —
    and, found the hard way during a from-scratch re-verification pass,
    a value of 4 also lets a transient failed task silently *retry* a
    non-idempotent `INSERT` against both the Iceberg and VASTDB sides,
    producing duplicate rows with no error at all when the retry happens
    to succeed. Confirmed live on both failure modes. Fix: both
    notebooks' `SparkSession.builder` now set
    `.config('spark.task.maxFailures', '1')`.
11. **The VAST connector logs ERROR stack traces for tables that are not
    its own.** Its Spark parser extension and resolution rule
    (`ndb.NDBParser`, `ndb.view.NDBTablesResolutionRule`) probe every
    relation Spark resolves, so each Iceberg `demo.*` query emits
    `could not find DataSource for provider: demo` and
    `Failed to resolve UnresolvedRelation as a RCLS-enabled table`,
    with full stack traces, before falling through correctly. The
    connector also requests row/column-level security metadata on every
    VASTDB table access, which a cluster without RCLS configured answers
    with a 500 — another WARN and stack trace per query, via
    `spark.sql.catalog.ndb.VastCatalogUtils`. All of it is noise, and
    enough of it that a working demo reads as broken. Fix: those three
    loggers are set to `off` in `jupyter/docker/log4j2.properties`.
    Measured on a full `Alice.ipynb` run: the executed notebook went from
    183 KB to 39 KB and from several hundred stack frames to one, with all
    three expected-fail gates still passing.
    `com.vastdata.client.VastClient` is deliberately left alone — it
    carries the genuine 403 that explains the VASTDB expected-fail cells.
    Its remaining one-liners (a 500 for the row-column-security probe, a
    400 for `LIST_SORTED_BY` on a cluster without Elysium) are expected
    here and are not failures.

**Pass/fail:** `curl --fail http://localhost:8888` (token is `sts-demo`, see
`jupyter/docker-compose.yml`).

## 8. `[NOTEBOOK]` Run `Alice.ipynb`

Open http://localhost:8888/lab, token `sts-demo`, run `notebooks/Alice.ipynb`
top to bottom. Confirms:
- Session builds against the Polaris REST catalog with vended credentials,
  *and* the `ndb` VASTDB catalog with alice-db's static key, in one session.
- Alice can create/read/write `demo.customers`, `demo.orders`,
  `demo.order_items` (Iceberg, Polaris-vended).
- The discovery cell lists all three Generic Tables but loads only
  `shipments` and `inventory_events`, printing `reviews` under
  `metadata denied (403)` — see step 6b for the full matrix.
- Alice can read/write VASTDB `shipments` and `inventory_events` via the
  `ndb` catalog (static key, VAST-enforced, not Polaris), addressed by
  identifiers built from the registration rather than hardcoded.
- The write to `demo.products` is **rejected** by Polaris (prints `PASS`).
- The write to VASTDB `reviews` is **rejected twice over** (prints two
  `PASS` lines): Polaris refused its metadata in the discovery cell, and
  VAST refuses the write itself — two independent enforcement layers,
  neither of them the Iceberg check above.

VAST's refusal of that write surfaces as Spark
`AnalysisException: TABLE_OR_VIEW_NOT_FOUND`, not a permission error: the
connector 403s on `LIST_COLUMNS` while resolving the table (visible in the
`VastClient` log line above it) and reports the unresolvable name instead.
A denial, not a missing table.

**A notebook session whose token has gone dead needs a kernel restart — a
re-run will not do it.** Polaris issues 1-hour tokens (no token-broker
config here, so the Quarkus default; measurable as `iat`→`exp` on the JWT
from `/v1/oauth/tokens`). The hour is not by itself a ceiling: Iceberg
refreshes the token in a background thread, measured on 2026-10-01 to
carry a session cleanly through t=60min and t=70min. What kills a session
is that refresh chain breaking — see the two causes below. Once it is
broken, every `polaris` catalog call in that session 401s — and Polaris
returns 401 with a **zero-byte body**, so
Iceberg's `ErrorHandlers` logs `Unable to parse error response` /
`No content to map due to end-of-input ... at [Source: (String)""]`
before the real `SHOW NAMESPACES` failure. That WARN is a symptom of an
expired session, not a parse bug to chase.

Re-running cell 3 does **not** fix it:
`SparkSession.builder...getOrCreate()` returns the *existing* session, and
its Iceberg catalog object was already built around the now-dead auth
session, so the fresh `credential` passed to the builder is ignored. Cell
3 appears to succeed and the next catalog call fails identically.

**Stop and restart the kernel**, then run cells 1-3, so that cell 1
fetches a new token and `getOrCreate` has no live session to hand back.
(Confirmed live on 2026-10-01: a kernel restart cleared the failure on the
stale session below. `spark.stop()` before re-running cell 3 is the same
fix without losing the kernel's other state.)

Diagnose it without guessing — the Spark UI reports the session's age, and
anything over an hour explains itself:

```bash
curl -s http://localhost:4040/api/v1/applications | jq '.[].attempts[].startTime'
```

(Hit live on 2026-10-01: an `alice-sts-demo` session started
2026-09-28T15:52 was still bound to port 4040 and had been failing every
Polaris call for three days, while fresh runs against the same cluster
passed.) The `ndb`/VASTDB cells keep working in such a session — that key
is static and never expires — so a half-dead notebook where only the
Iceberg cells fail is this, not a VASTDB problem.

Two things break the refresh chain, and the first is the one that actually
bit on 2026-09-28:

1. **Restarting or rebuilding Polaris under a live notebook.** The dead
   session above started at 15:52:29; `sts-polaris` reports
   `StartedAt 16:00:18` the same day with `RestartCount 0` — recreated by
   hand eight minutes into that session, as part of this file's
   2026-09-28 rebuild. A token cannot be refreshed against a Polaris that
   no longer recognizes it, and a `down -v` rebuild also re-bootstraps the
   realm, so every notebook open at the time is dead with no way back.
   **Restart the kernels of any open notebook after touching the
   `polaris/` or `postgres/` stacks.** (The timing and mechanism line up;
   whether the signing keys specifically are regenerated per boot was not
   separately tested.)
2. **A long-idle session**, if the refresh thread stops or its scheduled
   refresh lapses. Not reproduced in isolation — the 70-minute probe above
   kept its session alive, but that probe never had Polaris pulled out
   from under it, and its refresh thread runs whether or not cells are
   being executed, so it does not separate "idle" from "active."

So the expiry window alone is not a reason to restart a kernel mid-demo.
A Polaris restart is.

**Running these headless** (`jupyter nbconvert --execute`, for a scripted
re-validation rather than the browser): `pyspark` is only on `PYTHONPATH`
because the server process sourced
`before-notebook.d/10spark-config.sh` at container startup — a plain
`docker exec` shell has none of it, and cell 3 dies with
`ModuleNotFoundError: No module named 'pyspark'`. Source the hook first:

```bash
docker exec sts-jupyter bash -lc \
  'source /usr/local/bin/before-notebook.d/10spark-config.sh
   cd /home/jovyan/notebooks && jupyter nbconvert --to notebook --execute \
     --ExecutePreprocessor.timeout=1200 --output-dir=/tmp/revalidate \
     --output Alice.out.ipynb Alice.ipynb'
```

**The expected-fail cells verify *why* the write failed, not just that it
did.** They used to wrap the forbidden write in `except Exception`, which
caught any failure at all — a session that never built still printed
`PASS: write to demo.products correctly rejected`, and cell 11 printed
both its `PASS` lines, off a bare `NameError: name 'spark' is not
defined`. Seen live during the 2026-10-01 pass, from the missing-
`PYTHONPATH` failure above; an expired token (above that) fakes a PASS the
same way. A gate that passes while the demo is broken is worse than no
gate.

Both cells now catch only `(Py4JJavaError, AnalysisException)` and
re-`raise` unless the message carries the markers of a real refusal —
`ForbiddenException` plus `not authorized for op` on the Iceberg side, and
on the VASTDB side the forbidden table's name plus one of
`TABLE_OR_VIEW_NOT_FOUND` / `AccessDenied` / `Forbidden` / `not allowed` /
`403`. Anything else fails the cell loudly. Verified both directions on
2026-10-01: all four gates still `PASS` against live denials, and with
`pyspark` off the path the cells raise instead of printing `PASS`.

Keep the markers in step with reality — they are matched against exception
*text*, so a Polaris or connector upgrade that rewords a denial turns a
real PASS into a raised cell. That is the intended failure direction
(loud, not silent), but it is a thing to re-check after an upgrade.

Cell 11's first line, `PASS (Polaris)`, legitimately still prints in a
broken session: it asserts against `denied` from the pure-HTTP discovery
cell, which needs no Spark. Only its second, `PASS (VAST)`, depends on the
session.

## 9. `[NOTEBOOK]` Run `Bob.ipynb`

Same, for Bob. Confirms:
- Bob can create/read/write `demo.products`, `demo.events`,
  `demo.order_items` (Iceberg), and VASTDB `reviews`/`inventory_events`.
- `demo.order_items` and VASTDB `inventory_events` both show Alice's row
  from step 8 alongside Bob's own — proof of the intentional overlap on
  *both* the Iceberg and VASTDB sides.
- The writes to `demo.customers` (Iceberg) and VASTDB `shipments` are both
  **rejected** (prints `PASS` for each; `shipments` twice, once per layer).
- Re-run Alice's `order_items`/`inventory_events` cells afterward to see
  Bob's rows too.

## Teardown (when done iterating)

```bash
cd ~/bw/sts/jupyter && docker compose down
cd ~/bw/sts/polaris && docker compose down
cd ~/bw/sts/postgres && docker compose down   # add -v to also drop the Postgres volume
docker network rm sts_net
```

## Known open items

- `polaris-setup` writes credentials directly into `../jupyter/creds/` via a
  bind mount across compose projects — if that turns out to be fragile,
  fall back to copying the clientId/secret it prints to stdout by hand.
- **Credential files the Jupyter container reads are group-readable, not
  0600.** The image runs as `jovyan` (uid 1000, gid 100 `users`) while the
  host account is uid 1001, so a 0600 file written on the host is simply
  unreadable inside the container and the notebook fails on a missing key.
  `vms/provision.py` writes `jupyter/creds/*_db.env` as `0640` group
  `users`, which the host account is also a member of. Secrets only Polaris
  reads (`polaris/.env.secrets`) stay 0600 — that stack's containers run as
  root.
- Polaris's `storageConfigInfo.endpoint`/`stsEndpoint` currently point at a
  single VAST VIP (`172.200.203.1`). If that VIP becomes a bottleneck or
  goes down, we may need a small internal load-balancer (e.g. nginx stream
  proxy) across all six VIPs, similar to how Trino's `data_endpoints` spreads
  across them.
- **A Jupyter rebuild is slow and not free.** Its base image is pinned
  (`scipy-notebook:python-3.13.16`) precisely because re-resolving `:latest`
  invalidates every layer below it: a one-line change to
  `log4j2.properties` re-downloads the base, Spark and the connector, took
  over an hour here, and moved Python from 3.13.15 to 3.13.16 underneath a
  demo that was working. Bump the pin deliberately, and re-run both
  notebooks afterwards.
- `polaris/docker/vast-sts.pem` is baked into the Polaris image, so it has
  to be re-fetched (`vms/fetch_sts_cert.sh`) and the image rebuilt whenever
  VAST serves a different one — which a cluster rebuild always does, and a
  cert rotation would too.
- All three long-lived services set `restart: unless-stopped`, so anything
  that bounces the docker daemon brings them all back. **Expect noisy
  Polaris logs for the first few seconds after such a restart:** restart
  policies do not honor `depends_on`, so Polaris can start before Postgres
  is accepting connections and will log JDBC connection failures until it
  is. Docker retries with backoff and it self-heals — no action needed
  unless the container is still restarting after a minute or so, which
  would mean Postgres itself failed to come back. Check
  `docker logs sts-postgres` before touching Polaris.

  The one-shot sidecars (`polaris-bootstrap`, `vastdb-setup`,
  `polaris-setup`) deliberately have no restart policy — re-running
  `polaris-setup` would rewrite `jupyter/creds/*.env` on every daemon
  bounce.
