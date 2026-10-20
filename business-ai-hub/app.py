"""Business AI Hub — minimal FastAPI backend.

Three endpoints:
  GET  /api/health  — liveness check
  GET  /api/tasks   — returns real Task Manager configurations
  POST /api/chat    — proxies a question to Genie A1 or A2 via the Databricks SDK
"""
from __future__ import annotations

import json
import os
import time
import uuid
from datetime import timedelta
from typing import Any

import yaml
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.sql import ExecuteStatementRequestOnWaitTimeout
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field


# ============================================================
# App Configuration
# ============================================================

app = FastAPI(
    title="Business AI Hub",
    version="0.1.1",
)

# Task Manager root.
# Workspace API path format (no /Workspace prefix).
TASK_MANAGER_PATH = os.environ.get(
    "TASK_MANAGER_PATH",
    "/Workspace/Users/lithomas@wustl.edu/business-task-manager-v1",
)

# Genie Space IDs.
# These are resource identifiers, NOT credentials.
# Environment variables can override them if needed.
GENIE_SPACES = {
    "genie_a1": os.environ.get(
        "GENIE_A1_SPACE_ID",
        "01f1c60941551d9b94f73515f09e4b7b",
    ),
    "genie_a2": os.environ.get(
        "GENIE_A2_SPACE_ID",
        "01f1c60944fa1a43bf79ff847cd5323d",
    ),
}

_ws: WorkspaceClient | None = None

# SQL Warehouse for registry table access.
SQL_WAREHOUSE_ID = os.environ.get(
    "SQL_WAREHOUSE_ID",
    "a54c7a1cd51e6901",
)
REGISTRY_TABLE = "workspace.douyin.business_task_registry_v1"


# ============================================================
# Databricks Workspace Client
# ============================================================

def _workspace() -> WorkspaceClient:
    """Return a cached WorkspaceClient using App Authorization."""
    global _ws

    if _ws is None:
        _ws = WorkspaceClient()

    return _ws


# ============================================================
# Task Manager — Workspace API
# ============================================================

def _ws_read_yaml(path: str) -> dict[str, Any]:
    """Read a YAML file from Databricks Workspace via the API."""

    resp = _workspace().workspace.download(path)

    raw = resp.read() if hasattr(resp, "read") else resp

    text = (
        raw.decode("utf-8")
        if isinstance(raw, bytes)
        else str(raw)
    )

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

def _exec_sql(sql: str) -> list[dict[str, Any]] | None:
    """Execute SQL on the SQL warehouse and return rows as dicts.

    Returns None for statements that produce no result set (INSERT/UPDATE/DELETE).
    """

    ws = _workspace()

    resp = ws.statement_execution.execute_statement(
        warehouse_id=SQL_WAREHOUSE_ID,
        statement=sql,
        catalog="workspace",
        schema="douyin",
        wait_timeout="50s",
        on_wait_timeout=ExecuteStatementRequestOnWaitTimeout.CANCEL,
    )

    status = str(resp.status.state).upper()

    if not status.endswith("SUCCEEDED"):
        raise RuntimeError(
            f"SQL execution {status}: "
            f"{getattr(resp.status, 'error', None)}"
        )

    result = resp.result

    if result is None or result.data_array is None:
        return None

    columns = [c.name for c in resp.manifest.schema.columns]

    return [
        {columns[i]: row[i] for i in range(len(columns))}
        for row in result.data_array
    ]


def _load_registry_tasks() -> list[dict[str, Any]]:
    """Read all tasks from the Delta registry table."""

    rows = _exec_sql(
        f"SELECT task_id, name, description, enabled, "
        f"schedule_config, trigger_config, report_config, "
        f"owner, lifecycle_status, created_at, updated_at "
        f"FROM {REGISTRY_TABLE} ORDER BY created_at"
    )

    if not rows:
        return []

    tasks: list[dict[str, Any]] = []

    for row in rows:
        task: dict[str, Any] = {
            "task_id": row.get("task_id"),
            "name": row.get("name"),
            "description": row.get("description") or "",
            "enabled": bool(row.get("enabled")),
            "schedule": json.loads(row["schedule_config"]) if row.get("schedule_config") else {},
            "trigger": json.loads(row["trigger_config"]) if row.get("trigger_config") else {},
            "report": json.loads(row["report_config"]) if row.get("report_config") else {},
            "owner": row.get("owner") or "",
            "lifecycle_status": row.get("lifecycle_status") or "draft",
            "created_at": str(row.get("created_at") or ""),
            "updated_at": str(row.get("updated_at") or ""),
            "_source": "registry",
        }
        tasks.append(task)

    return tasks


