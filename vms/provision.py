#!/usr/bin/env python3
"""Provision everything this demo needs on the VAST side, idempotently.

Creates, on the cluster named in vms/.vms.env:

  * identity policy + AIM Role for the Polaris STS path, scoped to
    <bucket>/<prefix>/*, with a Trust Policy naming only the signer user;
  * the signer user itself, whose permanent key Polaris uses to call
    AssumeRole, written into polaris/.env.secrets;
  * the alice-db / bob-db users, their table-scoped VASTDB identity
    policies, and their access keys, written into jupyter/creds/*_db.env.

Bucket, prefix, schema, role ARN and tenant are read from polaris/.env, so
this script and Polaris cannot disagree about them.

Usage:
    ./vms/provision.py [--dry-run] [--rotate-keys]

An S3 secret is returned exactly once, at creation. The script writes each
one straight into its mode-600 destination file and prints only field names.
Re-running is safe: existing objects are patched to match, and keys are only
minted for an identity that has none (or for every identity, with
--rotate-keys).
"""

import argparse
import json
import os
import re
import stat
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
VASTPY_SITE = '/home/vastdata/.local/venvs/vast-admin-mcp/lib/python3.12/site-packages'
if os.path.isdir(VASTPY_SITE):
    sys.path.insert(0, VASTPY_SITE)

try:
    import vastpy
except ImportError:
    sys.exit('vastpy not importable; see vms/README.md')

SIGNER_USER = 'polaris-sts'
ROLE_POLICY = 'polaris-sts-demo'
DB_ADMIN_USER = 'polaris-db-admin'
DB_USERS = {
    # user -> the VASTDB tables it may read and write
    'alice-db': ['shipments', 'inventory_events'],
    'bob-db': ['reviews', 'inventory_events'],
}
LOCAL_PROVIDER_ID = 1


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


# The Jupyter image runs as jovyan, uid 1000 / gid 100 ("users"), which the
# host account is also a member of. Files it has to read therefore go to
# group "users" at 0640 rather than 0600 -- the container's uid does not
# match the host's, so a 0600 secret is simply unreadable inside it, and the
# notebook fails on a missing key rather than a permission error.
JOVYAN_GID = 100


def write_env_values(path, values, group=None):
    """Replace KEY=... lines in place, preserving comments and order."""
    with open(path) as f:
        lines = f.readlines()
    remaining = dict(values)
    out = []
    for line in lines:
        m = re.match(r'^(\w+)=', line)
        if m and m.group(1) in remaining:
            out.append(f'{m.group(1)}={remaining.pop(m.group(1))}\n')
        else:
            out.append(line)
    for k, v in remaining.items():
        out.append(f'{k}={v}\n')
    with open(path, 'w') as f:
        f.writelines(out)
    if group is None:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    else:
        os.chown(path, -1, group)
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)


def role_identity_policy(bucket, prefix):
    """The least privilege Polaris's vended credentials actually need.

    ListBucket has no prefix-scoped form without a Condition, so it is
    granted bucket-wide; every object action is confined to the prefix.
    """
    return {
        'Version': '2012-10-17',
        'Statement': [
            {'Sid': 'BucketLevel', 'Effect': 'Allow',
             'Action': ['s3:ListBucket', 's3:GetBucketLocation'],
             'Resource': [bucket]},
            {'Sid': 'DemoPrefix', 'Effect': 'Allow',
             'Action': ['s3:GetObject', 's3:PutObject', 's3:DeleteObject',
                        's3:AbortMultipartUpload', 's3:ListMultipartUploadParts'],
             'Resource': [f'{bucket}/{prefix}/*']},
        ],
    }


def db_admin_identity_policy(bucket, schema):
    """DDL rights over this demo's schema only, for the vastdb-setup sidecar.

    Kept off the STS signer deliberately: the signer's only privilege is to
    assume the role, so a leak of its key vends nothing by itself.
    """
    return {
        'Version': '2012-10-17',
        'Statement': [
            {'Sid': 'Bucket', 'Effect': 'Allow',
             'Action': ['s3:ListBucket', 's3:TabularListSchemas'],
             'Resource': [bucket]},
            # TabularCreateSchema is checked against the schema object being
            # created (the 403 names @s3_object_name=<schema>), not against
            # the bucket -- so it belongs on this statement, not the one above.
            {'Sid': 'SchemaDDL', 'Effect': 'Allow',
             'Action': ['s3:TabularCreateSchema',
                        's3:TabularListTables', 's3:TabularCreateTable',
                        's3:TabularDropTable', 's3:TabularAlterTable',
                        's3:TabularAlterSchema', 's3:TabularDropSchema',
                        's3:TabularGetTableStats', 's3:TabularListColumns',
                        's3:TabularBeginTransaction', 's3:TabularCommitTransaction',
                        's3:TabularRollbackTransaction', 's3:TabularGetTransaction'],
             'Resource': [f'{bucket}/{schema}', f'{bucket}/{schema}/*']},
        ],
    }


