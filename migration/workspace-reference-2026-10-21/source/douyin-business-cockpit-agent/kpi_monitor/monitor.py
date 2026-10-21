"""KPI anomaly monitor — orchestrates fetch, detect, persist.

Reads KPI values from the existing A1 metric view (read-only), computes
derived metrics, runs deterministic anomaly rules, and persists results
to two Delta tables with idempotent MERGE.

Key design decisions:
  * **Observation-date based** — the data cadence is every 3 days, not
    daily.  The monitor uses *actual available observation dates* only.
  * **Idempotent** — re-running for the same ``monitor_date`` overwrites
    previous results via ``MERGE INTO``.
  * **Safe** — missing data returns ``None``; zero denominators skip
    ratio-based rules instead of raising.
  * **No source mutation** — existing tables and metric views are never
    modified.
"""

from __future__ import annotations

import math
import uuid
from datetime import date, datetime, timezone
from typing import Dict, List, Optional, Tuple

from pyspark.sql import SparkSession, DataFrame
from pyspark.sql import functions as F
from pyspark.sql.types import (
    DateType, DecimalType, DoubleType, LongType,
    StringType, StructType, StructField, TimestampType,
)

from kpi_monitor.config import MonitorConfig, KPIDefinition, AnomalyThresholds
from kpi_monitor.anomaly import (
    AnomalyResult,
    Severity,
    evaluate_kpi,
    safe_divide,
    safe_std,
)


