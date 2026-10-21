-- Milestone 2C migration proposal ONLY. Not executed by the App or local tests.
-- Apply to the existing workspace.douyin schema after deployment approval.
CREATE TABLE IF NOT EXISTS workspace.douyin.business_task_runs_v1 (
  run_id STRING NOT NULL,
  attempt_id STRING NOT NULL,
  task_id STRING NOT NULL,
  task_name STRING,
  source STRING,
  scheduled_slot STRING,
  dispatch_date DATE,
  status STRING,
  trigger_reason STRING,
  report_text STRING,
  error_code STRING,
  notification_status STRING,
  config_json STRING,
  provider_message_id STRING,
  notification_error_code STRING,
  artifacts_json STRING,
  started_at TIMESTAMP,
  finished_at TIMESTAMP
) USING DELTA
COMMENT 'Task execution ledger and report history. QUEUED is not an email delivery receipt.';