def db_identity_policy(bucket, schema, tables):
    """Table-scoped VASTDB access for one principal.

    Four of these actions are not in VAST's documented example policy and
    were each found by a 403: plain s3:ListBucket (the tabular session open
    does a HeadBucket first), the schema object itself as a resource (listing
    tables checks the schema, not schema/*), TabularGetTableStats and
    TabularListColumns (both are hit while merely loading a table object,
    before any read).
    """
    qualified = [f'{bucket}/{schema}/{t}' for t in tables]
    return {
        'Version': '2012-10-17',
        'Statement': [
            {'Sid': 'Bucket', 'Effect': 'Allow',
             'Action': ['s3:ListBucket', 's3:TabularListSchemas'],
             'Resource': [bucket]},
            {'Sid': 'Schema', 'Effect': 'Allow',
             'Action': ['s3:TabularListTables', 's3:TabularBeginTransaction',
                        's3:TabularCommitTransaction', 's3:TabularRollbackTransaction',
                        's3:TabularGetTransaction'],
             'Resource': [f'{bucket}/{schema}', f'{bucket}/{schema}/*']},
            {'Sid': 'Tables', 'Effect': 'Allow',
             'Action': ['s3:TabularQueryData', 's3:TabularInsertRows',
                        's3:TabularUpdateRows', 's3:TabularGetTableStats',
                        's3:TabularListColumns'],
             'Resource': qualified},
        ],
    }


