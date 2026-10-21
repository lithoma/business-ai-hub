# Databricks notebook source
# DBTITLE 1,Demo Orchestrator — Overview
# MAGIC %md
# MAGIC # Demo Orchestrator — KPI Anomaly Notification Workflow
# MAGIC
# MAGIC This notebook runs **one simulated day** of the KPI monitoring demo:
# MAGIC
# MAGIC 1. **Simulation** — Generate scripted KPI observations (Day 1: ANOMALY, Day 2: NORMAL, Day 3: ANOMALY)
# MAGIC 2. **Anomaly Detection** — Run deterministic anomaly rules (z-score, pct-change, absolute bounds)
# MAGIC 3. **Genie A1 Analysis** — Call A1 Genie Space (or mock) for business insights
# MAGIC 4. **Persist Report** — Write PENDING report to `kpi_alert_reports` table
# MAGIC 5. **SQL Alert Trigger** — Trigger SQL Alert evaluation to send email notification
# MAGIC 6. **Re-arm** — Verify alert fired, mark report as NOTIFIED to re-arm for next anomaly day
# MAGIC 7. **Auto-stop** — After 3 simulated days, stop the job schedule
# MAGIC
# MAGIC **Safety:**
# MAGIC - Normal days skip Genie, report, and email entirely
# MAGIC - Reports are only marked NOTIFIED after confirming alert fired (delivery evidence)
# MAGIC - DEMO and PRODUCTION modes are strictly isolated
# MAGIC - `mock_agent=True` avoids live Genie calls during development

# COMMAND ----------

# DBTITLE 1,Configuration
# ── Configuration ──────────────────────────────────────────────
import os, sys, time, uuid
from datetime import datetime, timezone, timedelta

# Ensure project root is importable
_project_root = "/Workspace/Users/lithomas@wustl.edu/douyin-business-cockpit-agent"
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

from kpi_monitor import (
    KPIMonitor, MonitorConfig,
    SimulationEngine, AgentIntegration, ReportRenderer,
    AlertIntegration,
)

# ── Widgets (job parameters) ──
dbutils.widgets.text("job_id", "", "Job ID for auto-stop")
dbutils.widgets.text("dry_run", "true", "Dry run (skip alert trigger)")
dbutils.widgets.text("mock_agent", "true", "Mock agent (skip Genie)")
dbutils.widgets.text("reset_sim", "false", "Reset simulation cursor")
dbutils.widgets.text("live_demo", "false", "Live demo mode (validates config)")

# ── Config ──
config = MonitorConfig.default()
config.demo_mode = True
config.sim_scripted_mode = True
config.sim_scripted_scenarios = ["ANOMALY", "NORMAL", "ANOMALY"]

# Mock agent: skip live Genie calls during development.
# Set to False for the real demo run.
config.mock_agent = dbutils.widgets.get("mock_agent").lower() == "true"

# Dry run: when True, SQL Alert Task still evaluates but
# the notebook skips persisting PENDING reports (no notification).
# Set to False for the real demo run.
DRY_RUN = dbutils.widgets.get("dry_run").lower() == "true"

# Job ID for auto-stop (passed as {{job.id}} by the job)
JOB_ID = dbutils.widgets.get("job_id") or ""
SQL_ALERT_ID = "3526956768809863"
MAX_DEMO_DURATION_SECONDS = 600  # 10 minutes

