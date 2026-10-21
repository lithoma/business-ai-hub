"""Reproducible simulation engine for demo-mode KPI monitoring.

Generates synthetic daily KPI observations deterministically (seeded by
sim date + global seed), persists them to a Delta table, and advances a
simulation cursor by one day per successful run.

Key properties:
  * **Deterministic** — same seed + same sim date always yields the same data.
  * **Idempotent** — re-running for the same sim date overwrites via MERGE.
  * **Pausable** — cursor has RUNNING / PAUSED states.
  * **No source mutation** — only writes to dedicated sim tables.

Tables created (workspace.douyin):
  * ``kpi_sim_cursor`` — single-row cursor (sim_date, status, run_count)
  * ``kpi_sim_observations`` — one row per simulated day
  * ``kpi_sim_events`` — log of injected anomaly events
"""

from __future__ import annotations

import hashlib
import math
import random
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from pyspark.sql import SparkSession
from pyspark.sql import functions as F
from pyspark.sql.types import (
    BooleanType, DateType, DoubleType, IntegerType, LongType,
    StringType, StructType, StructField, TimestampType,
)


# Baselines calibrated from the synthetic 2025 dataset.
_BASELINES = {
    "gmv_with_coupon_before_refund": 2_700_000.0,   # CNY
    "sales_quantity": 10_000.0,                       # units
    "refund_amount": 270_000.0,                        # CNY
}

# Normal-day variation range (±5 %)
_NORMAL_VOLATILITY = 0.05

# Anomaly shift range when injected (±20 % to ±40 %)
_ANOMALY_MIN_SHIFT = 0.20
_ANOMALY_MAX_SHIFT = 0.40


