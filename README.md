# Polaris + VAST STS — Alice/Bob PySpark demo

A self-contained, three-stack `docker compose` demo in which **Apache Polaris**
(Iceberg REST catalog) vends **temporary, scoped S3 credentials** for a VAST
bucket via VAST's native **STS `AssumeRole`**, and two PySpark identities —
**Alice** and **Bob** — read and write partially overlapping sets of Iceberg
tables through it.

Access is enforced at two layers at once: the Polaris grant layer decides which
tables a principal may load, and the STS-vended credentials are only valid for
the role's S3 prefix. Each notebook ends with a deliberate **expected-fail**
write against a table it was not granted — that failure, not the successful
vending, is the proof that per-principal isolation works.

A second half of the demo extends this to **VAST Database** tables read through
the VAST DB Spark connector (`ndb` catalog) and registered in Polaris as
**Generic Tables** — catalog metadata only, with no credential vending. See
[Appendix: Design rationale](#appendix-design-rationale) for why.

**Start here: [runbook.md](runbook.md)** — the ordered, tagged procedure
(`[VMS]` / `[HOST]` / `[NOTEBOOK]`) from cluster prerequisites through both
notebooks, with the real bugs found during validation documented inline at the
steps they affect.

## Architecture

| Stack | Runs | Compose file |
|---|---|---|
| `postgres/` | Polaris's metadata store (`postgres:17-bookworm`) | [postgres/docker-compose.yml](postgres/docker-compose.yml) |
| `polaris/` | Polaris 1.7.0 server, plus three one-shot sidecars: schema bootstrap, VASTDB table creation, catalog/principal/grant setup | [polaris/docker-compose.yml](polaris/docker-compose.yml) |
| `jupyter/` | Jupyter + a hand-installed Spark 3.5.3 (Scala 2.13) with the Iceberg and VAST DB connector jars | [jupyter/docker-compose.yml](jupyter/docker-compose.yml) |

All three attach to one external docker network, `sts_net`, so the containers
resolve each other by name. Bring them up in the order above; ports are
`5432` (Postgres), `8181`/`8182`/`5005` (Polaris REST+management / health /
JVM debug), `8888` (Jupyter, token `sts-demo`), `4040`–`4041` (Spark UIs).

## Configuration

Every cluster-specific value — bucket, endpoints, role ARN, credentials, TLS
cert — is gitignored, each with a tracked `*.example` template to copy and fill
in per [runbook.md § 0 Prerequisites](runbook.md#0-prerequisites--cluster-specific-values).

| Fill in | From template | Holds |
|---|---|---|
| `polaris/.env` | [polaris/.env.example](polaris/.env.example) | Bucket, prefix, S3 + STS endpoints, STS VIP, role ARN, catalog/realm names |
| `polaris/.env.secrets` | [polaris/.env.secrets.example](polaris/.env.secrets.example) | Postgres password, Polaris root client secret, the permanent VAST key that signs `AssumeRole` |
| `postgres/.env.secrets` | [postgres/.env.secrets.example](postgres/.env.secrets.example) | Same Postgres credentials, for the DB side |
| `jupyter/creds/vastdb_endpoint.env` | [jupyter/creds/vastdb_endpoint.env.example](jupyter/creds/vastdb_endpoint.env.example) | VAST DB endpoint list for the `ndb` catalog |
| `jupyter/creds/{alice,bob}_db.env` | [alice_db.env.example](jupyter/creds/alice_db.env.example), [bob_db.env.example](jupyter/creds/bob_db.env.example) | Per-principal, narrowly scoped VASTDB access keys created by hand in VMS |
| `polaris/docker/vast-sts.pem` | — (fetch with `openssl s_client`, see [runbook.md § 6](runbook.md#6-host-start-polaris-bootstrap--server--catalogprincipal-setup)) | VAST's STS leaf cert, baked into the Polaris image's Java truststore |

`jupyter/creds/{alice,bob}.env` are not templates to fill in — the
`polaris-setup` sidecar writes each principal's Polaris client ID and secret
there as it creates them.

The rest is tracked and should not need editing per cluster:

| File | Purpose |
|---|---|
| [polaris/config/setup.sh](polaris/config/setup.sh) | Creates the catalog's STS storage config, the `demo` and `vastdb_demo` namespaces and tables, both principals, and all table-scoped grants |
| [polaris/config/application.properties](polaris/config/application.properties) | Quarkus overrides: `relational-jdbc` persistence pointed at Postgres |
| [polaris/docker/Dockerfile](polaris/docker/Dockerfile) | Imports the VAST STS cert into the JVM `cacerts` with `keytool` |
| [polaris/config/vastdb_setup/setup_vastdb.py](polaris/config/vastdb_setup/setup_vastdb.py) | Creates the VASTDB schema and its three tables |
| [jupyter/docker/Dockerfile](jupyter/docker/Dockerfile) | Spark 3.5.3 / Scala 2.13, Iceberg 1.10.1, VAST DB connector, minus three conflicting bundled jars |
| [jupyter/docker/spark-defaults.conf](jupyter/docker/spark-defaults.conf) | `extraClassPath` for the connector — must be static, read before JVM launch |
| [jupyter/docker/log4j2.properties](jupyter/docker/log4j2.properties) | Spark root logger at `warn` instead of `info` |
| [postgres/config/postgresql.conf](postgres/config/postgresql.conf) | Raised `max_connections` |

## Identities and grants

| | Iceberg (`demo.*`, STS-vended) | VASTDB (`vastdb_demo.*`, metadata only) |
|---|---|---|
| **Alice** ([Alice.ipynb](jupyter/notebooks/Alice.ipynb)) | `customers`, `orders`, `order_items` | `shipments`, `inventory_events` |
| **Bob** ([Bob.ipynb](jupyter/notebooks/Bob.ipynb)) | `order_items`, `products`, `events` | `reviews`, `inventory_events` |

`demo.order_items` and `vastdb_demo.inventory_events` are the deliberate
overlaps — each notebook writes a `written_by` marker and sees the other's rows
on the next run. Everything outside a principal's row is an expected-fail.

## Appendix: Design rationale

### Why STS `AssumeRole` rather than static keys

The point of the demo is credential *vending*: Spark never holds a durable VAST
key for the data path. Polaris's `storageConfigInfo` (type `S3`) accepts
`roleArn`, `stsEndpoint`, `endpoint`, `pathStyleAccess` and `allowedLocations`,
which is exactly enough to aim its STS calls at VAST instead of AWS. Each
`loadTable` with the `X-Iceberg-Access-Delegation: vended-credentials` header
returns short-lived credentials scoped to the role's prefix, so the storage
layer independently limits what a leaked token could reach.

On the VAST side this is an **AIM Role** — VAST's own object type, named here to
stay distinct from AWS IAM — carrying two policies: a **Trust Policy** naming the
permanent identity allowed to call `sts:AssumeRole`, and an **Identity Policy**
listing what the assumed role may do. Identity-policy `Resource` entries are
bare bucket and prefix strings (`whitmore/polaris_demo/*`), not full ARNs, and
the View backing the bucket must use the **S3 Native** policy flavor.

Two shapes of that ARN exist on VAST. Polaris 1.7.0 hardcodes a six-field
regex (`arn:vast:iam::<tenant>:role/<name>`) and rejects the five-field form
(`arn:vast::<tenant>:role/<name>`) with "Invalid role ARN format"
(apache/polaris#5099, unresolved upstream), so `VAST_ROLE_ARN` uses the
six-field shape even though a raw CLI `AssumeRole` accepts the other.

STS also requires HTTPS on 443 on this cluster — plain HTTP:80 returns
`AccessDenied` on `AssumeRole` regardless of policy — while the S3 data path is
happy on HTTP:80.

### Why Polaris talks to STS through a hostname, not the VIP

VAST's cert on the STS port has no SAN covering the CNode VIP itself. Trusting
the leaf cert fixes chain validation, but the AWS SDK's Apache HTTP client
separately checks hostname against SAN, and Polaris exposes no flag to disable
that. So `polaris/docker-compose.yml` adds a container-local `extra_hosts` entry
mapping `sts.vastdata.com` to the real VIP: a name matching the cert's
`*.vastdata.com` wildcard that still resolves to the right address. It is not
real DNS, and nothing on the VAST cluster is modified.

The cert itself is imported with `keytool` directly into the resolved `cacerts`
file rather than dropped into `/etc/pki/ca-trust/source/anchors/`, because
`update-ca-trust` silently discards non-CA leaf certs.

### Why Postgres instead of Polaris's default store

Polaris's in-memory persistence loses the catalog on restart, which would make
"the vended credentials still work after a restart" untestable and hide
misconfiguration behind a fresh bootstrap. The `relational-jdbc` backend against
Postgres makes persistence a thing the runbook can actually verify — restart the
`polaris` container alone and confirm the catalog survives. It also forces the
bootstrap step to be explicit: `polaris-admin-tool` must create
`polaris_schema` and the realm root principal *before* the server starts.

### Why table-scoped grants, and what they leak

Grants are issued per table rather than catalog-wide so the two principals'
table sets can partially overlap — that overlap is what shows authorization is
per-principal rather than per-cluster. Each granted table needs three
privileges, not one: `TABLE_FULL_METADATA` alone permits a plain metadata load,
but a load carrying the vended-credentials header 403s until `TABLE_READ_DATA`
and `TABLE_WRITE_DATA` are also granted.

`NAMESPACE_LIST` and `TABLE_LIST` have no per-namespace or per-table grant
target in Polaris 1.7.0 and are only grantable catalog-wide, but Spark's
`SHOW NAMESPACES` / `SHOW TABLES` need them. The accepted tradeoff: each
principal can see the *names* of tables it has no data access to. Visibility
only — every load and write still fails.

### Why the expected-fail cells matter

A demo that only shows successful reads proves that credentials vend, not that
they are scoped. So each notebook deliberately attempts a write to a table
granted to the other principal and expects a 403. That is the pass/fail gate.
The same principle orders the runbook: step 3 checks raw `AssumeRole` and
`aws s3 ls` with the returned temporary credentials *before* any Polaris
configuration exists, because there is no value in debugging Polaris storage
config against a broken AIM role.

### Why Spark is installed by hand at Scala 2.13

The prebuilt `pyspark-notebook` images are pinned to Scala 2.12, and VAST's
published Spark 3.5.x connector builds are Scala 2.13 only — no `_2.12` variant
exists for any `spark3-vast-3.5.x` release. Loading the connector against a
Scala 2.12 Spark fails at runtime with
`NoSuchMethodError: scala.PartialFunction.fromFunction`. So the image starts
from `scipy-notebook`, installs Spark 3.5.3 for Scala 2.13 itself, and takes the
`_2.13` Iceberg artifacts to match. `pyspark` is deliberately *not*
pip-installed — that wheel bundles its own Scala 2.12 jars, reintroducing the
exact mismatch — and is reached through `PYTHONPATH` instead.

Three jars shipped inside the connector archive are deleted during the build:
two netty 4.1.114 jars that shadow just 2 of the ~18 netty jars Spark needs
(an inconsistent partial replacement, which breaks Arrow's netty allocator with
`NoSuchFieldError: chunkSize`), and an Arrow 18.0.0 compression jar newer than
Spark's bundled 12.0.1 and unnecessary for insert/select.

### Why VASTDB tables are Generic Tables, not vended

The bucket is also a VAST Database, and its tables are reached through the VAST
DB Spark connector rather than the Iceberg REST path. Polaris can register these
as **Generic Tables**, which carry catalog metadata and grants but return
`storage-access-configs: []` — genuinely no credential vending. That is the
correct model here, not a limitation to work around: the connector authenticates
to VASTDB directly.

The consequence is that Alice and Bob each need their own narrowly scoped VASTDB
access key, created by hand in VMS, with an Identity Policy that goes beyond the
documented example — plain `s3:ListBucket`, the schema-object-itself resource
form, `TabularGetTableStats` and `TabularListColumns` all turned out to be
required. Testing those against the raw `vastdb` SDK is far faster than
rebuilding a notebook per attempt.

### Why credentials are written, not copied by hand

`polaris-setup` bind-mounts `../jupyter/creds` across compose projects and writes
each principal's client ID and secret straight into place. This removes a
copy-paste step from the runbook at the cost of a cross-project bind mount; if
that proves fragile, the same values are printed to the sidecar's stdout.

### Why a single VIP today

`endpoint` and `stsEndpoint` both point at one VAST VIP, because Polaris takes a
single endpoint string where the VAST DB connector and Trino accept an endpoint
list. For a demo that is fine. Spreading load across all six CNode VIPs would
need a small internal load balancer — an nginx stream proxy, say — in front of
Polaris's S3 and STS endpoints.
