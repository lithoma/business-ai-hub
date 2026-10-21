"""Deterministic anomaly detection rules for KPI monitoring.

Three rules, all configurable via :class:`~kpi_monitor.config.AnomalyThresholds`:

1. **Absolute bounds** – value outside ``[abs_min, abs_max]``.
2. **Percent change** – |Δ %| from previous observation exceeds threshold.
3. **Z-score** – |z| from rolling mean/std exceeds threshold sigma.

All rules are deterministic (no randomness, no ML models).
Missing data and zero denominators are handled safely:  a rule that
cannot be evaluated returns ``None`` (no anomaly) rather than raising.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import IntEnum
from typing import List, Optional


class Severity(IntEnum):
    """Anomaly severity (higher = more severe)."""

    NONE = 0
    INFO = 1
    WARNING = 2
    CRITICAL = 3


@dataclass
class AnomalyResult:
    """One triggered anomaly rule for one KPI on one observation date."""

    kpi_name: str
    kpi_display_name: str
    observation_date: str          # ISO date string
    rule_name: str                 # "absolute_bounds", "pct_change", "zscore"
    severity: Severity
    description: str
    observed_value: float
    threshold_value: str          # human-readable threshold description


# --------------------------------------------------------------------------- #
# Safe arithmetic helpers
# --------------------------------------------------------------------------- #

def safe_divide(numerator: Optional[float],
                 denominator: Optional[float]) -> Optional[float]:
    """Return ``numerator / denominator`` or ``None`` if undefined.

    Handles ``None``, ``NaN``, and zero-denominator cases.
    """
    if numerator is None or denominator is None:
        return None
    if math.isnan(numerator) or math.isnan(denominator):
        return None
    if denominator == 0:
        return None
    return numerator / denominator


def safe_pct_change(current: Optional[float],
                     previous: Optional[float]) -> Optional[float]:
    """Return absolute % change ``|current - previous| / |previous|``
    or ``None`` if previous is missing/zero."""
    if current is None or previous is None:
        return None
    if math.isnan(current) or math.isnan(previous):
        return None
    return safe_divide(abs(current - previous), abs(previous))


def safe_zscore(value: Optional[float],
                mean: Optional[float],
                std: Optional[float]) -> Optional[float]:
    """Return ``(value - mean) / std`` or ``None`` if undefined."""
    if value is None or mean is None or std is None:
        return None
    if math.isnan(value) or math.isnan(mean) or math.isnan(std):
        return None
    return safe_divide(value - mean, std)


def safe_std(values: List[float]) -> Optional[float]:
    """Population standard deviation of a non-empty list, or ``None``.

    Uses population std (ddof=0) for deterministic, sample-size-independent
    behaviour on small windows.
    """
    n = len(values)
    if n < 2:
        return None
    mean = sum(values) / n
    var = sum((v - mean) ** 2 for v in values) / n
    return math.sqrt(var)


# --------------------------------------------------------------------------- #
# Individual rule checks
# --------------------------------------------------------------------------- #

def check_absolute_bounds(
    value: Optional[float],
    abs_min: Optional[float],
    abs_max: Optional[float],
) -> Optional[tuple]:
    """Return ``(severity, description, threshold_str)`` or ``None``."""
    if value is None:
        return None

    if abs_min is not None and value < abs_min:
        return (
            Severity.WARNING,
            f"Value {value:,.2f} below absolute minimum {abs_min:,.2f}",
            f"abs_min={abs_min}",
        )
    if abs_max is not None and value > abs_max:
        return (
            Severity.WARNING,
            f"Value {value:,.2f} above absolute maximum {abs_max:,.2f}",
            f"abs_max={abs_max}",
        )
    return None


def check_pct_change(
    value: Optional[float],
    prev_value: Optional[float],
    warning_pct: float,
    critical_pct: float,
) -> Optional[tuple]:
    """Return ``(severity, description, threshold_str)`` or ``None``."""
    pct = safe_pct_change(value, prev_value)
    if pct is None:
        return None

    if pct >= critical_pct:
        return (
            Severity.CRITICAL,
            f"Change of {pct:.1%} from previous {prev_value:,.2f} "
            f"exceeds critical threshold {critical_pct:.0%}",
            f"pct_change_critical={critical_pct}",
        )
    if pct >= warning_pct:
        return (
            Severity.WARNING,
            f"Change of {pct:.1%} from previous {prev_value:,.2f} "
            f"exceeds warning threshold {warning_pct:.0%}",
            f"pct_change_warning={warning_pct}",
        )
    return None


def check_zscore(
    value: Optional[float],
    mean: Optional[float],
    std: Optional[float],
    warning_z: float,
    critical_z: float,
) -> Optional[tuple]:
    """Return ``(severity, description, threshold_str)`` or ``None``."""
    z = safe_zscore(value, mean, std)
    if z is None:
        return None
    abs_z = abs(z)

    if abs_z >= critical_z:
        return (
            Severity.CRITICAL,
            f"Z-score {z:+.2f} (value={value:,.2f}, "
            f"mean={mean:,.2f}, std={std:,.2f}) "
            f"exceeds critical ±{critical_z}σ",
            f"zscore_critical={critical_z}",
        )
    if abs_z >= warning_z:
        return (
            Severity.WARNING,
            f"Z-score {z:+.2f} (value={value:,.2f}, "
            f"mean={mean:,.2f}, std={std:,.2f}) "
            f"exceeds warning ±{warning_z}σ",
            f"zscore_warning={warning_z}",
        )
    return None


# --------------------------------------------------------------------------- #
# Combined anomaly evaluation
# --------------------------------------------------------------------------- #

def evaluate_kpi(
    kpi_name: str,
    kpi_display_name: str,
    observation_date: str,
    value: Optional[float],
    prev_value: Optional[float],
    rolling_mean: Optional[float],
    rolling_std: Optional[float],
    abs_min: Optional[float] = None,
    abs_max: Optional[float] = None,
    pct_change_warning: float = 0.30,
    pct_change_critical: float = 0.50,
    zscore_warning: float = 2.5,
    zscore_critical: float = 3.0,
    pct_change_baseline: Optional[float] = None,
) -> List[AnomalyResult]:
    """Run all three rules and return a list of triggered anomalies.

    If ``value`` is ``None`` (missing data), returns an empty list —
    missing data is not an anomaly, it is a data gap.
    """
    if value is None:
        return []

    results: List[AnomalyResult] = []

    # Rule 1: absolute bounds
    r = check_absolute_bounds(value, abs_min, abs_max)
    if r:
        sev, desc, thresh = r
        results.append(AnomalyResult(
            kpi_name=kpi_name,
            kpi_display_name=kpi_display_name,
            observation_date=observation_date,
            rule_name="absolute_bounds",
            severity=sev,
            description=desc,
            observed_value=value,
            threshold_value=thresh,
        ))

    # Rule 2: percent change
    # When pct_change_baseline is provided (e.g. rolling mean used as a
    # healthy baseline when the previous day was anomalous), compare
    # against that instead of the raw previous value.  This prevents
    # false positives from recovery after an anomaly day.
    pct_ref = pct_change_baseline if pct_change_baseline is not None else prev_value
    r = check_pct_change(
        value, pct_ref, pct_change_warning, pct_change_critical,
    )
    if r:
        sev, desc, thresh = r
        results.append(AnomalyResult(
            kpi_name=kpi_name,
            kpi_display_name=kpi_display_name,
            observation_date=observation_date,
            rule_name="pct_change",
            severity=sev,
            description=desc,
            observed_value=value,
            threshold_value=thresh,
        ))

    # Rule 3: z-score
    r = check_zscore(
        value, rolling_mean, rolling_std,
        zscore_warning, zscore_critical,
    )
    if r:
        sev, desc, thresh = r
        results.append(AnomalyResult(
            kpi_name=kpi_name,
            kpi_display_name=kpi_display_name,
            observation_date=observation_date,
            rule_name="zscore",
            severity=sev,
            description=desc,
            observed_value=value,
            threshold_value=thresh,
        ))

    return results