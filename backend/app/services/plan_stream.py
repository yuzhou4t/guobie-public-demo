"""Stream public plan text from an isolated, tool-free local Codex session."""

from __future__ import annotations

import json
import os
import re
import selectors
import subprocess
import tempfile
import time
from pathlib import Path

from app.services.agent_runtime import AgentRuntimeError, _codex_environment, _strict_json_schema


class PlanCancelled(Exception):
    pass


def preview_fields(text: str) -> dict:
    """Read only public plan fields from an incomplete structured answer."""
    result = {}
    string = r'"((?:[^"\\]|\\.)*)(?:"|$)'

    def decode(value):
        try:
            return json.loads('"' + value + '"')
        except ValueError:
            return ""

    match = re.search(r'"summary"\s*:\s*' + string, text)
    if match:
        result["summary"] = decode(match[1])
    for key in ("steps", "research_paths", "evidence_gaps"):
        match = re.search(r'"' + key + r'"\s*:\s*\[', text)
        if not match:
            continue
        values = []
        rest = text[match.end() :]
        while rest:
            part = re.match(r"\s*,?\s*" + string, rest)
            if not part:
                break
            value = decode(part[1])
            if value:
                values.append(value)
            rest = rest[part.end() :]
        result[key] = values[:8]
    return result


def stream_codex_plan(runtime, *, instructions, prompt, schema, on_preview, cancelled):
    env = _codex_environment()
    auth_root = Path(env.get("CODEX_HOME", str(Path.home() / ".codex")))
    with tempfile.TemporaryDirectory(prefix="guobie-plan-stream-") as directory:
        root = Path(directory)
        config_root = root / "codex"
        config_root.mkdir(mode=0o700)
        # Reuse authentication only; never load personal instructions, plugins or MCP servers.
        auth = auth_root / "auth.json"
        if not auth.is_file():
            raise AgentRuntimeError("本地流式计划需要文件登录凭据")
        (config_root / "auth.json").symlink_to(auth)
        env["CODEX_HOME"] = str(config_root)
        disabled = (
            "apps",
            "browser_use",
            "computer_use",
            "goals",
            "image_generation",
            "memories",
            "multi_agent",
            "plugins",
            "shell_tool",
            "skill_search",
            "code_mode",
        )
        command = [runtime.binary, "app-server", "--stdio", "-c", 'web_search="disabled"']
        for name in disabled:
            command.extend(["--disable", name])
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd=root,
            env=env,
            start_new_session=True,
        )
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ)
        started = time.monotonic()
        buffer = b""
        answer = ""
        preview = {}
        final_ids = set()

        def send(message):
            process.stdin.write((json.dumps(message, ensure_ascii=False) + "\n").encode())
            process.stdin.flush()

        def request(identifier, method, params):
            send({"id": identifier, "method": method, "params": params})

        try:
            request(1, "initialize", {"clientInfo": {"name": "guobie-plan", "version": "1.0"}})
            while True:
                if cancelled.is_set():
                    raise PlanCancelled()
                if time.monotonic() - started > runtime.timeout_seconds:
                    raise AgentRuntimeError("计划生成超时")
                if not selector.select(timeout=0.2):
                    if process.poll() is not None:
                        raise AgentRuntimeError("计划生成进程提前结束")
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    raise AgentRuntimeError("计划生成连接提前结束")
                buffer += chunk
                if len(buffer) > 2_000_000:
                    raise AgentRuntimeError("计划生成消息超过大小限制")
                while b"\n" in buffer:
                    line, buffer = buffer.split(b"\n", 1)
                    message = json.loads(line)
                    if "method" not in message and "error" in message:
                        raise AgentRuntimeError("计划生成请求失败")
                    if message.get("id") == 1 and "result" in message:
                        send({"method": "initialized", "params": {}})
                        params = {
                            "cwd": str(root),
                            "ephemeral": True,
                            "sandbox": "read-only",
                            "approvalPolicy": "never",
                            "baseInstructions": instructions,
                        }
                        if runtime.model:
                            params["model"] = runtime.model
                        request(2, "thread/start", params)
                    elif message.get("id") == 2 and "result" in message:
                        request(
                            3,
                            "turn/start",
                            {
                                "threadId": message["result"]["thread"]["id"],
                                "input": [{"type": "text", "text": prompt}],
                                "outputSchema": _strict_json_schema(schema),
                            },
                        )
                    method = message.get("method", "")
                    params = message.get("params", {})
                    if "id" in message and method:
                        # No approval or tool request can expand this session's capabilities.
                        send({"id": message["id"], "error": {"code": -32601, "message": "Tools disabled"}})
                    if method == "item/started":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                            final_ids.add(item["id"])
                    elif method == "item/agentMessage/delta" and params.get("itemId") in final_ids:
                        answer += params.get("delta", "")
                        if len(answer) > 200_000:
                            raise AgentRuntimeError("计划正文超过大小限制")
                        current = {**preview, **preview_fields(answer)}
                        if current != preview:
                            preview = current
                            on_preview(current)
                    elif method == "item/completed":
                        item = params.get("item", {})
                        if item.get("type") == "agentMessage" and item.get("phase") == "final_answer":
                            answer = item.get("text", answer)
                    elif method == "turn/completed":
                        if params.get("turn", {}).get("status") != "completed":
                            raise AgentRuntimeError("计划生成未完成")
                        return json.loads(answer)
        finally:
            selector.close()
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
            process.stdin.close()
            process.stdout.close()