# ── LIVE-mode configuration validation ──
# When live_demo=true, validate that all required params are correctly set.
# This prevents silent fallback to widget defaults if notebook_params are lost.
_RESET_SIM = dbutils.widgets.get("reset_sim").lower() == "true"
LIVE_DEMO = dbutils.widgets.get("live_demo").lower() == "true"
if LIVE_DEMO:
    _config_errors = []
    if DRY_RUN:
        _config_errors.append("dry_run=true (must be false for live demo)")
    if config.mock_agent:
        _config_errors.append("mock_agent=true (must be false for live demo)")
    if not JOB_ID:
        _config_errors.append("job_id is empty (must be {{job.id}} for auto-stop)")
    # Note: reset_sim=true is valid for initialization runs — not validated here.
    # Its widget default is "false" (safe for scheduled runs).
    if _config_errors:
        print("✗ LIVE DEMO CONFIGURATION ERROR:")
        for err in _config_errors:
            print(f"  - {err}")
        print("  notebook_params may have been lost during job edit.")
        print("  Aborting to prevent DRY_RUN fallback.")
        dbutils.jobs.taskValues.set("should_trigger_alert", "false")
        dbutils.notebook.exit("CONFIG_ERROR: " + "; ".join(_config_errors))

# ── Task values for alert gate (safety default) ──
# Default: do not trigger alert. Set to "true" only at the end
# of the notebook if anomalies found and not in dry-run mode.
# This ensures the SQL Alert Task is skipped on early exits.
try:
    dbutils.jobs.taskValues.set("should_trigger_alert", "false")
except Exception:
    pass

# ── 10-minute expiration safeguard ──
# Independent of notebook logic: disables the schedule even if
# subsequent cells fail. Uses a control table to track demo start time.
spark.sql(f"""
    CREATE TABLE IF NOT EXISTS {config.catalog}.{config.schema}.kpi_demo_control (
        control_key    STRING NOT NULL,
        control_value  STRING NOT NULL,
        updated_at     TIMESTAMP
    ) USING DELTA
    COMMENT 'Demo orchestrator control table for expiration safeguard.'
""")

_control = spark.sql(
    f"SELECT control_value FROM {config.catalog}.{config.schema}.kpi_demo_control "
    f"WHERE control_key = 'demo_start_time'"
).collect()

_RESET_SIM = dbutils.widgets.get("reset_sim").lower() == "true"

if _control and _RESET_SIM:
    # Reset mode: clear stale control record so the timer starts fresh
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key IN ('demo_start_time', 'demo_deadline')")
    _control = []
    print("[DEMO] Cleared stale demo control records (reset_sim=true).")

if _control:
    demo_start = datetime.fromisoformat(_control[0]["control_value"])
    elapsed = (datetime.now(timezone.utc) - demo_start).total_seconds()
    if elapsed > MAX_DEMO_DURATION_SECONDS:
        print(f"WARNING: Demo expired ({elapsed:.0f}s > {MAX_DEMO_DURATION_SECONDS}s).")
        print("  Current day will be processed, then schedule will be stopped.")
        _DEMO_EXPIRED = True
        # Delete control record so future runs start fresh
        spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key = 'demo_start_time'")
        # Do NOT exit — let the current day complete processing
    else:
        _DEMO_EXPIRED = False
else:
    _DEMO_EXPIRED = False
    now_str = datetime.now(timezone.utc).isoformat()
    _deadline_str = (datetime.now(timezone.utc) + timedelta(seconds=MAX_DEMO_DURATION_SECONDS)).isoformat()
    spark.sql(
        f"INSERT INTO {config.catalog}.{config.schema}.kpi_demo_control "
        f"VALUES ('demo_start_time', '{now_str}', current_timestamp())"
    )
    spark.sql(
        f"INSERT INTO {config.catalog}.{config.schema}.kpi_demo_control "
        f"VALUES ('demo_deadline', '{_deadline_str}', current_timestamp())"
    )
    print(f"  Demo start time recorded: {now_str}")
    print(f"  Demo deadline recorded: {_deadline_str}")

print("=" * 60)
print("Demo Orchestrator — KPI Anomaly Notification")
print(f"Scripted scenarios: {config.sim_scripted_scenarios}")
print(f"Mock agent: {config.mock_agent}")
print(f"Dry run: {DRY_RUN}")
print(f"Job ID: {JOB_ID or '(not set)'}")
print(f"SQL Alert ID: {SQL_ALERT_ID}")

