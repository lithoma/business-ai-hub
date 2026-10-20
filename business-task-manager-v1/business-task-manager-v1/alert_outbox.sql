-- Create a NEW SQL Alert V2, not the existing M6 legacy numeric alert.
-- Alert condition: should_notify > 0 (column / first row).
-- SQL Alert Job Task runs once AFTER main.py completes successfully.
-- Disable the Alert's independent recurring schedule.
-- Notification: notify_on_ok=false; retrigger_seconds=1 (via UI, if available).
-- Use {{QUERY_RESULT_TABLE}} in its Chinese email template.
-- IMPORTANT: One evaluation sends ONE email to the fixed recipient group;
-- multiple triggered task reports are already combined in report_text.
SELECT
    CAST(should_notify AS INT) AS should_notify,
    report_count,
    CONCAT('经营自动报告 | ', CAST(dispatch_date AS STRING)) AS report_subject,
    report_text
FROM workspace.douyin.business_task_outbox_v1
WHERE dispatch_date = CAST(from_utc_timestamp(current_timestamp(), 'Asia/Shanghai') AS DATE)
  AND recipient_group = 'default'
ORDER BY generated_at DESC
LIMIT 1