class Vms:
    def __init__(self, client, tenant_id, dry_run):
        self.c = client
        self.tenant_id = tenant_id
        self.dry_run = dry_run

    def say(self, *a):
        print(('[dry-run] ' if self.dry_run else '') + ' '.join(str(x) for x in a))

    def one(self, result):
        return result[0] if isinstance(result, list) else result

    def policy(self, name, document):
        body = json.dumps(document, indent=2)
        found = next((p for p in self.c.s3policies.get() if p['name'] == name), None)
        if found:
            current = self.one(self.c.s3policies[found['id']].get())
            if json.loads(current['policy']) != document or not current['enabled']:
                self.say('policy', name, 'differs -> patching')
                if not self.dry_run:
                    self.c.s3policies[found['id']].patch(policy=body, enabled=True)
            else:
                self.say('policy', name, 'up to date')
            return found['id']
        self.say('policy', name, '-> creating')
        if self.dry_run:
            return None
        return self.c.s3policies.post(name=name, policy=body, enabled=True,
                                      tenant_id=self.tenant_id)['id']

    def role(self, name, trust, policy_id, max_session_duration=3600):
        trust_body = json.dumps(trust)
        found = next((r for r in self.c.iamroles.get() if r['name'] == name), None)
        if found:
            # NOTE: the write field is `s3_policies` (a list of ids); the same
            # object reads back as `s3_policies_ids`. Sending s3_policies_ids
            # on POST or PATCH is accepted and silently ignored, which leaves a
            # role with no identity policy and every vended credential 403ing.
            self.say('role', name, '-> patching trust policy and attachment')
            if not self.dry_run:
                self.c.iamroles[found['id']].patch(
                    trust_policy=trust_body, s3_policies=[policy_id],
                    max_session_duration=max_session_duration)
            return found['id']
        self.say('role', name, '-> creating')
        if self.dry_run:
            return None
        return self.c.iamroles.post(
            name=name, tenant_id=self.tenant_id, trust_policy=trust_body,
            s3_policies=[policy_id], max_session_duration=max_session_duration,
            description='Polaris + VAST STS Alice/Bob demo (~/bw/sts)')['id']

    def user(self, name, policy_ids):
        found = next((u for u in self.c.users.get() if u['name'] == name), None)
        if found:
            if sorted(found.get('s3_policies_ids') or []) != sorted(policy_ids):
                self.say('user', name, '-> reattaching identity policy')
                if not self.dry_run:
                    self.c.users[found['id']].patch(s3_policies_ids=policy_ids)
            else:
                self.say('user', name, 'up to date')
            return found['id']
        self.say('user', name, '-> creating')
        if self.dry_run:
            return None
        uid = self.c.users.post(name=name, local_provider_id=LOCAL_PROVIDER_ID,
                                allow_create_bucket=False,
                                allow_delete_bucket=False)['id']
        self.c.users[uid].patch(s3_policies_ids=policy_ids)
        return uid

    def access_key(self, user_id, name, rotate):
        """Return (access_key, secret) if a key was minted, else None."""
        if self.dry_run:
            self.say('access key for', name, '-> would mint if absent')
            return None
        existing = self.one(self.c.users[user_id].get()).get('access_keys') or []
        if existing and not rotate:
            self.say('access key for', name, 'already exists; not rotating')
            return None
        for k in existing:
            self.c.users[user_id].access_keys[k['access_key']].delete()
        self.say('access key for', name, '-> minting')
        r = self.c.users[user_id].access_keys.post(tenant_id=self.tenant_id)
        return r['access_key'], r['secret_key']


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--dry-run', action='store_true',
                    help='report what would change, touch nothing')
    ap.add_argument('--rotate-keys', action='store_true',
                    help='replace existing access keys instead of keeping them')
    args = ap.parse_args()

    polaris_env = read_env(os.path.join(REPO, 'polaris', '.env'))
    vms_env = read_env(os.path.join(REPO, 'vms', '.vms.env'))

    bucket = polaris_env['VAST_BUCKET']
    prefix = polaris_env['VAST_BUCKET_PREFIX']
    schema = polaris_env['VASTDB_SCHEMA']
    role_arn = polaris_env['VAST_ROLE_ARN']
    # arn:vast:iam::<tenant>:role/<name> -- the 6-field form Polaris 1.7.0
    # requires (see README's design rationale).
    m = re.match(r'^arn:vast:iam::([^:]+):role/(.+)$', role_arn)
    if not m:
        sys.exit(f'VAST_ROLE_ARN is not the 6-field form Polaris needs: {role_arn}')
    tenant_name, role_name = m.groups()

    client = vastpy.VASTClient(address=vms_env['VMS_ADDRESS'],
                               token=vms_env.get('VMS_TOKEN'),
                               user=vms_env.get('VMS_USER'),
                               password=vms_env.get('VMS_PASSWORD'))
    tenant = next((t for t in client.tenants.get() if t['name'] == tenant_name), None)
    if tenant is None:
        sys.exit(f'no tenant named {tenant_name} on this cluster')

    v = Vms(client, tenant['id'], args.dry_run)
    print(f'cluster {vms_env["VMS_ADDRESS"]}  tenant {tenant_name}({tenant["id"]})  '
          f'bucket {bucket}  prefix {prefix}  schema {schema}')

    # --- the STS path: signer user -> role -> prefix-scoped identity policy
    signer_id = v.user(SIGNER_USER, [])
    pol_id = v.policy(ROLE_POLICY, role_identity_policy(bucket, prefix))
    trust = {'Version': '2012-10-17',
             'Statement': [{'Effect': 'Allow',
                            'Principal': {'User': [SIGNER_USER]},
                            'Action': 'sts:AssumeRole'}]}
    v.role(role_name, trust, pol_id)

    if signer_id is not None:
        minted = v.access_key(signer_id, SIGNER_USER, args.rotate_keys)
        if minted:
            path = os.path.join(REPO, 'polaris', '.env.secrets')
            write_env_values(path, {'AWS_ACCESS_KEY_ID': minted[0],
                                    'AWS_SECRET_ACCESS_KEY': minted[1]})
            print(f'  wrote AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY -> {path} (0600)')

    # --- the VASTDB DDL identity the vastdb-setup sidecar runs as
    admin_pol = v.policy(f'{DB_ADMIN_USER}-pol',
                         db_admin_identity_policy(bucket, schema))
    admin_id = v.user(DB_ADMIN_USER, [admin_pol] if admin_pol else [])
    if admin_id is not None:
        minted = v.access_key(admin_id, DB_ADMIN_USER, args.rotate_keys)
        if minted:
            path = os.path.join(REPO, 'polaris', '.env.secrets')
            write_env_values(path, {'VASTDB_ADMIN_ACCESS_KEY_ID': minted[0],
                                    'VASTDB_ADMIN_SECRET_ACCESS_KEY': minted[1]})
            print('  wrote VASTDB_ADMIN_ACCESS_KEY_ID, '
                  f'VASTDB_ADMIN_SECRET_ACCESS_KEY -> {path} (0600)')

    # --- the VASTDB path: one scoped user per principal
    for user_name, tables in DB_USERS.items():
        pid = v.policy(f'{user_name}-pol', db_identity_policy(bucket, schema, tables))
        uid = v.user(user_name, [pid] if pid else [])
        if uid is None:
            continue
        minted = v.access_key(uid, user_name, args.rotate_keys)
        if minted:
            stem = user_name.split('-')[0]
            path = os.path.join(REPO, 'jupyter', 'creds', f'{stem}_db.env')
            write_env_values(path, {'VASTDB_ACCESS_KEY_ID': minted[0],
                                    'VASTDB_SECRET_ACCESS_KEY': minted[1]},
                             group=JOVYAN_GID)
            print('  wrote VASTDB_ACCESS_KEY_ID, VASTDB_SECRET_ACCESS_KEY -> '
                  f'{path} (0640, group users)')

    print('done. Verify with vms/verify_sts.sh before starting Polaris.')


if __name__ == '__main__':
    main()
