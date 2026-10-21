"""Alert integration for KPI anomaly reports via Databricks SQL Alerts.

After the KPI monitoring pipeline detects anomalies and the A1 Agent
provides analysis, this module persists a structured report to a Delta
table.  A Databricks SQL Alert queries PENDING reports and sends an
email notification via native Databricks notification destinations.

Key design:
  * Normal days (no anomalies) never produce a PENDING report
  * Each alert_id is unique per mode + brand + scenario + business date
  * MERGE prevents duplicate reports for the same alert_id
  * report_status: PENDING -> NOTIFIED (updated after alert fires)
  * DEMO and PRODUCTION modes are strictly separated
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Dict, List, Optional


class AlertIntegration:
    """Persists structured anomaly reports for SQL Alert notification.

    Parameters
    ----------
    config : MonitorConfig
        Provides mode, brand, business_scope, catalog, schema.
    spark : SparkSession, optional
    """

    REPORTS_TABLE = "kpi_alert_reports"

    def __init__(self, config, spark=None):
        self.config = config
        if spark is None:
            from pyspark.sql import SparkSession
            spark = SparkSession.builder.getOrCreate()
        self.spark = spark

    @property
    def full_table(self) -> str:
        return f"{self.config.catalog}.{self.config.schema}.{self.REPORTS_TABLE}"

    def _build_alert_id(self, monitor_date: str) -> str:
        """Build stable alert ID: MODE|BRAND|SCENARIO|DATE."""
        mode = self.config.email_mode_label
        brand = "".join(c if c.isalnum() or c == "_" else "_" for c in self.config.brand)
        scenario = "".join(c if c.isalnum() or c == "_" else "_" for c in self.config.business_scope)
        return f"{mode}|{brand}|{scenario}|{monitor_date}"

    def _build_kpi_summary(self, monitoring_rows: List[dict]) -> str:
        """Build human-readable KPI summary from monitoring rows."""
        lines = []
        for r in monitoring_rows:
            val = f"{r['kpi_value']:,.2f}" if r.get('kpi_value') is not None else "N/A"
            prev = f"{r['prev_value']:,.2f}" if r.get('prev_value') is not None else "N/A"
            change = f"{r['pct_change']:+.1%}" if r.get('pct_change') is not None else "N/A"
            status = "异常" if r.get('is_anomaly') else "正常"
            lines.append(
                f"{r['kpi_display_name']}: 当前={val} 上期={prev} 变化={change} 状态={status}"
            )
        return "\n".join(lines)

    def _build_ai_insights(self, insights) -> str:
        """Build human-readable AI insights from AgentInsights.

        Never exposes raw SDK/Python error strings in the report.
        If Genie failed, returns a clean status indicating AI analysis
        was not available — the report never claims successful AI analysis.
        """
        if insights is None or not getattr(insights, 'success', False):
            error = getattr(insights, 'error', None) if insights else "Agent unavailable"
            # Sanitize: never expose raw SDK/Python errors in the report
            _RAW_KW = ['attribute', 'traceback', 'typeerror', 'module',
                       'importerror', 'no attribute', 'has no attribute',
                       'attributeerror', 'connection', 'timeout']
            if error and any(kw in error.lower() for kw in _RAW_KW):
                error = "Genie A1 分析服务不可用"
            return f"AI 分析暂时不可用: {error}"
        parts = [
            f"核心发现: {insights.core_finding or '(无)'}",
            f"分析: {insights.analysis or '(无)'}",
            f"尚待验证因素: {insights.uncertainties or '(无)'}",
        ]
        return "\n".join(parts)

    def _build_recommendations(self, anomaly_results) -> str:
        """Build human-readable recommendations from anomaly results.

        Uses the max severity per KPI (not the first anomaly's severity).
        """
        if not anomaly_results:
            return "继续正常监控，无需特殊行动。"
        sev_order = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
        kpi_max_sev = {}
        kpi_display = {}
        for a in anomaly_results:
            sev = sev_order.get(a.severity.name, 0)
            if a.kpi_name not in kpi_max_sev or sev > kpi_max_sev[a.kpi_name]:
                kpi_max_sev[a.kpi_name] = sev
                kpi_display[a.kpi_name] = a.kpi_display_name
        # Reverse map severity int back to name
        sev_names = {0: "INFO", 1: "WARNING", 2: "CRITICAL"}
        recs = []
        for kpi_name, sev_int in sorted(kpi_max_sev.items(), key=lambda x: -x[1]):
            recs.append(
                f"关注 {kpi_display[kpi_name]} 异常变化（{sev_names[sev_int]}），进一步排查原因。"
            )
        recs.append("将异常情况同步至相关业务团队，持续跟踪指标恢复情况。")
        return "\n".join(recs)

    def persist_report(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List,
        insights,
        monitor_date: str,
        run_id: str = "",
    ) -> dict:
        """Persist structured anomaly report to Delta table.

        Only called when anomalies are detected (normal days skip this).
        Uses MERGE to prevent duplicate reports for the same alert_id.

        Returns dict with alert_id, report_status, and row count.
        """
        alert_id = self._build_alert_id(monitor_date)
        now = datetime.now(timezone.utc)

        # Build severity string
        sev_order = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
        max_severity = max(
            (a.severity.name for a in anomaly_results),
            key=lambda s: sev_order.get(s, 0),
            default="NONE",
        ) if anomaly_results else "NONE"

        kpi_summary = self._build_kpi_summary(monitoring_rows)
        ai_insights = self._build_ai_insights(insights)
        recommendations = self._build_recommendations(anomaly_results)

        # Idempotent MERGE: only insert if alert_id doesn't exist
        self.spark.sql(f"""
            MERGE INTO {self.full_table} AS t
            USING (
                SELECT
                    '{alert_id}' AS alert_id,
                    '{monitor_date}' AS business_date,
                    '{self.config.brand}' AS brand,
                    '{self.config.email_mode_label}' AS mode,
                    {len(anomaly_results)} AS anomaly_count,
                    '{max_severity}' AS severity,
                    '''{kpi_summary.replace("'", "''")}''' AS kpi_summary,
                    '''{ai_insights.replace("'", "''")}''' AS ai_insights,
                    '''{recommendations.replace("'", "''")}''' AS recommendations,
                    'PENDING' AS report_status,
                    '{now.strftime("%Y-%m-%d %H:%M:%S")}' AS created_at
            ) AS s
              ON t.alert_id = s.alert_id AND t.mode = s.mode
            WHEN NOT MATCHED THEN INSERT *
        """)

        # Verify insertion
        rows = self.spark.sql(
            f"SELECT report_status FROM {self.full_table} "
            f"WHERE alert_id = '{alert_id}' AND mode = '{self.config.email_mode_label}'"
        ).collect()

        return {
            "alert_id": alert_id,
            "report_status": rows[0]["report_status"] if rows else "ERROR",
            "row_count": len(rows),
            "anomaly_count": len(anomaly_results),
            "severity": max_severity,
        }

    def mark_notified(self, alert_id: str, mode: str = None) -> int:
        """Mark a report as NOTIFIED after the SQL Alert fires.

        Returns the number of rows updated.
        """
        mode = mode or self.config.email_mode_label
        result = self.spark.sql(
            f"UPDATE {self.full_table} "
            f"SET report_status = 'NOTIFIED' "
            f"WHERE alert_id = '{alert_id}' "
            f"  AND mode = '{mode}' "
            f"  AND report_status = 'PENDING'"
        )
        return result.count // 1  # Approximate; Databricks doesn't expose exact count

    def mark_all_notified(self, mode: str = None) -> int:
        """Mark all PENDING reports as NOTIFIED for the given mode.

        WARNING: This should only be called in live mode after confirming
        the SQL Alert actually fired.  In DRY_RUN mode, use
        :meth:`mark_all_dry_run` instead — never mark reports NOTIFIED
        without delivery evidence.
        """
        mode = mode or self.config.email_mode_label
        self.spark.sql(
            f"UPDATE {self.full_table} "
            f"SET report_status = 'NOTIFIED' "
            f"WHERE mode = '{mode}' AND report_status = 'PENDING'"
        )
        pending = self.spark.sql(
            f"SELECT COUNT(*) AS cnt FROM {self.full_table} "
            f"WHERE mode = '{mode}' AND report_status = 'PENDING'"
        ).collect()[0]["cnt"]
        return pending

    def mark_all_dry_run(self, mode: str = None) -> int:
        """Mark all PENDING reports as DRY_RUN for the given mode.

        Used in DRY_RUN mode to simulate the alert lifecycle without
        sending real emails.  Reports marked DRY_RUN are NOT re-armed
        by re_arm_report (which only targets PENDING), so the SQL Alert
        query (which counts PENDING) will find 0 and evaluate to OK —
        no email is sent.
        """
        mode = mode or self.config.email_mode_label
        self.spark.sql(
            f"UPDATE {self.full_table} "
            f"SET report_status = 'DRY_RUN' "
            f"WHERE mode = '{mode}' AND report_status = 'PENDING'"
        )
        pending = self.spark.sql(
            f"SELECT COUNT(*) AS cnt FROM {self.full_table} "
            f"WHERE mode = '{mode}' AND report_status = 'PENDING'"
        ).collect()[0]["cnt"]
        return pending

    def get_pending_count(self, mode: str = None) -> int:
        """Return the number of PENDING reports for the given mode."""
        mode = mode or self.config.email_mode_label
        return self.spark.sql(
            f"SELECT COUNT(*) AS cnt FROM {self.full_table} "
            f"WHERE mode = '{mode}' AND report_status = 'PENDING'"
        ).collect()[0]["cnt"]

    # ------------------------------------------------------------------ #
    # SQL Alert trigger and re-arm (M4 demo automation)
    # ------------------------------------------------------------------ #

    SQL_ALERT_ID = "3526956768809863"

    def trigger_alert_evaluation(self, alert_id: str = None) -> dict:
        """Trigger a SQL Alert evaluation via the Databricks SDK.

        Returns a dict with status ("triggered" or "error") and details.
        """
        alert_id = alert_id or self.SQL_ALERT_ID
        try:
            from databricks.sdk import WorkspaceClient
            w = WorkspaceClient()
            w.alerts.run(alert_id=alert_id)
            return {"status": "triggered", "alert_id": alert_id}
        except Exception as e:
            return {"status": "error", "alert_id": alert_id, "error": str(e)}

    def check_alert_fired(
        self, alert_id: str = None, after_time: str = None,
        poll_seconds: int = 90, poll_interval: int = 5,
    ) -> dict:
        """Check whether the SQL Alert has fired since ``after_time``.

        Polls ``system.alert.alert_evaluation_history`` for up to
        ``poll_seconds``, waiting for delayed system-table entries.

        Parameters
        ----------
        after_time : str
            ISO timestamp.  Only evaluations starting at or after this
            time are considered, so the result correlates with the
            correct PENDING report (not a stale prior evaluation).
        poll_seconds : int
            Total seconds to wait for delayed history entries.
            System tables can lag by 10-30 seconds.
        poll_interval : int
            Seconds between poll attempts.

        Returns a dict with:
            fired (bool), evaluated_state (str), alert_run_id (str),
            start_time (str), end_time (str), error (str|None),
            attempts (int).
        """
        import time
        alert_id = alert_id or self.SQL_ALERT_ID

        query = (
            f"SELECT alert_run_id, evaluated_state, start_time, end_time "
            f"FROM system.alert.alert_evaluation_history "
            f"WHERE alert_id = '{alert_id}'"
        )
        if after_time:
            query += f" AND start_time >= '{after_time}'"
        query += " ORDER BY start_time DESC LIMIT 1"

        deadline = time.time() + poll_seconds
        attempt = 0
        while time.time() < deadline:
            attempt += 1
            rows = self.spark.sql(query).collect()
            if rows:
                r = rows[0]
                state = r["evaluated_state"]
                fired = state in ("TRIGGERED", "ALERT")
                return {
                    "fired": fired,
                    "evaluated_state": state,
                    "alert_run_id": r["alert_run_id"],
                    "start_time": str(r["start_time"]),
                    "end_time": str(r["end_time"]),
                    "error": None,
                    "attempts": attempt,
                }
            time.sleep(poll_interval)

        return {
            "fired": False,
            "evaluated_state": "UNKNOWN",
            "alert_run_id": None,
            "start_time": None,
            "end_time": None,
            "error": f"No evaluation found within {poll_seconds}s",
            "attempts": attempt,
        }

    def re_arm_report(
        self, alert_id: str, mode: str = None,
        after_time: str = None, poll_seconds: int = 90,
    ) -> dict:
        """Re-arm a PENDING report after confirming the SQL Alert fired.

        Polls ``system.alert.alert_evaluation_history`` for a TRIGGERED
        evaluation occurring after ``after_time`` (the PENDING report's
        creation timestamp).  Only marks the report NOTIFIED if the alert
        actually fired; handles delayed system-table entries with retries.

        IMPORTANT: Only targets reports with status ``PENDING`` \u2014 reports
        with status ``DRY_RUN`` are left untouched (they were created in
        dry-run mode and should not be re-armed).

        Parameters
        ----------
        alert_id : str
            The report alert_id to re-arm.
        after_time : str
            ISO timestamp of the PENDING report's creation.  Only
            evaluations after this time are considered.
        poll_seconds : int
            How long to wait for delayed history entries.

        Returns a dict with:
            re_armed (bool), fired (bool), evaluated_state (str),
            remaining_pending (int), error (str|None).
        """
        mode = mode or self.config.email_mode_label

        # Check that the report is still PENDING (not already DRY_RUN or NOTIFIED)
        rows = self.spark.sql(
            f"SELECT report_status FROM {self.full_table} "
            f"WHERE alert_id = '{alert_id}' AND mode = '{mode}'"
        ).collect()
        if not rows:
            return {
                "alert_id": alert_id,
                "re_armed": False,
                "fired": False,
                "evaluated_state": "NOT_FOUND",
                "remaining_pending": self.get_pending_count(mode),
                "error": "Report not found",
            }
        current_status = rows[0]["report_status"]
        if current_status != "PENDING":
            return {
                "alert_id": alert_id,
                "re_armed": False,
                "fired": False,
                "evaluated_state": current_status,
                "remaining_pending": self.get_pending_count(mode),
                "error": f"Report is {current_status}, not PENDING \u2014 skipping re-arm",
            }

        # Poll for alert evaluation after the report was created
        result = self.check_alert_fired(
            alert_id=self.SQL_ALERT_ID,
            after_time=after_time,
            poll_seconds=poll_seconds,
        )

        if not result["fired"]:
            return {
                "alert_id": alert_id,
                "re_armed": False,
                "fired": False,
                "evaluated_state": result["evaluated_state"],
                "remaining_pending": self.get_pending_count(mode),
                "error": result.get("error", "Alert did not fire"),
            }

        # Alert fired \u2014 mark as NOTIFIED to re-arm
        self.spark.sql(
            f"UPDATE {self.full_table} "
            f"SET report_status = 'NOTIFIED' "
            f"WHERE alert_id = '{alert_id}' "
            f"  AND mode = '{mode}' "
            f"  AND report_status = 'PENDING'"
        )
        remaining = self.get_pending_count(mode)
        return {
            "alert_id": alert_id,
            "re_armed": remaining == 0,
            "fired": True,
            "evaluated_state": result["evaluated_state"],
            "remaining_pending": remaining,
            "error": None,
        }