# ── SDK & Genie connectivity check (runs on every job start) ──
try:
    import importlib.metadata as _im
    _sdk_ver = _im.version('databricks-sdk')
    from databricks.sdk import WorkspaceClient as _WC
    _wc = _WC()
    _has_genie = hasattr(_wc, 'genie')
    print(f"  Databricks SDK: v{_sdk_ver}")
    print(f"  Genie API available: {_has_genie}")
    dbutils.jobs.taskValues.set("sdk_version", _sdk_ver)
    dbutils.jobs.taskValues.set("genie_available", str(_has_genie).lower())
    del _wc
except Exception as _e:
    print(f"  SDK check failed: {_e}")
    dbutils.jobs.taskValues.set("sdk_version", f"error: {type(_e).__name__}")
    dbutils.jobs.taskValues.set("genie_available", "false")

print("=" * 60)

# COMMAND ----------

# DBTITLE 1,Simulation Step
sim_engine = SimulationEngine(config)

# ── Initialization mode (reset_sim=true): warmup only, no Day 1 ──
if dbutils.widgets.get("reset_sim").lower() == "true":
    print("[INIT] Resetting simulation cursor...")
    sim_engine.reset()
    print(f"  Cursor reset to {config.sim_start_date}")
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key IN ('demo_start_time', 'demo_deadline')")
    # Also clean old DEMO reports so DRY_RUN status starts fresh
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_alert_reports WHERE mode = 'DEMO'")
    print("  Cleared old DEMO alert reports")
    print("[INIT] Warming up simulation (10 normal days)...")
    sim_engine.warmup(num_days=10)
    print("\n[INIT] Initialization complete. Set reset_sim=false for Day 1.")
    dbutils.notebook.exit("Initialization complete - warmup done, no scenario consumed.")

# ── Step 0: Re-arm PENDING reports from previous runs (live mode only) ──
# In live mode, check if the SQL Alert fired for any PENDING reports from
# previous runs. If TRIGGERED, mark as NOTIFIED to re-arm for the next anomaly.
# In DRY_RUN mode, reports are DRY_RUN (not PENDING), so this is a no-op.
if not DRY_RUN:
    _alert_int = AlertIntegration(config)
    _pending_rows = spark.sql(
        f"SELECT alert_id, created_at FROM {config.catalog}.{config.schema}.kpi_alert_reports "
        f"WHERE mode = '{config.email_mode_label}' AND report_status = 'PENDING'"
    ).collect()
    if _pending_rows:
        print(f"\n--- Step 0: Re-arming {len(_pending_rows)} PENDING report(s) ---")
    for _row in _pending_rows:
        _aid = _row["alert_id"]
        _cat = _row["created_at"]
        _cat_str = _cat.isoformat() if hasattr(_cat, "isoformat") else str(_cat)
        _result = _alert_int.re_arm_report(
            alert_id=_aid,
            after_time=_cat_str,
            poll_seconds=30,
        )
        if _result["re_armed"]:
            print(f"  [RE-ARM] Report {_aid} -> NOTIFIED (alert was TRIGGERED)")
        elif _result.get("error"):
            print(f"  [RE-ARM] Pending for {_aid}: {_result['error']}")
    if _pending_rows:
        print("  Re-arm check complete.")

sim_result = sim_engine.run()

# If simulation was already PAUSED (no scenario was run), exit.
# auto_paused=True means the last scenario WAS run and cursor was set
# to PAUSED — in that case, continue processing the generated day.
if sim_result["status"] == "PAUSED" and not sim_result.get("auto_paused"):
    print(f"\n[DEMO] Simulation is PAUSED at {sim_result.get('sim_date', 'N/A')}.")
    print("  All scripted scenarios complete. Use --reset-sim to restart.")
    # Auto-stop the job if simulation is done
    if JOB_ID:
        try:
            from kpi_monitor.job_control import pause_job_schedule
            result = pause_job_schedule(JOB_ID)
            if result:
                print(f"  Job {JOB_ID} {result} PAUSED (auto-stop).")
            else:
                print(f"  Job {JOB_ID} has no active schedule/trigger - nothing to pause.")
            print("  All 3 demo days complete within 10-minute window.")
        except Exception as e:
            print(f"  Auto-stop failed: {e}")
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key = 'demo_start_time'")
    dbutils.jobs.taskValues.set("should_trigger_alert", "false")
    dbutils.notebook.exit("Simulation complete - stopping.")

