from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic
from typing import Any
from urllib.parse import parse_qsl, urljoin, urlsplit, urlunsplit

import httpx

from app.collectors.base import FetchedResource
from app.core.config import Settings, get_settings

Resolver = Callable[..., list[tuple[Any, ...]]]
_ALLOWED_DOH_URLS = {
    "https://dns.alidns.com/resolve",
    "https://doh.pub/resolve",
    "https://dns.google/resolve",
}
_DOH_BOOTSTRAP_ADDRESSES = {
    "https://dns.alidns.com/resolve": ("223.5.5.5", "223.6.6.6"),
    "https://doh.pub/resolve": ("1.12.12.12", "120.53.53.53"),
    "https://dns.google/resolve": ("8.8.8.8", "8.8.4.4"),
}
_COMTRADE_API_HOST = "comtradeapi.un.org"
_COMTRADE_API_PATH = "/data/v1/get/C/A/HS"
_COMTRADE_SUBSCRIPTION_HEADER = "Ocp-Apim-Subscription-Key"
_NCPSD_API_HOST = "www.ncpssd.cn"
_NCPSD_ARTICLE_API_PATH = "/articleinfoHandler/getjournalarticletable"
_COMTRADE_QUERY_KEYS = {
    "reporterCode",
    "period",
    "partnerCode",
    "partner2Code",
    "cmdCode",
    "flowCode",
    "customsCode",
    "motCode",
    "maxRecords",
    "format",
    "breakdownMode",
    "includeDesc",
}
_COMTRADE_REPORTER_CODES = {"180", "710", "716", "894"}
_COMTRADE_COMMODITY_CODES = {"260300", "260500", "283691"}
_OECD_SDMX_HOST = "sdmx.oecd.org"
_OECD_ODA_PATH = "/public/rest/data/OECD.DCD.FSD,DSD_DAC2@DF_DAC2A,/ALLD.COD+ZWE+ZMB+ZAF.206.USD.V"
_OECD_ODA_QUERY = {
    "startPeriod": "2015",
    "endPeriod": "2024",
    "dimensionAtObservation": "AllDimensions",
    "format": "jsondata",
}
_OECD_SDMX_JSON_TYPE = "application/vnd.sdmx.data+json"
_WITS_HOST = "wits.worldbank.org"
_WITS_AVAILABILITY_PATH = (
    "/API/V1/wits/datasource/trn/dataavailability/country/180;716;894;710/"
    "year/2015;2016;2017;2018;2019;2020;2021"
)
_WITS_REPORTERS = {"180", "710", "716", "894"}
_WITS_PRODUCTS = "260300;260500;283691"
_WITS_YEARS = {str(year) for year in range(2015, 2022)}
_WITS_SDMX_XML_TYPE = "application/vnd.sdmx.structurespecificdata+xml"
_IMF_WEO_EXCEL_URL = (
    "https://data.imf.org/-/media/iData/External-Storage/Documents/"
    "2F78EE59F79143A7921E5E203D3AAA80/en/WEOApr2026all.xlsx"
)
_IMF_EXCEL_CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
_UNCTAD_FDI_BULK_URL = "https://unctadstat-api.unctad.org/bulkdownload/US.FdiFlowsStock/US_FdiFlowsStock"
_SEVEN_Z_CONTENT_TYPE = "application/x-7z-compressed"
_XLSX_MAGIC = b"PK\x03\x04"
_SEVEN_Z_MAGIC = b"7z\xbc\xaf'\x1c"
_SENSITIVE_QUERY_KEY_TOKENS = {
    "accesstoken",
    "apikey",
    "authorization",
    "key",
    "ocpapimsubscriptionkey",
    "subscriptionkey",
    "token",
    "xapikey",
}