class KPIMonitor:
    """Reusable KPI anomaly monitor.

    Parameters
    ----------
    config : MonitorConfig
        KPI definitions, thresholds, and Delta table locations.
    spark : SparkSession, optional
        Uses the active session if not provided.
    """

    def __init__(
        self,
        config: MonitorConfig,
        spark: Optional[SparkSession] = None,
    ):
        self.config = config
        self.spark = spark or SparkSession.builder.getOrCreate()

    # ------------------------------------------------------------------ #
    # 1. Data fetching
    # ------------------------------------------------------------------ #

    def _raw_measure_names(self) -> List[str]:
        """Return distinct raw measure names needed across all KPIs."""
        names = set()
        for kpi in self.config.kpis:
            if not kpi.is_derived and kpi.raw_measure:
                names.add(kpi.raw_measure)
            if kpi.is_derived:
                names.add(kpi.numerator)
                names.add(kpi.denominator)
        return sorted(names)

    def fetch_kpi_data(self, end_date: str) -> DataFrame:
        """Fetch raw daily KPI aggregates.

        In production mode, queries the A1 metric view using MEASURE()
        syntax.  In demo mode, reads from the simulation observations
        Delta table (a plain table, no MEASURE() needed).

        Returns a DataFrame with columns:
            date (DATE), plus one column per raw measure (lowercase,
            underscored).
        """
        measures = self._raw_measure_names()

        if self.config.demo_mode:
            # Demo mode: read from simulation table (plain Delta, no MEASURE)
            cols = ["date"] + [
                m.lower().replace(" ", "_").replace("(", "").replace(")", "")
                for m in measures
            ]
            col_list = ", ".join(f"`{c}`" for c in cols)
            sql = (
                f"SELECT {col_list}\n"
                f"FROM {self.config.full_sim_observations_table}\n"
                f"WHERE `date` <= '{end_date}'\n"
                f"ORDER BY `date` ASC"
            )
            return self.spark.sql(sql)

        # Production mode: query metric view with MEASURE()
        select_parts = ["`date`"]
        for m in measures:
            alias = m.lower().replace(" ", "_").replace("(", "").replace(")", "")
            select_parts.append(f"MEASURE(`{m}`) AS `{alias}`")

        sql = (
            f"SELECT {', '.join(select_parts)}\n"
            f"FROM {self.config.source_view}\n"
            f"WHERE `date` <= '{end_date}'\n"
            f"GROUP BY ALL\n"
            f"ORDER BY `date` ASC"
        )
        return self.spark.sql(sql)

    def check_data_freshness(self) -> Optional[str]:
        """Check production data freshness.  Returns None if fresh,
        or an error message string if stale/missing.

        Only called in production mode.  In demo mode, always returns None.
        """
        if self.config.demo_mode:
            return None
        try:
            df = self.fetch_kpi_data("9999-12-31")
            if df.count() == 0:
                return "No data found in source metric view"
            latest = df.select(F.max("date")).collect()[0][0]
            if latest is None:
                return "Latest date is NULL in source metric view"
            from datetime import date as dt_date, datetime as dt_datetime, timezone
            if isinstance(latest, dt_date):
                latest_dt = dt_datetime.combine(latest, dt_datetime.min.time())
            else:
                latest_dt = latest
            now_utc = dt_datetime.now(timezone.utc)
            age_hours = (now_utc - latest_dt.replace(tzinfo=timezone.utc)).total_seconds() / 3600
            if age_hours > self.config.freshness_max_age_hours:
                return (
                    f"Data is stale: latest observation {latest} is "
                    f"{age_hours:.0f} hours old "
                    f"(max allowed: {self.config.freshness_max_age_hours}h)"
                )
            return None
        except Exception as e:
            return f"Freshness check failed: {e}"

    # ------------------------------------------------------------------ #
    # 2. Derived metric computation
    # ------------------------------------------------------------------ #

    def compute_derived_metrics(self, df: DataFrame) -> DataFrame:
        """Add derived KPI columns (refund_rate, atv) to the DataFrame.

        Derived KPIs are computed from raw measures using safe division.
        If the denominator is 0 or NULL, the derived value is NULL.
        """
        for kpi in self.config.kpis:
            if not kpi.is_derived:
                continue
            num_col = kpi.numerator.lower().replace(" ", "_").replace("(", "").replace(")", "")
            den_col = kpi.denominator.lower().replace(" ", "_").replace("(", "").replace(")", "")
            df = df.withColumn(
                kpi.name,
                F.when(F.col(den_col) != 0, F.col(num_col) / F.col(den_col))
                  .otherwise(None),
            )
        return df

    # ------------------------------------------------------------------ #
    # 3. Anomaly detection
    # ------------------------------------------------------------------ #

    def detect_anomalies(
        self,
        df: DataFrame,
        monitor_date: str,
    ) -> Tuple[List[dict], List[AnomalyResult]]:
        """Run anomaly rules for ``monitor_date``.

        Returns
        -------
        monitoring_rows : list[dict]
            One row per KPI with value, stats, and anomaly flag.
        anomaly_results : list[AnomalyResult]
            Flattened list of all triggered anomaly rules.
        """
        # Collect to driver for deterministic computation
        rows = df.orderBy("date").collect()
        if not rows:
            return [], []

        # Build a list of (date_str, {kpi_name: value})
        observations: List[Tuple[str, Dict[str, Optional[float]]]] = []
        for row in rows:
            d = row["date"]
            date_str = d.isoformat() if isinstance(d, date) else str(d)
            vals: Dict[str, Optional[float]] = {}
            for kpi in self.config.kpis:
                if kpi.is_derived:
                    v = row[kpi.name]
                    vals[kpi.name] = float(v) if v is not None else None
                else:
                    col = kpi.raw_measure.lower().replace(" ", "_").replace("(", "").replace(")", "")
                    v = row[col]
                    vals[kpi.name] = float(v) if v is not None else None
            observations.append((date_str, vals))

        # Find the index of monitor_date
        monitor_idx = None
        for i, (d, _) in enumerate(observations):
            if d == monitor_date:
                monitor_idx = i
                break
        if monitor_idx is None:
            return [], []

        monitoring_rows: List[dict] = []
        anomaly_results: List[AnomalyResult] = []
        run_id = str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        for kpi in self.config.kpis:
            thresholds = self.config.get_thresholds(kpi.name)
            window = thresholds.rolling_window

            current_val = observations[monitor_idx][1].get(kpi.name)
            prev_val = (
                observations[monitor_idx - 1][1].get(kpi.name)
                if monitor_idx > 0 else None
            )

            # Rolling stats: use observations *before* monitor_idx
            window_start = max(0, monitor_idx - window)
            window_vals = [
                observations[j][1].get(kpi.name)
                for j in range(window_start, monitor_idx)
            ]
            window_vals = [v for v in window_vals if v is not None and not math.isnan(v)]

            rolling_mean = (
                sum(window_vals) / len(window_vals)
                if window_vals else None
            )
            rolling_std = safe_std(window_vals) if window_vals else None

            pct_change = None
            if current_val is not None and prev_val is not None and prev_val != 0:
                pct_change = abs(current_val - prev_val) / abs(prev_val)

            # Z-score: require minimum observations to avoid inflated z
            min_obs = getattr(thresholds, "min_observations_for_zscore",
                              self.config.min_observations_for_zscore)
            z_score = None
            if (
                current_val is not None
                and rolling_mean is not None
                and rolling_std is not None
                and rolling_std > 0
                and len(window_vals) >= min_obs
            ):
                z_score = (current_val - rolling_mean) / rolling_std

            # If insufficient observations, suppress z-score by passing None
            effective_rolling_mean = rolling_mean
            effective_rolling_std = rolling_std
            if len(window_vals) < min_obs:
                effective_rolling_mean = None
                effective_rolling_std = None

            # Compute healthy baseline for pct_change: if the previous
            # observation was itself an outlier (|z| >= 2.0 relative to
            # the rolling window), use the rolling mean instead of the
            # raw previous value.  This prevents false positives from
            # recovery after an anomaly day without using simulation labels.
            pct_change_baseline = None
            if (
                prev_val is not None
                and effective_rolling_mean is not None
                and effective_rolling_std is not None
                and effective_rolling_std > 0
            ):
                prev_z = abs(prev_val - effective_rolling_mean) / effective_rolling_std
                if prev_z >= 2.0:
                    pct_change_baseline = effective_rolling_mean

            # Run anomaly rules
            anomalies = evaluate_kpi(
                kpi_name=kpi.name,
                kpi_display_name=kpi.display_name,
                observation_date=monitor_date,
                value=current_val,
                prev_value=prev_val,
                rolling_mean=effective_rolling_mean,
                rolling_std=effective_rolling_std,
                abs_min=thresholds.abs_min,
                abs_max=thresholds.abs_max,
                pct_change_warning=thresholds.pct_change_warning,
                pct_change_critical=thresholds.pct_change_critical,
                zscore_warning=thresholds.zscore_warning,
                zscore_critical=thresholds.zscore_critical,
                pct_change_baseline=pct_change_baseline,
            )

            max_severity = max((a.severity for a in anomalies), default=Severity.NONE)

            monitoring_rows.append({
                "monitor_date": monitor_date,
                "kpi_name": kpi.name,
                "kpi_display_name": kpi.display_name,
                "observation_date": monitor_date,
                "kpi_value": current_val,
                "prev_value": prev_val,
                "pct_change": pct_change,
                "rolling_mean": rolling_mean,
                "rolling_std": rolling_std,
                "z_score": z_score,
                "is_anomaly": max_severity > Severity.NONE,
                "anomaly_count": len(anomalies),
                "max_severity": max_severity.name,
                "run_id": run_id,
                "created_at": now,
            })

            anomaly_results.extend(anomalies)

        return monitoring_rows, anomaly_results

    # ------------------------------------------------------------------ #
    # 4. Persistence (idempotent MERGE)
    # ------------------------------------------------------------------ #

    def persist_results(
        self,
        monitoring_rows: List[dict],
        anomaly_results: List[AnomalyResult],
    ) -> None:
        """Write monitoring results and anomaly records to Delta tables.

        Uses MERGE INTO for idempotency: re-running for the same
        monitor_date + kpi_name overwrites previous results.
        """
        run_id = monitoring_rows[0]["run_id"] if monitoring_rows else str(uuid.uuid4())
        now = datetime.now(timezone.utc)

        # --- results table ---
        if monitoring_rows:
            results_schema = StructType([
                StructField("monitor_date", StringType(), False),
                StructField("kpi_name", StringType(), False),
                StructField("kpi_display_name", StringType(), True),
                StructField("observation_date", StringType(), False),
                StructField("kpi_value", DoubleType(), True),
                StructField("prev_value", DoubleType(), True),
                StructField("pct_change", DoubleType(), True),
                StructField("rolling_mean", DoubleType(), True),
                StructField("rolling_std", DoubleType(), True),
                StructField("z_score", DoubleType(), True),
                StructField("is_anomaly", StringType(), True),
                StructField("anomaly_count", LongType(), True),
                StructField("max_severity", StringType(), True),
                StructField("run_id", StringType(), True),
                StructField("created_at", TimestampType(), True),
            ])

            results_data = [
                (
                    r["monitor_date"],
                    r["kpi_name"],
                    r["kpi_display_name"],
                    r["observation_date"],
                    r["kpi_value"],
                    r["prev_value"],
                    r["pct_change"],
                    r["rolling_mean"],
                    r["rolling_std"],
                    r["z_score"],
                    str(r["is_anomaly"]),
                    r["anomaly_count"],
                    r["max_severity"],
                    r["run_id"],
                    r["created_at"],
                )
                for r in monitoring_rows
            ]

            src_df = self.spark.createDataFrame(results_data, results_schema)
            src_df.createOrReplaceTempView("_kpi_results_src")

            self.spark.sql(f"""
                MERGE INTO {self.config.full_results_table} AS t
                USING _kpi_results_src AS s
                  ON t.monitor_date = s.monitor_date
                 AND t.kpi_name = s.kpi_name
                WHEN MATCHED THEN UPDATE SET *
                WHEN NOT MATCHED THEN INSERT *
            """)

        # --- anomalies table ---
        if anomaly_results:
            anomaly_schema = StructType([
                StructField("monitor_date", StringType(), False),
                StructField("kpi_name", StringType(), False),
                StructField("kpi_display_name", StringType(), True),
                StructField("observation_date", StringType(), False),
                StructField("rule_name", StringType(), False),
                StructField("severity", StringType(), False),
                StructField("description", StringType(), True),
                StructField("observed_value", DoubleType(), True),
                StructField("threshold_value", StringType(), True),
                StructField("run_id", StringType(), True),
                StructField("created_at", TimestampType(), True),
            ])

            anomaly_data = [
                (
                    a.observation_date,  # monitor_date == observation_date
                    a.kpi_name,
                    a.kpi_display_name,
                    a.observation_date,
                    a.rule_name,
                    a.severity.name,
                    a.description,
                    a.observed_value,
                    a.threshold_value,
                    run_id,
                    now,
                )
                for a in anomaly_results
            ]

            anom_df = self.spark.createDataFrame(anomaly_data, anomaly_schema)
            anom_df.createOrReplaceTempView("_kpi_anomalies_src")

            self.spark.sql(f"""
                MERGE INTO {self.config.full_anomalies_table} AS t
                USING _kpi_anomalies_src AS s
                  ON t.monitor_date = s.monitor_date
                 AND t.kpi_name = s.kpi_name
                 AND t.rule_name = s.rule_name
                WHEN MATCHED THEN UPDATE SET *
                WHEN NOT MATCHED THEN INSERT *
            """)

    # ------------------------------------------------------------------ #
    # 5. Orchestration
    # ------------------------------------------------------------------ #

    def run(
        self,
        monitor_date: Optional[str] = None,
        persist: bool = True,
    ) -> Tuple[List[dict], List[AnomalyResult]]:
        """Run the full monitoring pipeline for one observation date.

        Parameters
        ----------
        monitor_date : str, optional
            ISO date string (e.g. ``"2025-06-15"``).  If ``None``, uses
            the latest available observation date.
        persist : bool
            If ``True``, writes results to Delta tables.

        Returns
        -------
        (monitoring_rows, anomaly_results)
        """
        # Fetch all data up to monitor_date (or all if not specified)
        end_date = monitor_date or "9999-12-31"
        raw_df = self.fetch_kpi_data(end_date)
        df = self.compute_derived_metrics(raw_df)

        # If monitor_date is None, use the latest observation
        if monitor_date is None:
            latest = df.select(F.max("date")).collect()[0][0]
            if latest is None:
                return [], []
            monitor_date = latest.isoformat() if isinstance(latest, date) else str(latest)

        # Detect anomalies
        monitoring_rows, anomaly_results = self.detect_anomalies(df, monitor_date)

        # Persist
        if persist and monitoring_rows:
            self.persist_results(monitoring_rows, anomaly_results)

        return monitoring_rows, anomaly_results

    def run_backtest(
        self,
        start_date: str,
        end_date: str,
        persist: bool = False,
    ) -> Tuple[List[dict], List[AnomalyResult]]:
        """Run the monitor for every observation date in [start, end].

        Returns combined results across all dates.  Does not persist
        by default to avoid expensive writes during exploratory backtests.
        """
        raw_df = self.fetch_kpi_data(end_date)
        df = self.compute_derived_metrics(raw_df)

        all_rows: List[dict] = []
        all_anomalies: List[AnomalyResult] = []

        obs_dates = [
            r["date"].isoformat() if isinstance(r["date"], date) else str(r["date"])
            for r in df.orderBy("date").collect()
        ]
        obs_dates = [d for d in obs_dates if start_date <= d <= end_date]

        for d in obs_dates:
            rows, anomalies = self.detect_anomalies(df, d)
            all_rows.extend(rows)
            all_anomalies.extend(anomalies)

        if persist and all_rows:
            self.persist_results(all_rows, all_anomalies)

        return all_rows, all_anomalies