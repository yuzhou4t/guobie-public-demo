from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError


class AgentRuntimeError(RuntimeError):
    pass


class AgentRequest(BaseModel):
    country_iso3: str = Field(min_length=3, max_length=3)
    country_name: str
    question: str = Field(min_length=1, max_length=500)
    output_type: str = "country_analysis"
    scope_type: str = "country"
    scope_key: str | None = None
    scope_context: dict[str, Any] = Field(default_factory=dict)
    conversation_history: list[dict[str, str]] = Field(default_factory=list, max_length=12)
    answer_mode: str = Field(default="formal_artifact", pattern=r"^(research_chat|formal_artifact)$")


class CapabilitySpec(BaseModel):
    name: str
    description: str


class ExecutionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    workflow: str
    selected_capabilities: list[str]
    scope_summary: str
    public_steps: list[str] = Field(default_factory=list, max_length=8)


class EvidenceItem(BaseModel):
    evidence_id: str
    kind: str
    title: str
    source_name: str
    source_url: str | None
    published_at: str | None
    observed_at: str | None
    published_at_precision: str = "unknown"
    evidence_locator: dict[str, Any] = Field(default_factory=dict)
    review_status: str = "confirmed"
    origin: str = "database"
    verification_status: str = "reviewed"
    retrieved_at: str | None = None
    payload: dict[str, Any]


class FactItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact_id: str
    text: str
    evidence_ids: list[str]
    calculation_refs: list[str] = Field(default_factory=list)
    review_status: str = "confirmed"
    origin: str = "database"
    verification_status: str = "reviewed"


class ToolResult(BaseModel):
    capability: str
    status: str
    summary: str
    evidence: list[EvidenceItem]
    facts: list[FactItem] = Field(default_factory=list)
    calculations: list[dict[str, Any]]
    limitations: list[str]


