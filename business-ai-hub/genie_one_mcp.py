"""Identity-scoped Genie One MCP transport; no implicit inference or identity fallback.

The caller supplies an authenticated WorkspaceClient and a trusted identity key.
Do not construct these values from browser-supplied owner/email fields. App OBO
middleware and Hub task/report authorization are separate integration work.
"""
from __future__ import annotations

from dataclasses import dataclass
import asyncio
from datetime import timedelta
import json
import time
from typing import Any, Literal
from urllib.parse import urlsplit
from telemetry import span, usage_fields


SERVICE_PATH = '/ai-gateway/mcp-services/system.ai.genie_one_mcp'
TOOLS = frozenset(('genie_ask', 'genie_poll_response', 'genie_get_query_result', 'genie_cancel_response'))


class GenieOneError(RuntimeError):
    pass


@dataclass(frozen=True)
class Caller:
    """Trusted server-side context, not a claim received from an API request."""
    subject: str
    mode: Literal['local_user', 'obo_user', 'service_principal']

    def __post_init__(self):
        if not self.subject.strip() or self.mode not in ('local_user','obo_user','service_principal'):
            raise ValueError('An explicit authenticated caller is required')


def plain(value):
    return value.model_dump(by_alias=True, mode='json') if hasattr(value, 'model_dump') else value


class SDKMCPTransport:
    """Small MCP Python client authenticated with the existing Databricks SDK.

    Use from a synchronous route/worker, not an active asyncio event loop. Each
    operation gets a fresh authenticated session; no tokens or user data are
    shared with other adapter instances. No outer model is involved.
    """
    def __init__(self, workspace, server_url):
        self.workspace, self.server_url = workspace, server_url

    async def _run(self, tool=None, arguments=None):
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        with span('sdk_authenticate',kind='authentication',provider='databricks'):
            headers=self.workspace.config.authenticate()
        async with streamablehttp_client(self.server_url,headers=headers,
                timeout=timedelta(seconds=30),sse_read_timeout=timedelta(seconds=60)) as (read,write,_):
            async with ClientSession(read,write) as session:
                with span('mcp_initialize',kind='mcp_setup',provider='genie_one'):
                    await session.initialize()
                if tool is not None:
                    return await session.call_tool(tool,arguments)
                tools, cursor = [], None
                for _ in range(10):
                    page=await session.list_tools(cursor=cursor)
                    tools.extend(page.tools)
                    cursor=page.nextCursor
                    if not cursor: return tools
                raise GenieOneError('MCP tool listing exceeded the pagination limit')

    def list_tools(self):
        return asyncio.run(self._run())

    def call_tool(self, tool, arguments):
        return asyncio.run(self._run(tool,arguments))


class GenieOneMCP:
    def __init__(self, workspace, caller: Caller, *, server_url: str = '', transport=None):
        host = urlsplit(workspace.config.host)
        if host.scheme != 'https' or not host.hostname or host.username or host.password or host.query or host.fragment:
            raise ValueError('A valid HTTPS Databricks Workspace host is required')
        self.server_url = server_url or f'https://{host.netloc}{SERVICE_PATH}'
        target = urlsplit(self.server_url)
        if (target.scheme != 'https' or target.netloc != host.netloc or target.path != SERVICE_PATH
                or target.username or target.password or target.query or target.fragment):
            raise ValueError('Genie One MCP must use this Workspace and the current Unity Gateway service URL')
        self.caller = caller
        self._workspace = workspace
        self._transport = transport
        self._tools: dict[str, dict] | None = None

    @property
    def transport(self):
        if self._transport is None:
            self._transport = SDKMCPTransport(self._workspace,self.server_url)
        return self._transport

    def discover(self) -> dict[str, dict]:
        """Only list tool contracts. This never asks a question or starts a query."""
        try:
            with span('tools_list',kind='mcp_discovery',provider='genie_one'):
                tools = self.transport.list_tools()
        except Exception as exc:
            raise GenieOneError('Genie One MCP discovery failed; check SDK login, MCP grants and network access.') from exc
        discovered = {}
        for item in tools:
            item = plain(item)
            if not isinstance(item, dict):
                raise GenieOneError('MCP returned an unsupported tool definition')
            if item.get('name') in TOOLS:
                schema = item.get('inputSchema')
                if not isinstance(schema,dict):
                    raise GenieOneError('MCP tool is missing its input schema')
                discovered[item['name']] = {key:item[key] for key in ('name','inputSchema','outputSchema') if key in item}
        if not {'genie_ask','genie_poll_response'}.issubset(discovered):
            raise GenieOneError('Required Genie One tools are unavailable; no legacy endpoint or Agent fallback was attempted.')
        self._tools = discovered
        return dict(discovered)

    def ask(self, question: str, *, caller: Caller, conversation_id: str | None = None):
        arguments = {'question':question}
        if conversation_id is not None: arguments['conversation_id'] = conversation_id
        return self.invoke('genie_ask',arguments,caller=caller)

    def poll(self, conversation_id: str, response_id: str, *, caller: Caller):
        return self.invoke('genie_poll_response',{'conversation_id':conversation_id,'response_id':response_id},caller=caller)

    def query_result(self, conversation_id: str, response_id: str, item_id: str, *, caller: Caller):
        return self.invoke('genie_get_query_result',{'conversation_id':conversation_id,'response_id':response_id,'item_id':item_id},caller=caller)

    def cancel(self, conversation_id: str, *, caller: Caller):
        # Actual tool schema is conversation-scoped and only requests cancellation.
        return self.invoke('genie_cancel_response',{'conversation_id':conversation_id},caller=caller)

    def invoke(self, tool: str, arguments: dict[str, Any], *, caller: Caller) -> dict:
        """Call exactly one allowlisted tool using the discovered schema.

        The application must pass verified caller context again for each request.
        Instances and their conversations must never be shared across principals.
        Output fields deliberately remain provider-defined until live schema and
        response acceptance; plain text is not mistaken for a completed SQL result.
        """
        if caller != self.caller:
            raise PermissionError('MCP client belongs to a different authenticated caller')
        if tool not in TOOLS:
            raise ValueError('Tool is outside the Genie One analytics allowlist')
        if not isinstance(arguments,dict):
            raise ValueError('MCP arguments must be a JSON object')
        tools = self._tools if self._tools is not None else self.discover()
        if tool not in tools:
            raise GenieOneError('Requested tool is not available on this MCP service')
        from jsonschema import Draft202012Validator
        validator = Draft202012Validator(tools[tool]['inputSchema'])
        if next(validator.iter_errors(arguments), None):
            # Validation messages may echo arguments; never forward them or credentials.
            raise ValueError('Arguments do not match the discovered MCP tool schema')
        try:
            with span(tool,kind='mcp_tool',provider='genie_one') as event:
                result = plain(self.transport.call_tool(tool, arguments))
                if isinstance(result,dict):
                    event['tool_error']=bool(result.get('isError'))
                    structured=result.get('structuredContent') or {}
                    if isinstance(structured,dict):
                        for key in ('conversation_id','response_id','status'):
                            value=structured.get(key)
                            if isinstance(value,str): event[key]=value
        except Exception as exc:
            raise GenieOneError('Genie One MCP call failed; no application retry or identity fallback was attempted.') from exc
        if not isinstance(result,dict):
            raise GenieOneError('MCP returned an unsupported response envelope')
        if result.get('isError'):
            raise GenieOneError('Genie One MCP returned a tool error; response was not treated as an answer.')
        if len(json.dumps(result,ensure_ascii=False)) > 2_000_000:
            raise GenieOneError('MCP response exceeds the local result limit; request a smaller result')
        structured = result.get('structuredContent')
        schema = tools[tool].get('outputSchema')
        if schema and (not isinstance(structured,dict) or next(Draft202012Validator(schema).iter_errors(structured),None)):
            raise GenieOneError('MCP structured output does not match its advertised schema')
        return {'provider':'genie_one_mcp','tool':tool,
                'structured_content':structured, 'content':result.get('content',[])}


