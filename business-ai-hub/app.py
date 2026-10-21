"""Business AI Hub — FastAPI backend.

Core endpoints:
  GET  /api/health  — liveness check
  GET  /api/tasks   — returns real Task Manager configurations
  POST /api/chat    — user-authorized Genie One analysis
  POST /api/conversation/route, /analysis — local unified conversation workflow
  POST /api/tasks/generate — proposes a validated draft through a configured LLM
  Registry CRUD / lifecycle — explicit draft review and activation; no Job start
  GET /api/reports — persisted execution and report history
"""
from __future__ import annotations

import json
import os
import time
import threading
import uuid
from typing import Any

import yaml
import requests
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout, StatementParameterListItem
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field, model_validator

from task_drafts import (
    GenerateRequest, ReportConfig, ScheduleConfig, TriggerConfig,
    extract_proposal, has_condition, review_proposal,
)
from task_llm import LLMNotConfigured, LLMAccessError, generate_configuration
from task_execution import register_execution_routes, table_name
from conversation_workflow import AnalysisRequest, AnalysisTickets, RouteRequest, extract_plan, generate_route, task_action_request
from telemetry import TestMetricsMiddleware, observed_workspace, recorder, span
from hub_auth import (install_auth, current_identity, business_workspace, owns, ownership_sql, ownership_params)


# ============================================================
# App Configuration
# ============================================================

app = FastAPI(
    title="Business AI Hub",
    version="0.4.0",
)
app.add_middleware(TestMetricsMiddleware)
install_auth(app)

# Task Manager root.
# Workspace API path format (no /Workspace prefix).
from pathlib import Path
TASK_MANAGER_PATH = os.getenv('HUB_TASK_MANAGER_ROOT', str(Path(__file__).resolve().parents[1] /
    'business-task-manager-v1' / 'business-task-manager-v1'))

_ws: WorkspaceClient | None = None

# SQL Warehouse for registry table access.
SQL_WAREHOUSE_ID = os.environ.get(
    "SQL_WAREHOUSE_ID",
    "",
)
REGISTRY_TABLE = table_name(os.getenv("TASK_REGISTRY_TABLE", "workspace.douyin.business_task_registry_v1"))


# ============================================================
# Databricks Workspace Client
# ============================================================

def _workspace() -> WorkspaceClient:
    """Business queries always use the authenticated user, never storage identity."""
    return business_workspace()


def _storage_workspace():
    global _ws
    if _ws is None:
        profile = os.getenv('HUB_STORAGE_PROFILE')
        if not profile:
            raise HTTPException(503, '项目元数据存储身份尚未配置；未使用用户或共享业务身份回退。')
        _ws = observed_workspace(WorkspaceClient(profile=profile))
    return _ws


# ============================================================
# Task Manager — Workspace API
# ============================================================

def _ws_read_yaml(path: str) -> dict[str, Any]:
    """Read a YAML file from Databricks Workspace via the API."""

    target = Path(path).resolve()
    if not target.is_relative_to(Path(TASK_MANAGER_PATH).resolve()):
        raise ValueError('Configuration must belong to the installed Task Manager directory')
    text = target.read_text(encoding='utf-8')

    result = yaml.safe_load(text)

    if not isinstance(result, dict):
        raise ValueError(f"Expected YAML mapping: {path}")

    return result


def _ws_list_children(path: str) -> list[str]:
    """List immediate child paths under a Workspace directory."""

    return [
        obj.path
        for obj in _workspace().workspace.list(path)
        if obj.path
    ]


