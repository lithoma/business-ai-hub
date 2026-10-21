#!/usr/bin/env python3
"""Databricks Job entry point for KPI anomaly monitoring.

Usage:
    # Production mode (default)
    python run_kpi_monitor.py --monitor-date 2025-06-15

    # Demo / simulation mode
    python run_kpi_monitor.py --demo

    # Demo mode with custom seed
    python run_kpi_monitor.py --demo --seed 123

    # Skip Genie agent and report generation
    python run_kpi_monitor.py --demo --no-agent
"""

from __future__ import annotations

import argparse
import os
import sys

# Ensure the project root is importable when run as a standalone file
_project_root = os.path.dirname(os.path.abspath(__file__))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Douyin KPI Anomaly Monitor (V2 M3)",
    )
    parser.add_argument(
        "--monitor-date", type=str, default=None,
        help="ISO date to evaluate. If omitted, uses latest available.",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help="Enable demo / simulation mode.",
    )
    parser.add_argument(
        "--seed", type=int, default=None,
        help="Simulation random seed (demo mode only).",
    )
    parser.add_argument(
        "--no-persist", action="store_true",
        help="Skip writing results to Delta tables.",
    )
    parser.add_argument(
        "--no-agent", action="store_true",
        help="Skip Genie agent call and report generation.",
    )
    parser.add_argument(
        "--no-email", action="store_true",
        help="Skip email delivery.",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Prepare email but do not actually send (overrides config).",
    )
    parser.add_argument(
        "--reset-sim", action="store_true",
        help="Reset simulation cursor before running (demo mode only).",
    )
    parser.add_argument(
        "--pause-sim", action="store_true",
        help="Pause simulation cursor (demo mode only).",
    )
    args = parser.parse_args()

    # Import after sys.path is set
    from kpi_monitor import (
        KPIMonitor, MonitorConfig,
        SimulationEngine, AgentIntegration, ReportRenderer,
        EmailSender, AlertIntegration,
    )

    config = MonitorConfig.default()
    config.demo_mode = args.demo
    if args.seed is not None:
        config.sim_seed = args.seed

    if args.dry_run:
        config.email_dry_run = True
    if args.no_email:
        config.email_enabled = False

    print("=" * 60)
    print("KPI Anomaly Monitor — V2 M3")
    print(f"Mode         : {'DEMO (simulation)' if args.demo else 'PRODUCTION'}")
    print(f"Monitor date : {args.monitor_date or '(auto)'}")
    print(f"Source       : {'sim table' if args.demo else config.source_view}")
    print(f"Results table: {config.full_results_table}")
    print(f"Anomaly table: {config.full_anomalies_table}")
    print(f"KPIs         : {[k.name for k in config.kpis]}")
    print("=" * 60)

    sim_info = None

    # --- Demo mode: run simulation step first ---
    if args.demo:
        sim_engine = SimulationEngine(config)

        if args.reset_sim:
            print("\n[DEMO] Resetting simulation cursor...")
            sim_engine.reset()
            print(f"  Cursor reset to {config.sim_start_date}")

        if args.pause_sim:
            print("\n[DEMO] Pausing simulation...")
            sim_engine.pause()
            print("  Simulation paused.")
            return

        print("\n[DEMO] Running simulation step...")
        sim_result = sim_engine.run()

        if sim_result["status"] == "PAUSED":
            print(f"  Simulation is PAUSED at {sim_result['sim_date']}. Use --reset-sim to resume.")
            return

        sim_date = sim_result["sim_date"]
        obs = sim_result["observations"]
        print(f"  Sim date: {sim_date}")
        print(f"  GMV: {obs['gmv_with_coupon_before_refund']:,.2f}")
        print(f"  Sales qty: {obs['sales_quantity']:,}")
        print(f"  Refund amt: {obs['refund_amount']:,.2f}")
        print(f"  Anomaly injected: {obs['injected_anomaly']} ({obs['affected_kpi'] or 'none'})")
        print(f"  Advanced to: {sim_result['advanced_to']}")

        sim_info = {
            "sim_date": sim_date,
            "comparison_date": "上一模拟日",
            "run_id": sim_result.get("run_id", ""),
        }
        monitor_date = sim_date
    else:
        # --- Production mode: check freshness ---
        monitor = KPIMonitor(config)
        freshness = monitor.check_data_freshness()
        if freshness:
            print(f"\n[PROD] Data freshness warning: {freshness}")
            print("  Continuing with available data...")
        monitor_date = args.monitor_date

    # --- Run anomaly detection ---
    monitor = KPIMonitor(config)
    monitoring_rows, anomaly_results = monitor.run(
        monitor_date=monitor_date,
        persist=not args.no_persist,
    )

    print(f"\n--- Monitoring Results ({len(monitoring_rows)} KPIs) ---")
    for r in monitoring_rows:
        val_str = f"{r['kpi_value']:,.2f}" if r["kpi_value"] is not None else "N/A"
        prev_str = f"{r['prev_value']:,.2f}" if r["prev_value"] is not None else "N/A"
        z_str = f"{r['z_score']:+.2f}" if r["z_score"] is not None else "N/A"
        flag = "ANOMALY " if r["is_anomaly"] else "  "
        print(
            f"{flag}{r['kpi_display_name']:30s} "
            f"value={val_str:>15s}  prev={prev_str:>15s}  "
            f"z={z_str:>7s}  anomalies={r['anomaly_count']}"
        )

    # --- Agent integration (only if anomalies detected) ---
    if anomaly_results:
        print(f"\n--- Anomalies Detected ({len(anomaly_results)}) ---")
        for a in anomaly_results:
            print(f"  [{a.severity.name}] {a.kpi_display_name} | {a.rule_name} | {a.description}")

        if not args.no_agent:
            print("\n--- Calling A1 Genie Agent ---")
            agent = AgentIntegration(config)
            insights = agent.analyze_anomaly(
                monitoring_rows, anomaly_results, sim_info=sim_info,
            )
            if insights.success:
                print(f"  Core finding: {insights.core_finding[:100]}...")
            else:
                print(f"  Agent failed: {insights.error}")

            # --- Render report ---
            print("\n--- Generating HTML Report ---")
            renderer = ReportRenderer()
            run_id = sim_info.get("run_id", "") if sim_info else ""
            html = renderer.render_report(
                monitoring_rows, anomaly_results, insights,
                config, sim_info=sim_info, run_id=run_id,
            )
            report_path = os.path.join(_project_root, "output", f"report_{monitor_date}.html")
            os.makedirs(os.path.dirname(report_path), exist_ok=True)
            with open(report_path, "w", encoding="utf-8") as f:
                f.write(html)
            print(f"  Report saved to: {report_path}")

            # --- Persist structured report for SQL Alert ---
            print("\n--- SQL Alert Report Persistence ---")
            try:
                alert_int = AlertIntegration(config)
                report_info = alert_int.persist_report(
                    monitoring_rows=monitoring_rows,
                    anomaly_results=anomaly_results,
                    insights=insights,
                    monitor_date=monitor_date,
                    run_id=run_id,
                )
                print(f"  Alert ID      : {report_info['alert_id']}")
                print(f"  Report status : {report_info['report_status']}")
                print(f"  Anomaly count : {report_info['anomaly_count']}")
                print(f"  Severity      : {report_info['severity']}")
                print(f"  Table         : {alert_int.full_table}")
                pending = alert_int.get_pending_count()
                print(f"  Pending count : {pending}")
                print("  → SQL Alert will detect PENDING report and send notification.")
            except Exception as e:
                print(f"  Report persistence failed: {e}")
                print("  Continuing — monitoring results are already saved.")

            # --- Send email (legacy, optional) ---
            if not args.no_email:
                print("\n--- Email Delivery ---")
                anomaly_count = len(anomaly_results)
                sev_order = {"INFO": 0, "WARNING": 1, "CRITICAL": 2}
                max_sev = max(
                    (a.severity.name for a in anomaly_results),
                    key=lambda s: sev_order.get(s, 0),
                    default="NONE",
                )
                try:
                    sender = EmailSender(config)
                    delivery = sender.send(
                        html_content=html,
                        monitor_date=monitor_date,
                        anomaly_count=anomaly_count,
                        max_severity=max_sev,
                        run_id=run_id,
                    )
                    print(f"  Status     : {delivery.status}")
                    print(f"  Alert ID  : {delivery.alert_id}")
                    print(f"  Subject   : {delivery.subject}")
                    print(f"  Recipients: {delivery.recipients or '(none)'}")
                    if delivery.error_message:
                        print(f"  Error     : {delivery.error_message}")
                except Exception as e:
                    print(f"  Email delivery failed (monitoring data persisted): {e}")
                    print("  Continuing — monitoring results are already saved.")
    else:
        print("\n--- No anomalies detected (NORMAL) ---")
        if not args.no_agent:
            print("  Skipping Genie call, report generation, and email delivery (no anomaly).")

    print("\nMonitor run complete.")


if __name__ == "__main__":
    main()