sim_date = sim_result["sim_date"]
obs = sim_result["observations"]
cursor = sim_engine.get_cursor()
scenario_idx = cursor["run_count"] - 1  # just advanced
scenarios = config.sim_scripted_scenarios
scenario_label = scenarios[scenario_idx] if 0 <= scenario_idx < len(scenarios) else "UNKNOWN"

print(f"\n[DEMO] Sim date: {sim_date}")
print(f"  Scenario: {scenario_label}")
print(f"  GMV: {obs['gmv_with_coupon_before_refund']:,.2f}")
print(f"  Sales qty: {obs['sales_quantity']:,}")
print(f"  Refund amt: {obs['refund_amount']:,.2f}")
print(f"  Anomaly injected: {obs['injected_anomaly']} ({obs['affected_kpi'] or 'none'})")
if sim_result.get("auto_paused"):
    print("  All scenarios complete - simulation auto-paused.")

# COMMAND ----------

# DBTITLE 1,Anomaly Detection
# ── Step 2: Anomaly Detection ─────────────────────────────────
monitor = KPIMonitor(config)
monitoring_rows, anomaly_results = monitor.run(
    monitor_date=sim_date,
    persist=True,
)

# The detector now uses a healthy baseline (rolling mean) for pct_change
# when the previous day was an outlier, so recovery false positives are
# handled without relying on simulation labels.

print(f"\n--- Monitoring Results ({len(monitoring_rows)} KPIs) ---")
for r in monitoring_rows:
    val_str = f"{r['kpi_value']:,.2f}" if r['kpi_value'] is not None else "N/A"
    flag = "ANOMALY " if r['is_anomaly'] else "  "
    print(f"{flag}{r['kpi_display_name']:30s} value={val_str:>15s}  anomalies={r['anomaly_count']}")

# ── Normal day: skip Genie, report, and email ──
if not anomaly_results:
    print("\n--- No anomalies detected (NORMAL) ---")
    print("  Skipping Genie call, report generation, and alert notification.")
    print("\nDemo run complete (NORMAL day).")
else:
    print(f"\n--- Anomalies Detected ({len(anomaly_results)}) ---")
    for a in anomaly_results:
        print(f"  [{a.severity.name}] {a.kpi_display_name} | {a.rule_name} | {a.description}")

# COMMAND ----------

# DBTITLE 1,Genie Analysis + Persist Report
# ── Step 3: Genie A1 Analysis + Persist Report (ANOMALY days only) ─
if anomaly_results:
    # Call Genie A1 (or mock if mock_agent=True)
    print("\n--- Calling A1 Genie Agent ---")
    agent = AgentIntegration(config)
    insights = agent.analyze_anomaly(
        monitoring_rows, anomaly_results,
        sim_info={"sim_date": sim_date, "run_id": sim_result.get("run_id", "")},
    )
    if insights.success:
        print(f"  ✅ Genie A1 analysis succeeded")
        print(f"  Core finding: {insights.core_finding[:100]}...")
    else:
        print(f"  ❌ Genie A1 analysis FAILED: {insights.error}")
        print(f"  Report will note AI analysis unavailable.")

    # Persist PENDING report for SQL Alert
    print("\n--- SQL Alert Report Persistence ---")
    alert_int = AlertIntegration(config)
    report_info = alert_int.persist_report(
        monitoring_rows=monitoring_rows,
        anomaly_results=anomaly_results,
        insights=insights,
        monitor_date=sim_date,
        run_id=sim_result.get("run_id", ""),
    )
    print(f"  Alert ID      : {report_info['alert_id']}")
    print(f"  Report status : {report_info['report_status']}")
    print(f"  Anomaly count : {report_info['anomaly_count']}")
    print(f"  Severity      : {report_info['severity']}")
    pending = alert_int.get_pending_count()
    print(f"  Pending count : {pending}")