class AnswerClaim(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str
    fact_ids: list[str]
    evidence_ids: list[str]
    origin: str = "database"
    verification_status: str = "reviewed"


class AnswerSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    claims: list[AnswerClaim]
    origin: str = "database"


class AnswerDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sections: list[AnswerSection]
    limitations: list[str]
    related_questions: list[str]


class AnswerArtifact(BaseModel):
    status: str
    runtime: str
    workflow: str
    sections: list[AnswerSection]
    citations: list[EvidenceItem]
    calculations: list[dict[str, Any]]
    tool_trace: list[dict[str, Any]]
    limitations: list[str]
    related_questions: list[str]
    source_mode: str = "database"
    online_status: dict[str, Any] = Field(default_factory=dict)
    proposed_actions: list[dict[str, Any]] = Field(default_factory=list)


class PolicyTranslationSegment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source_id: str
    locator: str
    original_excerpt: str
    translated_excerpt: str
    key_terms: list[dict[str, str]] = Field(default_factory=list)
    ambiguities: list[str] = Field(default_factory=list)


class PolicyTranslationDraft(BaseModel):
    model_config = ConfigDict(extra="forbid")

    segments: list[PolicyTranslationSegment]
    institutions: list[str] = Field(default_factory=list)
    key_provisions: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class StructuredAgentRuntime(ABC):
    runtime_name: str

    def __init__(self, *, model: str, timeout_seconds: float) -> None:
        self.model = model
        self.timeout_seconds = timeout_seconds

    @abstractmethod
    def _invoke_json(self, *, instructions: str, prompt: str, schema: dict[str, Any]) -> Any:
        raise NotImplementedError

    def create_plan(
        self,
        request: AgentRequest,
        capabilities: list[CapabilitySpec],
    ) -> ExecutionPlan:
        prompt = json.dumps(
            {
                "request": request.model_dump(),
                "available_capabilities": [item.model_dump() for item in capabilities],
                "rules": {
                    "select_only_from_available_capabilities": True,
                    "maximum_capabilities": 6,
                    "do_not_answer_the_question": True,
                    "public_steps_must_be_short_action_descriptions": True,
                },
            },
            ensure_ascii=False,
        )
        payload = self._invoke_json(
            instructions=(
                "你是国别研究工作流规划器。只选择完成问题所需的最少白名单能力，"
                "不调用工具、不补充事实、不输出隐藏推理。"
            ),
            prompt=prompt,
            schema=ExecutionPlan.model_json_schema(),
        )
        try:
            return ExecutionPlan.model_validate(payload)
        except ValidationError as exc:
            raise AgentRuntimeError("agent returned an invalid execution plan") from exc

    def synthesize(
        self,
        request: AgentRequest,
        plan: ExecutionPlan,
        results: list[ToolResult],
    ) -> AnswerDraft:
        prompt = json.dumps(
            {
                "request": request.model_dump(),
                "plan": plan.model_dump(),
                "tool_results": [item.model_dump(mode="json") for item in results],
                "rules": {
                    "answer_mode": request.answer_mode,
                    "claims_must_copy_fact_text_exactly": request.answer_mode == "formal_artifact",
                    "research_chat_may_paraphrase_or_combine_cited_facts": (
                        request.answer_mode == "research_chat"
                    ),
                    "claims_must_reference_fact_ids_and_their_exact_evidence_ids": True,
                    "claims_must_copy_fact_origin_and_verification_status": True,
                    "do_not_invent_facts_or_sources": True,
                    "preserve_source_disagreements": True,
                    "state_data_gaps_explicitly": True,
                    "answer_in_simplified_chinese": True,
                },
            },
            ensure_ascii=False,
        )
        if len(prompt.encode("utf-8")) > 160_000:
            raise AgentRuntimeError("controlled evidence bundle exceeds the agent input limit")
        payload = self._invoke_json(
            instructions=(
                "你是国别智枢研判助手。所有事实性表达都必须来自工具结果中的 FactItem，并列出支撑它的 "
                "fact_ids 与这些事实的全部 evidence_ids。research_chat 可以综合或改写多个已引用事实，"
                "但不得增加新数字、实体、时间或因果；formal_artifact 必须逐字复制单一 FactItem.text。"
                "如果证据不足，明确说明缺口。不要判断冲突来源谁真谁假，也不要展示隐藏推理。"
            ),
            prompt=prompt,
            schema=AnswerDraft.model_json_schema(),
        )
        try:
            return AnswerDraft.model_validate(payload)
        except ValidationError as exc:
            raise AgentRuntimeError("agent returned an invalid answer artifact") from exc

    def translate_policy_segments(
        self,
        *,
        source_language: str,
        target_language: str,
        sources: list[dict[str, Any]],
    ) -> PolicyTranslationDraft:
        prompt = json.dumps(
            {
                "source_language": source_language,
                "target_language": target_language,
                "sources": sources,
                "rules": {
                    "copy_original_excerpt_exactly": True,
                    "preserve_locator_and_source_id": True,
                    "do_not_add_entities_numbers_dates_or_causality": True,
                    "mark_ambiguity_instead_of_guessing": True,
                    "maximum_segments": 12,
                },
            },
            ensure_ascii=False,
        )
        if len(prompt.encode("utf-8")) > 160_000:
            raise AgentRuntimeError("policy source bundle exceeds the agent input limit")
        payload = self._invoke_json(
            instructions=(
                "你是区域国别政策译审助手。原文片段必须从输入逐字复制，不得补充未出现"
                "的实体、数字、时间或因果。保留来源标识和页码，不确定处列为歧义，不要展示"
                "隐藏推理。"
            ),
            prompt=prompt,
            schema=PolicyTranslationDraft.model_json_schema(),
        )
        try:
            return PolicyTranslationDraft.model_validate(payload)
        except ValidationError as exc:
            raise AgentRuntimeError("agent returned an invalid policy translation") from exc


class OpenAIResponsesRuntime(StructuredAgentRuntime):
    runtime_name = "openai_responses"
    _ENDPOINT = "https://api.openai.com/v1/responses"
    _MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        api_key: SecretStr,
        model: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not model.strip():
            raise AgentRuntimeError("GUOBIE_AGENT_MODEL is required for openai_responses")
        super().__init__(model=model.strip(), timeout_seconds=timeout_seconds)
        self._api_key = api_key
        self._transport = transport

    def _invoke_json(self, *, instructions: str, prompt: str, schema: dict[str, Any]) -> Any:
        strict_schema = _strict_json_schema(schema)
        request_body = {
            "model": self.model,
            "instructions": instructions,
            "input": prompt,
            "store": False,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "guobie_agent_payload",
                    "strict": True,
                    "schema": strict_schema,
                }
            },
        }
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(
                    self._ENDPOINT,
                    headers={
                        "Authorization": f"Bearer {self._api_key.get_secret_value()}",
                        "Content-Type": "application/json",
                    },
                    json=request_body,
                )
        except httpx.HTTPError as exc:
            raise AgentRuntimeError("OpenAI Responses API request failed") from exc
        if response.is_redirect:
            raise AgentRuntimeError("OpenAI Responses API redirect was rejected")
        if response.status_code != 200:
            raise AgentRuntimeError(f"OpenAI Responses API returned HTTP {response.status_code}")
        if len(response.content) > self._MAX_RESPONSE_BYTES:
            raise AgentRuntimeError("OpenAI Responses API response exceeded the size limit")
        try:
            payload = response.json()
            return _parse_json_text(_response_output_text(payload))
        except (ValueError, TypeError, json.JSONDecodeError) as exc:
            raise AgentRuntimeError("OpenAI Responses API returned invalid structured output") from exc


