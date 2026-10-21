"""Lifecycle and report history API. No run-now, Job mutation, or provisioning routes."""
from __future__ import annotations
import os
import re
from typing import Literal
import sqlglot
from sqlglot import exp
from fastapi import HTTPException, Query
from pydantic import BaseModel, ConfigDict
from hub_auth import current_identity, ownership_sql, ownership_params
from task_drafts import ScheduleConfig, TriggerConfig, ReportConfig, has_condition


def table_name(value):
    if not re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*\.[A-Za-z_][A-Za-z_0-9]*', value):
        raise ValueError('Expected catalog.schema.table identifier')
    return value


def validate_read_sql(query):
    from condition_sql import validate_sql
    return validate_sql(query)


def execution_enabled():
    return os.getenv('TASK_EXECUTION_ENABLED', '').lower() == 'true'


def readiness(task):
    errors = []
    try:
        schedule = ScheduleConfig.model_validate(task['schedule'])
        trigger = TriggerConfig.model_validate(task['trigger'])
        report = ReportConfig.model_validate(task['report'])
        dispatch_time = os.getenv('TASK_DISPATCH_TIME', '09:00')
        dispatch_timezone = os.getenv('TASK_DISPATCH_TIMEZONE', 'Asia/Shanghai')
        if schedule.time != dispatch_time or schedule.timezone != dispatch_timezone:
            errors.append(f'现有 Job 每天仅在 {dispatch_time} · {dispatch_timezone} 调用；此时间不兼容，请修改草稿。')
        if schedule.frequency == 'interval' and not schedule.start_date:
            errors.append('间隔任务需要开始日期。')
        if trigger.type == 'unconfigured':
            errors.append('请补充完整触发条件。')
        if trigger.type == 'agent' and (not trigger.agent or not trigger.prompt.strip()):
            errors.append('请补充触发 Agent 和判断提示词。')
        if trigger.type == 'agent':
            from condition_sql import verified_test
            from condition_decision import agent_test_key
            if not verified_test(agent_test_key(trigger.agent,trigger.prompt,trigger.context_agents), report.condition_agent):
                errors.append('请先运行一次 AI 条件测试；修改判断条件、Agent 或比较周期后需要重新测试。')
        if trigger.type == 'sql':
            validate_read_sql(trigger.sql)
            from condition_sql import verified_test
            if not verified_test(trigger.sql, report.condition_sql):
                errors.append('请先运行一次只读 SQL 条件测试；修改 SQL 或比较周期后需要重新测试。')
        if not report.agent or not report.prompt.strip():
            errors.append('请补充报告 Agent 和分析要求。')
        if report.delivery.enabled:
            if not report.delivery.to:
                errors.append('邮件通知需要收件人。')
            if os.getenv('TASK_EMAIL_ENABLED', '').lower() != 'true':
                errors.append('项目邮件通道尚未配置；可以先保存草稿。')
        review = report.draft_review
        if review and (review.conditional or has_condition(review.original_request)) and trigger.type == 'always':
            errors.append('原始需求包含条件，不能无条件执行。')
    except (ValueError, KeyError, TypeError):
        errors.append('任务配置无效；请核对调度、SQL 和 Agent 字段。')
    return {
        'errors': errors,
        'configuration_ready': not errors,
        'execution_enabled': execution_enabled(),
        'can_activate': not errors and execution_enabled(),
        'warnings': [
            '启用表示允许执行器读取；不会启动或恢复 Job。',
            '请审核原始需求、生成假设和待补充信息；AI 判断及指标口径尚未业务验收。',
            '报告写入通知队列不代表邮件已送达。',
        ],
        'background_authorization_ready': False,
        'scheduler': {'time': os.getenv('TASK_DISPATCH_TIME', '09:00'),
                      'timezone': os.getenv('TASK_DISPATCH_TIMEZONE', 'Asia/Shanghai'),
                      'job_id': '997638013141610'},
    }


class LifecycleRequest(BaseModel):
    model_config = ConfigDict(extra='forbid')
    action: Literal['activate', 'pause', 'edit']
    confirm: bool = False
    reviewed: bool = False
    expected_updated_at: str


