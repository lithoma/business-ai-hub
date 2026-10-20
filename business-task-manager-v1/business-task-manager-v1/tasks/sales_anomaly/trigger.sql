-- REQUIRED: Replace this file with the existing VERIFIED KPI detection logic.
-- Contract: return exactly one row with BOOLEAN should_trigger and optional STRING reason.
-- Use CAST(:business_date AS DATE) for the date parameter.
-- Do not use kpi_alert_reports: it is a legacy DEMO/notification state table,
-- not a reliable raw business-data source for new daily detection.
SELECT CAST(raise_error('Configure sales_anomaly/trigger.sql using your verified real KPI detection SQL') AS BOOLEAN) AS should_trigger,
       'NOT_CONFIGURED' AS reason