class CozeTestRuntime(StructuredAgentRuntime):
    runtime_name = "coze_test"
    _CHAT_ENDPOINT = "https://api.coze.com/v3/chat"
    _RETRIEVE_ENDPOINT = "https://api.coze.com/v3/chat/retrieve"
    _MESSAGES_ENDPOINT = "https://api.coze.com/v3/chat/message/list"
    _MAX_RESPONSE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        *,
        token: SecretStr,
        bot_id: str,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if not bot_id.strip():
            raise AgentRuntimeError("GUOBIE_AGENT_COZE_BOT_ID is required for coze_test")
        super().__init__(model="coze-bot", timeout_seconds=timeout_seconds)
        self._token = token
        self._bot_id = bot_id.strip()
        self._transport = transport

    def _invoke_json(self, *, instructions: str, prompt: str, schema: dict[str, Any]) -> Any:
        strict_schema = _strict_json_schema(schema)
        content = (
            f"{instructions}\n\n输入 JSON：\n{prompt}\n\n"
            f"输出必须是符合以下 JSON Schema 的单个 JSON 对象：\n"
            f"{json.dumps(strict_schema, ensure_ascii=False)}"
        )
        headers = {
            "Authorization": f"Bearer {self._token.get_secret_value()}",
            "Content-Type": "application/json",
        }
        deadline = time.monotonic() + self.timeout_seconds
        try:
            with httpx.Client(
                timeout=httpx.Timeout(self.timeout_seconds),
                follow_redirects=False,
                transport=self._transport,
            ) as client:
                response = client.post(
                    self._CHAT_ENDPOINT,
                    headers=headers,
                    json={
                        "bot_id": self._bot_id,
                        "user_id": "guobie-mvp-controlled-evidence",
                        "stream": False,
                        "auto_save_history": False,
                        "additional_messages": [{"role": "user", "content": content, "content_type": "text"}],
                    },
                )
                payload = self._coze_payload(response)
                data = payload.get("data") or {}
                conversation_id = data.get("conversation_id")
                chat_id = data.get("id")
                if not conversation_id or not chat_id:
                    raise AgentRuntimeError("Coze chat response did not include conversation identifiers")
                status = data.get("status")
                while status not in {"completed", "failed", "canceled"}:
                    if time.monotonic() >= deadline:
                        raise AgentRuntimeError("Coze chat timed out")
                    time.sleep(0.5)
                    payload = self._coze_payload(
                        client.get(
                            self._RETRIEVE_ENDPOINT,
                            headers=headers,
                            params={"conversation_id": conversation_id, "chat_id": chat_id},
                        )
                    )
                    status = (payload.get("data") or {}).get("status")
                if status != "completed":
                    raise AgentRuntimeError(f"Coze chat ended with status {status}")
                messages = self._coze_payload(
                    client.get(
                        self._MESSAGES_ENDPOINT,
                        headers=headers,
                        params={"conversation_id": conversation_id, "chat_id": chat_id},
                    )
                ).get("data", [])
        except httpx.HTTPError as exc:
            raise AgentRuntimeError("Coze API request failed") from exc
        for message in reversed(messages if isinstance(messages, list) else []):
            if message.get("role") == "assistant" and message.get("type") == "answer":
                try:
                    return _parse_json_text(message.get("content", ""))
                except json.JSONDecodeError as exc:
                    raise AgentRuntimeError("Coze returned invalid structured output") from exc
        raise AgentRuntimeError("Coze did not return an assistant answer")

    def _coze_payload(self, response: httpx.Response) -> dict[str, Any]:
        if response.is_redirect:
            raise AgentRuntimeError("Coze API redirect was rejected")
        if response.status_code != 200:
            raise AgentRuntimeError(f"Coze API returned HTTP {response.status_code}")
        if len(response.content) > self._MAX_RESPONSE_BYTES:
            raise AgentRuntimeError("Coze API response exceeded the size limit")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentRuntimeError("Coze API returned invalid JSON") from exc
        if not isinstance(payload, dict) or payload.get("code") not in {None, 0}:
            raise AgentRuntimeError("Coze API returned an application error")
        return payload