def _load_task_configs() -> list[dict[str, Any]]:
    """Read the four existing task configs directly from Workspace."""

    task_names = [
        "competitor_risk",
        "daily_sales",
        "sales_anomaly",
        "weekly_sales",
    ]

    configs = []

    for name in task_names:
        config_path = (
            f"{TASK_MANAGER_PATH}/tasks/{name}/config.yaml"
        )

        try:
            task = _ws_read_yaml(config_path)
            task["_task_id"] = name
            task["_config_path"] = config_path
            configs.append(task)

        except Exception as exc:
            raise RuntimeError(
                f"Cannot read {config_path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc

    return configs


# ============================================================
# Task Registry — SQL via Statement Execution API
# ============================================================

def _exec_sql(sql: str, parameters: dict[str, str] | None = None) -> list[dict[str, Any]] | None:
    """Execute SQL on the SQL warehouse and return rows as dicts.

    Follows inline result chunks, including DML affected-row metadata. Never returns a truncated result.
    """

    ws = _storage_workspace()
    if ws.config.host.rstrip('/') != current_identity().workspace:
        raise HTTPException(503, '元数据存储与用户所属 Workspace 不一致；已阻止访问。')
    if not SQL_WAREHOUSE_ID:
        raise HTTPException(503, 'SQL Warehouse 尚未配置。')

    resp = ws.statement_execution.execute_statement(
        warehouse_id=SQL_WAREHOUSE_ID,
        statement=sql,
        parameters=[StatementParameterListItem(name=k, value=str(v)) for k, v in (parameters or {}).items()],
        catalog=REGISTRY_TABLE.split(".")[0],
        schema=REGISTRY_TABLE.split(".")[1],
        wait_timeout="50s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
    )

    status = str(resp.status.state).upper()

    if not status.endswith("SUCCEEDED"):
        raise RuntimeError(
            f"SQL execution {status}: "
            f"{getattr(resp.status, 'error', None)}"
        )

    if resp.manifest and resp.manifest.truncated:
        raise RuntimeError('SQL result was truncated; refusing incomplete task/history data')
    result = resp.result
    if result is None or result.data_array is None:
        return None
    columns = [c.name for c in resp.manifest.schema.columns]
    data = list(result.data_array)
    seen = set()
    while result.next_chunk_index is not None:
        index = result.next_chunk_index
        if index in seen or len(seen) >= 1000:
            raise RuntimeError('Invalid SQL result chunk sequence')
        seen.add(index)
        result = ws.statement_execution.get_statement_result_chunk_n(resp.statement_id, index)
        data.extend(result.data_array or [])
    return [dict(zip(columns, row)) for row in data]


def _registry_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ('true', 'false'):
        return value.strip().lower() == 'true'
    raise ValueError('Registry enabled value must be BOOLEAN')


def _load_registry_tasks() -> list[dict[str, Any]]:
    """Read all tasks from the Delta registry table."""

    rows = _exec_sql(
        f"SELECT task_id, name, description, enabled, "
        f"schedule_config, trigger_config, report_config, "
        f"owner, lifecycle_status, CAST(created_at AS STRING) AS created_at, "
        f"CAST(updated_at AS STRING) AS updated_at "
        f"FROM {REGISTRY_TABLE} WHERE {ownership_sql()} ORDER BY created_at", ownership_params()
    )

    if not rows:
        return []

    tasks: list[dict[str, Any]] = []

    for row in rows:
        task: dict[str, Any] = {
            "task_id": row.get("task_id"),
            "name": row.get("name"),
            "description": row.get("description") or "",
            "enabled": _registry_bool(row.get("enabled")),
            "schedule": json.loads(row["schedule_config"]) if row.get("schedule_config") else {},
            "trigger": json.loads(row["trigger_config"]) if row.get("trigger_config") else {},
            "report": json.loads(row["report_config"]) if row.get("report_config") else {},
            "owner": row.get("owner") or "",
            "lifecycle_status": row.get("lifecycle_status") or "draft",
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "_source": "registry",
        }
        if owns(task):
            tasks.append(task)

    return tasks


# ============================================================
# Pydantic Models — Task Registry
# ============================================================

class TaskCreateRequest(BaseModel):
    task_id: str = Field(
        ...,
        min_length=1,
        description="Unique task identifier",
    )
    name: str = Field(..., min_length=1, description="Display name")
    description: str = Field(default="", description="Task description")
    enabled: bool = Field(default=False, description="Enabled flag")
    schedule: ScheduleConfig = Field(..., description="Schedule configuration")
    trigger: TriggerConfig = Field(..., description="Trigger configuration")
    report: ReportConfig = Field(
        default_factory=ReportConfig,
        description="Report configuration",
    )
    owner: str = Field(default="", description="Task owner")

    @model_validator(mode='after')
    def preserve_condition(self):
        review = self.report.draft_review
        if review and (review.conditional or has_condition(review.original_request)) and self.trigger.type == 'always':
            raise ValueError('原始需求包含触发条件，请保留条件或标记为待补充。')
        return self


class TaskUpdateRequest(BaseModel):
    expected_updated_at: str | None = None
    name: str = Field(default="", description="Display name")
    description: str = Field(default="", description="Task description")
    enabled: bool = Field(default=False, description="Enabled flag")
    schedule: ScheduleConfig | None = Field(
        default=None, description="Schedule configuration"
    )
    trigger: TriggerConfig | None = Field(
        default=None, description="Trigger configuration"
    )
    report: ReportConfig | None = Field(
        default=None, description="Report configuration"
    )
    owner: str = Field(default="", description="Task owner")


class TaskDeleteRequest(BaseModel):
    confirm: bool = Field(
        ...,
        description="Must be true to confirm deletion",
    )


# ============================================================
# Genie — Response Extraction
# ============================================================

def _extract_answer(msg: Any) -> str:
    """Extract generated text from Genie message attachments.

    Important:
    GenieMessage.content can contain the original user prompt.

    The actual generated response is found in:
        msg.attachments[*].text.content
    """

    parts: list[str] = []

    for attachment in getattr(msg, "attachments", None) or []:

        text_attachment = getattr(attachment, "text", None)

        if text_attachment is not None:

            text = getattr(text_attachment, "content", None)

            if text and str(text).strip():
                parts.append(str(text).strip())

    return "\n\n".join(parts)

def _chat_workspace():
    return _workspace()


# ============================================================
# API — Health
# ============================================================

@app.get("/api/health")
async def health():
    """Health check."""

    return {
        "status": "ok",
    }


# ============================================================
# API — Tasks
# ============================================================

@app.get("/api/tasks")
def get_tasks():
    """Return the real Task Manager configurations."""

    try:
        yaml_tasks = [t for t in _load_task_configs() if owns(t)]

        # Tag YAML tasks with their source.
        for t in yaml_tasks:
            t["_source"] = "yaml"
            t["lifecycle_status"] = "active"

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail="无法读取 Task Manager 配置，请检查 Workspace 目录权限。",
        )

    # Read registry tasks (drafts and beyond).
    try:
        registry_tasks = _load_registry_tasks()
    except Exception as exc:
        raise HTTPException(503, "Registry 暂不可用，任务列表可能不完整；请稍后刷新。") from exc

    all_tasks = yaml_tasks + registry_tasks

    return {
        "tasks": all_tasks,
        "task_count": len(all_tasks),
        "yaml_count": len(yaml_tasks),
        "registry_count": len(registry_tasks),
    }


