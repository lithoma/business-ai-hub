CREATE TABLE workspace.douyin.business_task_registry_v1 (
  task_id STRING COLLATE UTF8_BINARY NOT NULL,
  name STRING COLLATE UTF8_BINARY NOT NULL,
  description STRING COLLATE UTF8_BINARY,
  enabled BOOLEAN NOT NULL,
  schedule_config STRING COLLATE UTF8_BINARY,
  trigger_config STRING COLLATE UTF8_BINARY,
  report_config STRING COLLATE UTF8_BINARY,
  owner STRING COLLATE UTF8_BINARY,
  lifecycle_status STRING COLLATE UTF8_BINARY NOT NULL,
  created_at TIMESTAMP NOT NULL,
  updated_at TIMESTAMP NOT NULL)
USING delta
COMMENT 'Business AI Hub task registry for Milestone 2A draft management'
TBLPROPERTIES (
  'delta.checkpointPolicy' = 'v2',
  'delta.enableChangeDataFeed' = 'true',
  'delta.enableDeletionVectors' = 'true',
  'delta.enableRowTracking' = 'true',
  'delta.feature.appendOnly' = 'supported',
  'delta.feature.changeDataFeed' = 'supported',
  'delta.feature.deletionVectors' = 'supported',
  'delta.feature.domainMetadata' = 'supported',
  'delta.feature.invariants' = 'supported',
  'delta.feature.rowTracking' = 'supported',
  'delta.feature.v2Checkpoint' = 'supported',
  'delta.minReaderVersion' = '3',
  'delta.minWriterVersion' = '7',
  'delta.parquet.compression.codec' = 'zstd',
  'delta.parquet.format.version' = '2.12.0',
  'delta.parquet.format.version.afe.internal' = '2.12.0')
