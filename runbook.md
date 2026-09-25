# Polaris + STS AssumeRole Alice/Bob Demo — Runbook

**Status (2026-09-22):** The Iceberg/STS core (steps 1-9, minus 6b) is
validated end-to-end against the real `whitmore` bucket on var203,
including both notebooks and both expected-fail isolation checks. Four
real bugs were found and fixed along the way (documented inline at step 2
and step 6) — re-read those before changing storage config, grants, or the
Polaris image.

**VASTDB extension: fully validated end-to-end, including both notebooks
(2026-09-22).** VASTDB schema/tables created, all three registered as
Generic Tables (confirmed `storage-access-configs: []`, genuinely no
credential vending, as expected). The Jupyter image's engineering side
needed five real bugs fixed getting the `ndb` catalog to load and behave
reasonably (wrong Scala version, extraClassPath timing, jar classpath
order, two conflicting bundled jars, and noisy default logging — see
step 7). The Identity Policy needed **four** iterative fixes beyond the
docs' example (missing plain `s3:ListBucket`, schema-object-itself
resource form, `TabularGetTableStats`, `TabularListColumns` — see step
6b) — each found fastest by testing the raw `vastdb` SDK directly against
VAST rather than rebuilding notebooks each time. Both `Alice.ipynb` and
`Bob.ipynb` now run correctly end-to-end, Iceberg and VASTDB cells alike,
confirmed by the user.

**Update (2026-09-22):** a from-scratch re-verification pass (full
`docker compose down -v` across all three stacks, re-bootstrap, re-run)
surfaced one more real bug — `spark.task.maxFailures` bug 10 in step 7 —
found because scripted headless re-runs hit task retries that interactive
notebook use hadn't. Fixed in both notebooks. Everything else (Postgres,
Polaris bootstrap/catalog/grants, the STS AssumeRole path, and the
`ndb` catalog's real endpoint connectivity) re-validated clean against the
real cluster.

Directory: `~/bw/sts/` — `postgres/`, `polaris/`, `jupyter/`, each with its own
`docker-compose.yml`. Run them in the order below. Tags: `[VMS]` = manual step
in the VAST VMS UI/CLI, `[HOST]` = shell/docker on this host, `[NOTEBOOK]` =
inside a Jupyter notebook cell.

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
| AIM Role ARN | `arn:vast:iam::default:role/test` | `polaris/.env` → `VAST_ROLE_ARN` |
| VAST DB (ndb) endpoint(s) | `172.200.203.1`–`.6` | `jupyter/creds/vastdb_endpoint.env` |
| Postgres password | — | `postgres/.env.secrets` + `polaris/.env.secrets` (must match) |
| Polaris root client secret | — | `polaris/.env.secrets` → `ROOT_CLIENT_ID`/`ROOT_CLIENT_SECRET` |
| Admin VAST access key (var203/AllowAllTabular) | — | `polaris/.env.secrets` → `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY` |
| Alice/Bob Polaris client creds | — | `jupyter/creds/alice.env`, `bob.env` (auto-written by step 6, no template needed) |
| Alice/Bob VASTDB keys | — | `jupyter/creds/alice_db.env`, `bob_db.env` (manual, from VMS — see step 6b) |
| VAST STS leaf cert | — | `polaris/docker/vast-sts.pem` (fetch via `openssl s_client -connect <VAST_STS_IP>:443 -showcerts`, see step 6 bug 2) |

None of the files in the left column of the table above are committed —
see `.gitignore`. The rest of this runbook states the real values inline
for narrative clarity (this is a lab-only demo), but the files above are
the actual source of truth and what a fresh checkout needs filled in.

## 1. `[VMS]` Confirm the whitmore view's policy flavor

In VMS, confirm the View backing the `whitmore` bucket uses an **S3 Native**
policy flavor and the S3 protocol is enabled. (STS AssumeRole and AIM roles
require S3 Native — see kb.vastdata.com "Security Token Service (STS) and IAM
Roles" — the VAST object type is what we're calling an **AIM Role** below.)

## 2. `[VMS]` Reuse the existing "test" AIM Role — done

Creating a *new* AIM Role on the default tenant failed with "Cannot create
an IAM Role for this tenant as it has replications configured" — this
tenant's replication config blocks new role creation (undocumented
limitation, no known workaround found). Instead we're reusing an existing
AIM Role already on the default tenant, named **`test`**:

