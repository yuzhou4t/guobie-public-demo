from __future__ import annotations

import json
import os
import subprocess
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator

from app.services.agent_runtime import AgentRuntimeError, _response_output_text


class LiveEvidenceLocator(BaseModel):
    model_config = ConfigDict(extra="forbid")

    locator_type: str = "page"
    page: int | None = None
    section: str | None = None
    paragraph: str | None = None
    quote: str | None = Field(default=None, max_length=300)


class LiveSearchFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    claim: str = Field(min_length=1, max_length=800)
    title: str = Field(min_length=1, max_length=300)
    source_name: str = Field(min_length=1, max_length=160)
    source_url: str
    published_at: str | None = None
    published_at_precision: str = "unknown"
    evidence_locator: LiveEvidenceLocator = Field(default_factory=LiveEvidenceLocator)

    @field_validator("source_url")
    @classmethod
    def validate_source_url(cls, value: str) -> str:
        if not value.startswith(("https://", "http://")):
            raise ValueError("live search sources must use http or https")
        return value


class LiveSearchBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    findings: list[LiveSearchFinding] = Field(default_factory=list, max_length=6)
    limitations: list[str] = Field(default_factory=list)


class LiveSearchProvider(ABC):
    provider_name: str

    @abstractmethod
    def search(self, *, question: str, scope_context: dict[str, Any]) -> LiveSearchBundle:
        raise NotImplementedError


class DisabledLiveSearchProvider(LiveSearchProvider):
    provider_name = "disabled"

    def search(self, *, question: str, scope_context: dict[str, Any]) -> LiveSearchBundle:
        return LiveSearchBundle(findings=[], limitations=["联网检索未启用。"])


class CodexLocalLiveSearchProvider(LiveSearchProvider):
    provider_name = "codex_local"

    def __init__(self, *, binary: str, model: str, timeout_seconds: float) -> None:
        self.binary = binary
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    def search(self, *, question: str, scope_context: dict[str, Any]) -> LiveSearchBundle:
        prompt = json.dumps(
            {
                "task": "只检索当前问题所需的最新公开信息，并逐条给出原始来源链接。",
                "question": question,
                "scope_context": scope_context,
                "rules": [
                    "优先官方和一手来源",
                    "不要访问登录、付费墙、验证码或受限内容",
                    "不要执行 shell、文件写入、插件、Computer Use 或多 Agent",
                    "每条 finding 只承载一个来源支持的一个简短事实",
                    "无法确认发布日期时使用 unknown，不能用检索时间代替",
                ],
            },
            ensure_ascii=False,
        )
        with tempfile.TemporaryDirectory(prefix="guobie-live-search-") as temp_dir:
            root = Path(temp_dir)
            schema_path = root / "output-schema.json"
            output_path = root / "output.json"
            schema_path.write_text(
                json.dumps(_strict_json_schema(LiveSearchBundle), ensure_ascii=False),
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
                "--enable",
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
                completed = subprocess.run(
                    command,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    check=False,
                    env=_codex_environment(),
                )
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AgentRuntimeError("local Codex live search failed to start or timed out") from exc
            if completed.returncode != 0 or not output_path.exists():
                raise AgentRuntimeError("local Codex live search did not produce structured output")
            try:
                return LiveSearchBundle.model_validate_json(output_path.read_text(encoding="utf-8"))
            except (OSError, ValidationError) as exc:
                raise AgentRuntimeError("local Codex live search returned invalid structured output") from exc


class OpenAIResponsesLiveSearchProvider(LiveSearchProvider):
    provider_name = "openai_responses"
    _ENDPOINT = "https://api.openai.com/v1/responses"

    def __init__(self, *, api_key: SecretStr, model: str, timeout_seconds: float) -> None:
        if not model.strip():
            raise AgentRuntimeError("GUOBIE_LIVE_SEARCH_MODEL is required")
        self.api_key = api_key
        self.model = model.strip()
        self.timeout_seconds = timeout_seconds

    def search(self, *, question: str, scope_context: dict[str, Any]) -> LiveSearchBundle:
        body = {
            "model": self.model,
            "store": False,
            "tools": [{"type": "web_search"}],
            "input": json.dumps({"question": question, "scope_context": scope_context}, ensure_ascii=False),
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "guobie_live_search",
                    "strict": True,
                    "schema": _strict_json_schema(LiveSearchBundle),
                }
            },
        }
        try:
            response = httpx.post(
                self._ENDPOINT,
                headers={"Authorization": f"Bearer {self.api_key.get_secret_value()}"},
                json=body,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise AgentRuntimeError("OpenAI live search request failed") from exc
        if response.status_code != 200 or response.is_redirect:
            raise AgentRuntimeError(f"OpenAI live search returned HTTP {response.status_code}")
        try:
            return LiveSearchBundle.model_validate_json(_response_output_text(response.json()))
        except (ValueError, ValidationError) as exc:
            raise AgentRuntimeError("OpenAI live search returned invalid structured output") from exc


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


def _strict_json_schema(model: type[BaseModel]) -> dict[str, Any]:
    schema = model.model_json_schema()

    def normalize(node: Any) -> None:
        if isinstance(node, dict):
            properties = node.get("properties")
            if isinstance(properties, dict):
                node["additionalProperties"] = False
                node["required"] = list(properties)
            node.pop("default", None)
            for value in node.values():
                normalize(value)
        elif isinstance(node, list):
            for value in node:
                normalize(value)

    normalize(schema)
    return schema
