"""Adapt a guest's API to the existing structured research runtime and evidence validators."""

import json
from urllib.parse import urlsplit

from fastapi import HTTPException
from jsonschema import ValidationError, validate

from app.db.session import get_session_factory
from app.models.public_demo import PublicDemoSession
from app.services.agent_runtime import AgentRuntimeError, StructuredAgentRuntime
from app.services.community_auth import CURRENT_REQUEST
from app.services.model_http import ProviderError, SafeModelHttpClient, normalize_base_url
from app.services.public_demo_sessions import now, provider_key, release, reserve


class UserAPIRuntime(StructuredAgentRuntime):
    runtime_name = "user_api"

    def __init__(self, *, sid, connection, api_key, timeout_seconds=45, client=None):
        super().__init__(model=connection["model"], timeout_seconds=timeout_seconds)
        self.sid, self.connection, self.api_key = sid, connection, api_key
        self.client = client or SafeModelHttpClient(timeout=timeout_seconds)

    def _invoke_json(self, *, instructions, prompt, schema):
        with get_session_factory()() as db:
            row = db.get(PublicDemoSession, self.sid)
            if row is None or row.expires_at <= now() or row.connection != self.connection:
                raise AgentRuntimeError("体验会话已变化，请重新连接 API")
            provider_key(row)
            lease = reserve(db, self.sid)
        try:
            result = self.invoke(instructions=instructions, prompt=prompt, schema=schema)
            with get_session_factory()() as db:
                row = db.get(PublicDemoSession, self.sid)
                if row is None or row.expires_at <= now() or row.connection != self.connection:
                    raise AgentRuntimeError("体验会话已失效，本轮结果不可采用")
                provider_key(row)
            return result
        except (ProviderError, HTTPException) as exc:
            raise AgentRuntimeError(str(exc) if isinstance(exc, ProviderError) else str(exc.detail)) from None
        finally:
            with get_session_factory()() as db:
                release(db, self.sid, lease)

    def invoke(self, *, instructions, prompt, schema, max_tokens=6000):
        base = normalize_base_url(self.connection["base_url"])
        protocol = self.connection["protocol"]
        if protocol == "responses":
            endpoint = base + "/responses"
            payload = {
                "model": self.model,
                "instructions": instructions,
                "input": prompt,
                "store": False,
                "max_output_tokens": max_tokens,
                "text": {
                    "format": {
                        "type": "json_schema",
                        "name": "research_result",
                        "strict": True,
                        "schema": schema,
                    }
                },
            }
        else:
            endpoint = base + "/chat/completions"
            payload = {
                "model": self.model,
                "messages": [
                    {
                        "role": "system",
                        "content": instructions + "\nReturn JSON matching: " + json.dumps(schema),
                    },
                    {"role": "user", "content": prompt},
                ],
                "response_format": {"type": "json_object"},
                "max_tokens": max_tokens,
                "stream": False,
            }
            if urlsplit(base).hostname == "api.deepseek.com":
                payload["thinking"] = {"type": "disabled"}
        raw = self.client.post_json(endpoint, api_key=self.api_key, payload=payload)
        try:
            if protocol == "responses":
                if raw.get("status") in {"failed", "incomplete", "cancelled"}:
                    raise ValueError("incomplete")
                content = "".join(
                    part["text"]
                    for message in raw["output"]
                    if message.get("type") == "message"
                    for part in message.get("content", [])
                    if part.get("type") == "output_text"
                )
            else:
                choice = raw["choices"][0]
                if choice.get("finish_reason") not in {None, "stop"}:
                    raise ValueError("incomplete")
                content = choice["message"]["content"]
            result = json.loads(content)
            validate(result, schema)
            return result
        except (ValueError, KeyError, IndexError, TypeError, ValidationError):
            raise ProviderError("output_contract", "模型输出未通过结构校验，未采用为研究成果") from None


def current_runtime(settings):
    if not settings.public_demo_enabled:
        raise AgentRuntimeError("用户 API 运行时需要独立公开 Demo 入口")
    request = CURRENT_REQUEST.get()
    sid = getattr(request.state, "public_demo_session_id", None) if request else None
    if not sid:
        raise AgentRuntimeError("体验会话缺失，请刷新页面")
    with get_session_factory()() as db:
        row = db.get(PublicDemoSession, sid)
        try:
            key = provider_key(row)
        except HTTPException as exc:
            raise AgentRuntimeError(str(exc.detail)) from None
        return UserAPIRuntime(
            sid=sid,
            connection=dict(row.connection),
            api_key=key,
            timeout_seconds=settings.agent_timeout_seconds,
        )
