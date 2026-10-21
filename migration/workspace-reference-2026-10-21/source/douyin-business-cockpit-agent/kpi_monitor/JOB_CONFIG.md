# KPI Anomaly Monitor — Job Configuration

## Databricks Job

| Property | Value |
| --- | --- |
| Job ID | 684930910671649 |
| Name | Douyin KPI Anomaly Monitor (V2 M1) |
| Task key | run_kpi_monitor |
| Type | SparkPythonTask (Serverless) |
| Python file | /Workspace/Users/lithomas@wustl.edu/douyin-business-cockpit-agent/run_kpi_monitor.py |
| Environment | Default (client=1, Serverless) |
| Schedule | None (manual trigger only) |
| Parameters | `monitor_date` (default: empty = latest available) |

## Usage

### Trigger via UI
Navigate to Jobs > Douyin KPI Anomaly Monitor (V2 M1) > Run Now.
Set `monitor_date` to an ISO date (e.g. `2025-06-15`) or leave empty
for the latest available observation date.

### Trigger via CLI / SDK
```bash
databricks jobs run-now --job-id 684930910671649 --job-parameters '{"monitor_date": "2025-06-15"}'
```

```python
from databricks.sdk import WorkspaceClient
w = WorkspaceClient()
run = w.jobs.run_now(684930910671649, job_parameters={"monitor_date": "2025-06-15"})
```

## Idempotency

The monitor uses `MERGE INTO` on `(monitor_date, kpi_name)` for results
and `(monitor_date, kpi_name, rule_name)` for anomalies. Re-running for
the same date overwrites previous results — no duplicates.

## Constraints (V2 Milestone 1)

- No recurring schedule (manual trigger only)
- No email delivery
- No LLM / Genie calls
- Does not modify source tables or metric views
- Synthetic data covers Jan–Jun 2025 only