"""Utility functions for job schedule control (pause/resume).

Provides a single function to pause a job's schedule or trigger,
used by the demo orchestrator for auto-stop and expiration safeguard.
"""
from __future__ import annotations


def pause_job_schedule(job_id: str) -> str | None:
    """Pause the job's schedule or trigger.

    Reads the current job settings, then re-sends the schedule or
    trigger with ``pause_status`` set to ``"PAUSED"``.  This pauses
    (does not remove) the schedule so it can be resumed later.

    Uses the REST API directly via ``api_client.do()`` to avoid
    SDK version compatibility issues with ``w.jobs.get()`` in
    serverless environments.

    Returns ``'schedule'``, ``'trigger'``, or ``None`` if nothing to pause.
    """
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()

    # Use REST API directly to avoid SDK version compatibility issues
    job_info = w.api_client.do(
        method="GET",
        path=f"/api/2.1/jobs/get?job_id={job_id}",
    )
    settings = job_info.get("settings", {})

    schedule = settings.get("schedule")
    if schedule and schedule.get("quartz_cron_expression"):
        w.api_client.do(
            method="POST",
            path="/api/2.1/jobs/update",
            body={
                "job_id": int(job_id),
                "new_settings": {
                    "schedule": {
                        "quartz_cron_expression": schedule["quartz_cron_expression"],
                        "timezone_id": schedule.get("timezone_id", "UTC"),
                        "pause_status": "PAUSED",
                    }
                },
            },
            headers={"Content-Type": "application/json"},
        )
        return "schedule"

    trigger = settings.get("trigger")
    if trigger:
        tp: dict = {"pause_status": "PAUSED"}
        if trigger.get("periodic"):
            tp["periodic"] = trigger["periodic"]
        elif trigger.get("file_arrival"):
            tp["file_arrival"] = trigger["file_arrival"]
        elif trigger.get("table_update"):
            tp["table_update"] = trigger["table_update"]
        w.api_client.do(
            method="POST",
            path="/api/2.1/jobs/update",
            body={"job_id": int(job_id), "new_settings": {"trigger": tp}},
            headers={"Content-Type": "application/json"},
        )
        return "trigger"

    return None