class SimulationEngine:
    """Reproducible simulation engine for demo-mode monitoring.

    Parameters
    ----------
    config : MonitorConfig
        Must have ``demo_mode=True`` and sim-related fields set.
    spark : SparkSession, optional
    """

    def __init__(self, config, spark: Optional[SparkSession] = None):
        self.config = config
        self.spark = spark or SparkSession.builder.getOrCreate()
        self._init_tables()

    # ------------------------------------------------------------------ #
    # Table initialisation
    # ------------------------------------------------------------------ #

    def _init_tables(self) -> None:
        """Create sim tables if they don't exist."""

        self.spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {self.config.full_sim_cursor_table} (
                sim_date     DATE NOT NULL,
                status       STRING NOT NULL,
                run_count    INT NOT NULL,
                updated_at   TIMESTAMP
            ) USING DELTA
            COMMENT 'KPI simulation cursor — single row, updated via MERGE.'
        """)

        self.spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {self.config.full_sim_observations_table} (
                date                            DATE NOT NULL,
                gmv_with_coupon_before_refund   DOUBLE,
                sales_quantity                   BIGINT,
                refund_amount                    DOUBLE,
                injected_anomaly                 BOOLEAN,
                affected_kpi                     STRING,
                run_id                           STRING,
                created_at                       TIMESTAMP
            ) USING DELTA
            COMMENT 'Simulated daily KPI observations for demo mode.'
        """)

        self.spark.sql(f"""
            CREATE TABLE IF NOT EXISTS {self.config.full_sim_events_table} (
                sim_date        DATE NOT NULL,
                affected_kpi    STRING NOT NULL,
                event_type      STRING,
                description     STRING,
                severity        STRING,
                run_id          STRING,
                created_at      TIMESTAMP
            ) USING DELTA
            COMMENT 'Log of injected anomaly events in demo simulation.'
        """)

    # ------------------------------------------------------------------ #
    # Deterministic data generation
    # ------------------------------------------------------------------ #

    def _seeded_rng(self, sim_date_str: str) -> random.Random:
        """Create a deterministic RNG seeded by (global_seed, sim_date)."""
        raw = f"{self.config.sim_seed}:{sim_date_str}"
        seed_int = int(hashlib.sha256(raw.encode()).hexdigest(), 16) % (2**32)
        return random.Random(seed_int)

    def _get_scripted_scenario(self, run_count: int) -> Optional[str]:
        """Return the scripted scenario for the given run count.

        Returns "ANOMALY", "NORMAL", or None if past the end of the list.
        """
        if not self.config.sim_scripted_mode:
            return None
        scenarios = self.config.sim_scripted_scenarios
        if run_count < 0 or run_count >= len(scenarios):
            return None
        return scenarios[run_count]

    def generate_observations(self, sim_date_str: str) -> Dict:
        """Generate deterministic synthetic KPI data for one date.

        In scripted mode, the anomaly injection is determined by the
        scripted_scenarios list (indexed by run_count) rather than
        random probability.

        Returns a dict with keys:
            date, gmv_with_coupon_before_refund, sales_quantity,
            refund_amount, injected_anomaly, affected_kpi
        """
        rng = self._seeded_rng(sim_date_str)

        # Decide whether to inject an anomaly this day
        if self.config.sim_scripted_mode:
            cursor = self.get_cursor()
            scenario = self._get_scripted_scenario(cursor["run_count"])
            if scenario is None:
                inject = False  # Past end of scripted scenarios
            else:
                inject = (scenario.upper() == "ANOMALY")
        else:
            inject = rng.random() < self.config.sim_anomaly_probability
        affected_kpi = ""

        gmv = _BASELINES["gmv_with_coupon_before_refund"]
        sales_qty = _BASELINES["sales_quantity"]
        refund_amt = _BASELINES["refund_amount"]

        # Apply normal random variation
        gmv *= 1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)
        sales_qty = round(sales_qty * (1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)))
        refund_amt *= 1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)

        if inject:
            # Pick a KPI to perturb
            kpi_choices = ["gmv", "sales_qty", "refund_amt"]
            affected_kpi = rng.choice(kpi_choices)
            shift = rng.uniform(_ANOMALY_MIN_SHIFT, _ANOMALY_MAX_SHIFT)
            if rng.random() < 0.5:
                shift = -shift

            if affected_kpi == "gmv":
                gmv *= (1 + shift)
            elif affected_kpi == "sales_qty":
                sales_qty = round(sales_qty * (1 + shift))
            elif affected_kpi == "refund_amt":
                refund_amt *= (1 + shift)

        return {
            "date": sim_date_str,
            "gmv_with_coupon_before_refund": round(gmv, 2),
            "sales_quantity": int(sales_qty),
            "refund_amount": round(refund_amt, 2),
            "injected_anomaly": inject,
            "affected_kpi": affected_kpi,
        }

    # ------------------------------------------------------------------ #
    # Cursor management
    # ------------------------------------------------------------------ #

    def get_cursor(self) -> Dict:
        """Read the current simulation cursor.  Returns dict with
        sim_date, status, run_count, or defaults if no row exists."""
        rows = self.spark.sql(
            f"SELECT sim_date, status, run_count FROM {self.config.full_sim_cursor_table}"
        ).collect()
        if not rows:
            return {
                "sim_date": self.config.sim_start_date,
                "status": "RUNNING",
                "run_count": 0,
            }
        r = rows[0]
        d = r["sim_date"]
        date_str = d.isoformat() if isinstance(d, date) else str(d)
        return {
            "sim_date": date_str,
            "status": r["status"],
            "run_count": r["run_count"],
        }

    def _write_cursor(self, sim_date_str: str, status: str, run_count: int) -> None:
        """Upsert cursor (single-row table)."""
        now = datetime.now(timezone.utc)
        schema = StructType([
            StructField("sim_date", DateType(), False),
            StructField("status", StringType(), False),
            StructField("run_count", IntegerType(), False),
            StructField("updated_at", TimestampType(), True),
        ])
        df = self.spark.createDataFrame([(
            datetime.strptime(sim_date_str, "%Y-%m-%d").date(),
            status,
            run_count,
            now,
        )], schema)
        df.createOrReplaceTempView("_sim_cursor_src")
        self.spark.sql(f"""
            MERGE INTO {self.config.full_sim_cursor_table} AS t
            USING _sim_cursor_src AS s
              ON 1 = 1
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #

    def _persist_observation(self, obs: Dict, run_id: str) -> None:
        """Idempotent MERGE of one observation into the sim table."""
        now = datetime.now(timezone.utc)
        schema = StructType([
            StructField("date", DateType(), False),
            StructField("gmv_with_coupon_before_refund", DoubleType(), True),
            StructField("sales_quantity", LongType(), True),
            StructField("refund_amount", DoubleType(), True),
            StructField("injected_anomaly", BooleanType(), True),
            StructField("affected_kpi", StringType(), True),
            StructField("run_id", StringType(), True),
            StructField("created_at", TimestampType(), True),
        ])
        df = self.spark.createDataFrame([(
            datetime.strptime(obs["date"], "%Y-%m-%d").date(),
            obs["gmv_with_coupon_before_refund"],
            obs["sales_quantity"],
            obs["refund_amount"],
            obs["injected_anomaly"],
            obs["affected_kpi"],
            run_id,
            now,
        )], schema)
        df.createOrReplaceTempView("_sim_obs_src")
        self.spark.sql(f"""
            MERGE INTO {self.config.full_sim_observations_table} AS t
            USING _sim_obs_src AS s
              ON t.date = s.date
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

    def _persist_event(self, sim_date_str: str, obs: Dict, run_id: str) -> None:
        """Idempotent MERGE of an injection event (only if anomaly injected)."""
        if not obs["injected_anomaly"]:
            return
        now = datetime.now(timezone.utc)
        schema = StructType([
            StructField("sim_date", DateType(), False),
            StructField("affected_kpi", StringType(), False),
            StructField("event_type", StringType(), True),
            StructField("description", StringType(), True),
            StructField("severity", StringType(), True),
            StructField("run_id", StringType(), True),
            StructField("created_at", TimestampType(), True),
        ])
        df = self.spark.createDataFrame([(
            datetime.strptime(sim_date_str, "%Y-%m-%d").date(),
            obs["affected_kpi"],
            "injected_anomaly",
            f"Simulated anomaly injected into {obs['affected_kpi']} on {sim_date_str}",
            "INFO",
            run_id,
            now,
        )], schema)
        df.createOrReplaceTempView("_sim_event_src")
        self.spark.sql(f"""
            MERGE INTO {self.config.full_sim_events_table} AS t
            USING _sim_event_src AS s
              ON t.sim_date = s.sim_date AND t.affected_kpi = s.affected_kpi
            WHEN MATCHED THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT *
        """)

    # ------------------------------------------------------------------ #
    # Lifecycle controls
    # ------------------------------------------------------------------ #

    def pause(self) -> None:
        cursor = self.get_cursor()
        self._write_cursor(cursor["sim_date"], "PAUSED", cursor["run_count"])

    def resume(self) -> None:
        cursor = self.get_cursor()
        self._write_cursor(cursor["sim_date"], "RUNNING", cursor["run_count"])

    def reset(self, start_date: Optional[str] = None) -> None:
        """Reset cursor to start_date and clear all sim observations/events."""
        sd = start_date or self.config.sim_start_date
        self._write_cursor(sd, "RUNNING", 0)
        self.spark.sql(f"DELETE FROM {self.config.full_sim_observations_table}")
        self.spark.sql(f"DELETE FROM {self.config.full_sim_events_table}")

    def warmup(self, num_days: int = 10) -> None:
        """Pre-populate normal observations so anomaly detection has history.

        Generates ``num_days`` of normal (no-anomaly) observations,
        advances the cursor past them, and leaves run_count at 0 so
        the first scripted scenario starts at run_count=0.

        This is essential for the scripted demo: without warmup data,
        Day 1 has no previous value for pct-change and fewer than 7
        observations for z-score, so injected anomalies may go undetected.
        """
        cursor = self.get_cursor()
        sim_date = datetime.strptime(cursor["sim_date"], "%Y-%m-%d").date()
        for i in range(num_days):
            sim_date_str = sim_date.isoformat()
            # Generate normal observation (temporarily disable scripted mode)
            saved_scripted = self.config.sim_scripted_mode
            self.config.sim_scripted_mode = False
            # Force no anomaly: use a dedicated RNG with normal variation only
            rng = self._seeded_rng(sim_date_str)
            gmv = _BASELINES["gmv_with_coupon_before_refund"]
            sales_qty = _BASELINES["sales_quantity"]
            refund_amt = _BASELINES["refund_amount"]
            gmv *= 1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)
            sales_qty = round(sales_qty * (1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)))
            refund_amt *= 1 + rng.uniform(-_NORMAL_VOLATILITY, _NORMAL_VOLATILITY)
            obs = {
                "date": sim_date_str,
                "gmv_with_coupon_before_refund": round(gmv, 2),
                "sales_quantity": int(sales_qty),
                "refund_amount": round(refund_amt, 2),
                "injected_anomaly": False,
                "affected_kpi": "",
            }
            self.config.sim_scripted_mode = saved_scripted
            run_id = str(uuid.uuid4())
            self._persist_observation(obs, run_id)
            sim_date = sim_date + timedelta(days=1)
        # Advance cursor past warmup days, keep run_count at 0
        self._write_cursor(sim_date.isoformat(), cursor["status"], 0)
        print(f"  Warmup: {num_days} normal days populated, cursor at {sim_date.isoformat()}")

    # ------------------------------------------------------------------ #
    # Main run
    # ------------------------------------------------------------------ #

    def run(self) -> Dict:
        """Execute one simulation step.

        1. Read cursor.  If PAUSED, return without action.
        2. Generate deterministic observations for cursor date.
        3. Persist observation (idempotent MERGE).
        4. Persist injection event if applicable.
        5. Advance cursor by one day.

        Returns dict with sim_date, observations, advanced_to, status.
        """
        cursor = self.get_cursor()

        if cursor["status"] == "PAUSED":
            return {
                "sim_date": cursor["sim_date"],
                "status": "PAUSED",
                "observations": None,
                "advanced_to": cursor["sim_date"],
            }

        sim_date_str = cursor["sim_date"]
        run_id = str(uuid.uuid4())

        # Generate and persist observations
        obs = self.generate_observations(sim_date_str)
        self._persist_observation(obs, run_id)
        self._persist_event(sim_date_str, obs, run_id)

        # Advance cursor by one day
        sim_date = datetime.strptime(sim_date_str, "%Y-%m-%d").date()
        next_date = sim_date + timedelta(days=1)
        new_run_count = cursor["run_count"] + 1

        # Auto-pause after all scripted scenarios complete
        auto_paused = False
        if self.config.sim_scripted_mode:
            if new_run_count >= len(self.config.sim_scripted_scenarios):
                self._write_cursor(next_date.isoformat(), "PAUSED", new_run_count)
                auto_paused = True
            else:
                self._write_cursor(next_date.isoformat(), cursor["status"], new_run_count)
        else:
            self._write_cursor(next_date.isoformat(), cursor["status"], new_run_count)

        return {
            "sim_date": sim_date_str,
            "status": "RUNNING",
            "observations": obs,
            "advanced_to": next_date.isoformat(),
            "run_id": run_id,
            "auto_paused": auto_paused,
        }