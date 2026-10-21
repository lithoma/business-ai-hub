"""One bounded inference request, using existing Databricks App Authorization."""
from __future__ import annotations

import os
import re
import json
from typing import Any
from urllib.parse import quote

import requests

from task_drafts import SYSTEM_PROMPT, proposal_schema
from telemetry import span, usage_fields


class LLMNotConfigured(RuntimeError):
    pass


class LLMAccessError(RuntimeError):
    """Sanitized provider evidence; never infer a missing privilege from a generic 403."""
    def __init__(self, status, endpoint, payload):
        self.status = status
        self.endpoint = endpoint
        canonical = {'USE CATALOG', 'USE SCHEMA', 'EXECUTE', 'CAN QUERY', 'READ METADATA'}
        payload = payload if isinstance(payload, dict) else {}
        nested = payload.get('error') if isinstance(payload.get('error'), dict) else {}
        code = payload.get('error_code') or nested.get('code') or ''
        self.error_code = code if isinstance(code, str) and re.fullmatch(r'[A-Z0-9_.-]{1,100}', code) else ''
        message = payload.get('message') or nested.get('message') or ''
        missing = []
        for value in payload.get('missing_privileges', []) if isinstance(payload.get('missing_privileges'), list) else []:
            if isinstance(value, str) and value.upper().replace('_', ' ') in canonical:
                missing.append(value.upper().replace('_', ' '))
        if isinstance(message, str):
            pattern = r"(?:does not have|missing|lacks?|requires?|needs?|must have)\s+(?:the\s+)?(?:(?:privileges?|permissions?)\s+)?['\"]?(USE[ _]CATALOG|USE[ _]SCHEMA|EXECUTE|CAN[ _]QUERY|READ[ _]METADATA)\b"
            missing.extend(value.upper().replace('_', ' ') for value in re.findall(pattern, message[:10000], re.I))
        self.missing_privileges = sorted(set(missing))
        reason = ('缺失权限：' + '、'.join(self.missing_privileges)) if self.missing_privileges else '服务未明确报告缺失的具体权限；不能据此推断是 EXECUTE 或目录权限'
        code_text = ' ' + self.error_code if self.error_code else ''
        super().__init__(f'模型访问失败（HTTP {status}{code_text}，{endpoint}）。{reason}。未重试、未保存草稿。')


def generate_configuration(workspace: Any, requirements: str, *, conversation=None, source_agent=None) -> dict[str, Any]:
    return structured_completion(workspace, requirements, conversation=conversation, source_agent=source_agent,
                                 prompt=SYSTEM_PROMPT, schema=proposal_schema(), name='business_task_proposal')


def structured_completion(workspace: Any, requirements: str, *, prompt: str, schema: dict,
                          name: str, conversation=None, source_agent=None, endpoint=None) -> dict[str, Any]:
    """Provider boundary shared by task parsing and routing: exactly one request."""
    endpoint = (os.environ.get('TASK_PARSER_ENDPOINT', '') if endpoint is None else endpoint).strip()
    if not endpoint:
        raise LLMNotConfigured('Model not configured：尚未配置任务生成模型，请设置 TASK_PARSER_ENDPOINT；仍可手动创建草稿。')
    is_service = bool(re.fullmatch(r'[A-Za-z0-9_-]{1,128}(?:\.[A-Za-z0-9_-]{1,128}){2}', endpoint))
    if not is_service and not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', endpoint):
        raise LLMNotConfigured('任务生成模型 endpoint 名称配置无效。')
    config = workspace().config
    user_input = requirements
    if conversation or source_agent:
        user_input = json.dumps({'current_request': requirements, 'source_agent': source_agent,
                                 'quoted_conversation': conversation or []}, ensure_ascii=False)
    body = {
        'messages': [
            {'role': 'system', 'content': prompt},
            {'role': 'user', 'content': user_input},
        ],
        'response_format': {'type': 'json_schema', 'json_schema': {
            'name': name, 'strict': True, 'schema': schema,
        }},
        'max_tokens': 3000,
        'stream': False,
    }
    if is_service:
        path = '/ai-gateway/mlflow/v1/chat/completions'
        body['model'] = endpoint
    else:
        path = f"/serving-endpoints/{quote(endpoint, safe='')}/invocations"
    # requests does not retry by default. Do not use SDK inference retries or repair calls.
    with span('structured_completion',kind='inference',provider='databricks',model=endpoint,purpose=name,
              history_turns=len(conversation or []),history_characters=sum(len(turn.get('content','')) for turn in (conversation or [])),
              max_output_tokens=3000,**usage_fields(None),actual_cost=None) as event:
        with span('sdk_authenticate',kind='authentication',provider='databricks'):
            headers={**config.authenticate(), 'Content-Type': 'application/json'}
        response = requests.post(
            f"{config.host.rstrip('/')}{path}",headers=headers,json=body,
            timeout=(5, 60),allow_redirects=False,
        )
        event['http_status']=response.status_code
        content=getattr(response,'content',None)
        if isinstance(content,(bytes,bytearray)): event['response_bytes']=len(content)
        prepared=getattr(getattr(response,'request',None),'body',None)
        if isinstance(prepared,(bytes,bytearray,str)):
            event['request_bytes']=len(prepared.encode('utf-8') if isinstance(prepared,str) else prepared)
        if response.status_code in (401, 403):
            try: evidence = response.json()
            except (ValueError, TypeError): evidence = {}
            raise LLMAccessError(response.status_code, endpoint, evidence)
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError('模型没有返回完整响应。')
        result=response.json()
        event.update(usage_fields(result.get('usage')))
        return result