# COMMAND ----------

# DBTITLE 1,Alert Trigger + Re-arm
# ── Step 4: Alert notification (via SQL Alert Task in job) ───────
# In the real demo, the job has a SQL Alert Task that runs AFTER this
# notebook task. It evaluates the SQL Alert and sends email if TRIGGERED.
# The re-arm happens at the START of the next run (Step 0).
#
# In DRY_RUN mode: persist PENDING report, then immediately mark as
# DRY_RUN so the SQL Alert Task (if present) finds pending_count=0
# and does NOT send a real email. This allows testing the full state
# transition without triggering real notifications.
# Reports marked DRY_RUN are NOT re-armed by re_arm_report (which only
# targets PENDING), so they won't interfere with live alert evaluations.
if anomaly_results and DRY_RUN:
    print("\n--- [DRY RUN] Simulating alert lifecycle ---")
    print(f"  PENDING report persisted: {report_info['alert_id']}")
    # Mark as DRY_RUN to prevent SQL Alert Task from finding PENDING reports
    alert_int.mark_all_dry_run()
    print(f"  Report marked DRY_RUN (no real email sent)")
    print(f"  Remaining pending: {alert_int.get_pending_count()}")
elif anomaly_results:
    print("\n--- [LIVE] PENDING report persisted for SQL Alert Task ---")
    print(f"  Alert ID: {report_info['alert_id']}")
    print(f"  Pending count: {alert_int.get_pending_count()}")
    print("  SQL Alert Task will evaluate and send notification if TRIGGERED.")
    print("  Re-arm will happen at the start of the next run.")

# ── Set task values for alert gate ──
try:
    # Always evaluate alert on every day (including NORMAL) so TRIGGERED→OK transition occurs.
    # The scoped SQL Alert query returns OK on normal days (no PENDING report for current sim date).
    _should_trigger = str(not DRY_RUN).lower()
    dbutils.jobs.taskValues.set("should_trigger_alert", _should_trigger)
    print(f"[GATE] should_trigger_alert = {_should_trigger}")
except Exception as e:
    print(f"[GATE] Failed to set task values: {e}")

print("\nDemo run complete.")

# COMMAND ----------

# DBTITLE 1,Auto-stop
# ── Step 5: Auto-stop after all scenarios ──────────────────────
if sim_result.get("auto_paused") and JOB_ID:
    print("\n--- Auto-stopping Demo Job ---")
    try:
        from kpi_monitor.job_control import pause_job_schedule
        result = pause_job_schedule(JOB_ID)
        if result:
            print(f"  Job {JOB_ID} {result} PAUSED (auto-stop).")
        else:
            print(f"  Job {JOB_ID} has no active schedule/trigger - nothing to pause.")
        print("  All 3 demo days complete within 10-minute window.")
    except Exception as e:
        print(f"  Auto-stop failed: {e}")
        print("  Manual pause required.")
    # Clear demo control record
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key = 'demo_start_time'")
elif sim_result.get("auto_paused"):
    print("\n  All scenarios complete but no JOB_ID set - cannot auto-stop.")
    print("  Manually disable the job trigger.")
elif _DEMO_EXPIRED and JOB_ID:
    print("\n--- Stopping Demo Job (expired) ---")
    try:
        from kpi_monitor.job_control import pause_job_schedule
        result = pause_job_schedule(JOB_ID)
        if result:
            print(f"  Job {JOB_ID} {result} PAUSED (expiration safeguard).")
        else:
            print(f"  Job {JOB_ID} has no active schedule/trigger - nothing to pause.")
    except Exception as e:
        print(f"  Auto-stop failed: {e}")
    spark.sql(f"DELETE FROM {config.catalog}.{config.schema}.kpi_demo_control WHERE control_key = 'demo_start_time'")

# COMMAND ----------