def register_execution_routes(app, load_registry, load_yaml, query, registry_table, load_email_catalog=lambda: {}):
    history_table = table_name(os.getenv('TASK_RUNS_TABLE', 'workspace.douyin.business_task_runs_v1'))

    def find(task_id):
        try:
            tasks = [task for task in load_registry() if task['task_id'] == task_id]
        except Exception as exc:
            raise HTTPException(503, '任务 Registry 暂不可用。') from exc
        if not tasks:
            raise HTTPException(404, 'Registry 任务不存在；YAML 任务请通过原配置管理。')
        if len(tasks) != 1:
            raise HTTPException(409, '任务 ID 重复，请检查 Registry。')
        return tasks[0]

    @app.get('/api/report-options')
    def report_options():
        try:
            catalog = load_email_catalog()
            return {
                'templates': [{'id': key, 'label': value.get('label', key)} for key, value in catalog.get('templates', {}).items()],
                'csv_exports': [{'id': key, 'label': value.get('label', key)} for key, value in catalog.get('exports', {}).items()],
                'email_enabled': os.getenv('TASK_EMAIL_ENABLED', '').lower() == 'true',
            }
        except Exception as exc:
            raise HTTPException(503, '邮件模板/CSV 数据集目录暂不可用，请检查 Task Manager 配置。') from exc

    @app.get('/api/tasks/{task_id}/readiness')
    def check_readiness(task_id: str):
        task = find(task_id)
        result = readiness(task)
        if task.get('report', {}).get('delivery', {}).get('enabled'):
            try:
                catalog = load_email_catalog()
                delivery = task['report']['delivery']
                allowed = {value.lower() for value in catalog.get('allowed_recipients', [])}
                if any(value.lower() not in allowed for value in delivery.get('to', []) + delivery.get('cc', [])):
                    result['errors'].append('收件人尚未加入项目允许发送的名单。')
                if delivery.get('template_id', 'default') not in catalog.get('templates', {}):
                    result['errors'].append('所选邮件模板尚未接入。')
                if any(key not in catalog.get('exports', {}) for key in delivery.get('csv_exports', [])):
                    result['errors'].append('所选 CSV 数据集尚未接入。')
            except Exception:
                result['errors'].append('无法核对邮件模板和 CSV 数据集。')
        result['configuration_ready'] = not result['errors']
        result['can_activate'] = result['configuration_ready'] and result['execution_enabled']
        result.update(task_id=task_id, updated_at=task['updated_at'], lifecycle_status=task['lifecycle_status'],
                      configuration={key: task[key] for key in ('task_id', 'name', 'schedule', 'trigger', 'report')})
        return result

    @app.post('/api/tasks/{task_id}/lifecycle')
    def change_lifecycle(task_id: str, req: LifecycleRequest):
        if not req.confirm:
            raise HTTPException(400, '请明确确认状态变更。')
        task = find(task_id)
        if task['updated_at'] != req.expected_updated_at:
            raise HTTPException(409, '任务已被修改，请刷新后重新审核。')
        current = task['lifecycle_status']
        if req.action == 'activate':
            if not req.reviewed:
                raise HTTPException(400, '启用前请确认已审核配置、假设与待补充信息。')
            result = check_readiness(task_id)
            if result['errors']:
                raise HTTPException(422, '；'.join(result['errors']))
            if not result['execution_enabled']:
                raise HTTPException(503, '执行集成尚未开放；草稿已保留，需完成部署验收后启用。')
            if current not in ('draft', 'paused'):
                raise HTTPException(409, '只有草稿或暂停任务可以启用。')
            try:
                if any(str(t.get('task_id') or t.get('_task_id')) == task_id for t in load_yaml()):
                    raise HTTPException(409, '任务 ID 与 YAML 配置重复。')
            except HTTPException:
                raise
            except Exception as exc:
                raise HTTPException(503, '无法核对原有 YAML 任务，未启用。') from exc
            status, enabled = 'active', 'true'
        elif req.action == 'pause':
            if current != 'active':
                raise HTTPException(409, '只有启用任务可以暂停。')
            status, enabled = 'paused', 'false'
        else:
            if current != 'paused':
                raise HTTPException(409, '请先暂停，再转回草稿编辑。')
            status, enabled = 'draft', 'false'
        try:
            result = query(f'''UPDATE {registry_table} SET lifecycle_status=:status,
                enabled={enabled}, updated_at=current_timestamp()
                WHERE task_id=:task_id AND lifecycle_status=:current
                AND CAST(updated_at AS STRING)=:version AND {ownership_sql()}''', {
                    **ownership_params(),
                    'task_id': task_id, 'status': status, 'current': current, 'version': req.expected_updated_at,
                })
            if not result or int(result[0].get('num_affected_rows', 0)) != 1:
                raise HTTPException(409, '任务状态已改变，请刷新后重试。')
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, '状态更新未确认，请刷新核对；不会自动重试。') from exc
        return {'task_id': task_id, 'lifecycle_status': status, 'enabled': enabled == 'true'}

    @app.get('/api/reports')
    def reports(task_id: str = '', status: Literal['', 'RUNNING', 'REPORT_CREATED', 'NOT_TRIGGERED', 'TRIGGER_CONFIRMED', 'FAILED'] = '',
                limit: int = Query(25, ge=1, le=100), offset: int = Query(0, ge=0, le=10000)):
        clauses, params = [ownership_sql('config_json')], ownership_params()
        if task_id:
            clauses.append('task_id=:task_id'); params['task_id'] = task_id
        if status:
            clauses.append('status=:status'); params['status'] = status
        where = 'WHERE ' + ' AND '.join(clauses) if clauses else ''
        try:
            rows = query(f'''SELECT run_id,task_id,task_name,source,scheduled_slot,dispatch_date,
                status,trigger_reason,report_text,error_code,notification_status,started_at,finished_at,
                provider_message_id,notification_error_code,artifacts_json
                FROM {history_table} {where} ORDER BY started_at DESC,run_id DESC
                LIMIT {limit + 1} OFFSET {offset}''', params) or []
        except Exception as exc:
            raise HTTPException(503, '报告历史暂不可用；请检查执行记录表及 App 读取权限。') from exc
        return {'runs': rows[:limit], 'next_offset': offset + limit if len(rows) > limit else None}
