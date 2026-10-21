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
from typing import TYPE_CHECKING
if TYPE_CHECKING:
    from pyspark.sql import SparkSession

from agent_client import AgentClient, parse_boolean_decision
from condition_decision import decision_prompt, gather_context, context_text
from registry_tasks import due_slot, registry_task, table_name, validate_executable, condition_parameters
from run_store import RunStore

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
    def __init__(self, spark: SparkSession, settings_path: Path, *, creator_runner=None):
        self.creator_runner = creator_runner
        self.spark = spark
        self.root = settings_path.resolve().parent
        self.settings = read_yaml(settings_path)
        self.agents = AgentClient(self.settings["agents"])

    def run(self, *, now=None, agents=None, store=None, publisher=None, mail_provider=None) -> dict[str, Any]:
        # Deployment must explicitly opt in; local tests use injected adapters.
        if self.settings.get("execution", {}).get("enabled") is not True:
            raise RuntimeError("Execution disabled in settings; no tasks or notifications were run")
        notification = self.settings['notification']
        provider = notification.get('provider', 'outbox')
        if provider not in ('outbox', 'gmail'):
            raise ValueError('Unknown notification provider')
        if provider == 'gmail' and notification.get('legacy_alert_disabled') is not True:
            raise RuntimeError('Gmail rollout requires disabling the existing send_alert Job task first')
        schedule = self.settings["schedule"]
        now = now or datetime.now(ZoneInfo(schedule["timezone"]))
        dispatch_date = now.astimezone(ZoneInfo(schedule["timezone"])).date()
        business_date = dispatch_date - timedelta(days=int(schedule.get("business_date_offset_days", 1)))
        variables = {
            "dispatch_date": dispatch_date.isoformat(), "business_date": business_date.isoformat(),
            "period_start": (business_date - timedelta(days=6)).isoformat(),
            "period_end": business_date.isoformat(),
            "previous_period_start": (business_date - timedelta(days=13)).isoformat(),
            "previous_period_end": (business_date - timedelta(days=7)).isoformat(),
        }
        if agents is not None:
            self.agents = agents
        ledger = store or RunStore(self.spark, self.settings["execution"]["runs_table"])
        configs = []
        for path in sorted((self.root / "tasks").glob("*/config.yaml")):
            task = read_yaml(path)
            task['_source'] = 'yaml'
            configs.append((task, path.parent))
        registry = self.settings.get('registry', {})
        if registry.get('enabled') is True:
            table = table_name(registry['table'])
            # Only active, enabled Registry tasks may enter execution.
            for row in self.spark.sql(f'SELECT * FROM {table}').collect():
                value = row.asDict(recursive=True)
                if value.get('lifecycle_status') != 'active' or value.get('enabled') is not True:
                    continue
                try:
                    task = registry_task(value)
                except (ValueError, KeyError, TypeError):
                    task = {key: value.get(key) for key in ('task_id', 'name', 'enabled', 'lifecycle_status')}
                    task.update(_source='registry', schedule={'frequency': 'invalid'}, trigger={}, report={})
                configs.append((task, self.root))
        ids = [t['task_id'] for t, _ in configs]
        if len(ids) != len(set(ids)):
            raise ValueError('Duplicate task IDs across YAML / Registry; refusing dispatch')
        summary = []
        base_variables = variables
        for task, directory in configs:
            variables = dict(base_variables)
            mode = task.get("trigger", {}).get("type")
            review = task.get("report", {}).get("condition_agent" if mode == "agent" else "condition_sql", {})
            if task.get("_source") == "registry" and mode in ("sql", "agent") and review.get("window") in ("day", "week", "rolling7", "day_vs_week"):
                variables.update(condition_parameters(variables["business_date"], review["window"]))
            task['_execution_context'] = dict(variables)
            task_id = task['task_id']
            if task.get('enabled') is not True:
                summary.append({'task': task_id, 'status': 'DISABLED'})
                continue
            try:
                slot = due_slot(task, schedule, now)
                if slot is not None and task['_source'] == 'registry':
                    validate_executable(task, schedule)
                if task.get('report', {}).get('delivery', {}).get('enabled') and provider != 'gmail':
                    raise ValueError('Email report cannot be sent through SQL Alert')
            except ValueError:
                slot = dispatch_date.isoformat() + 'T' + schedule['time'] + '[' + schedule['timezone'] + ']'
                key = ledger.claim(task, slot, dispatch_date, json.dumps(task, ensure_ascii=False, default=str))
                if key:
                    ledger.finish(key, 'FAILED', error='InvalidConfiguration')
                summary.append({'task': task_id, 'status': 'FAILED', 'run_id': key})
                continue
            if slot is None:
                summary.append({'task': task_id, 'status': 'NOT_DUE'})
                continue
            key = ledger.claim(task, slot, dispatch_date, json.dumps(task, ensure_ascii=False, default=str))
            if key is None:
                summary.append({'task': task_id, 'status': 'ALREADY_CLAIMED'})
                continue
            try:
                # An unattended Spark job cannot impersonate the task creator. Require
                # an explicit creator-scoped runner; never use the broad Job/SP identity.
                creator_runner = getattr(self, 'creator_runner', None)
                if creator_runner is None:
                    raise CreatorAuthorizationRequired('Creator delegation is not configured')
                fired, reason, report = creator_runner(task, directory, variables)
                if type(fired) is not bool:
                    raise ValueError('Creator runner must return a strict boolean')
                if not fired and report:
                    raise ValueError('Non-triggered tasks cannot produce a report')
                if len(report) > int(self.settings['notification'].get('max_report_chars', 12000)):
                    raise ValueError('Report exceeds configured size limit')
                status = 'REPORT_CREATED' if fired else 'NOT_TRIGGERED'
            except Exception as exc:
                # No secrets/raw upstream response in the browser-visible error field.
                ledger.finish(key, 'FAILED', error=type(exc).__name__)
                log.warning('Task %s failed: %s', task_id, type(exc).__name__)
                summary.append({'task': task_id, 'status': 'FAILED', 'run_id': key})
                continue
            ledger.finish(key, status, reason=reason, report=report)
            summary.append({'task': task_id, 'status': status, 'run_id': key})
        notification = self.settings['notification']
        if any(item['status'] == 'FAILED' for item in summary):
            raise RuntimeError('One or more tasks failed; reports remain pending; inspect run history')
        if provider == 'gmail':
            from report_email import deliver_emails
            delivery = deliver_emails(self.spark, self.root, notification.get('email', {}), ledger,
                                      dispatch_date, provider=mail_provider)
            return {'date': dispatch_date.isoformat(), 'tasks': summary, 'email': delivery}
        pending = ledger.pending_reports(dispatch_date)
        body = '\n\n'.join('【' + item['task_name'] + ' | 执行日 ' + str(item['dispatch_date']) + '】\n' + item['report_text'] for item in pending)
        if len(body) > int(notification.get('max_report_chars', 12000)):
            raise RuntimeError('Combined reports exceed max_report_chars; reports remain unqueued')
        # Rewrite daily outbox to false on an empty repeat dispatch, avoiding stale email payloads.
        if publisher is None:
            from outbox import publish_batch
            publisher = publish_batch
        publisher(self.spark, notification['outbox_table'], dispatch_date,
                  notification['recipient_group'], body, len(pending))
        ledger.mark_queued([item['run_id'] for item in pending])
        return {'date': dispatch_date.isoformat(), 'reports': len(pending), 'tasks': summary,
                'notification_status': 'QUEUED' if pending else 'NOT_REQUIRED'}

    def _trigger(self, task: dict[str, Any], directory: Path, variables: dict[str, str]) -> tuple[bool, str]:
        trig = task["trigger"]
        mode = trig["type"]
        if mode == "always":
            return True, "Scheduled unconditional report"
        if mode == "sql":
            if task.get('_source') == 'registry':
                query = trig['sql']
                from registry_tasks import validate_read_sql
                validate_read_sql(query)
                names = set(re.findall(r'(?<!:):([A-Za-z_][A-Za-z_0-9]*)', query))
                if names - variables.keys():
                    raise ValueError('Unknown SQL bind parameter')
                rows = [r.asDict(recursive=True) for r in self.spark.sql(
                    query, args={name: variables[name] for name in names}).limit(2).collect()]
            else:
                rows = sql_file(self.spark, directory / trig.get("file", "trigger.sql"), variables, limit=2)
            if len(rows) != 1:
                raise ValueError(f"Task {task['task_id']} SQL trigger must return exactly one row")
            decision = rows[0].get("should_trigger")
            if type(decision) is not bool:
                raise ValueError(f"Task {task['task_id']} SQL should_trigger must be BOOLEAN, got {decision!r}")
            return decision, str(rows[0].get("reason") or "SQL condition met")
        if mode == "agent":
            path = directory / trig.get("prompt", "trigger_prompt.md")
            condition = trig['prompt'] if task.get('_source') == 'registry' else path.read_text(encoding="utf-8")
            question = decision_prompt(condition, variables)
            context_agents = trig.get('context_agents', [])
            if task.get('_source') == 'registry' and any(a != trig['agent'] for a in context_agents):
                from registry_tasks import validate_read_sql
                sources=gather_context(self.agents.workspace,self.agents.config,context_agents,
                    trig['agent'],condition,variables,validate_read_sql)
                question += context_text(sources)
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
        prompt = render(report_cfg['prompt'] if task.get('_source') == 'registry' else prompt_path.read_text(encoding="utf-8"), report_variables)
        prompt += "\nExecution context: " + json.dumps(report_variables, ensure_ascii=False)
        report = self.agents.ask(report_cfg["agent"], prompt)
        if not report.strip():
            raise RuntimeError(f"Task {task['task_id']}: agent returned empty report")
        template_path = report_cfg.get("template")
        if template_path:
            template = template_path if task.get('_source') == 'registry' else (directory / template_path).read_text(encoding='utf-8')
            final_vars = dict(report_variables)
            final_vars["report_body"] = report
            return render(template, final_vars)
        return report


class CreatorAuthorizationRequired(PermissionError):
    """No business query/report is allowed under an unrelated scheduled Job identity."""