def wait_for_answer(adapter, question, *, conversation_id=None, on_started=lambda value: None,
                    timeout=480, poll_interval=3, require_text=True):
    with span('genie_one_answer',kind='agent_cycle',provider='genie_one',**usage_fields(None),actual_cost=None) as event:
        result=_wait_for_answer(adapter,question,conversation_id=conversation_id,on_started=on_started,
                                timeout=timeout,poll_interval=poll_interval,require_text=require_text)
        event['response_id']=result['response_id']
        return result


def _wait_for_answer(adapter, question, *, conversation_id=None, on_started=lambda value: None,
                     timeout=480,poll_interval=3,require_text=True):
    """One ask followed by serial polls, never a re-ask on timeout/failure.

    Schema discovery alone is not live answer acceptance. Keep chat disabled until
    Workspace/identity and native action boundaries have been verified.
    """
    result = adapter.ask(question,caller=adapter.caller,conversation_id=conversation_id)
    started = result.get('structured_content') or {}
    conversation_id, response_id = started.get('conversation_id'), started.get('response_id')
    if not conversation_id or not response_id:
        raise GenieOneError('Genie One did not return conversation and response IDs')
    on_started(conversation_id)
    deadline = time.monotonic()+timeout
    while time.monotonic() < deadline:
        result = adapter.poll(conversation_id,response_id,caller=adapter.caller)
        state = result.get('structured_content') or {}
        if state.get('status') == 'completed':
            answer = state.get('final_answer')
            if require_text and (not isinstance(answer,str) or not answer.strip()):
                raise GenieOneError('Genie One completed without a text answer')
            return {'provider':'genie_one_mcp','answer':answer or '','conversation_id':conversation_id,
                    'response_id':response_id,'status':'completed','deep_link':state.get('deep_link'),
                    'query_items':state.get('query_items',[])}
        if state.get('status') != 'in_progress':
            raise GenieOneError('Genie One did not complete; no retry was submitted')
        time.sleep(poll_interval)
    raise GenieOneError('Genie One timed out; the remote response may still be running. No retry was submitted.')


def adapter_for_user(workspace=None):
    """Construct per-request adapter with verified Hub identity and that user's SDK."""
    import os
    if os.getenv('GENIE_ONE_ENABLED') != 'true':
        raise GenieOneError('Genie One 尚未启用；请先配置目标 Workspace 登录和权限。')
    from hub_auth import current_identity
    user = current_identity()
    client = workspace or user.client
    # Do not permit an injected storage or unrelated Workspace client.
    if client is not user.client:
        raise PermissionError('Business adapter must use the authenticated user client')
    caller = Caller(user.key, 'service_principal' if user.mode=='databricks_app' else 'local_user' if user.mode == 'local_cli' else 'obo_user')
    return GenieOneMCP(client, caller)
