"""Douyin KPI Anomaly Monitoring Module (V2 Milestone 1).

Reusable, deterministic KPI monitoring with configurable thresholds.
Queries existing A1 metric views (read-only), computes derived
metrics, detects anomalies, and persists results to Delta tables.

Public API:
    from kpi_monitor import KPIMonitor, MonitorConfig
    monitor = KPIMonitor(MonitorConfig.default())
    results = monitor.run(monitor_date="2025-06-15")
"""

from kpi_monitor.config import MonitorConfig, KPIDefinition, AnomalyThresholds
from kpi_monitor.monitor import KPIMonitor
from kpi_monitor.anomaly import AnomalyResult, Severity
from kpi_monitor.simulation import SimulationEngine
from kpi_monitor.agent_integration import AgentIntegration, AgentInsights
from kpi_monitor.report import ReportRenderer
from kpi_monitor.email_sender import EmailSender, DeliveryResult, UncertainDeliveryError
from kpi_monitor.alert_integration import AlertIntegration

__all__ = [
    "KPIMonitor",
    "MonitorConfig",
    "KPIDefinition",
    "AnomalyThresholds",
    "AnomalyResult",
    "Severity",
    "SimulationEngine",
    "AgentIntegration",
    "AgentInsights",
    "ReportRenderer",
    "EmailSender",
    "DeliveryResult",
    "UncertainDeliveryError",
    "AlertIntegration",
]

__version__ = "2.0.0-m3.2"