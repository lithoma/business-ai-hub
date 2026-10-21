# Databricks notebook source
# DBTITLE 1,Demo Watchdog — Independent Shutdown
# ── Demo Watchdog — Independent Shutdown Safeguard ──────────
# Runs as a SEPARATE job, independently of the main demo notebook.
# Reads demo_deadline from kpi_demo_control table (persists independently of demo_start_time).
# If the 10-minute deadline has passed, pauses the main job schedule.
# Idempotent: safe to rerun; never activates a paused schedule.
# Uses REST API directly (avoids SDK version compatibility issues).

import json, sys, time
from datetime import datetime, timezone, timedelta

# ── Parameters ──
dbutils.widgets.text("main_job_id", "154252282617018", "Main demo job ID")
dbutils.widgets.text("max_duration", "600", "Max demo duration (seconds)")
dbutils.widgets.text("control_catalog", "workspace", "Control table catalog")
dbutils.widgets.text("control_schema", "douyin", "Control table schema")
dbutils.widgets.text("test_mode", "false", "Test mode (skip actual pause, just report)")
dbutils.widgets.text("watchdog_job_id", "480870378532889", "Watchdog job ID for self-shutdown")
dbutils.widgets.text("watchdog_job_id", "480870378532889", "Watchdog job ID for self-shutdown")

MAIN_JOB_ID = dbutils.widgets.get("main_job_id")
MAX_DURATION = int(dbutils.widgets.get("max_duration"))
CONTROL_CATALOG = dbutils.widgets.get("control_catalog")
CONTROL_SCHEMA = dbutils.widgets.get("control_schema")
TEST_MODE = dbutils.widgets.get("test_mode").lower() == "true"

CONTROL_TABLE = f"{CONTROL_CATALOG}.{CONTROL_SCHEMA}.kpi_demo_control"

print("=" * 60)
print("Demo Watchdog — Independent Shutdown Safeguard")
print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
print(f"Main Job ID: {MAIN_JOB_ID}")
print(f"Max Duration: {MAX_DURATION}s")
print(f"Test Mode: {TEST_MODE}")
print("=" * 60)

# ── 1. Read demo deadline ──
print("\n[1] Reading demo deadline...")
try:
    control_rows = spark.sql(
        f"SELECT control_value FROM {CONTROL_TABLE} "
        f"WHERE control_key = 'demo_deadline'"
    ).collect()
except Exception as e:
    print(f"  Control table not accessible: {e}")
    print("  No demo session found — nothing to do.")
    dbutils.jobs.taskValues.set("watchdog_action", "NO_SESSION")
    dbutils.notebook.exit(json.dumps({"action": "NO_SESSION", "reason": "control table not accessible"}))

if not control_rows:
    print("  No demo_deadline record found.")
    print("  Demo not started or already cleaned up — nothing to do.")
    dbutils.jobs.taskValues.set("watchdog_action", "NO_SESSION")
    dbutils.notebook.exit(json.dumps({"action": "NO_SESSION", "reason": "no demo_deadline record"}))

demo_deadline_str = control_rows[0]["control_value"]
try:
    demo_deadline = datetime.fromisoformat(demo_deadline_str)
except Exception as e:
    print(f"  Could not parse demo_deadline: {demo_deadline_str} ({e})")
    print("  Corrupted deadline record — cleaning up.")
    spark.sql(f"DELETE FROM {CONTROL_TABLE} WHERE control_key = 'demo_deadline'")
    dbutils.jobs.taskValues.set("watchdog_action", "CLEANED_CORRUPT")
    dbutils.notebook.exit(json.dumps({"action": "CLEANED_CORRUPT", "reason": f"unparseable: {e}"}))

now = datetime.now(timezone.utc)
exceeded_by = (now - demo_deadline).total_seconds()

print(f"  Demo deadline: {demo_deadline.isoformat()}")
print(f"  Current time: {now.isoformat()}")
if exceeded_by <= 0:
    print(f"  Remaining: {-exceeded_by:.0f}s")
else:
    print(f"  Exceeded by: {exceeded_by:.0f}s")

if exceeded_by <= 0:
    remaining = -exceeded_by
    print(f"\n  Demo still active ({remaining:.0f}s remaining). No action needed.")
    dbutils.jobs.taskValues.set("watchdog_action", "STILL_ACTIVE")
    dbutils.notebook.exit(json.dumps({
        "action": "STILL_ACTIVE",
        "remaining_seconds": round(remaining, 0),
    }))

# ── 2. Deadline exceeded — pause main job schedule ──
print(f"\n[2] Deadline exceeded by {exceeded_by:.0f}s. Pausing main job...")

if TEST_MODE:
    print("  [TEST MODE] Skipping actual pause. Would pause job schedule.")
    dbutils.jobs.taskValues.set("watchdog_action", "TEST_EXPIRED")
    dbutils.notebook.exit(json.dumps({
        "action": "TEST_EXPIRED",
        "exceeded_by": round(exceeded_by, 0),
        "test_mode": True,
    }))