# ============================================================
# API — Task Registry CRUD
# ============================================================

@app.post('/api/tasks/generate')
def generate_task(req: GenerateRequest):
    """Generate a suggestion only: never read/write Registry or call Genie."""
    try:
        context = {'conversation':[turn.model_dump() for turn in req.conversation], 'source_agent':req.source_agent}
        raw = generate_configuration(_workspace, req.requirements, **context)
        with span('validate_task_proposal',kind='validation'):
            proposal = extract_proposal(raw)
        configuration = _proposal_configuration(proposal, req)
    except LLMAccessError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except requests.Timeout as exc:
        raise HTTPException(status_code=504, detail='模型生成超时，未保存草稿；可手动配置或主动重试。') from exc
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise HTTPException(status_code=502, detail='模型返回的任务配置无效或不完整，未保存；请补充需求或手动配置。') from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail='无法调用任务生成模型，请检查 endpoint、结构化输出兼容性及 App 调用权限。') from exc
    return {'configuration': configuration.model_dump(mode='json'), 'lifecycle_status': 'draft'}


def _proposal_configuration(proposal, req):
    with span('build_task_draft',kind='validation',initial_trigger_type=proposal.trigger.type) as event:
        review = review_proposal(proposal, req.requirements)
        review.conversation = req.conversation
        review.source_agent = req.source_agent
        configuration = TaskCreateRequest(
            task_id=f'task_{uuid.uuid4().hex}', name=proposal.name,
            description=proposal.description, schedule=proposal.schedule,
            trigger=proposal.trigger,
            report=ReportConfig(**proposal.report.model_dump(), draft_review=review),
        )
        event['final_trigger_type']=configuration.trigger.type
        return configuration


# Unified workflow is deliberately limited to the loopback, single-user dev launcher.
# A browser-provided owner/identity cannot enable it or select a different Workspace.
_analysis_tickets = AnalysisTickets()


