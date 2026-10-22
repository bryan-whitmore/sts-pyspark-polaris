# `vms/` — the VAST side of the demo

Everything this demo needs on the cluster itself: four identities, one AIM
Role, four identity policies, three access keys, and the STS certificate the
Polaris image has to trust. All of it is scripted, because a lab cluster
rebuild invalidates all of it at once.

| Script | Does |
|---|---|
| `provision.py` | Creates or updates every VAST-side object, idempotently, and writes the keys it mints into `polaris/.env.secrets` and `jupyter/creds/*_db.env`. `--dry-run` reports without changing; `--rotate-keys` replaces existing keys. |
| `fetch_sts_cert.sh` | Re-fetches `polaris/docker/vast-sts.pem` from the live STS endpoint. Rebuild the Polaris image afterwards. |
| `verify_sts.sh` | Pass/fail for the STS path: AssumeRole, then list/put/get/delete inside the demo prefix and a refusal outside it. |
| `verify_vastdb.py` | Pass/fail for the VASTDB grants, against the raw `vastdb` SDK: each principal's own tables plus the shared one, and a refusal on the other principal's. |

Run them in that order; `runbook.md` steps 1-3 and 6b are the narrative
version, with the reasoning behind each policy.

## Credentials

`provision.py` reads `vms/.vms.env` (gitignored, mode 600) for the VMS
address and an admin API token — see `.vms.env.example`. It imports `vastpy`
from the `vast-admin-mcp` venv; the `vast-admin` MCP server itself cannot do
this work, having no user, access-key, identity-policy or role creation tool.

Access-key secrets are returned by VMS exactly once, at creation. Each is
written straight into its destination file in the same step that mints it,
and only field names are printed.