def _sq(s: str) -> str:
    """SQL-escape a single-quoted string value."""
    return s.replace("'", "''")


# ============================================================
# Pydantic Models — Task Registry
# ============================================================

class ScheduleConfig(BaseModel):
    frequency: str = Field(
        ...,
        description="daily, weekly, or interval",
    )
    cron: str = Field(default="", description="Cron expression")
    interval_days: int = Field(
        default=0,
        description="Days between runs (for interval frequency)",
    )


class TriggerConfig(BaseModel):
    type: str = Field(
        ...,
        description="always, sql, or agent",
    )
    sql: str = Field(default="", description="SQL condition")
    agent: str = Field(default="", description="genie_a1 or genie_a2")
    prompt: str = Field(default="", description="Trigger prompt")


class ReportConfig(BaseModel):
    agent: str = Field(default="", description="genie_a1 or genie_a2")
    prompt: str = Field(default="", description="Report prompt")
    template: str = Field(default="", description="Report template")


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


class TaskUpdateRequest(BaseModel):
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

def _ask_genie(space_id: str, question: str) -> tuple[str, str]:
    """Call Genie and expose its actual failure reason."""

    ws = _workspace()

    # Start the conversation without the SDK's automatic waiter.
    response = ws.api_client.do(
        "POST",
        f"/api/2.0/genie/spaces/{space_id}/start-conversation",
        body={"content": question},
    )

    conversation_id = response.get("conversation_id")
    message_id = response.get("message_id")

    if not conversation_id or not message_id:
        raise RuntimeError(
            "Genie did not return conversation/message IDs"
        )

    deadline = time.monotonic() + 480

    while time.monotonic() < deadline:

        msg = ws.genie.get_message(
            space_id=space_id,
            conversation_id=conversation_id,
            message_id=message_id,
        )

        status = str(msg.status).upper()

        if status.endswith("COMPLETED"):
            answer = _extract_answer(msg)

            if not answer:
                raise RuntimeError(
                    "Genie completed but returned no text answer"
                )

            return answer, status

        if status.endswith(("FAILED", "CANCELLED")):
            error = getattr(msg, "error", None)

            error_type = getattr(error, "type", None)
            error_message = getattr(error, "error", None)

            raise RuntimeError(
                f"Genie {status}: "
                f"type={error_type}, "
                f"reason={error_message}"
            )

        time.sleep(3)

    raise RuntimeError("Genie timed out after 8 minutes")



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
async def get_tasks():
    """Return the real Task Manager configurations."""

    try:
        yaml_tasks = _load_task_configs()

        # Tag YAML tasks with their source.
        for t in yaml_tasks:
            t["_source"] = "yaml"
            t["lifecycle_status"] = "active"

    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to read Task Manager configs: {exc}",
        )

    # Read registry tasks (drafts and beyond).
    try:
        registry_tasks = _load_registry_tasks()
    except Exception:
        registry_tasks = []  # Don't fail the whole endpoint if registry is unreachable.

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

@app.post("/api/tasks/create")
async def create_task(req: TaskCreateRequest):
    """Create a new task as DRAFT in the Delta registry.

    Drafts are never automatically executed.
    """

    if req.trigger.type not in ("always", "sql", "agent"):
        raise HTTPException(
            status_code=400,
            detail=f"Invalid trigger type: {req.trigger.type}",
        )

    existing = _exec_sql(
        f"SELECT task_id FROM {REGISTRY_TABLE} "
        f"WHERE task_id = '{_sq(req.task_id)}'"
    )
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"Task ID already exists: {req.task_id}",
        )

    sj = json.dumps(req.schedule.model_dump(), ensure_ascii=False)
    tj = json.dumps(req.trigger.model_dump(), ensure_ascii=False)
    rj = json.dumps(req.report.model_dump(), ensure_ascii=False)

    sql = (
        f"INSERT INTO {REGISTRY_TABLE} "
        f"(task_id, name, description, enabled, "
        f"schedule_config, trigger_config, report_config, "
        f"owner, lifecycle_status, created_at, updated_at) "
        f"VALUES ("
        f"'{_sq(req.task_id)}', '{_sq(req.name)}', '{_sq(req.description)}', "
        f"{str(req.enabled).lower()}, "
        f"'{_sq(sj)}', '{_sq(tj)}', '{_sq(rj)}', "
        f"'{_sq(req.owner)}', 'draft', "
        f"current_timestamp(), current_timestamp())"
    )

    try:
        _exec_sql(sql)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to create task: {exc}",
        )

    return {
        "status": "created",
        "task_id": req.task_id,
        "lifecycle_status": "draft",
    }


