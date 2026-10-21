"""Two stable Genie SDK calls, plus one optional Supervisor App HTTP adapter.

The Supervisor URL and route are intentionally NOT guessed. A1/A2 IDs are known.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from datetime import timedelta
from typing import Any

import requests
from databricks.sdk import WorkspaceClient


class AgentClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self._workspace: WorkspaceClient | None = None

    @property
    def workspace(self) -> WorkspaceClient:
        if self._workspace is None:
            self._workspace = WorkspaceClient()
        return self._workspace

    def ask(self, agent_name: str, instruction: str) -> str:
        if agent_name in ("genie_a1", "genie_a2"):
            space_id = self.config.get(agent_name)
            if not space_id:
                raise ValueError(f"Missing Genie space ID: {agent_name}")
            msg = self.workspace.genie.start_conversation_and_wait(
                space_id=str(space_id),
                content=instruction,
                timeout=timedelta(minutes=8),
            )
            status = str(getattr(msg, "status", "")).upper()

            if "COMPLETED" not in status:
                raise RuntimeError(
                    f"Genie {agent_name} did not complete successfully: {status}"
                )

            parts = []

            for attachment in getattr(msg, "attachments", None) or []:
                text_attachment = getattr(attachment, "text", None)

                if text_attachment is not None:
                    text = getattr(text_attachment, "content", None)

                    if text and str(text).strip():
                        parts.append(str(text).strip())

            answer = "\n\n".join(parts)

            if not answer:
                raise RuntimeError(
                    f"Genie {agent_name} completed but returned no text answer."
                )

            return answer
        
        
        if agent_name == "supervisor":
            return self._ask_supervisor(instruction)
        raise ValueError(f"Unknown agent: {agent_name!r}")

    def _ask_supervisor(self, instruction: str) -> str:
        conf = self.config.get("supervisor", {})
        url = str(conf.get("app_url") or "").rstrip("/")
        path = str(conf.get("endpoint_path") or "")
        token_env = str(conf.get("oauth_token_env") or "DATABRICKS_APP_OAUTH_TOKEN")
        token = os.getenv(token_env)
        if not url or path not in ("/responses", "/api/invocations"):
            raise RuntimeError("Supervisor app_url/endpoint_path not configured; Genie A1/A2 remain available")
        if not token:
            raise RuntimeError(f"Supervisor needs a Databricks Apps OAuth token in {token_env}")
        msg_input = [{"role": "user", "content": instruction}]
        if path == "/responses":
            payload = {"input": msg_input, "stream": False}
        else:
            payload = {
                "id": str(uuid.uuid4()),
                "session_id": str(uuid.uuid4()),
                "input": msg_input,
                "stream": False,
            }
        r = requests.post(
            url + path,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            json=payload,
            timeout=480,
        )
        r.raise_for_status()
        result = r.json()
        text = _response_text(result)
        if not text:
            raise RuntimeError(f"Supervisor returned no recognizable text. Keys: {list(result) if isinstance(result, dict) else type(result)}")
        return text


def _response_text(value: Any) -> str:
    """Read text from common Responses API and Agent Runtime response structures."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts = [_response_text(v) for v in value]
        return "\n".join(x for x in parts if x).strip()
    if not isinstance(value, dict):
        return ""
    for key in ("output_text", "answer", "text"):
        if isinstance(value.get(key), str) and value[key].strip():
            return value[key].strip()
    if isinstance(value.get("content"), str):
        return value["content"].strip()
    for key in ("output", "content", "messages", "response", "result"):
        if key in value:
            answer = _response_text(value[key])
            if answer:
                return answer
    return ""


def parse_boolean_decision(reply: str) -> tuple[bool, str]:
    """Accept ONLY a single valid JSON object with a JSON boolean; no semantic guessing."""
    text = reply.strip()
    match = re.fullmatch(r"```(?:json)?\s*(\{.*\})\s*```", text, flags=re.S | re.I)
    if match:
        text = match.group(1)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("Agent trigger must return JSON: {\"trigger\":true/false,\"reason\":\"...\"}") from exc
    if not isinstance(payload, dict) or type(payload.get("trigger")) is not bool:
        raise ValueError("Agent trigger response must include a genuine JSON boolean in 'trigger'")
    reason = payload.get("reason", "")
    return payload["trigger"], (reason if isinstance(reason, str) else "")
