# Pinned, not :latest. This image installs Spark and the VAST connector into
# the base, so re-resolving the tag invalidates every layer below and can move
# the Python/Scala environment underneath a demo that was working. This tag is
# the one the demo is validated against.
FROM quay.io/jupyter/scipy-notebook:python-3.13.16

USER root

RUN apt-get update && apt-get install -y --no-install-recommends unzip openjdk-17-jre-headless ca-certificates-java && \
    apt-get clean && rm -rf /var/lib/apt/lists/*

# Install Spark ourselves (Scala 2.13) instead of using a prebuilt
# pyspark-notebook image (those are pinned to Scala 2.12, but VAST's
# published Spark-3.5.x connector builds are Scala-2.13-only — confirmed,
# no _2.12 variant exists for any spark3-vast-3.5.x release). Script
# copied from jupyter/docker-stacks via
# ~/vast-docker-compose-examples/jupyter-pyspark/docker/setup_spark.py.
ENV SPARK_HOME=/usr/local/spark
ENV PATH="${PATH}:${SPARK_HOME}/bin"
RUN pip install --no-cache-dir requests beautifulsoup4
COPY setup_spark.py /opt/setup-scripts/setup_spark.py
RUN chmod a+x /opt/setup-scripts/setup_spark.py && \
    /opt/setup-scripts/setup_spark.py \
      --spark-version=3.5.3 \
      --hadoop-version=3 \
      --scala-version=2.13 \
      --spark-download-url=https://archive.apache.org/dist/spark/

# Iceberg Spark runtime + AWS bundle — _2.13 to match Spark's Scala
# version above (NOT _2.12, unlike the quay prebuilt image this used to
# be based on).
ARG ICEBERG_VERSION=1.10.1
RUN cd /usr/local/spark/jars && \
    curl -fLO "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-spark-runtime-3.5_2.13/${ICEBERG_VERSION}/iceberg-spark-runtime-3.5_2.13-${ICEBERG_VERSION}.jar" && \
    curl -fLO "https://repo1.maven.org/maven2/org/apache/iceberg/iceberg-aws-bundle/${ICEBERG_VERSION}/iceberg-aws-bundle-${ICEBERG_VERSION}.jar"

# VAST DB Spark connector ("ndb" catalog) — Scala 2.13, matching the Spark
# install above (this is also why we can't just use the prebuilt Scala
# 2.12 pyspark-notebook image at all: confirmed live,
# "java.lang.NoSuchMethodError: scala.PartialFunction.fromFunction" when
# this jar was previously loaded against a Scala 2.12 Spark).
ARG VAST_CONNECTOR_RELEASE=spark3-vast-3.5.1-e92442418427
RUN cd /usr/local/spark/jars && \
    curl -fLO "https://github.com/vast-data/vast-db-connectors/releases/download/${VAST_CONNECTOR_RELEASE}/${VAST_CONNECTOR_RELEASE}.zip" && \
    unzip -q "${VAST_CONNECTOR_RELEASE}.zip" && \
    rm "${VAST_CONNECTOR_RELEASE}.zip" && \
    # These two conflict with Spark's own bundled versions and break
    # Arrow's netty-based allocator at runtime — confirmed live:
    # "NoSuchFieldError: chunkSize" in Netty's PooledByteBufAllocatorL,
    # thrown from arrow-memory-netty (Spark bundles netty 4.1.96.Final;
    # the connector's netty 4.1.114.Final jars shadow just 2 of the ~18
    # netty jars Spark needs, an inconsistent partial replacement).
    # arrow-compression-18.0.0.jar is similarly a newer Arrow release
    # than Spark's bundled 12.0.1 and isn't needed for basic insert/select.
    rm -f "${VAST_CONNECTOR_RELEASE}/netty-buffer-4.1.114.Final.jar" \
          "${VAST_CONNECTOR_RELEASE}/netty-common-4.1.114.Final.jar" \
          "${VAST_CONNECTOR_RELEASE}/arrow-compression-18.0.0.jar"

# spark.driver.extraClassPath cannot be set from SparkSession.builder in
# client/local mode (JVM already started) — must be a static file read
# before JVM launch. See comments in spark-defaults.conf.
COPY spark-defaults.conf /usr/local/spark/conf/spark-defaults.conf

# Spark's own default log4j2 template has rootLogger.level=info, which is
# very noisy for this demo — every cell logs pages of Spark/Hadoop/VAST
# connector INFO lines. Turned down to warn.
COPY log4j2.properties /usr/local/spark/conf/log4j2.properties

USER ${NB_UID}

# Deliberately NOT pip-installing pyspark — that wheel bundles its own
# (Scala 2.12) Spark jars, which is exactly the mismatch we're avoiding.
# pyspark is importable via PYTHONPATH, set at container startup by the
# before-notebook.d/10spark-config.sh hook from setup_spark.py above.
RUN pip install --no-cache-dir python-dotenv