class DnsOverHttpsResolver:
    """Resolve public targets through one fixed, TLS-authenticated DoH service."""

    def __init__(
        self,
        endpoint: str,
        *,
        timeout_seconds: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        if endpoint not in _ALLOWED_DOH_URLS:
            raise ValueError("source_probe_doh_url is not an approved resolver endpoint")
        self.endpoint = endpoint
        self.timeout_seconds = timeout_seconds
        self.transport = transport
        self._cache: dict[tuple[str, int], list[tuple[Any, ...]]] = {}

    def __call__(self, host: str, port: int, *, type: int) -> list[tuple[Any, ...]]:
        if type != socket.SOCK_STREAM:
            raise socket.gaierror("DoH resolver supports stream targets only")
        cache_key = (host.casefold(), port)
        cached = self._cache.get(cache_key)
        if cached is not None:
            return list(cached)

        addresses: list[str] = []
        endpoints_to_try = [self.endpoint] + [ep for ep in _ALLOWED_DOH_URLS if ep != self.endpoint]
        for ep in endpoints_to_try:
            try:
                addresses = self._query(host, "A", endpoint=ep)
                if not addresses:
                    addresses = self._query(host, "AAAA", endpoint=ep)
                if addresses:
                    break
            except socket.gaierror:
                addresses = []

        if not addresses:
            try:
                raw_records = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
                for rec in raw_records:
                    sockaddr = rec[4]
                    if sockaddr and sockaddr[0] not in addresses:
                        addresses.append(str(sockaddr[0]))
            except socket.gaierror:
                pass

        if not addresses:
            raise socket.gaierror("DoH response contained no address records")

        records: list[tuple[Any, ...]] = []
        for address in addresses:
            ip = ipaddress.ip_address(address)
            family = socket.AF_INET6 if ip.version == 6 else socket.AF_INET
            sockaddr: tuple[Any, ...] = (
                (str(ip), port, 0, 0) if family == socket.AF_INET6 else (str(ip), port)
            )
            records.append((family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr))
        self._cache[cache_key] = records
        return list(records)

    def _query(
        self,
        host: str,
        record_type: str,
        *,
        endpoint: str | None = None,
        cname_depth: int = 0,
    ) -> list[str]:
        target_endpoint = endpoint or self.endpoint
        last_error: Exception | None = None
        payload: Any = None
        parsed_endpoint = urlsplit(target_endpoint)
        endpoint_host = parsed_endpoint.hostname
        assert endpoint_host is not None
        for _attempt in range(2):
            for address in _DOH_BOOTSTRAP_ADDRESSES[target_endpoint]:
                try:
                    pinned_endpoint = urlunsplit(
                        (
                            parsed_endpoint.scheme,
                            address,
                            parsed_endpoint.path,
                            parsed_endpoint.query,
                            "",
                        )
                    )
                    with httpx.Client(
                        timeout=min(self.timeout_seconds, 3.0),
                        follow_redirects=False,
                        trust_env=False,
                        transport=self.transport,
                        headers={
                            "Accept": "application/dns-json",
                            "User-Agent": "GuobieSourceProbe/0.1",
                        },
                    ) as client:
                        response = client.get(
                            pinned_endpoint,
                            params={"name": host, "type": record_type},
                            headers={"Host": endpoint_host},
                            extensions={"sni_hostname": endpoint_host},
                        )
                    response.raise_for_status()
                    if len(response.content) > 65536:
                        raise socket.gaierror("DoH response exceeded the size limit")
                    payload = response.json()
                    break
                except (httpx.HTTPError, json.JSONDecodeError, ValueError) as exc:
                    last_error = exc
            if payload is not None:
                break
        else:
            raise socket.gaierror("DoH resolution failed") from last_error

        if not isinstance(payload, dict) or payload.get("Status") != 0:
            raise socket.gaierror("DoH resolver returned an unsuccessful DNS status")
        expected_type = 1 if record_type == "A" else 28
        addresses: list[str] = []
        canonical_names: list[str] = []
        answers = payload.get("Answer", [])
        if not isinstance(answers, list):
            return []
        for answer in answers:
            if not isinstance(answer, dict):
                continue
            if answer.get("type") == 5:
                canonical_name = str(answer.get("data", "")).rstrip(".")
                if canonical_name and canonical_name not in canonical_names:
                    canonical_names.append(canonical_name)
                continue
            if answer.get("type") != expected_type:
                continue
            value = answer.get("data")
            try:
                parsed = ipaddress.ip_address(str(value))
            except ValueError:
                continue
            if parsed.version == (4 if expected_type == 1 else 6):
                addresses.append(str(parsed))
        if not addresses and canonical_names and cname_depth < 4:
            return self._query(
                canonical_names[-1],
                record_type,
                endpoint=target_endpoint,
                cname_depth=cname_depth + 1,
            )
        return addresses


class ProbeError(Exception):
    def __init__(
        self,
        category: str,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        retry_after: str | None = None,
    ) -> None:
        super().__init__(message)
        self.category = category
        self.code = code
        self.message = message
        self.retryable = retryable
        self.retry_after = retry_after


def normalize_http_url(url: str) -> str:
    parsed = urlsplit(url.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ProbeError("policy", "invalid_url", "only absolute http(s) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise ProbeError("policy", "url_credentials_forbidden", "URL credentials are not allowed")
    try:
        port = parsed.port
    except ValueError as exc:
        raise ProbeError("policy", "invalid_port", "URL port is invalid") from exc
    if port not in {None, 80, 443}:
        raise ProbeError("policy", "port_forbidden", "only ports 80 and 443 are allowed")

    scheme = parsed.scheme.lower()
    host = parsed.hostname.rstrip(".").lower()
    if not host:
        raise ProbeError("policy", "invalid_host", "URL host is invalid")
    try:
        host = host.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise ProbeError("policy", "invalid_host", "URL host is invalid") from exc
    default_port = 443 if scheme == "https" else 80
    normalized_port = port or default_port
    rendered_host = f"[{host}]" if ":" in host else host
    netloc = rendered_host if normalized_port == default_port else f"{rendered_host}:{normalized_port}"
    path = parsed.path or "/"
    return urlunsplit((scheme, netloc, path, parsed.query, ""))


def _is_public_address(address: str) -> bool:
    ip = ipaddress.ip_address(address)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global


def classify_content(resource: FetchedResource) -> str:
    content_type = resource.content_type.lower().split(";", 1)[0].strip()
    sample = resource.body[:8192].lstrip()
    lowered = sample.lower()

    if sample.startswith(b"%PDF-"):
        return "pdf"
    if content_type in {"application/json", "application/feed+json"} or sample[:1] in {
        b"{",
        b"[",
    }:
        try:
            json.loads(resource.body)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return "unknown"
        return "api"
    if (
        b"<rss" in lowered
        or b"<feed" in lowered
        or b"<rdf:rdf" in lowered
        or b"<urlset" in lowered
        or content_type in {"application/rss+xml", "application/atom+xml"}
    ):
        return "rss"
    if b"<!doctype html" in lowered or b"<html" in lowered:
        return "html"
    return "unknown"


class SafeHttpClient:
    _MAX_PINNED_ADDRESSES = 4
    _PDF_PREFIX_BYTES = 4096
    _ALLOWED_CONTENT_TYPES = {
        "application/atom+xml",
        "application/feed+json",
        "application/hal+json",
        "application/json",
        "application/octet-stream",
        "application/pdf",
        "application/rss+xml",
        "application/xhtml+xml",
        "application/xml",
        "text/html",
        "text/plain",
        "text/xml",
    }

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        resolver: Resolver = socket.getaddrinfo,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if resolver is socket.getaddrinfo and self.settings.source_probe_doh_url:
            self._resolver = DnsOverHttpsResolver(
                self.settings.source_probe_doh_url,
                timeout_seconds=self.settings.source_probe_timeout_seconds,
            )
        else:
            self._resolver = resolver
        self._transport = transport

    def fetch(self, url: str) -> FetchedResource:
        return self._fetch(url)

    def fetch_geojson(self, url: str) -> FetchedResource:
        """Fetch a bounded JSON object, including GeoJSON served as octet-stream."""

        return self._fetch(
            url,
            extra_allowed_content_types={"application/geo+json"},
            expected_binary_magic=b"{",
        )

    def fetch_without_redirects(self, url: str) -> FetchedResource:
        """Fetch one reviewed URL without following a redirect."""

        return self._fetch(url, max_redirects=0)

    def fetch_ncpssd_article_metadata(self, article_id: str) -> FetchedResource:
        """POST one reviewed NCPSD article id to its public metadata endpoint."""

        if not article_id.isascii() or not article_id.isalnum() or not 1 <= len(article_id) <= 64:
            raise ProbeError(
                "policy",
                "ncpssd_article_id_invalid",
                "NCPSD article id is invalid",
            )
        request_url = f"https://{_NCPSD_API_HOST}{_NCPSD_ARTICLE_API_PATH}"
        body = json.dumps(
            {
                "lngid": article_id,
                "type": "中文期刊文章",
                "pageType": 1,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
        return self._fetch(
            request_url,
            max_redirects=0,
            request_method="POST",
            request_body=body,
            request_headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json; charset=UTF-8",
                "Origin": f"https://{_NCPSD_API_HOST}",
                "Referer": f"https://{_NCPSD_API_HOST}/Literature/articleinfo?id={article_id}",
            },
        )

    def fetch_pdf_prefix(self, url: str) -> FetchedResource:
        """Fetch only a fixed PDF prefix when the server honors byte ranges."""

        prefix_bytes = min(self._PDF_PREFIX_BYTES, self.settings.source_probe_max_bytes)
        return self._fetch(url, pdf_prefix_bytes=prefix_bytes)

    def fetch_comtrade(self, url: str) -> FetchedResource:
        """Fetch the reviewed Comtrade endpoint with a transport-only candidate credential."""

        normalization_error: ProbeError | None = None
        try:
            raw_url = url.strip()
            raw_parsed = urlsplit(raw_url)
            request_url = normalize_http_url(raw_url)
            parsed = urlsplit(request_url)
        except ProbeError as exc:
            normalization_error = ProbeError(
                exc.category,
                exc.code,
                exc.message,
                retryable=exc.retryable,
            )
        except Exception:
            normalization_error = ProbeError(
                "policy",
                "comtrade_url_invalid",
                "UN Comtrade URL is invalid",
            )
        if normalization_error is not None:
            raise normalization_error from None

        if (
            raw_parsed.fragment
            or parsed.scheme != "https"
            or parsed.hostname != _COMTRADE_API_HOST
            or parsed.port not in {None, 443}
            or parsed.path != _COMTRADE_API_PATH
        ):
            raise ProbeError(
                "policy",
                "comtrade_url_forbidden",
                "only the reviewed UN Comtrade annual goods endpoint is allowed",
            )
        invalid_query = False
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            invalid_query = True
            query = []
        if invalid_query:
            raise ProbeError(
                "policy",
                "comtrade_query_invalid",
                "UN Comtrade query parameters are invalid",
            ) from None
        if any(_is_sensitive_query_key(key) for key, _value in query):
            raise ProbeError(
                "policy",
                "comtrade_credentials_in_url",
                "UN Comtrade credentials are forbidden in URLs",
            )

        configured_key = self.settings.comtrade_subscription_key
        if configured_key is None:
            raise ProbeError(
                "policy",
                "comtrade_subscription_key_missing",
                "UN Comtrade subscription key is not configured",
            )
        subscription_key = configured_key.get_secret_value()
        if not _is_valid_sensitive_header_value(subscription_key):
            raise ProbeError(
                "policy",
                "comtrade_subscription_key_invalid",
                "UN Comtrade subscription key is invalid",
            )
        if any(subscription_key in value for _key, value in query):
            raise ProbeError(
                "policy",
                "comtrade_credentials_in_url",
                "UN Comtrade credentials are forbidden in URLs",
            )
        if not _is_reviewed_comtrade_query(query):
            raise ProbeError(
                "policy",
                "comtrade_query_out_of_scope",
                "UN Comtrade query is outside the reviewed collection scope",
            )

        safe_error: ProbeError | None = None
        try:
            return self._fetch(
                request_url,
                max_redirects=0,
                comtrade_subscription_key=subscription_key,
            )
        except ProbeError as exc:
            safe_error = ProbeError(
                exc.category,
                exc.code,
                exc.message,
                retryable=exc.retryable,
            )
        except Exception:
            safe_error = ProbeError(
                "internal",
                "comtrade_transport_failed",
                "UN Comtrade authenticated request failed",
            )
        raise safe_error from None

    def fetch_oecd_oda(self, url: str) -> FetchedResource:
        """Fetch only the reviewed OECD DAC2A ODA grid using the SDMX JSON MIME."""

        request_url = normalize_http_url(url)
        parsed = urlsplit(request_url)
        try:
            query = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
        except ValueError:
            query = []
        if (
            parsed.scheme != "https"
            or parsed.hostname != _OECD_SDMX_HOST
            or parsed.port not in {None, 443}
            or parsed.path != _OECD_ODA_PATH
            or len(query) != len(_OECD_ODA_QUERY)
            or dict(query) != _OECD_ODA_QUERY
        ):
            raise ProbeError(
                "policy",
                "oecd_oda_url_forbidden",
                "only the reviewed OECD DAC2A ODA query is allowed",
            )
        return self._fetch(
            request_url,
            max_redirects=0,
            extra_allowed_content_types={_OECD_SDMX_JSON_TYPE},
        )

    def fetch_wits_availability(self, url: str) -> FetchedResource:
        """Fetch only the reviewed WITS TRAINS availability grid."""

        request_url = normalize_http_url(url)
        parsed = urlsplit(request_url)
        if (
            urlsplit(url.strip()).fragment
            or parsed.scheme != "https"
            or parsed.hostname != _WITS_HOST
            or parsed.port not in {None, 443}
            or parsed.path != _WITS_AVAILABILITY_PATH
            or parsed.query
        ):
            raise ProbeError(
                "policy",
                "wits_availability_url_forbidden",
                "only the reviewed WITS TRAINS availability query is allowed",
            )
        return self._fetch(request_url, max_redirects=0)

    def fetch_wits_tariff(self, url: str) -> FetchedResource:
        """Fetch one reviewed WITS TRAINS reporter-year tariff shard."""

        request_url = normalize_http_url(url)
        parsed = urlsplit(request_url)
        parts = parsed.path.strip("/").split("/")
        if (
            urlsplit(url.strip()).fragment
            or parsed.scheme != "https"
            or parsed.hostname != _WITS_HOST
            or parsed.port not in {None, 443}
            or parsed.query
            or len(parts) != 16
            or parts[:7] != ["API", "V1", "SDMX", "V21", "datasource", "TRN", "reporter"]
            or parts[7] not in _WITS_REPORTERS
            or parts[8:11] != ["partner", "000", "product"]
            or parts[11] != _WITS_PRODUCTS
            or parts[12] != "year"
            or parts[13] not in _WITS_YEARS
            or parts[14:] != ["datatype", "reported"]
        ):
            raise ProbeError(
                "policy",
                "wits_tariff_url_forbidden",
                "only reviewed WITS TRAINS reporter-year tariff queries are allowed",
            )
        return self._fetch(
            request_url,
            max_redirects=0,
            extra_allowed_content_types={_WITS_SDMX_XML_TYPE},
        )

    def fetch_imf_weo_excel(self, url: str) -> FetchedResource:
        """Fetch only the reviewed official April 2026 WEO Excel workbook."""

        request_url = normalize_http_url(url)
        if urlsplit(url.strip()).fragment or request_url != _IMF_WEO_EXCEL_URL:
            raise ProbeError(
                "policy",
                "imf_weo_excel_url_forbidden",
                "only the reviewed official April 2026 WEO Excel file is allowed",
            )
        return self._fetch(
            request_url,
            max_redirects=0,
            extra_allowed_content_types={_IMF_EXCEL_CONTENT_TYPE},
            expected_binary_magic=_XLSX_MAGIC,
        )

    def fetch_unctad_fdi_bulk(self, url: str) -> FetchedResource:
        """Fetch only the reviewed official UNCTAD FDI bulk archive."""

        request_url = normalize_http_url(url)
        if urlsplit(url.strip()).fragment or request_url != _UNCTAD_FDI_BULK_URL:
            raise ProbeError(
                "policy",
                "unctad_fdi_bulk_url_forbidden",
                "only the reviewed official UNCTAD FDI bulk file is allowed",
            )
        return self._fetch(
            request_url,
            max_redirects=0,
            extra_allowed_content_types={_SEVEN_Z_CONTENT_TYPE},
            expected_binary_magic=_SEVEN_Z_MAGIC,
        )

    def _fetch(
        self,
        url: str,
        *,
        pdf_prefix_bytes: int | None = None,
        max_redirects: int | None = None,
        comtrade_subscription_key: str | None = None,
        extra_allowed_content_types: set[str] | None = None,
        expected_binary_magic: bytes | None = None,
        request_method: str = "GET",
        request_body: bytes | None = None,
        request_headers: dict[str, str] | None = None,
    ) -> FetchedResource:
        request_url = normalize_http_url(url)
        current_url = request_url
        timeout = httpx.Timeout(self.settings.source_probe_timeout_seconds)
        deadline = monotonic() + self.settings.source_probe_timeout_seconds
        if comtrade_subscription_key is not None:
            redirect_limit = 0
        else:
            redirect_limit = (
                self.settings.source_probe_max_redirects if max_redirects is None else max_redirects
            )

        try:
            with httpx.Client(
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
                http2=False,
                limits=httpx.Limits(max_keepalive_connections=0),
                transport=self._transport,
                headers={
                    "Accept": (
                        "text/html,application/xhtml+xml,application/xml;q=0.9,"
                        "application/rss+xml,application/atom+xml,application/json,application/pdf,*/*;q=0.8"
                    ),
                    "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7",
                    "User-Agent": (
                        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
                    ),
                },
            ) as client:
                for redirect_count in range(redirect_limit + 1):
                    status_code, response_headers, body = self._fetch_validated_hop(
                        client,
                        current_url,
                        deadline=deadline,
                        pdf_prefix_bytes=pdf_prefix_bytes,
                        comtrade_subscription_key=comtrade_subscription_key,
                        extra_allowed_content_types=extra_allowed_content_types,
                        expected_binary_magic=expected_binary_magic,
                        request_method=request_method,
                        request_body=request_body,
                        extra_request_headers=request_headers,
                    )
                    if status_code in {301, 302, 303, 307, 308}:
                        if redirect_count >= redirect_limit:
                            raise ProbeError("http", "too_many_redirects", "redirect limit exceeded")
                        location = response_headers.get("location")
                        if not location:
                            raise ProbeError("http", "redirect_without_location", "redirect has no location")
                        current_url = normalize_http_url(urljoin(current_url, location))
                        continue

                    content_type = response_headers.get("content-type", "")
                    return FetchedResource(
                        request_url=request_url,
                        final_url=current_url,
                        status_code=status_code,
                        headers=response_headers,
                        body=body,
                        fetched_at=datetime.now(UTC),
                        sha256=hashlib.sha256(body).hexdigest(),
                        content_type=content_type,
                        etag=response_headers.get("etag"),
                        last_modified=response_headers.get("last-modified"),
                    )
        except ProbeError:
            raise
        except httpx.TimeoutException as exc:
            raise ProbeError("network", "timeout", "source request timed out", retryable=True) from exc
        except httpx.RequestError as exc:
            raise ProbeError(
                "network", "connection_failed", "source connection failed", retryable=True
            ) from exc

        raise ProbeError("internal", "unreachable", "probe ended unexpectedly")

    def _fetch_validated_hop(
        self,
        client: httpx.Client,
        url: str,
        *,
        deadline: float,
        pdf_prefix_bytes: int | None,
        comtrade_subscription_key: str | None,
        extra_allowed_content_types: set[str] | None,
        expected_binary_magic: bytes | None,
        request_method: str,
        request_body: bytes | None,
        extra_request_headers: dict[str, str] | None,
    ) -> tuple[int, dict[str, str], bytes]:
        last_error: httpx.RequestError | None = None
        targets = tuple(self._validated_targets(url))
        for target_index, (pinned_url, headers, extensions) in enumerate(targets):
            try:
                remaining = self._remaining_timeout(deadline)
                remaining_targets = len(targets) - target_index
                attempt_timeout = min(
                    remaining,
                    max(1.0, remaining / remaining_targets),
                )
                request_headers = dict(headers)
                request_headers["Connection"] = "close"
                request_headers.update(extra_request_headers or {})
                if comtrade_subscription_key is not None:
                    request_headers[_COMTRADE_SUBSCRIPTION_HEADER] = comtrade_subscription_key
                if pdf_prefix_bytes is not None:
                    request_headers.update(
                        {
                            "Accept": "application/pdf, application/octet-stream;q=0.9",
                            "Accept-Encoding": "identity",
                            "Range": f"bytes=0-{pdf_prefix_bytes - 1}",
                        }
                    )
                with client.stream(
                    request_method,
                    pinned_url,
                    headers=request_headers,
                    extensions=extensions,
                    timeout=attempt_timeout,
                    content=request_body,
                ) as response:
                    response_headers = {key.lower(): value for key, value in response.headers.items()}
                    if response.status_code in {301, 302, 303, 307, 308}:
                        result = (response.status_code, response_headers, b"")
                    else:
                        body = self._read_validated_body(
                            response,
                            response_headers,
                            deadline=deadline,
                            pdf_prefix_bytes=pdf_prefix_bytes,
                            extra_allowed_content_types=extra_allowed_content_types,
                            expected_binary_magic=expected_binary_magic,
                        )
                        result = (response.status_code, response_headers, body)
                self._remaining_timeout(deadline)
                return result
            except httpx.RequestError as exc:
                last_error = exc
                if monotonic() >= deadline:
                    raise ProbeError(
                        "network", "timeout", "source request timed out", retryable=True
                    ) from exc
        if isinstance(last_error, httpx.TimeoutException):
            raise ProbeError("network", "timeout", "source request timed out", retryable=True) from last_error
        raise ProbeError(
            "network", "connection_failed", "source connection failed", retryable=True
        ) from last_error

    def _read_validated_body(
        self,
        response: httpx.Response,
        response_headers: dict[str, str],
        *,
        deadline: float,
        pdf_prefix_bytes: int | None,
        extra_allowed_content_types: set[str] | None,
        expected_binary_magic: bytes | None,
    ) -> bytes:
        self._raise_for_status(response.status_code, response.headers.get("retry-after"))
        expected_range_length: int | None = None
        if pdf_prefix_bytes is not None:
            expected_range_length = self._validate_pdf_prefix_headers(
                response.status_code,
                response_headers,
                limit=pdf_prefix_bytes,
            )
        content_type = response_headers.get("content-type", "").lower()
        media_type = content_type.split(";", 1)[0].strip()
        if pdf_prefix_bytes is not None and media_type not in {
            "application/pdf",
            "application/octet-stream",
        }:
            raise ProbeError(
                "policy",
                "content_type_forbidden",
                f"bounded PDF response has an invalid content type: {media_type or 'missing'}",
            )
        allowed_content_types = self._ALLOWED_CONTENT_TYPES | (extra_allowed_content_types or set())
        if media_type not in allowed_content_types:
            raise ProbeError(
                "policy",
                "content_type_forbidden",
                f"content type is not allowed: {media_type or 'missing'}",
            )
        body_limit = pdf_prefix_bytes or self.settings.source_probe_max_bytes
        self._check_declared_size(response_headers.get("content-length"), limit=body_limit)
        body = self._read_limited(response, deadline=deadline, limit=body_limit)
        if not body:
            raise ProbeError("quality", "empty_response", "response body is empty")
        if expected_range_length is not None:
            if len(body) != expected_range_length:
                raise ProbeError(
                    "quality",
                    "invalid_content_range",
                    "partial response length does not match Content-Range",
                )
            if not body.startswith(b"%PDF-"):
                raise ProbeError(
                    "quality",
                    "invalid_pdf_header",
                    "partial response does not start with a PDF header",
                )
        if expected_binary_magic is not None and not body.startswith(expected_binary_magic):
            raise ProbeError(
                "quality",
                "invalid_binary_header",
                "binary response does not match the reviewed file signature",
            )
        if (
            media_type == "application/octet-stream"
            and not body.startswith(b"%PDF-")
            and expected_binary_magic is None
        ):
            raise ProbeError(
                "policy",
                "ambiguous_binary_content",
                "binary response is not a recognized PDF",
            )
        feed_sample = body[:8192].lower()
        looks_like_xml = (
            media_type.endswith("+xml")
            or media_type
            in {
                "application/atom+xml",
                "application/rss+xml",
                "application/xhtml+xml",
                "application/xml",
                "text/xml",
            }
            or any(marker in feed_sample for marker in (b"<rss", b"<feed", b"<rdf:rdf", b"<!doctype rss"))
        )
        if looks_like_xml and self._contains_xml_entity_declaration(body):
            raise ProbeError(
                "policy",
                "unsafe_xml_declaration",
                "XML document type and entity declarations are not allowed",
            )
        return body

    @staticmethod
    def _remaining_timeout(deadline: float) -> float:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise ProbeError("network", "timeout", "source request timed out", retryable=True)
        return remaining

    def _validated_targets(self, url: str) -> tuple[tuple[str, dict[str, str], dict[str, Any]], ...]:
        normalized = normalize_http_url(url)
        parsed = urlsplit(normalized)
        assert parsed.hostname is not None
        port = parsed.port or (443 if parsed.scheme == "https" else 80)

        try:
            records = self._resolver(parsed.hostname, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ProbeError(
                "network",
                "dns_resolution_failed",
                "source hostname could not be resolved",
                retryable=True,
            ) from exc

        addresses: list[str] = []
        for record in records:
            sockaddr = record[4]
            address = str(sockaddr[0]) if sockaddr else ""
            if address and address not in addresses:
                addresses.append(address)

        if not addresses:
            raise ProbeError(
                "network", "dns_no_addresses", "source hostname has no addresses", retryable=True
            )
        if any(not _is_public_address(address) for address in addresses):
            raise ProbeError(
                "policy", "non_public_address", "source hostname resolves to a non-public address"
            )

        default_port = 443 if parsed.scheme == "https" else 80
        rendered_original_host = f"[{parsed.hostname}]" if ":" in parsed.hostname else parsed.hostname
        host_header = rendered_original_host if port == default_port else f"{rendered_original_host}:{port}"
        extensions: dict[str, Any] = {}
        if parsed.scheme == "https":
            extensions["sni_hostname"] = parsed.hostname

        targets: list[tuple[str, dict[str, str], dict[str, Any]]] = []
        for pinned_ip in addresses[: self._MAX_PINNED_ADDRESSES]:
            pinned_host = f"[{pinned_ip}]" if ":" in pinned_ip else pinned_ip
            pinned_netloc = pinned_host if port == default_port else f"{pinned_host}:{port}"
            pinned_url = urlunsplit((parsed.scheme, pinned_netloc, parsed.path or "/", parsed.query, ""))
            targets.append((pinned_url, {"Host": host_header}, dict(extensions)))

        return tuple(targets)

    @staticmethod
    def _validate_pdf_prefix_headers(
        status_code: int,
        headers: dict[str, str],
        *,
        limit: int,
    ) -> int:
        if status_code != 206:
            raise ProbeError(
                "quality",
                "range_not_supported",
                "source did not honor the bounded PDF range request",
            )
        content_range = headers.get("content-range", "")
        try:
            unit, value = content_range.split(" ", 1)
            span, total_text = value.split("/", 1)
            start_text, end_text = span.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            total = int(total_text)
        except (ValueError, TypeError) as exc:
            raise ProbeError(
                "quality",
                "invalid_content_range",
                "partial PDF response has an invalid Content-Range",
            ) from exc
        expected_end = min(limit, total) - 1
        if unit.casefold() != "bytes" or start != 0 or total <= 0 or end != expected_end or total <= end:
            raise ProbeError(
                "quality",
                "invalid_content_range",
                "partial PDF response has an invalid Content-Range",
            )
        return end - start + 1

    def _check_declared_size(self, value: str | None, *, limit: int | None = None) -> None:
        if value is None:
            return
        try:
            size = int(value)
        except ValueError:
            return
        if size > (limit or self.settings.source_probe_max_bytes):
            raise ProbeError("policy", "response_too_large", "response exceeds the byte limit")

    def _read_limited(
        self,
        response: httpx.Response,
        *,
        deadline: float,
        limit: int | None = None,
    ) -> bytes:
        byte_limit = limit or self.settings.source_probe_max_bytes
        chunks: list[bytes] = []
        size = 0
        for chunk in response.iter_bytes():
            self._remaining_timeout(deadline)
            size += len(chunk)
            if size > byte_limit:
                raise ProbeError("policy", "response_too_large", "response exceeds the byte limit")
            chunks.append(chunk)
        return b"".join(chunks)

    @staticmethod
    def _contains_xml_entity_declaration(body: bytes) -> bool:
        sample = body[:65536].upper()
        return b"<!DOCTYPE" in sample or b"<!ENTITY" in sample

    @staticmethod
    def _raise_for_status(status_code: int, retry_after: str | None = None) -> None:
        if 200 <= status_code < 300:
            return
        if status_code in {401, 403, 407, 429}:
            raise ProbeError(
                "access",
                f"http_{status_code}",
                f"source returned HTTP {status_code}",
                retryable=status_code == 429,
                retry_after=retry_after[:128] if retry_after else None,
            )
        raise ProbeError(
            "http",
            f"http_{status_code}",
            f"source returned HTTP {status_code}",
            retryable=status_code >= 500,
        )


def _is_sensitive_query_key(value: str) -> bool:
    token = value.casefold().replace("-", "").replace("_", "")
    return token in _SENSITIVE_QUERY_KEY_TOKENS


def _is_valid_sensitive_header_value(value: str) -> bool:
    return 0 < len(value) <= 256 and all(33 <= ord(character) <= 126 for character in value)


def _is_reviewed_comtrade_query(query: list[tuple[str, str]]) -> bool:
    if len(query) != len(_COMTRADE_QUERY_KEYS):
        return False
    parameters = dict(query)
    if set(parameters) != _COMTRADE_QUERY_KEYS:
        return False
    period = parameters["period"]
    return (
        parameters["reporterCode"] in _COMTRADE_REPORTER_CODES
        and period.isascii()
        and period.isdigit()
        and 2017 <= int(period) <= 2024
        and parameters["partnerCode"] == "0,156"
        and parameters["partner2Code"] == "0"
        and parameters["cmdCode"] in _COMTRADE_COMMODITY_CODES
        and parameters["flowCode"] == "M,X"
        and parameters["customsCode"] == "C00"
        and parameters["motCode"] == "0"
        and parameters["maxRecords"] == "500"
        and parameters["format"] == "JSON"
        and parameters["breakdownMode"] == "classic"
        and parameters["includeDesc"] == "false"
    )
