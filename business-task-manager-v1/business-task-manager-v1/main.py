"""Single Databricks Job Python-script entry point. No internal scheduler."""
from __future__ import annotations

import argparse
import logging
from pathlib import Path

from pyspark.sql import SparkSession

from task_engine import TaskManager


def main() -> None:
    parser = argparse.ArgumentParser(description="Daily business-report Task Manager")
    parser.add_argument(
    "--settings",
    default=str(
        Path(globals().get("__file__", main.__code__.co_filename))
        .resolve()
        .parent / "settings.yaml"
    ),
)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    spark = SparkSession.builder.getOrCreate()
    result = TaskManager(spark, Path(args.settings)).run()
    logging.info("Task Manager finished: %s", result)


if __name__ == "__main__":
    main()
