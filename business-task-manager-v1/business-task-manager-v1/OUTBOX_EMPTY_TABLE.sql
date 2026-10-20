-- Optional one-time DDL if the Alert editor insists on existing table during setup.
-- Creating an EMPTY table is not a test and sends no email.
CREATE TABLE IF NOT EXISTS workspace.douyin.business_task_outbox_v1 (
    dispatch_date DATE,
    recipient_group STRING,
    should_notify BOOLEAN,
    report_count INT,
    report_text STRING,
    generated_at TIMESTAMP
) USING DELTA;
