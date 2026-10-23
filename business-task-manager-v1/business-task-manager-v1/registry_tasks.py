"""Registry configuration and calendar contract. Pure Python; no remote calls."""
from __future__ import annotations
import json
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

FQN = re.compile(r'^[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*$')
DAYS = ('monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday')


def table_name(value):
    if not FQN.fullmatch(value):
        raise ValueError('Expected catalog.schema.table identifier')
    return value


def registry_task(row):
    task = {key: row[key] for key in ('task_id', 'name', 'enabled', 'lifecycle_status')}
    if type(task['enabled']) is not bool:
        raise ValueError('Registry enabled must be BOOLEAN')
    task['_source'] = 'registry'
    for key in ('schedule', 'trigger', 'report'):
        task[key] = json.loads(row[key + '_config'] or '{}')
        if not isinstance(task[key], dict):
            raise ValueError('Invalid Registry JSON object')
    return task


def validate_schedule(schedule, dispatch):
    if dispatch.get('mode')=='per_task':
        from schedule_core import validate
        validate(schedule,dispatch)
        return
    if schedule.get('time', dispatch['time']) != dispatch['time'] or schedule.get('timezone', dispatch['timezone']) != dispatch['timezone']:
        raise ValueError('Existing Job supports only %s %s; save other times as drafts' % (dispatch['time'], dispatch['timezone']))
    frequency = schedule.get('frequency')
    if frequency not in ('daily', 'weekly', 'interval'):
        raise ValueError('Unsupported frequency')
    if frequency == 'weekly' and schedule.get('weekday', 'monday') not in DAYS:
        raise ValueError('Invalid weekday')
    if frequency == 'interval':
        days = schedule.get('interval_days')
        if type(days) is not int or not 1 <= days <= 365 or not schedule.get('start_date'):
            raise ValueError('Interval requires 1–365 days and start_date')
    if schedule.get('start_date'):
        date.fromisoformat(schedule['start_date'])
    ZoneInfo(dispatch['timezone'])


def due_slot(task, dispatch, now: datetime):
    """One local-calendar slot per eligible day; no implicit backfill or retry."""
    if dispatch.get('mode')=='per_task':
        from schedule_core import plan
        return plan(task['schedule'],now,dispatch)['slot']
    schedule = task['schedule']
    validate_schedule(schedule, dispatch)
    local = now.astimezone(ZoneInfo(dispatch['timezone']))
    if local.strftime('%H:%M') < dispatch['time']:
        return None
    day = local.date()
    anchor = date.fromisoformat(schedule['start_date']) if schedule.get('start_date') else None
    if anchor and day < anchor:
        return None
    frequency = schedule['frequency']
    if frequency == 'weekly' and DAYS[day.weekday()] != schedule.get('weekday', 'monday'):
        return None
    if frequency == 'interval' and (day - anchor).days % schedule['interval_days']:
        return None
    return day.isoformat() + 'T' + dispatch['time'] + '[' + dispatch['timezone'] + ']'


def validate_executable(task, dispatch):
    validate_schedule(task['schedule'], dispatch)
    trigger, report = task['trigger'], task['report']
    if trigger.get('type') not in ('always', 'agent', 'sql'):
        raise ValueError('Trigger is incomplete')
    review = report.get('draft_review') or {}
    if review.get('conditional') and trigger['type'] == 'always':
        raise ValueError('Conditional request cannot become unconditional')
    if trigger['type'] == 'agent' and (trigger.get('agent') not in ('genie_one',) or not trigger.get('prompt', '').strip()):
        raise ValueError('Agent trigger needs configured agent and prompt')
    if trigger['type'] == 'sql':
        validate_read_sql(trigger.get('sql', ''))
    delivery = report.get('delivery') or {}
    if delivery.get('enabled'):
        from report_email import email_address
        if not delivery.get('to'):
            raise ValueError('Email recipients missing')
        for address in delivery.get('to', []) + delivery.get('cc', []):
            email_address(address)
    if report.get('agent') not in ('genie_one',) or not report.get('prompt', '').strip():
        raise ValueError('Report needs configured Genie agent and prompt')


def validate_read_sql(query):
    import sqlglot
    from sqlglot import exp
    try:
        trees = sqlglot.parse(query, read='databricks')
    except sqlglot.errors.SqlglotError as exc:
        raise ValueError('SQL 无法解析。') from exc
    if len(trees) != 1 or not isinstance(trees[0], (exp.Select, exp.Union)):
        raise ValueError('只允许单条 SELECT / WITH 查询。')
    for node in trees[0].walk():
        if isinstance(node, (exp.DDL, exp.DML, exp.Command, exp.Into)):
            raise ValueError('禁止写入、建表、导出或执行命令。')
        if isinstance(node, exp.Anonymous) and not (
            not isinstance(node.parent, exp.Dot) and (
                node.name.upper() == 'MEASURE' or node.name.upper() == 'TRY_DIVIDE' and len(node.expressions) == 2)):
            raise ValueError('暂不允许自定义或未识别函数；请使用标准 SQL 函数。')
        if isinstance(node, exp.Table) and not isinstance(node.this, exp.Identifier):
            raise ValueError('暂不允许外部数据源或表函数。')
        if isinstance(node, exp.Func) and node.sql_name().upper().startswith(('AI_', 'HTTP_', 'REMOTE_', 'WRITE_')):
            raise ValueError('条件 SQL 不允许调用 AI、远程服务或写入函数。')
    return trees[0]


def condition_parameters(business_date, window):
    day = date.fromisoformat(str(business_date))
    if window == 'day_vs_week':
        previous_end = day - timedelta(days=day.weekday()+1)
        return {'business_date': day.isoformat(), 'period_start': day.isoformat(), 'period_end': day.isoformat(),
                'previous_period_start': (previous_end-timedelta(days=6)).isoformat(),
                'previous_period_end': previous_end.isoformat()}
    if window == 'week':
        end = day - timedelta(days=(day.weekday() + 1) % 7)
        start = end - timedelta(days=6)
    elif window == 'rolling7':
        end, start = day, day - timedelta(days=6)
    elif window == 'day':
        end = start = day
    else:
        raise ValueError('未知比较周期。')
    length = (end-start).days + 1
    return {'business_date': day.isoformat(), 'period_start': start.isoformat(), 'period_end': end.isoformat(),
            'previous_period_start': (start-timedelta(days=length)).isoformat(),
            'previous_period_end': (start-timedelta(days=1)).isoformat()}