def _require_local_workflow():
    if os.getenv('HUB_LOCAL_WORKFLOW') != 'true' and os.getenv('HUB_WORKFLOW_ENABLED') != 'true':
        raise HTTPException(503, '统一对话工作流尚未开启；公司多用户接入需先完成身份授权。')


@app.get('/api/conversation/settings')
def conversation_settings():
    return {'enabled': os.getenv('HUB_LOCAL_WORKFLOW') == 'true' or os.getenv('HUB_WORKFLOW_ENABLED') == 'true',
            'analysis_provider': 'genie_one',
            'router_model': os.getenv('CONVERSATION_ROUTER_ENDPOINT') or os.getenv('TASK_PARSER_ENDPOINT', ''),
            'metrics_enabled': recorder() is not None,
            'identity_mode': current_identity().mode, 'task_provider': 'genie_one'}


@app.get('/api/test-metrics/export')
def export_test_metrics():
    _require_local_workflow()
    if current_identity().mode != 'local_cli':
        raise HTTPException(403, '全部本地测试数据仅允许本地 CLI 用户导出。')
    store=recorder()
    if store is None:
        raise HTTPException(503,'本地测试记录尚未开启。')
    from fastapi.responses import JSONResponse
    return JSONResponse(store.export(),headers={'Content-Disposition':'attachment; filename="business-ai-hub-test-metrics.json"',
                                                'Cache-Control':'no-store'})


@app.post('/api/conversation/route')
def route_conversation(req: RouteRequest):
    """One model call: classify AND extract a task. No Genie calls or persistence."""
    _require_local_workflow()
    started = time.monotonic()
    try:
        raw = generate_route(_workspace, req.requirements,
                             conversation=[turn.model_dump() for turn in req.conversation],
                             source_agent=req.source_agent)
        with span('validate_route_plan',kind='validation') as event:
            plan = extract_plan(raw)
            event.update(intent=plan.intent,execution_order=plan.execution_order,analysis_agent=plan.analysis_agent)
        # A model miss must never forward an obvious scheduling command to analytics.
        if plan.intent == 'analysis' and task_action_request(req.requirements):
            return {'intent':'clarify', 'clarification':'你希望只做一次分析，还是设置后台自动任务？请明确说明。',
                    'execution_order':'independent', 'configuration':None, 'analysis_ticket':None}
        task_req = req
        if plan.intent == 'mixed':
            # Review only the task subrequest; a conditional analysis question must
            # not turn an explicitly unconditional recurring report into a trigger.
            # Preserve the full original mixed request as quoted provenance.
            history = [*req.conversation[-11:], {'role':'user','content':req.requirements}]
            history = [turn.model_dump() if hasattr(turn,'model_dump') else turn for turn in history]
            while sum(len(turn['content']) for turn in history) > 24000:
                history.pop(0)
            task_req = RouteRequest(requirements=plan.task_requirements,conversation=history,source_agent=req.source_agent)
        configuration = _proposal_configuration(plan.task_proposals[0], task_req).model_dump(mode='json') if plan.task_proposals else None
        ticket = _analysis_tickets.put(plan.analysis_question, plan.analysis_agent, subject=current_identity().key) if plan.analysis_question else None
    except LLMAccessError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except LLMNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except requests.Timeout as exc:
        raise HTTPException(504, '分流超时，未提交 Genie、未保存任务；仍可手动创建。') from exc
    except Exception as exc:
        raise HTTPException(502, '无法生成有效的对话计划；未提交 Genie、未保存任务。请补充需求或使用手动创建。') from exc
    usage = raw.get('usage') or {}
    return {'intent':plan.intent, 'execution_order':plan.execution_order,
            'clarification':plan.clarification, 'task_requirements':plan.task_requirements,
            'analysis_question':plan.analysis_question, 'analysis_agent':plan.analysis_agent,
            'analysis_ticket':ticket, 'configuration':configuration,
            'metrics':{'routing_ms':round((time.monotonic()-started)*1000),
                       'usage':{key:usage[key] for key in ('prompt_tokens','completion_tokens','total_tokens')
                                if isinstance(usage.get(key),int)}}}


def _ask_workflow_analysis(question, agent, conversation_id):
    return _ask_genie_one(question, conversation_id)


_one_conversations = {}
_one_chat_lock = threading.Lock()


