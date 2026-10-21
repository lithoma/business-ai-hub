"""Douyin Business Cockpit — LangGraph orchestration agent.

Coordinates two Genie Agents (A1 internal sales, A2 external competition)
as tools, with intelligent routing, business guardrails, and Chinese response.
"""

import os
from collections.abc import AsyncGenerator, Callable
from typing import Any

from databricks.sdk import WorkspaceClient
from databricks_langchain import ChatDatabricks
from langchain.agents import create_agent
from langchain.agents.middleware import HumanInTheLoopMiddleware
# System prompt is passed directly to create_agent via system_prompt parameter

from agent.mcps import build_mcp_servers
from agent.prompts import SYSTEM_PROMPT

# Importing the tools package auto-registers every tool module.
from agent.tools import all_tools
from databricks_agentkit import workspace_client, workspace_headers
from databricks_agentkit.langgraph import (
    checkpointer,
    configure_tracing,
    genie_tools,
    mcp_tools,
    memory_tools,
    start_trace,
    thread_config,
)
from databricks_agentkit.langgraph.session_store import (
    invocation_id_from_metadata,
    invocation_metadata,
)
from databricks_agentkit.runtime.auth import AuthError

# Unity Catalog AI Gateway model — Claude Sonnet 4.5 for Chinese reasoning.
MODEL = "databricks-qwen35-122b-a10b"

# Tools requiring human approval (none for this orchestration agent).
REQUIRE_APPROVAL: dict[str, Any] = {}


class _RoutedChatDatabricks(ChatDatabricks):
    """Forward account-host workspace routing to the underlying OpenAI clients."""

    def _get_client_kwargs(self) -> dict[str, Any]:
        kwargs = super()._get_client_kwargs()
        if headers := workspace_headers():
            kwargs["default_headers"] = headers
        return kwargs


def configure() -> None:
    """Wire up global state; call once at server startup (not at import)."""
    _check_databricks_auth()
    configure_tracing()


def _check_databricks_auth() -> None:
    """Fail fast at startup if Databricks auth isn't configured."""
    try:
        workspace_client()
    except Exception as e:
        profile = os.getenv("DATABRICKS_CONFIG_PROFILE")
        target = (
            f"profile {profile!r}" if profile else "the DEFAULT profile / DATABRICKS_HOST+TOKEN"
        )
        raise RuntimeError(
            f"Databricks auth is not configured. Tried {target}.\n"
            "Fix: set DATABRICKS_CONFIG_PROFILE in .env, or run "
            f"`databricks auth login --profile <name>`.\n"
            f"(underlying error: {e})"
        ) from e


async def create_agent_graph(
    actor: str,
    model: str | None = None,
    *,
    workspace_client_for: Callable[[str], WorkspaceClient] | None = None,
):
    """Build the LangGraph agent with Genie tools, memory, and routing prompt.

    The genie_tools() function reads agent.toml and creates LangChain tools
    for each declared Genie Agent (a1_sales_operations, a2_competition_market).
    The system prompt tells the LLM which tool to use for each question type.
    """
    mcp = await mcp_tools(build_mcp_servers(), workspace_client_for=workspace_client_for)
    tools = [
        *all_tools(),
        *memory_tools(actor),
        *genie_tools(workspace_client_for=workspace_client_for),
        *mcp,
    ]
    middleware = (
        [HumanInTheLoopMiddleware(interrupt_on=REQUIRE_APPROVAL)] if REQUIRE_APPROVAL else []
    )
    endpoint = model or MODEL
    return create_agent(
        model=_RoutedChatDatabricks(
            endpoint=endpoint, workspace_client=workspace_client(), use_ai_gateway=False
        ),
        tools=tools,
        system_prompt=SYSTEM_PROMPT,
        middleware=middleware,
        checkpointer=checkpointer(),
    )


async def recovery_input(
    agent_input: Any,
    *,
    session_id: str,
    actor: str,
    invocation_id: str,
) -> Any:
    """Return the input for run_agent after worker loss."""
    checkpoint = await checkpointer().aget_tuple(thread_config(session_id, actor))
    current_invocation_checkpointed = bool(
        checkpoint and invocation_id_from_metadata(checkpoint.metadata) == invocation_id
    )
    return None if current_invocation_checkpointed else agent_input


async def run_agent(
    agent_input: Any,
    *,
    session_id: str,
    actor: str | None = None,
    model: str | None = None,
    invocation_id: str | None = None,
    workspace_client_for: Callable[[str], WorkspaceClient] | None = None,
    graph: Any = None,
) -> AsyncGenerator[Any, None]:
    """Run the agent and yield native LangGraph stream events."""
    actor = actor or session_id
    if graph is None:
        graph = await create_agent_graph(actor, model, workspace_client_for=workspace_client_for)
    config = thread_config(session_id, actor)
    if invocation_id:
        config["metadata"] = invocation_metadata(invocation_id)

    last_update: Any = None
    with start_trace(name="invoke", inputs=agent_input, session_id=session_id) as span:
        async for event in graph.astream(
            input=agent_input,
            config=config,
            stream_mode=["updates", "messages"],
            durability="sync",
        ):
            if event[0] == "updates":
                last_update = event[1]
                if workspace_client_for is not None and last_update.get("__interrupt__"):
                    raise AuthError(
                        "MCP_USER_AUTH_HITL_UNSUPPORTED",
                        "Request-user invocations do not support paused approvals.",
                        400,
                    )
            yield event
        if span is not None and last_update is not None:
            span.set_outputs(last_update)
