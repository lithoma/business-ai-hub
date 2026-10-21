"""One Delta-table row per local dispatch day/group. Not an email delivery receipt."""
from __future__ import annotations

import re
from datetime import date

from pyspark.sql import SparkSession
from pyspark.sql.types import BooleanType, DateType, IntegerType, StringType, StructField, StructType, TimestampType
from datetime import datetime, timezone


_FQN = re.compile(r"^[A-Za-z_][\w]*\.[A-Za-z_][\w]*\.[A-Za-z_][\w]*$")


def publish_batch(
    spark: SparkSession,
    table: str,
    dispatch_date: date,
    group: str,
    report_text: str,
    report_count: int,
) -> None:
    if not _FQN.fullmatch(table):
        raise ValueError("Outbox must be a valid three-part catalog.schema.table identifier")
    # Existing Outbox must be provisioned separately; the executor creates no tables.
    schema = StructType([
        StructField("dispatch_date", DateType(), False),
        StructField("recipient_group", StringType(), False),
        StructField("should_notify", BooleanType(), False),
        StructField("report_count", IntegerType(), False),
        StructField("report_text", StringType(), False),
        StructField("generated_at", TimestampType(), False),
    ])
    source = spark.createDataFrame([(
        dispatch_date,
        group,
        report_count > 0,
        report_count,
        report_text,
        datetime.now(timezone.utc).replace(tzinfo=None),
    )], schema)
    # Use a temporary view only inside this job. One daily MERGE, not an Alert state machine.
    source.createOrReplaceTempView("_business_task_outbox_new")
    spark.sql(f"""MERGE INTO {table} AS t
        USING _business_task_outbox_new AS s
        ON t.dispatch_date = s.dispatch_date
           AND t.recipient_group = s.recipient_group
        WHEN MATCHED THEN UPDATE SET
          should_notify=s.should_notify,
          report_count=s.report_count,
          report_text=s.report_text,
          generated_at=s.generated_at
        WHEN NOT MATCHED THEN INSERT
          (dispatch_date, recipient_group, should_notify, report_count, report_text, generated_at)
          VALUES (s.dispatch_date, s.recipient_group, s.should_notify,
                  s.report_count, s.report_text, s.generated_at)""")