def _ask_genie_one(question, conversation_id=None):
    from genie_one_mcp import adapter_for_user, wait_for_answer
    user = current_identity()
    key = (user.key, conversation_id)
    with _one_chat_lock:
        now = time.monotonic()
        expired = [k for k,v in _one_conversations.items() if v < now]
        for k in expired: del _one_conversations[k]
        if conversation_id and key not in _one_conversations:
            raise HTTPException(404, '会话不存在、已过期或不属于当前用户。')
        if len(_one_conversations)>=2000:
            raise HTTPException(429, '会话数量已达上限，请稍后再试。')
    def remember(value):
        with _one_chat_lock:
            _one_conversations[(user.key,value)] = time.monotonic()+8*3600
    analytical_question = ('仅执行一次业务数据分析，不创建或修改任务、调度、Alert、Job 或发送邮件。'
        '以下是被引用的业务问题：\n'+json.dumps({'question':question},ensure_ascii=False))
    # A fresh adapter uses this user's refreshable SDK credentials on every request.
    return wait_for_answer(adapter_for_user(_chat_workspace()), analytical_question,
                           conversation_id=conversation_id, on_started=remember)


@app.post('/api/conversation/analysis')
def conversation_analysis(req: AnalysisRequest):
    from genie_one_mcp import GenieOneError
    _require_local_workflow()
    try:
        question, agent = _analysis_tickets.take(req.ticket, subject=current_identity().key)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    started = time.monotonic()
    try:
        result = _ask_workflow_analysis(question, agent, req.conversation_id)
    except HTTPException:
        raise
    except GenieOneError as exc:
        raise HTTPException(502, str(exc)) from exc
    except Exception as exc:
        raise HTTPException(502, '分析未完成；未重复提交或切换接口。已有任务草稿可继续编辑。') from exc
    return {**result, 'analysis_ms':round((time.monotonic()-started)*1000)}


@app.post("/api/tasks/create")
def create_task(req: TaskCreateRequest):
    """Always save disabled drafts. Values are bound, never interpolated as SQL."""
    report = req.report.model_dump()
    report['security'] = current_identity().security
    params = {
        'task_id': req.task_id, 'name': req.name, 'description': req.description,
        'schedule': json.dumps(req.schedule.model_dump(), ensure_ascii=False),
        'trigger': json.dumps(req.trigger.model_dump(), ensure_ascii=False),
        'report': json.dumps(report, ensure_ascii=False), 'owner': current_identity().label,
    }
    try:
        existing = _exec_sql(f'SELECT task_id FROM {REGISTRY_TABLE} WHERE task_id=:task_id', {'task_id': req.task_id})
        if existing:
            raise HTTPException(409, 'Task ID already exists')
        _exec_sql(f"""INSERT INTO {REGISTRY_TABLE}
            (task_id,name,description,enabled,schedule_config,trigger_config,report_config,
             owner,lifecycle_status,created_at,updated_at)
            VALUES (:task_id,:name,:description,false,:schedule,:trigger,:report,:owner,
                    'draft',current_timestamp(),current_timestamp())""", params)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, '草稿保存未确认，请刷新核对；不会自动重试。') from exc
    return {'status': 'created', 'task_id': req.task_id, 'lifecycle_status': 'draft'}


def _find_registry_task(task_id):
    tasks = [task for task in _load_registry_tasks() if task['task_id'] == task_id]
    if not tasks:
        raise HTTPException(404, 'Task not found')
    if len(tasks) != 1:
        raise HTTPException(409, 'Duplicate task ID in Registry')
    if tasks[0]['lifecycle_status'] != 'draft':
        raise HTTPException(409, 'Only draft tasks can be edited or deleted')
    return tasks[0]


def _require_one_changed(result):
    if not result or int(result[0].get('num_affected_rows', 0)) != 1:
        raise HTTPException(409, '任务已变更，请刷新后重试。')