class CodexLocalRuntime(StructuredAgentRuntime):
    runtime_name = "codex_local"

    def __init__(
        self,
        *,
        binary: str,
        model: str,
        timeout_seconds: float,
    ) -> None:
        super().__init__(model=model.strip(), timeout_seconds=timeout_seconds)
        self.binary = binary

    def _invoke_json(self, *, instructions: str, prompt: str, schema: dict[str, Any]) -> Any:
        combined_prompt = (
            f"{instructions}\n\n输入 JSON：\n{prompt}\n\n不要使用任何工具，只返回符合 schema 的 JSON。"
        )
        with tempfile.TemporaryDirectory(prefix="guobie-codex-agent-") as temp_dir:
            root = Path(temp_dir)
            schema_path = root / "output-schema.json"
            output_path = root / "output.json"
            schema_path.write_text(
                json.dumps(_strict_json_schema(schema), ensure_ascii=False),
                encoding="utf-8",
            )
            command = [
                self.binary,
                "exec",
                "--ephemeral",
                "--ignore-user-config",
                "--ignore-rules",
                "--disable",
                "apps",
                "--disable",
                "browser_use",
                "--disable",
                "computer_use",
                "--disable",
                "goals",
                "--disable",
                "image_generation",
                "--disable",
                "memories",
                "--disable",
                "multi_agent",
                "--disable",
                "plugins",
                "--disable",
                "shell_tool",
                "--disable",
                "skill_search",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--cd",
                temp_dir,
                "--output-schema",
                str(schema_path),
                "--output-last-message",
                str(output_path),
                "-",
            ]
            if self.model:
                command[2:2] = ["--model", self.model]
            try:
                from app.services.skill_execution_control import run_process

                completed = run_process(
                    command,
                    input=combined_prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=_codex_environment(),
                )
            except FileNotFoundError as exc:
                raise AgentRuntimeError(
                    f"找不到 Codex 可执行文件 `{self.binary}`。"
                    "本机演示请安装 Codex CLI，或设置 GUOBIE_AGENT_CODEX_BINARY。"
                ) from exc
            except subprocess.TimeoutExpired as exc:
                raise AgentRuntimeError("local Codex runtime timed out") from exc
            except OSError as exc:
                raise AgentRuntimeError("local Codex runtime failed to start") from exc
            if completed.returncode != 0:
                raise AgentRuntimeError(f"local Codex runtime exited with status {completed.returncode}")
            if not output_path.exists():
                raise AgentRuntimeError("local Codex runtime did not produce structured output")
            try:
                return _parse_json_text(output_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise AgentRuntimeError("local Codex runtime returned invalid JSON") from exc


def _response_output_text(payload: dict[str, Any]) -> str:
    direct = payload.get("output_text")
    if isinstance(direct, str) and direct.strip():
        return direct
    for item in payload.get("output", []):
        if not isinstance(item, dict) or item.get("type") != "message":
            continue
        for content in item.get("content", []):
            if isinstance(content, dict) and content.get("type") == "output_text":
                text = content.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    raise ValueError("response has no output_text")


def _parse_json_text(value: str) -> Any:
    stripped = value.strip()
    if stripped.startswith("```json") and stripped.endswith("```"):
        stripped = stripped[7:-3].strip()
    elif stripped.startswith("```") and stripped.endswith("```"):
        stripped = stripped[3:-3].strip()
    return json.loads(stripped)


def _strict_json_schema(schema: dict[str, Any]) -> dict[str, Any]:
    normalized = json.loads(json.dumps(schema))

    def visit(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            node.pop("default", None)
            for value in node.values():
                visit(value)
        elif isinstance(node, list):
            for value in node:
                visit(value)

    visit(normalized)
    return normalized


def _codex_environment() -> dict[str, str]:
    allowed = {
        "ALL_PROXY",
        "CODEX_HOME",
        "HOME",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "LANG",
        "LC_ALL",
        "NO_PROXY",
        "PATH",
        "SSL_CERT_DIR",
        "SSL_CERT_FILE",
        "TMPDIR",
    }
    return {key: value for key, value in os.environ.items() if key in allowed}
