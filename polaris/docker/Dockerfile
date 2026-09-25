ARG POLARIS_VERSION=1.7.0
FROM apache/polaris:${POLARIS_VERSION}

# update-ca-trust silently drops non-CA (leaf/self-signed server) certs
# dropped into /etc/pki/ca-trust/source/anchors/ — confirmed by testing,
# the cert didn't end up in the extracted java cacerts. Import it directly
# into the resolved cacerts file with keytool instead (follows the
# $JAVA_HOME/lib/security/cacerts -> /etc/pki/ca-trust/extracted/java/cacerts
# symlink chain transparently).
USER root
COPY vast-sts.pem /tmp/vast-sts.pem
RUN keytool -importcert -trustcacerts -noprompt \
      -alias vast-sts \
      -file /tmp/vast-sts.pem \
      -keystore "$(readlink -f "${JAVA_HOME}/lib/security/cacerts")" \
      -storepass changeit \
    && rm /tmp/vast-sts.pem
USER 10000