@app.put("/api/tasks/{task_id}")
def update_task(task_id: str, req: TaskUpdateRequest):
    try:
        old = _find_registry_task(task_id)
        candidate = {key: old[key] for key in ('task_id', 'name', 'description', 'enabled', 'schedule', 'trigger', 'report', 'owner')}
        if req.expected_updated_at is not None and req.expected_updated_at != old['updated_at']:
            raise HTTPException(409, '任务已被修改，请刷新后重新编辑。')
        candidate.update(req.model_dump(exclude_unset=True, exclude={'expected_updated_at'}))
        # Preserve provenance when older clients omit it during a report update.
        old_review = old.get('report', {}).get('draft_review')
        if old_review and candidate.get('report') and not candidate['report'].get('draft_review'):
            candidate['report']['draft_review'] = old_review
        candidate['report']['security'] = old['report']['security']
        candidate['owner'] = old['owner']
        candidate['enabled'] = False
        validated = TaskCreateRequest.model_validate(candidate)
        result = _exec_sql(f"""UPDATE {REGISTRY_TABLE} SET name=:name,description=:description,
            enabled=false,schedule_config=:schedule,trigger_config=:trigger,report_config=:report,
            owner=:owner,updated_at=current_timestamp()
            WHERE task_id=:task_id AND lifecycle_status='draft'
              AND CAST(updated_at AS STRING)=:version AND {ownership_sql()}""", {
                **ownership_params(),
                'task_id': task_id, 'name': validated.name, 'description': validated.description,
                'schedule': json.dumps(validated.schedule.model_dump(), ensure_ascii=False),
                'trigger': json.dumps(validated.trigger.model_dump(), ensure_ascii=False),
                'report': json.dumps(validated.report.model_dump(), ensure_ascii=False),
                'owner': validated.owner, 'version': old['updated_at'],
            })
        _require_one_changed(result)
    except HTTPException:
        raise
    except ValueError as exc:
        raise HTTPException(422, '修改后的配置无效；请保留原始需求的触发条件。') from exc
    except Exception as exc:
        raise HTTPException(502, '草稿更新未确认，请刷新核对。') from exc
    return {'status': 'updated', 'task_id': task_id}


@app.delete("/api/tasks/{task_id}")
def delete_task(task_id: str, req: TaskDeleteRequest):
    if not req.confirm:
        raise HTTPException(400, 'Confirmation required: set confirm=true')
    try:
        old = _find_registry_task(task_id)
        result = _exec_sql(f"""DELETE FROM {REGISTRY_TABLE} WHERE task_id=:task_id
            AND lifecycle_status='draft' AND CAST(updated_at AS STRING)=:version AND {ownership_sql()}""",
            {**ownership_params(), 'task_id': task_id, 'version': old['updated_at']})
        _require_one_changed(result)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, '删除未确认，请刷新核对。') from exc
    return {'status': 'deleted', 'task_id': task_id}


from condition_sql import register_condition_routes


def _condition_workspace():
    return _workspace()


register_condition_routes(app, lambda: _condition_workspace(), {}, SQL_WAREHOUSE_ID,
    lambda: _ws_read_yaml(f'{TASK_MANAGER_PATH}/settings.yaml'))


register_execution_routes(
    app, lambda: _load_registry_tasks(), lambda: _load_task_configs(),
    lambda statement, parameters: _exec_sql(statement, parameters), REGISTRY_TABLE,
    load_email_catalog=lambda: _ws_read_yaml(f'{TASK_MANAGER_PATH}/settings.yaml').get('notification', {}).get('email', {}),
)


# ============================================================
# API — Chat
# ============================================================

class ChatRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        max_length=12000,
        description="Question to ask Genie",
    )

    agent: str = Field(
        ...,
        description="Target agent: genie_one",
    )
    conversation_id: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,128}$')


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Send a question to the selected Genie Space."""

    if task_action_request(req.question):
        raise HTTPException(409, '此消息包含任务或资源操作要求，请使用统一任务草稿入口；未提交 Genie。')
    if req.agent != 'genie_one':
        raise HTTPException(422, '业务接口已统一为 Genie One，请刷新页面。')
    try:
        result = _ask_genie_one(req.question, req.conversation_id)
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(502, 'Genie One 调用未完成；请检查当前用户权限和网络。未切换身份或重复提交。') from exc

    # Step 4: Return the actual generated answer.
    return {
        "agent": req.agent,
        **result,
    }


    # React frontend
from pathlib import Path
from fastapi.staticfiles import StaticFiles

FRONTEND_DIST = Path(__file__).resolve().parent / "frontend" / "dist"

if FRONTEND_DIST.is_dir():
    app.mount(
        "/",
        StaticFiles(directory=str(FRONTEND_DIST), html=True),
        name="frontend",
    )