- **Trust Policy** (already in place — the VAST-documented default, allows
  any identity to assume it):
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {"Effect": "Allow", "Principal": "*", "Action": "sts:*"}
    ]
  }
  ```
- **Identity Policy** (already in place, `Sid: AllowAllTabular` — broader
  than this demo strictly needs, `s3:*` on `Resource: ["*"]`, a superset of
  the `GetObject`/`PutObject`/`DeleteObject`/`ListBucket`/`GetBucketLocation`
  on `whitmore/polaris_demo/*` Polaris needs):
  ```json
  {
    "Version": "2012-10-17",
    "Statement": [
      {
        "Sid": "AllowAllTabular",
        "Effect": "Allow",
        "Action": ["s3:*", "dataengine:Get*", "s3:QueryEngine"],
        "Resource": ["*"]
      }
    ]
  }
  ```
- **Bucket owner** for `whitmore` has been changed to this `test` role
  (`--bucket-owner-type ROLE`), matching VAST's documented pattern for
  STS-vended access. Note this is a change to a bucket also used outside
  this demo — accepted deliberately, not something to redo casually.

**Security note:** because the Trust Policy is wide-open (`Principal: "*"`)
and the Identity Policy is `s3:*` on `*`, this role provides **no isolation
at the storage layer** — anyone who can reach the STS endpoint can assume it
and touch any bucket. Alice/Bob isolation in this demo comes entirely from
Polaris's own per-table grants (see `polaris/config/setup.sh`), not from
this role. Don't reuse `test` for anything where storage-layer isolation
actually matters.

Role ARN — VAST supports two formats: the newer 5-field
`arn:vast::<tenant>:role/test` and the older 6-field
`arn:vast:iam::<tenant>:role/test` (added pre-5.4.6, kept for backward
compatibility). **Use the 6-field form everywhere in this demo** —
`polaris/.env`'s `VAST_ROLE_ARN` is already set to
`arn:vast:iam::default:role/test` — because Apache Polaris 1.7.0 hardcodes a
6-field ARN regex and rejects the 5-field form with `Invalid role ARN
format` (apache/polaris#5099, open/unresolved upstream). Step 3b below
confirms VAST itself still accepts the 6-field form directly, independent
of Polaris.

## 3. `[HOST]` Pass/fail: raw AssumeRole + S3 check

Before touching Polaris, prove the role works standalone:

STS specifically requires **HTTPS on port 443** (self-signed cert on this
lab cluster, hence `--no-verify-ssl`) — plain `http://` on port 80 (which
works fine for ordinary S3 calls) gets rejected for `AssumeRole` with a
generic `AccessDenied`, per VAST's own documented example:

```bash
aws sts assume-role \
  --role-arn "arn:vast::default:role/test" \
  --role-session-name sts-demo-test \
  --endpoint-url https://172.200.203.1:443 \
  --no-verify-ssl \
  --profile var203
```

**3b.** Confirmed passing above with the 5-field ARN. Since Polaris will
actually be sent the 6-field form, also confirm VAST accepts *that* form
before retrying step 6 — swap in `arn:vast:iam::default:role/test` and
re-run the same two commands (assume-role, then `s3 ls` with the returned
temp creds). If this fails, the 6-field legacy format may not actually be
enabled on this cluster and we're back to an unresolved Polaris/VAST
incompatibility.

Take the returned `AccessKeyId`/`SecretAccessKey`/`SessionToken` and:

```bash
AWS_ACCESS_KEY_ID=<from above> \
AWS_SECRET_ACCESS_KEY=<from above> \
AWS_SESSION_TOKEN=<from above> \
aws s3 ls s3://whitmore/polaris_demo/ --endpoint-url https://172.200.203.1:443 --no-verify-ssl
```

**PASS** = the `s3 ls` succeeds (empty listing is fine, the prefix doesn't
exist yet). **FAIL** = double-check the tenant name in the role ARN, and
that `whitmore`'s owner really is the `test` role, before proceeding — do
not debug Polaris against a broken role.

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

Fill in `VAST_ROLE_ARN` in `polaris/.env` (from step 2) if you haven't
already, then:

```bash
cd ~/bw/sts/polaris
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
   available. `polaris/docker/Dockerfile` now builds a custom Polaris image
   that imports VAST's STS cert (`polaris/docker/vast-sts.pem`, fetched via
   `openssl s_client -connect 172.200.203.1:443 -showcerts`) into the JVM
   truststore with `keytool` — `update-ca-trust extract` silently drops it
   since it's not a CA cert, so that path doesn't work. Separately, the
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
`inventory_events`, browsable via `SHOW TABLES` same as the Iceberg ones),
but **actual read/write authorization is enforced by VAST directly**, via
two new, separate, narrowly-scoped VAST access keys — not by Polaris.

**`[VMS]` Create two new VAST Users with scoped Identity Policies:**

Create `alice-db` and `bob-db` (User Management > Users > Create User,
then generate an Access Key for each — this is a plain User/Access-Key
object, not an AIM Role, so it isn't subject to the tenant-replication
block from step 2). Attach an Identity Policy to each:

**alice-db** — access to `shipments` (hers) and `inventory_events` (the
deliberate overlap with Bob):
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["whitmore"]},
    {"Effect": "Allow", "Action": ["s3:TabularListSchemas"], "Resource": ["whitmore"]},
    {"Effect": "Allow", "Action": ["s3:TabularListTables", "s3:TabularBeginTransaction", "s3:TabularCommitTransaction", "s3:TabularRollbackTransaction", "s3:TabularGetTransaction"], "Resource": ["whitmore/polaris_demo_db", "whitmore/polaris_demo_db/*"]},
    {"Effect": "Allow", "Action": ["s3:TabularQueryData", "s3:TabularInsertRows", "s3:TabularUpdateRows", "s3:TabularGetTableStats", "s3:TabularListColumns"], "Resource": ["whitmore/polaris_demo_db/shipments", "whitmore/polaris_demo_db/inventory_events"]}
  ]
}
```

**bob-db** — same shape, access to `reviews` (his) and `inventory_events`:
```json
{
  "Version": "2012-10-17",
  "Statement": [
    {"Effect": "Allow", "Action": ["s3:ListBucket"], "Resource": ["whitmore"]},
    {"Effect": "Allow", "Action": ["s3:TabularListSchemas"], "Resource": ["whitmore"]},
    {"Effect": "Allow", "Action": ["s3:TabularListTables", "s3:TabularBeginTransaction", "s3:TabularCommitTransaction", "s3:TabularRollbackTransaction", "s3:TabularGetTransaction"], "Resource": ["whitmore/polaris_demo_db", "whitmore/polaris_demo_db/*"]},
    {"Effect": "Allow", "Action": ["s3:TabularQueryData", "s3:TabularInsertRows", "s3:TabularUpdateRows", "s3:TabularGetTableStats", "s3:TabularListColumns"], "Resource": ["whitmore/polaris_demo_db/reviews", "whitmore/polaris_demo_db/inventory_events"]}
  ]
}
```

Action names and the `bucket/schema/table` resource format are from VAST's
"Managing permissions for accessing VAST tabular databases" KB doc —
table-level scoping (not just schema-level) is explicitly supported, which
is what makes the same overlap pattern as the Iceberg side possible here.

**Three bugs found and fixed getting past the 403s, all isolated with the
`vastdb` Python SDK directly against VAST — no Spark involved at all,
much faster iteration than rebuilding notebooks each time:**
1. The policy originally had *only* `s3:Tabular*` actions, no plain S3
   action. A raw `aws s3api head-bucket --bucket whitmore` with alice-db's
   key 403'd identically to the Tabular calls, proving the gap wasn't
   Tabular-specific: VAST's `HeadBucket`/tabular-session-open path needs
   plain `s3:ListBucket` on the bucket first (AWS's own docs say the same
   about `HeadBucket`). Added above. **Confirmed fixed live.**
2. After that fix, bucket access and `TabularListSchemas` worked, but
   `TabularListTables` still 403'd — the error body named the denied
   object as `polaris_demo_db` with **no trailing wildcard**
   (`@s3_object_name=polaris_demo_db`), revealing that "list tables in
   schema X" checks permission on the schema object **itself**, not a
   `schema/*` "children of" pattern (the shape the docs' example implied
   was sufficient). Fixed by granting both
   `whitmore/polaris_demo_db` (the schema itself) *and*
   `whitmore/polaris_demo_db/*` (tables inside it) on that statement.
   **Confirmed fixed live** — `tables()` listing now succeeds for both
   alice-db and bob-db.
3. After that, listing tables worked but *loading a specific table*
   (`schema.table(name)`, which the SDK calls internally before any
   select/insert) still 403'd on a `GET .../shipments?stats` call — a
   distinct action, `s3:TabularGetTableStats`, that the table-scoped
   statement never granted (it only had `TabularQueryData`/
   `InsertRows`/`UpdateRows`). Added above. **Confirmed fixed live** —
   the `?stats` call now passes for granted tables.
4. Past `?stats`, table loading hit one more distinct check: `GET
   .../shipments?column` (fetching the table's column/schema list) —
   `s3:TabularListColumns`, a 4th action never granted. Added above.
   **Confirmed fixed live — this closes out the VASTDB grant chain.**
   `select().read_all()` now succeeds end-to-end for both alice-db and
   bob-db on their own tables and the shared `inventory_events`, and the
   cross-access checks (alice→reviews, bob→shipments) both correctly
   403 with a clean early rejection (no permissions granted there at
   all, so it fails at the first check rather than a deep one).

Pattern worth noting: merely *loading* a table object (before any actual
read/write) chains through `TabularGetTableStats` and
`TabularListColumns` as separate checks beyond `TabularQueryData` — the
docs' example policy didn't make this obvious; only live testing surfaced
it, one 403 at a time.

`[HOST]` Put the resulting access key/secret pairs into
`jupyter/creds/alice_db.env` and `jupyter/creds/bob_db.env` (replacing
`CHANGE_ME`).

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

**Pass/fail:** `curl --fail http://localhost:8888` (token is `sts-demo`, see
`jupyter/docker-compose.yml`).

## 8. `[NOTEBOOK]` Run `Alice.ipynb`

Open http://localhost:8888/lab, token `sts-demo`, run `notebooks/Alice.ipynb`
top to bottom. Confirms:
- Session builds against the Polaris REST catalog with vended credentials,
  *and* the `ndb` VASTDB catalog with alice-db's static key, in one session.
- Alice can create/read/write `demo.customers`, `demo.orders`,
  `demo.order_items` (Iceberg, Polaris-vended).
- Alice can read/write VASTDB `shipments` and `inventory_events` via the
  `ndb` catalog (static key, VAST-enforced, not Polaris).
- The write to `demo.products` is **rejected** by Polaris (prints `PASS`).
- The write to VASTDB `reviews` is **rejected** by VAST directly (prints
  `PASS`) — a different enforcement layer than the Iceberg check above.

## 9. `[NOTEBOOK]` Run `Bob.ipynb`

Same, for Bob. Confirms:
- Bob can create/read/write `demo.products`, `demo.events`,
  `demo.order_items` (Iceberg), and VASTDB `reviews`/`inventory_events`.
- `demo.order_items` and VASTDB `inventory_events` both show Alice's row
  from step 8 alongside Bob's own — proof of the intentional overlap on
  *both* the Iceberg and VASTDB sides.
- The writes to `demo.customers` (Iceberg) and VASTDB `shipments` are both
  **rejected** (prints `PASS` for each).
- Re-run Alice's `order_items`/`inventory_events` cells afterward to see
  Bob's rows too.

## Teardown (when done iterating)

```bash
cd ~/bw/sts/jupyter && docker compose down
cd ~/bw/sts/polaris && docker compose down
cd ~/bw/sts/postgres && docker compose down   # add -v to also drop the Postgres volume
docker network rm sts_net
```

## Known open items (from planning — revisit as we iterate)

- VMS UI field names in step 2 are from VAST KB docs; adjust to match this
  cluster's actual VMS version if labels differ.
- `polaris-setup` writes credentials directly into `../jupyter/creds/` via a
  bind mount across compose projects — if that turns out to be fragile
  (e.g. permissions), fall back to copying the clientId/secret it prints to
  stdout by hand.
- Polaris's `storageConfigInfo.endpoint`/`stsEndpoint` currently point at a
  single VAST VIP (`172.200.203.1`). If that VIP becomes a bottleneck or
  goes down, we may need a small internal load-balancer (e.g. nginx stream
  proxy) across all six VIPs, similar to how Trino's `data_endpoints` spreads
  across them.
- ~~TLS trust/hostname risk for step 6~~ — hit and fixed, see step 6's
  numbered writeup above (custom truststore + `extra_hosts` wildcard-SAN
  workaround).
- `polaris/docker/vast-sts.pem` has a fixed expiry (2036, checked at fetch
  time) — if VAST ever rotates this cert, `docker compose build polaris`
  needs to be rerun after re-fetching it via the `openssl s_client` command
  in step 6's writeup above.
