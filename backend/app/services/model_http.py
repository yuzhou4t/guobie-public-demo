"""Bounded model POST transport, at the same trust boundary as SafeHttpClient.

No redirects or environment proxies. All DNS answers must be public. The actual
connection is pinned to a checked address while retaining TLS SNI and Host.
Only this module may perform external HTTP requests in the public edition.
"""

import ipaddress
import json
import re
import socket
from time import monotonic
from urllib.parse import urlsplit, urlunsplit

import httpx


class ProviderError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def normalize_base_url(value: str, allowed_hosts: set[str] | None = None) -> str:
    try:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower()
        port = parsed.port
    except ValueError:
        raise ProviderError("unsafe_endpoint", "API 地址格式无效") from None
    if (
        parsed.scheme != "https"
        or not host
        or port not in (None, 443)
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(ord(c) < 33 or ord(c) > 126 for c in value)
        or not re.fullmatch(r"[a-z0-9.-]+", host)
        or host.endswith(".")
        or not re.fullmatch(r"/[A-Za-z0-9_./~-]*|", parsed.path)
        or any(part in {".", ".."} for part in parsed.path.split("/"))
        or (allowed_hosts and host not in allowed_hosts)
    ):
        raise ProviderError(
            "unsafe_endpoint", "仅支持允许的公网 HTTPS API 地址（443 端口），不含密钥、查询参数或跳转"
        )
    if parsed.path.rstrip("/").endswith(("/responses", "/chat/completions")):
        raise ProviderError("unsafe_endpoint", "请填写 Base URL，例如 https://api.openai.com/v1")
    return urlunsplit(("https", host, parsed.path.rstrip("/"), "", ""))


def public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return False
    return address.is_global and not address.is_multicast


class SafeModelHttpClient:
    max_bytes = 512 * 1024

    def __init__(self, *, timeout: float, resolver=None, transport=None):
        self.timeout = timeout
        self.resolver = resolver or socket.getaddrinfo
        self.transport = transport

    def post_json(self, url: str, *, api_key: str, payload: dict) -> dict:
        started = monotonic()
        parsed = urlsplit(url)
        host = parsed.hostname
        if parsed.scheme != "https" or not host or parsed.port not in (None, 443):
            raise ProviderError("unsafe_endpoint", "不支持的 API 地址")
        try:
            records = self.resolver(host, 443, type=socket.SOCK_STREAM)
        except OSError:
            raise ProviderError("dns_error", "无法解析 API 服务地址，请检查 Base URL") from None
        addresses = list(dict.fromkeys(str(row[4][0]) for row in records))
        if not addresses or any(not public_ip(address) for address in addresses):
            raise ProviderError("unsafe_endpoint", "API 地址未通过公网地址校验")
        remaining = self.timeout - (monotonic() - started)
        if remaining <= 0:
            raise ProviderError("timeout", "API 地址解析超时")
        ip = addresses[0]
        pinned = urlunsplit(("https", f"[{ip}]" if ":" in ip else ip, parsed.path, "", ""))
        try:
            with httpx.Client(
                timeout=httpx.Timeout(remaining, connect=min(10, remaining)),
                follow_redirects=False,
                trust_env=False,
                transport=self.transport,
            ) as client:
                with client.stream(
                    "POST",
                    pinned,
                    headers={
                        "Host": host,
                        "Authorization": f"Bearer {api_key}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                        "Accept-Encoding": "identity",
                    },
                    extensions={"sni_hostname": host},
                    json=payload,
                ) as response:
                    if response.status_code in {401, 403}:
                        raise ProviderError("authentication", "API Key 无效或没有调用该模型的权限")
                    if response.status_code in {402, 429}:
                        raise ProviderError("quota", "模型服务余额不足或调用频率受限，请在服务商处核实")
                    if response.is_redirect:
                        raise ProviderError("redirect", "API 返回跳转，已停止；请填写最终服务地址")
                    if response.status_code != 200:
                        raise ProviderError("provider_status", "模型服务未接受请求，请检查协议和模型名称")
                    mime = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if mime != "application/json" and not (
                        mime.startswith("application/") and mime.endswith("+json")
                    ):
                        raise ProviderError("content_type", "模型服务返回了非 JSON 内容")
                    length = response.headers.get("content-length")
                    if length and (not length.isdigit() or int(length) > self.max_bytes):
                        raise ProviderError("size_limit", "模型响应超过大小限制")
                    chunks, size = [], 0
                    for chunk in response.iter_bytes(chunk_size=16384):
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise ProviderError("size_limit", "模型响应超过大小限制")
                        if monotonic() - started > self.timeout:
                            raise ProviderError("timeout", "模型响应超时，请减少材料或稍后重试")
                        chunks.append(chunk)
                    result = json.loads(b"".join(chunks))
                    if not isinstance(result, dict):
                        raise ValueError("Expected object")
                    return result
        except httpx.TimeoutException:
            raise ProviderError("timeout", "模型响应超时，请稍后重试；未生成研究成果") from None
        except httpx.HTTPError:
            raise ProviderError("network", "无法安全连接模型服务，请检查地址与网络") from None
        except (ValueError, UnicodeError):
            raise ProviderError("invalid_json", "模型服务返回的 JSON 无效") from None
