"""Small config-driven task engine. No Databricks Jobs/Alerts REST manipulation."""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import yaml
from pyspark.sql import SparkSession

from agent_client import AgentClient, parse_boolean_decision
from outbox import publish_batch

log = logging.getLogger(__name__)


def read_yaml(path: Path) -> dict[str, Any]:
    result = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(result, dict):
        raise ValueError(f"Expected YAML mapping: {path}")
    return result


def render(text: str, variables: dict[str, Any]) -> str:
    # Explicit placeholders avoid executing arbitrary Python from task config.
    for key, value in variables.items():
        text = text.replace("{{" + key + "}}", str(value))
    return text


def sql_file(spark: SparkSession, path: Path, bindings: dict[str, str], limit: int = 50) -> list[dict[str, Any]]:
    if not path.is_file():
        raise FileNotFoundError(path)
    query = path.read_text(encoding="utf-8").strip()
    if not query:
        raise ValueError(f"Empty SQL file: {path}")
    # Bind only the parameter markers used by this SQL file. Never string-interpolate SQL.
    names = set(re.findall(r"(?<!:):([A-Za-z_][A-Za-z_0-9]*)", query))
    missing = names - bindings.keys()
    if missing:
        raise ValueError(f"Unknown SQL bind parameter(s): {sorted(missing)} in {path}")
    args = {name: bindings[name] for name in names}
    return [r.asDict(recursive=True) for r in spark.sql(query, args=args).limit(limit).collect()]


class TaskManager:
    def __init__(self, spark: SparkSession, settings_path: Path):
        self.spark = spark
        self.root = settings_path.resolve().parent
        self.settings = read_yaml(settings_path)
        self.agents = AgentClient(self.settings["agents"])

    def run(self) -> dict[str, Any]:
        schedule = self.settings["schedule"]
        now = datetime.now(ZoneInfo(schedule["timezone"]))
        dispatch_date = now.date()
        business_date = dispatch_date - timedelta(days=int(schedule.get("business_date_offset_days", 1)))
        variables = {
            "dispatch_date": dispatch_date.isoformat(),
            "business_date": business_date.isoformat(),
            "period_start": (business_date - timedelta(days=6)).isoformat(),
            "period_end": business_date.isoformat(),
        }
        task_dirs = sorted((self.root / "tasks").glob("*/config.yaml"))
        if not task_dirs:
            raise RuntimeError("No tasks/*/config.yaml files found")
        triggered_reports: list[tuple[str, str]] = []
        execution_summary: list[dict[str, Any]] = []
        task_ids: set[str] = set()
        for config_path in task_dirs:
            task = read_yaml(config_path)
            task_id = str(task.get("task_id", ""))
            if not task_id or task_id in task_ids:
                raise ValueError(f"Missing/duplicate task_id: {config_path}")
            task_ids.add(task_id)
            if not task.get("enabled", False):
                execution_summary.append({"task": task_id, "status": "DISABLED"})
                continue
            if not self._due(task, dispatch_date.weekday()):
                execution_summary.append({"task": task_id, "status": "NOT_DUE"})
                continue
            log.info("Executing task %s", task_id)
            fired, reason = self._trigger(task, config_path.parent, variables)
            if not fired:
                execution_summary.append({"task": task_id, "status": "NOT_TRIGGERED"})
                continue
            report = self._report(task, config_path.parent, variables, reason)
            triggered_reports.append((str(task.get("name", task_id)), report))
            execution_summary.append({"task": task_id, "status": "REPORT_CREATED"})

        notification = self.settings["notification"]
        size_limit = int(notification.get("max_report_chars", 12000))
        body = "\n\n".join(f"【{name}】\n{text}" for name, text in triggered_reports)
        if len(body) > size_limit:
            raise RuntimeError(f"Combined report exceeds max_report_chars ({size_limit}); refusing silent truncation")
        # Even normal days write should_notify=false. The downstream Alert Task runs once
        # and evaluates OK without relying on report status re-arming.
        publish_batch(
            self.spark,
            notification["outbox_table"],
            dispatch_date,
            notification["recipient_group"],
            body,
            len(triggered_reports),
        )
        log.info("Outbox written: date=%s report_count=%s (email delivery NOT confirmed)",
                 dispatch_date, len(triggered_reports))
        return {"date": dispatch_date.isoformat(), "reports": len(triggered_reports), "tasks": execution_summary}

    def _due(self, task: dict[str, Any], weekday: int) -> bool:
        schedule = task.get("schedule", {})
        # V1 deliberately supports one shared launch time; avoid silently promising
        # different times that the external Databricks Job won't invoke.
        task_time = schedule.get("time", self.settings["schedule"]["time"])
        if task_time != self.settings["schedule"]["time"]:
            raise ValueError(f"Task {task['task_id']} requests {task_time}; V1 has one shared Job launch time")
        frequency = schedule.get("frequency", "daily")
        if frequency == "daily":
            return True
        if frequency == "weekly":
            names = {"monday": 0, "tuesday": 1, "wednesday": 2,
                     "thursday": 3, "friday": 4, "saturday": 5, "sunday": 6}
            configured = str(schedule.get("weekday", "monday")).lower()
            if configured not in names:
                raise ValueError(f"Unknown weekday {configured!r}")
            return weekday == names[configured]
        raise ValueError(f"Unsupported frequency {frequency!r} (V1: daily/weekly only)")

    def _trigger(self, task: dict[str, Any], directory: Path, variables: dict[str, str]) -> tuple[bool, str]:
        trig = task["trigger"]
        mode = trig["type"]
        if mode == "always":
            return True, "Scheduled unconditional report"
        if mode == "sql":
            rows = sql_file(self.spark, directory / trig.get("file", "trigger.sql"), variables, limit=2)
            if len(rows) != 1:
                raise ValueError(f"Task {task['task_id']} SQL trigger must return exactly one row")
            decision = rows[0].get("should_trigger")
            if type(decision) is not bool:
                raise ValueError(f"Task {task['task_id']} SQL should_trigger must be BOOLEAN, got {decision!r}")
            return decision, str(rows[0].get("reason") or "SQL condition met")
        if mode == "agent":
            path = directory / trig.get("prompt", "trigger_prompt.md")
            question = render(path.read_text(encoding="utf-8"), variables)
            question += ("\n\nReturn exactly one JSON object, no prose or code fence: "
                         '{"trigger":true/false,"reason":"short factual explanation"}. '
                         "If you cannot verify the data, do not guess; explain the error instead.")
            response = self.agents.ask(trig["agent"], question)
            return parse_boolean_decision(response)
        raise ValueError(f"Unsupported trigger type {mode!r}")

    def _report(self, task: dict[str, Any], directory: Path,
                variables: dict[str, str], reason: str) -> str:
        report_cfg = task["report"]
        data_path = report_cfg.get("data_file")
        evidence = sql_file(self.spark, directory / data_path, variables) if data_path else []
        report_variables = dict(variables)
        report_variables["trigger_reason"] = reason
        report_variables["data_json"] = json.dumps(evidence, ensure_ascii=False, default=str)
        prompt_path = directory / report_cfg.get("prompt", "report_prompt.md")
        prompt = render(prompt_path.read_text(encoding="utf-8"), report_variables)
        report = self.agents.ask(report_cfg["agent"], prompt)
        if not report.strip():
            raise RuntimeError(f"Task {task['task_id']}: agent returned empty report")
        template_path = report_cfg.get("template")
        if template_path:
            template = (directory / template_path).read_text(encoding="utf-8")
            final_vars = dict(report_variables)
            final_vars["report_body"] = report
            return render(template, final_vars)
        return report
