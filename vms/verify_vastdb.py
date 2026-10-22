#!/usr/bin/env python3
"""Pass/fail for the VASTDB grants, against the raw vastdb SDK.

Merely loading a table object chains through several distinct permission
checks before any read happens, so a missing action shows up as a 403 deep
inside the SDK rather than at connect time. Testing that here -- no Spark, no
Polaris, no notebook -- is far faster than rebuilding a notebook per attempt.

For each principal: its own table and the shared inventory_events must load,
select and insert; the other principal's table must be refused.

The inserts are real rows, as the notebooks' are -- reset the VASTDB tables
if a demo needs clean counts.

    ./vms/verify_vastdb.py
"""

import os
import sys

import pyarrow as pa
import vastdb
from vastdb.errors import Forbidden, MissingTable, NotFound

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

EXPECTED = {
    # principal -> (creds file stem, allowed tables, refused table)
    'alice-db': ('alice', ['shipments', 'inventory_events'], 'reviews'),
    'bob-db': ('bob', ['reviews', 'inventory_events'], 'shipments'),
}
# A refusal arrives as Forbidden, or -- when the principal cannot even see
# the table to resolve it -- as MissingTable/NotFound.
DENIED = (Forbidden, MissingTable, NotFound)


def read_env(path):
    env = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                env[k.strip()] = v.strip()
    return env


def main():
    cfg = read_env(os.path.join(REPO, 'polaris', '.env'))
    endpoint = cfg['VAST_S3_ENDPOINT']
    bucket, schema_name = cfg['VAST_BUCKET'], cfg['VASTDB_SCHEMA']
    rc = 0

    def report(ok, msg):
        nonlocal rc
        print(('PASS: ' if ok else 'FAIL: ') + msg)
        if not ok:
            rc = 1

    for principal, (stem, allowed, refused) in EXPECTED.items():
        creds = read_env(os.path.join(REPO, 'jupyter', 'creds', f'{stem}_db.env'))
        session = vastdb.connect(endpoint=endpoint,
                                 access=creds['VASTDB_ACCESS_KEY_ID'],
                                 secret=creds['VASTDB_SECRET_ACCESS_KEY'])
        print(f'--- {principal}')
        with session.transaction() as tx:
            schema = tx.bucket(bucket).schema(schema_name)
            # Deliberately not schema.tables(): vastdb 2.0.14.1 eagerly loads
            # per-table stats while listing, so a bare listing raises 403 on
            # the first table this principal has no grant on. Table-scoped
            # grants and a whole-schema listing are mutually exclusive in this
            # SDK; the demo addresses tables by name instead (the notebooks
            # discover them from Polaris, never from tables()).
            for name in allowed:
                try:
                    table = schema.table(name)
                    rows = table.select().read_all().num_rows
                    table.insert(pa.table(
                        {f.name: pa.array(
                            [f'verify-{principal}'] if f.type == pa.string() else [0],
                            type=f.type) for f in table.arrow_schema}))
                    report(True, f'{principal} load+select+insert {name} '
                                 f'({rows} rows before insert)')
                except Exception as e:
                    report(False, f'{principal} on {name}: {type(e).__name__} '
                                  f'{str(e)[:160]}')

            try:
                schema.table(refused).select().read_all()
                report(False, f'{principal} reached {refused} -- policy too broad')
            except DENIED as e:
                report(True, f'{principal} refused {refused} ({type(e).__name__})')
            except Exception as e:
                report(False, f'{principal} on {refused} failed unexpectedly: '
                              f'{type(e).__name__} {str(e)[:160]}')

    sys.exit(rc)


if __name__ == '__main__':
    main()
