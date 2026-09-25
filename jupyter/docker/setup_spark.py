#!/usr/bin/env python3
# Copied from jupyter/docker-stacks (BSD licensed) via
# ~/vast-docker-compose-examples/jupyter-pyspark/docker/setup_spark.py.
# Needed because the prebuilt quay.io/jupyter/pyspark-notebook images are
# pinned to Scala 2.12, but VAST's published Spark-3.5.x connector builds
# are Scala 2.13-only (confirmed: no _2.12 variant exists for any
# spark3-vast-3.5.x release) — so we install Spark ourselves with the
# matching Scala version instead of using the prebuilt Spark install.
#
# Requirements:
# - Run as the root user
# - Required env variable: SPARK_HOME

import argparse
import logging
import os
import subprocess
from pathlib import Path

LOGGER = logging.getLogger(__name__)


def download_spark(
    spark_version: str,
    hadoop_version: str,
    scala_version: str,
    spark_download_url: Path,
) -> str:
    LOGGER.info("Downloading and unpacking Spark")
    spark_dir_name = f"spark-{spark_version}-bin-hadoop{hadoop_version}"
    if scala_version:
        spark_dir_name += f"-scala{scala_version}"
    LOGGER.info(f"Spark directory name: {spark_dir_name}")
    spark_url = spark_download_url / f"spark-{spark_version}" / f"{spark_dir_name}.tgz"

    tmp_file = Path("/tmp/spark.tar.gz")
    subprocess.check_call(
        ["curl", "--progress-bar", "--location", "--output", tmp_file, spark_url]
    )
    subprocess.check_call(
        [
            "tar",
            "xzf",
            tmp_file,
            "-C",
            "/usr/local",
            "--owner",
            "root",
            "--group",
            "root",
            "--no-same-owner",
        ]
    )
    tmp_file.unlink()
    return spark_dir_name


def configure_spark(spark_dir_name: str, spark_home: Path) -> None:
    LOGGER.info("Configuring Spark")
    subprocess.check_call(["ln", "-s", f"/usr/local/{spark_dir_name}", spark_home])

    # Source PYTHONPATH automatically for every notebook kernel.
    CONFIG_SCRIPT = "/usr/local/bin/before-notebook.d/10spark-config.sh"
    subprocess.check_call(
        ["ln", "-s", spark_home / "sbin/spark-config.sh", CONFIG_SCRIPT]
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    arg_parser = argparse.ArgumentParser()
    arg_parser.add_argument("--spark-version", required=True)
    arg_parser.add_argument("--hadoop-version", required=True)
    arg_parser.add_argument("--scala-version", required=True)
    arg_parser.add_argument("--spark-download-url", type=Path, required=True)
    args = arg_parser.parse_args()

    spark_dir_name = download_spark(
        spark_version=args.spark_version,
        hadoop_version=args.hadoop_version,
        scala_version=args.scala_version,
        spark_download_url=args.spark_download_url,
    )
    configure_spark(
        spark_dir_name=spark_dir_name, spark_home=Path(os.environ["SPARK_HOME"])
    )
