#!/bin/bash
# Pass/fail for the STS path, before any Polaris configuration exists.
# There is no value in debugging Polaris storage config against a broken role.
#
# Checks, with the signer key from polaris/.env.secrets:
#   1. AssumeRole against the role ARN in polaris/.env
#   2. the returned temporary credentials can list/put/get/delete under the
#      demo prefix
#   3. they are refused outside it -- the storage layer really is scoped
#
# STS requires HTTPS on 443 here (plain HTTP:80 returns AccessDenied on
# AssumeRole however the policies read), and the cert is self-signed, hence
# --no-verify-ssl. The S3 data path is happy on HTTP:80.
set -uo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
set -a; . ./polaris/.env; . ./polaris/.env.secrets; set +a

STS_URL="https://${VAST_STS_IP}:443"
rc=0
pass() { echo "PASS: $*"; }
fail() { echo "FAIL: $*"; rc=1; }

echo "role ${VAST_ROLE_ARN}  bucket ${VAST_BUCKET}/${VAST_BUCKET_PREFIX}  sts ${STS_URL}"

# Keep stderr out of $creds: --no-verify-ssl makes urllib3 print an
# InsecureRequestWarning, and folding that into the JSON breaks the parse.
err=$(mktemp)
creds=$(aws sts assume-role \
    --role-arn "${VAST_ROLE_ARN}" \
    --role-session-name sts-demo-verify \
    --endpoint-url "${STS_URL}" --no-verify-ssl 2>"$err")
if ! grep -q SessionToken <<<"$creds"; then
    fail "AssumeRole: $(grep -viE 'urllib3|InsecureRequest' "$err" | tail -1)"
    rm -f "$err"
    echo "Check that the signer user in polaris/.env.secrets is the one named"
    echo "in the role's Trust Policy, and that the role exists on this tenant."
    exit 1
fi
rm -f "$err"
pass "AssumeRole as ${VAST_ROLE_ARN}"

read_cred() { python3 -c "import json,sys;print(json.load(sys.stdin)['Credentials']['$1'])" <<<"$creds"; }
export AWS_ACCESS_KEY_ID=$(read_cred AccessKeyId)
export AWS_SECRET_ACCESS_KEY=$(read_cred SecretAccessKey)
export AWS_SESSION_TOKEN=$(read_cred SessionToken)

S3="aws --endpoint-url ${VAST_S3_ENDPOINT} s3"
KEY="s3://${VAST_BUCKET}/${VAST_BUCKET_PREFIX}/_sts_verify.txt"

# s3api, not `s3 ls`: the CLI exits 1 on an empty listing, which is the
# normal state of a fresh prefix and would read as a permission failure.
aws --endpoint-url "${VAST_S3_ENDPOINT}" s3api list-objects-v2 \
    --bucket "${VAST_BUCKET}" --prefix "${VAST_BUCKET_PREFIX}/" --max-items 1 >/dev/null 2>&1 \
    && pass "list under ${VAST_BUCKET_PREFIX}/" || fail "list under ${VAST_BUCKET_PREFIX}/"
echo sts-verify | $S3 cp - "$KEY" >/dev/null 2>&1 \
    && pass "put under ${VAST_BUCKET_PREFIX}/" || fail "put under ${VAST_BUCKET_PREFIX}/"
[[ "$($S3 cp "$KEY" - 2>/dev/null)" == sts-verify ]] \
    && pass "get under ${VAST_BUCKET_PREFIX}/" || fail "get under ${VAST_BUCKET_PREFIX}/"
$S3 rm "$KEY" >/dev/null 2>&1 \
    && pass "delete under ${VAST_BUCKET_PREFIX}/" || fail "delete under ${VAST_BUCKET_PREFIX}/"

# The isolation check: the same credentials must not reach the rest of the
# bucket. A pass here is what makes the vended credential worth vending.
if echo nope | $S3 cp - "s3://${VAST_BUCKET}/_sts_verify_outside.txt" >/dev/null 2>&1; then
    fail "put outside ${VAST_BUCKET_PREFIX}/ was ALLOWED -- role policy is too broad"
    $S3 rm "s3://${VAST_BUCKET}/_sts_verify_outside.txt" >/dev/null 2>&1
else
    pass "put outside ${VAST_BUCKET_PREFIX}/ refused"
fi

exit $rc