try:
    from databricks.sdk import WorkspaceClient
    w = WorkspaceClient()
    
    # Use REST API directly to avoid SDK version compatibility issues
    def _get_job(job_id):
        """Get job settings via REST API (returns dict)."""
        return w.api_client.do(
            method="GET",
            path=f"/api/2.1/jobs/get?job_id={job_id}",
        )
    
    def _update_job(job_id, new_settings):
        """Update job settings via REST API."""
        return w.api_client.do(
            method="POST",
            path="/api/2.1/jobs/update",
            body={"job_id": int(job_id), "new_settings": new_settings},
            headers={"Content-Type": "application/json"},
        )
    
    job_info = _get_job(MAIN_JOB_ID)
    settings = job_info.get("settings", {})
    
    paused_something = False
    action_detail = ""
    
    # Check schedule
    schedule = settings.get("schedule")
    if schedule and schedule.get("quartz_cron_expression"):
        current_pause = schedule.get("pause_status", "UNPAUSED")
        print(f"  Current schedule pause_status: {current_pause}")
        if current_pause == "PAUSED":
            print("  Schedule already PAUSED — nothing to do (idempotent).")
            paused_something = True
            action_detail = "already_paused"
        else:
            print("  Schedule is ACTIVE — pausing now.")
            _update_job(MAIN_JOB_ID, {
                "schedule": {
                    "quartz_cron_expression": schedule["quartz_cron_expression"],
                    "timezone_id": schedule.get("timezone_id", "UTC"),
                    "pause_status": "PAUSED",
                }
            })
            print("  ✓ Schedule PAUSED.")
            paused_something = True
            action_detail = "schedule_paused"
    
    # Check trigger (if no schedule)
    if not paused_something:
        trigger = settings.get("trigger")
        if trigger:
            current_pause = trigger.get("pause_status", "UNPAUSED")
            print(f"  Current trigger pause_status: {current_pause}")
            if current_pause == "PAUSED":
                print("  Trigger already PAUSED — nothing to do (idempotent).")
                paused_something = True
                action_detail = "already_paused"
            else:
                print("  Trigger is ACTIVE — pausing now.")
                tp = {"pause_status": "PAUSED"}
                if trigger.get("periodic"):
                    tp["periodic"] = trigger["periodic"]
                elif trigger.get("file_arrival"):
                    tp["file_arrival"] = trigger["file_arrival"]
                elif trigger.get("table_update"):
                    tp["table_update"] = trigger["table_update"]
                _update_job(MAIN_JOB_ID, {"trigger": tp})
                print("  ✓ Trigger PAUSED.")
                paused_something = True
                action_detail = "trigger_paused"
    
    if not paused_something:
        print("  No schedule or trigger found on main job — nothing to pause.")
        action_detail = "no_schedule"
    
    # ── 3. Verify the job is actually paused ──
    print("\n[3] Verifying main job is paused...")
    job_info2 = _get_job(MAIN_JOB_ID)
    settings2 = job_info2.get("settings", {})
    
    verified_paused = False
    schedule2 = settings2.get("schedule")
    if schedule2:
        ps = schedule2.get("pause_status", "UNPAUSED")
        print(f"  Schedule pause_status: {ps}")
        if ps == "PAUSED":
            verified_paused = True
    trigger2 = settings2.get("trigger")
    if trigger2:
        ps = trigger2.get("pause_status", "UNPAUSED")
        print(f"  Trigger pause_status: {ps}")
        if ps == "PAUSED":
            verified_paused = True
    if not schedule2 and not trigger2:
        print("  No schedule/trigger — job cannot auto-run. Verified.")
        verified_paused = True
    
    print(f"  Verified PAUSED: {verified_paused}")
    
    # ── 4. Clean up control record ──
    print("\n[4] Cleaning up demo control record...")
    spark.sql(f"DELETE FROM {CONTROL_TABLE} WHERE control_key = 'demo_deadline'")
    print("  ✓ Deadline record deleted.")
    
    # ── 5. Self-shutdown: pause watchdog's own schedule ──
    WATCHDOG_JOB_ID = dbutils.widgets.get("watchdog_job_id")
    if WATCHDOG_JOB_ID:
        print(f"\n[5] Self-shutdown: pausing watchdog job {WATCHDOG_JOB_ID}...")
        try:
            wd_info = _get_job(WATCHDOG_JOB_ID)
            wd_settings = wd_info.get("settings", {})
            wd_schedule = wd_settings.get("schedule")
            if wd_schedule and wd_schedule.get("quartz_cron_expression"):
                wd_pause = wd_schedule.get("pause_status", "UNPAUSED")
                if wd_pause != "PAUSED":
                    _update_job(WATCHDOG_JOB_ID, {
                        "schedule": {
                            "quartz_cron_expression": wd_schedule["quartz_cron_expression"],
                            "timezone_id": wd_schedule.get("timezone_id", "UTC"),
                            "pause_status": "PAUSED",
                        }
                    })
                    print(f"  ✓ Watchdog schedule PAUSED (self-shutdown).")
                else:
                    print(f"  Watchdog schedule already PAUSED.")
        except Exception as e:
            print(f"  Self-shutdown failed: {e}")
            # Non-fatal — main job is already paused
    
    result = {
        "action": "PAUSED",
        "detail": action_detail,
        "verified_paused": verified_paused,
        "exceeded_by": round(exceeded_by, 0),
    }
    print(f"\n{'=' * 60}")
    print(f"Watchdog result: {json.dumps(result, indent=2)}")
    print(f"{'=' * 60}")
    
    dbutils.jobs.taskValues.set("watchdog_action", action_detail)
    dbutils.jobs.taskValues.set("watchdog_verified_paused", verified_paused)
    _exit_payload = json.dumps(result)
    
except Exception as e:
    print(f"\n  ✗ Watchdog failed: {type(e).__name__}: {e}")
    try:
        dbutils.jobs.taskValues.set("watchdog_action", "ERROR")
        dbutils.jobs.taskValues.set("watchdog_error", str(e))
    except Exception:
        pass
    _exit_payload = json.dumps({"action": "ERROR", "error": str(e)})

# Exit outside try-except to avoid catching the internal exit exception
dbutils.notebook.exit(_exit_payload)

# COMMAND ----------

