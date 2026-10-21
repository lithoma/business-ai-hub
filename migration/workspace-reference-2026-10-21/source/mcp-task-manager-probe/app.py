"""MCP server for Business Task Manager — exposes the list_tasks tool.

Deployed as a Databricks App.  The tool reads the Task Manager's
tasks/*/config.yaml files (via the Databricks SDK when the app's
service principal has workspace read access, falling back to bundled
copies shipped with the app).
"""
import json
import os
import base64
from pathlib import Path

import yaml
from mcp.server.fastmcp import FastMCP

mcp = FastMCP(
    "mcp-task-manager-probe",
    streamable_http_path="/mcp",
    host="0.0.0.0",
    port=int(os.environ.get("DATABRICKS_APP_PORT", "8000")),
)

TASKS_BASE = "/Users/lithomas@wustl.edu/business-task-manager-v1/tasks"


def _extract(config: dict, fallback_id: str) -> dict:
    """Pull the fields we expose from a parsed config.yaml."""
    schedule = config.get("schedule") or {}
    trigger = config.get("trigger") or {}
    return {
        "task_id": config.get("task_id", fallback_id),
        "name": config.get("name"),
        "enabled": config.get("enabled"),
        "schedule_frequency": schedule.get("frequency"),
        "trigger_type": trigger.get("type"),
    }


def _read_via_sdk() -> list:
    """Read config files from the workspace via the Databricks SDK."""
    from databricks.sdk import WorkspaceClient

    w = WorkspaceClient()
    tasks = []
    for obj in w.workspace.list(TASKS_BASE):
        if str(obj.object_type).endswith("DIRECTORY"):
            task_name = os.path.basename(obj.path)
            resp = w.workspace.export(f"{obj.path}/config.yaml")
            content = base64.b64decode(resp.content).decode("utf-8")
            tasks.append(_extract(yaml.safe_load(content), task_name))
    return tasks


def _read_bundled() -> list:
    """Read bundled config files shipped with the app."""
    tasks_dir = Path(__file__).parent / "tasks"
    tasks = []
    for config_file in sorted(tasks_dir.glob("*/config.yaml")):
        config = yaml.safe_load(config_file.read_text(encoding="utf-8"))
        tasks.append(_extract(config, config_file.parent.name))
    return tasks


@mcp.tool()
def list_tasks() -> str:
    """List all Business Task Manager tasks.

    Returns task IDs, names, enabled states, schedule frequencies, and
    trigger types for every task in the project.
    """
    # Try reading the actual workspace files via SDK first.
    try:
        tasks = _read_via_sdk()
        if tasks:
            return json.dumps(
                {"source": "workspace_sdk", "tasks": tasks},
                ensure_ascii=False, indent=2,
            )
    except Exception:
        pass

    # Fall back to bundled copies.
    tasks = _read_bundled()
    return json.dumps(
        {"source": "bundled_fallback", "tasks": tasks},
        ensure_ascii=False, indent=2,
    )


if __name__ == "__main__":
    mcp.run(transport="streamable-http")
