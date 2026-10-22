#!/bin/bash
# Refresh polaris/docker/vast-sts.pem from the live STS endpoint.
#
# Polaris's Java AWS SDK client validates the chain itself, so the cert is
# imported into the JVM truststore with keytool at image build time (see
# polaris/docker/Dockerfile). A cluster rebuild replaces this cert, which
# makes a tracked copy stale and every Polaris STS call fail on chain
# validation -- so re-fetch, then `docker compose build polaris`.
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.." || exit 1
set -a; . ./polaris/.env; set +a

OUT=polaris/docker/vast-sts.pem
echo | openssl s_client -connect "${VAST_STS_IP}:443" -showcerts 2>/dev/null \
    | awk '/-----BEGIN CERTIFICATE-----/{n++} n==1' > "$OUT.new"

if ! openssl x509 -in "$OUT.new" -noout -subject >/dev/null 2>&1; then
    echo "no certificate came back from ${VAST_STS_IP}:443" >&2
    rm -f "$OUT.new"
    exit 1
fi
mv "$OUT.new" "$OUT"

openssl x509 -in "$OUT" -noout -subject -dates -ext subjectAltName
echo
echo "Wrote $OUT. The hostname in VAST_STS_ENDPOINT must match a SAN entry"
echo "above; rebuild Polaris so the new cert reaches its truststore:"
echo "  (cd polaris && docker compose build polaris && docker compose up -d polaris)"
