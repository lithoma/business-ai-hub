"""Configuration for the KPI anomaly monitoring module.

All thresholds are configurable.  The defaults below were chosen based
on exploratory analysis of the synthetic dataset (Jan–Jun 2025, every
3-day observation cadence, 60 data points).  Adjust per business needs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional


# --------------------------------------------------------------------------- #
# KPI definitions
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class KPIDefinition:
    """Describes a single KPI to monitor.

    raw_measure    – backtick-quoted measure name in the metric view
                     (used inside ``MEASURE(`...`)``)
    is_derived     – True for KPIs computed from other raw measures
                     (refund_rate, atv).  Raw measures are fetched from
                     the metric view; derived ones are computed in Python.
    numerator / denominator – for derived KPIs, the names of the raw
                     measures used as numerator and denominator.
    """

    name: str
    display_name: str
    unit: str
    raw_measure: str = ""          # backtick-quoted, e.g. ``GMV with coupon before refund``
    is_derived: bool = False
    numerator: str = ""            # raw measure name (without backticks)
    denominator: str = ""          # raw measure name (without backticks)


@dataclass
class AnomalyThresholds:
    """Thresholds for the three deterministic anomaly rules.

    A KPI triggers an anomaly when *any* rule fires.
    Severity is the worst (highest) among all triggered rules.
    """

    # Absolute bounds (None → not checked)
    abs_min: Optional[float] = None
    abs_max: Optional[float] = None

    # Percent-change from previous observation (fraction, e.g. 0.30 = 30 %)
    pct_change_warning: float = 0.30
    pct_change_critical: float = 0.50

    # Z-score from rolling mean/std (sigma units)
    zscore_warning: float = 2.5
    zscore_critical: float = 3.0

    # Rolling window: number of *observations* (not calendar days)
    rolling_window: int = 10

    # Minimum observations in the rolling window before z-score is applied.
    # If fewer observations are available, z-score detection is skipped
    # to avoid inflated z-scores from small-sample statistics.
    min_observations_for_zscore: int = 7


@dataclass
class MonitorConfig:
    """Top-level configuration for the KPI monitor."""

    catalog: str = "workspace"
    schema: str = "douyin"
    results_table: str = "kpi_monitoring_results"
    anomalies_table: str = "kpi_anomaly_records"

    # Metric view that provides raw GMV, sales quantity, refund amount
    source_view: str = "workspace.douyin.mv_sales_metrics"

    # KPI definitions (raw + derived)
    kpis: List[KPIDefinition] = field(default_factory=list)

    # Per-KPI thresholds (keyed by KPI name)
    thresholds: Dict[str, AnomalyThresholds] = field(default_factory=dict)

    # Number of historical observations to fetch for rolling stats
    lookback_observations: int = 30

    # Minimum observations in rolling window before z-score rule fires
    # (global default; per-KPI override via AnomalyThresholds.min_observations_for_zscore)
    min_observations_for_zscore: int = 7

    # ------------------------------------------------------------------ #
    # Demo / Production mode configuration
    # ------------------------------------------------------------------ #

    # True = synthetic simulation mode; False = production mode
    demo_mode: bool = False

    # Brand and scope shown in reports
    brand: str = "Haleon"
    business_scope: str = "Douyin E-commerce All Channels"

    # --- Simulation (demo) settings ---
    sim_seed: int = 42
    sim_anomaly_probability: float = 0.15
    sim_start_date: str = "2025-01-01"
    sim_cursor_table: str = "kpi_sim_cursor"
    sim_observations_table: str = "kpi_sim_observations"
    sim_events_table: str = "kpi_sim_events"

    # --- Scripted scenario mode (demo automation) ---
    # When True, the simulation engine uses the scripted_scenarios list
    # instead of random anomaly injection. This ensures deterministic
    # Day 1: ANOMALY, Day 2: NORMAL, Day 3: ANOMALY sequence.
    sim_scripted_mode: bool = False
    sim_scripted_scenarios: list = field(
        default_factory=lambda: ["ANOMALY", "NORMAL", "ANOMALY"]
    )

    # --- Mock agent (development) ---
    # When True, skip live Genie A1 calls and use a pre-defined mock response.
    # This avoids repeated live Genie calls during development/testing.
    mock_agent: bool = False

    # Separate demo result tables (kept distinct from production)
    demo_results_table: str = "kpi_demo_monitoring_results"
    demo_anomalies_table: str = "kpi_demo_anomaly_records"

    # --- Production settings ---
    # Maximum age (hours) of latest data before raising a freshness warning
    freshness_max_age_hours: int = 72

    # --- Notification (M3: email delivery) ---
    notification_test_recipients: list = field(default_factory=lambda: ["test@example.com"])

    # --- Email settings ---
    email_enabled: bool = False
    email_provider: str = "smtp"           # "smtp", "sendgrid", "ses"
    email_secret_scope: str = "douyin_kpi_monitor"  # Databricks Secrets scope
    email_sender: str = "kpi-monitor@databricks.com"
    email_sender_name: str = "Douyin KPI Monitor"

    # Demo recipients (test only)
    email_demo_recipients: list = field(default_factory=lambda: ["tianyili397@gmail.com"])

    # Production recipients (configure before enabling production email)
    email_prod_recipients: list = field(default_factory=list)

    # SMTP settings
    email_smtp_host: str = ""
    email_smtp_port: int = 587

    # DRY_RUN mode: prepare email but don't send (default True for safety)
    email_dry_run: bool = True

    # Delivery log table
    email_delivery_log_table: str = "kpi_email_delivery_log"

    # Email timeout and retries
    email_timeout_seconds: int = 30
    email_max_retries: int = 3

    # Concurrency: claim/lease duration (seconds). A CLAIMED record older
    # than this is considered stale and can be reclaimed by another run.
    email_claim_lease_seconds: int = 300

    # ------------------------------------------------------------------ #
    # Factory
    # ------------------------------------------------------------------ #

    @classmethod
    def default(cls) -> "MonitorConfig":
        """Return a config pre-loaded with the five core KPIs and
        sensible default thresholds for the synthetic dataset."""

        kpis = [
            KPIDefinition(
                name="gmv",
                display_name="GMV (含券退前)",
                unit="CNY",
                raw_measure="GMV with coupon before refund",
            ),
            KPIDefinition(
                name="sales_qty",
                display_name="Sales Quantity (销量)",
                unit="units",
                raw_measure="Sales quantity",
            ),
            KPIDefinition(
                name="refund_amt",
                display_name="Refund Amount (退款金额)",
                unit="CNY",
                raw_measure="Refund amount",
            ),
            KPIDefinition(
                name="refund_rate",
                display_name="Refund Rate (退款率)",
                unit="ratio",
                is_derived=True,
                numerator="Refund amount",
                denominator="GMV with coupon before refund",
            ),
            KPIDefinition(
                name="avg_unit_gmv",
                display_name="Avg Unit GMV (客单价)",
                unit="CNY",
                is_derived=True,
                numerator="GMV with coupon before refund",
                denominator="Sales quantity",
            ),
        ]

        thresholds = {
            # GMV: ~2.3M–2.9M in historical data; flag extreme outliers
            "gmv": AnomalyThresholds(
                abs_min=1_500_000,
                abs_max=3_500_000,
                pct_change_warning=0.20,
                pct_change_critical=0.35,
                zscore_warning=2.5,
                zscore_critical=3.0,
                rolling_window=10,
            ),
            # Sales quantity: ~9K–11K
            "sales_qty": AnomalyThresholds(
                abs_min=5_000,
                abs_max=15_000,
                pct_change_warning=0.25,
                pct_change_critical=0.40,
                zscore_warning=2.5,
                zscore_critical=3.0,
                rolling_window=10,
            ),
            # Refund amount: ~217K–301K
            "refund_amt": AnomalyThresholds(
                abs_min=100_000,
                abs_max=500_000,
                pct_change_warning=0.30,
                pct_change_critical=0.50,
                zscore_warning=2.5,
                zscore_critical=3.0,
                rolling_window=10,
            ),
            # Refund rate: ~9.2%–10.7%
            "refund_rate": AnomalyThresholds(
                abs_min=0.05,
                abs_max=0.15,
                pct_change_warning=0.25,
                pct_change_critical=0.40,
                zscore_warning=2.5,
                zscore_critical=3.0,
                rolling_window=10,
            ),
            # Avg Unit GMV (was 'atv'): ~220–305
            "avg_unit_gmv": AnomalyThresholds(
                abs_min=150,
                abs_max=400,
                pct_change_warning=0.20,
                pct_change_critical=0.35,
                zscore_warning=2.5,
                zscore_critical=3.0,
                rolling_window=10,
            ),
        }

        return cls(
            kpis=kpis,
            thresholds=thresholds,
        )

    # ------------------------------------------------------------------ #
    # Convenience
    # ------------------------------------------------------------------ #

    # ------------------------------------------------------------------ #
    # Table-name convenience (mode-aware)
    # ------------------------------------------------------------------ #

    @property
    def full_results_table(self) -> str:
        """Returns the demo results table in demo mode, production otherwise."""
        if self.demo_mode:
            return f"{self.catalog}.{self.schema}.{self.demo_results_table}"
        return f"{self.catalog}.{self.schema}.{self.results_table}"

    @property
    def full_anomalies_table(self) -> str:
        """Returns the demo anomalies table in demo mode, production otherwise."""
        if self.demo_mode:
            return f"{self.catalog}.{self.schema}.{self.demo_anomalies_table}"
        return f"{self.catalog}.{self.schema}.{self.anomalies_table}"

    @property
    def full_sim_cursor_table(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.sim_cursor_table}"

    @property
    def full_sim_observations_table(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.sim_observations_table}"

    @property
    def full_sim_events_table(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.sim_events_table}"

    @property
    def full_email_log_table(self) -> str:
        return f"{self.catalog}.{self.schema}.{self.email_delivery_log_table}"

    @property
    def email_recipients(self) -> list:
        """Return recipients based on demo_mode.  Never mix demo and prod."""
        if self.demo_mode:
            return self.email_demo_recipients
        return self.email_prod_recipients

    @property
    def email_mode_label(self) -> str:
        """Label for email subject prefix and delivery log."""
        return "DEMO" if self.demo_mode else "PROD"

    def get_kpi(self, name: str) -> Optional[KPIDefinition]:
        for k in self.kpis:
            if k.name == name:
                return k
        return None

    def get_thresholds(self, name: str) -> AnomalyThresholds:
        return self.thresholds.get(name, AnomalyThresholds())