@app.put("/api/tasks/{task_id}")
async def update_task(task_id: str, req: TaskUpdateRequest):
    """Update an existing draft task in the registry."""

    existing = _exec_sql(
        f"SELECT lifecycle_status FROM {REGISTRY_TABLE} "
        f"WHERE task_id = '{_sq(task_id)}'"
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Task not found: {task_id}",
        )

    if existing[0].get("lifecycle_status") != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only draft tasks can be edited",
        )

    sets: list[str] = []

    if req.name:
        sets.append(f"name = '{_sq(req.name)}'")
    if req.description:
        sets.append(f"description = '{_sq(req.description)}'")
    sets.append(f"enabled = {str(req.enabled).lower()}")
    if req.schedule:
        sets.append(f"schedule_config = '{_sq(json.dumps(req.schedule.model_dump(), ensure_ascii=False))}'")
    if req.trigger:
        if req.trigger.type not in ("always", "sql", "agent"):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid trigger type: {req.trigger.type}",
            )
        sets.append(f"trigger_config = '{_sq(json.dumps(req.trigger.model_dump(), ensure_ascii=False))}'")
    if req.report:
        sets.append(f"report_config = '{_sq(json.dumps(req.report.model_dump(), ensure_ascii=False))}'")
    if req.owner:
        sets.append(f"owner = '{_sq(req.owner)}'")

    sets.append("updated_at = current_timestamp()")

    sql = f"UPDATE {REGISTRY_TABLE} SET {', '.join(sets)} WHERE task_id = '{_sq(task_id)}'"

    try:
        _exec_sql(sql)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to update task: {exc}",
        )

    return {
        "status": "updated",
        "task_id": task_id,
    }


@app.delete("/api/tasks/{task_id}")
async def delete_task(task_id: str, req: TaskDeleteRequest):
    """Delete a draft task from the registry (requires confirmation)."""

    if not req.confirm:
        raise HTTPException(
            status_code=400,
            detail="Confirmation required: set confirm=true",
        )

    existing = _exec_sql(
        f"SELECT lifecycle_status FROM {REGISTRY_TABLE} "
        f"WHERE task_id = '{_sq(task_id)}'"
    )
    if not existing:
        raise HTTPException(
            status_code=404,
            detail=f"Task not found: {task_id}",
        )

    if existing[0].get("lifecycle_status") != "draft":
        raise HTTPException(
            status_code=409,
            detail="Only draft tasks can be deleted",
        )

    sql = f"DELETE FROM {REGISTRY_TABLE} WHERE task_id = '{_sq(task_id)}'"

    try:
        _exec_sql(sql)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to delete task: {exc}",
        )

    return {
        "status": "deleted",
        "task_id": task_id,
    }


# ============================================================
# API — Chat
# ============================================================

class ChatRequest(BaseModel):
    question: str = Field(
        ...,
        min_length=1,
        description="Question to ask Genie",
    )

    agent: str = Field(
        ...,
        description="Target agent: genie_a1 or genie_a2",
    )


@app.post("/api/chat")
async def chat(req: ChatRequest):
    """Send a question to the selected Genie Space."""

    # Step 1: Validate agent name.
    if req.agent not in GENIE_SPACES:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Unknown agent: {req.agent!r}. "
                f"Supported: {', '.join(GENIE_SPACES)}"
            ),
        )

    # Step 2: Obtain Genie Space ID directly.
    # No dependency on Task Manager settings.yaml.
    space_id = GENIE_SPACES[req.agent]

    if not space_id:
        raise HTTPException(
            status_code=500,
            detail=f"Missing Genie space ID for {req.agent}",
        )

    # Step 3: Call Genie.
    try:
        answer, status = _ask_genie(
            space_id=space_id,
            question=req.question,
        )

    except RuntimeError as exc:
        raise HTTPException(
            status_code=502,
            detail=str(exc),
        )

    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Genie call failed: {exc}",
        )

    # Step 4: Return the actual generated answer.
    return {
        "agent": req.agent,
        "answer": answer,
        "status": status,
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