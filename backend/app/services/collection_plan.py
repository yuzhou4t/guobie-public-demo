from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from app.collectors.reviewed_html import REVIEWED_HTML_PROFILES

SCHEMA_VERSION = 1
EXPECTED_EXCEL_ROWS = tuple(range(4, 54))
EXPECTED_CHANNEL_COUNT = 53

_ROOT_KEYS = {
    "schema_version",
    "source_file",
    "excel_rows",
    "policy_defaults",
    "plans",
}
_PLAN_REQUIRED_KEYS = {"excel_row", "automation_status", "reason", "channels"}
_PLAN_OPTIONAL_KEYS = {"official_homepage_url", "source_name_override"}
_CHANNEL_KEYS = {
    "name",
    "entry_url",
    "collector_type",
    "link_role",
    "status",
    "collector_config",
}
_CHANNEL_OPTIONAL_KEYS = {"policy"}
_CHANNEL_POLICY_KEYS = {
    "terms_state",
    "terms_url",
    "storage_scope",
    "rag_scope",
    "reviewed_at",
    "notes",
}
_POLICY_DEFAULTS = {
    "storage_scope": "metadata",
    "rag_scope": "none",
    "channel_status": "shadow",
}
_AUTOMATION_STATUSES = {"runnable", "metadata_only", "blocked"}
_COLLECTOR_TYPES = {"rss", "api", "html", "pdf"}
_HTML_MODES = {
    "auto_list",
    "cie_current",
    "cssn_periodical",
    "iss_africa_report",
    "management_world",
    "reviewed_metadata",
}
_LINK_ROLES = {"official", "discovery", "unknown"}
_REVIEWED_HTML_ABSTRACT_PROFILES = {
    "eiti_country_reports",
    "iea_critical_minerals",
    "iea_data_product",
    "iss_africa_report",
    "world_bank_metals",
    "usgs_nmic_news",
    "usgs_nmic_publications",
    "erj_ajcass",
    "cswe_cssn",
    "iwep_cssn",
    "mworld_org",
    "ciejournal_ajcass",
}
_CHANNEL_STATUSES = {"shadow", "blocked"}
_COMTRADE_PROFILE = "un_comtrade_goods_annual_hs"
_COMTRADE_ENTRY_URL = (
    "https://comtradeapi.un.org/data/v1/get/C/A/HS?reporterCode=180&period=2017"
    "&partnerCode=0,156&partner2Code=0&cmdCode=260300&flowCode=M,X&customsCode=C00"
    "&motCode=0&maxRecords=500&format=JSON&breakdownMode=classic&includeDesc=false"
)
_IMF_WEO_PROFILE = "imf_weo_manual"
_IMF_WEO_ENTRY_URL = "https://data.imf.org/en/datasets/IMF.RES%3AWEO?indicator_id=GGXWDG_NGDP"
_NBS_ANNUAL_PROFILE = "nbs_annual_manual"
_NBS_ANNUAL_ENTRY_URL = "https://data.stats.gov.cn/dg/website/page.html#/pc/national/yearData"
_MOFCOM_TRADE_PROFILE = "mofcom_trade_manual"
_MOFCOM_TRADE_ENTRY_URL = "https://data.mofcom.gov.cn/hwmy/imexCountry.shtml"
_MOFCOM_ODI_PROFILE = "mofcom_odi_manual"
_MOFCOM_ODI_ENTRY_URL = "https://data.mofcom.gov.cn/tzhz/fordirinvest.shtml"
_OECD_ODA_PROFILE = "oecd_oda"
_OECD_ODA_ENTRY_URL = (
    "https://sdmx.oecd.org/public/rest/data/OECD.DCD.FSD,DSD_DAC2@DF_DAC2A,/"
    "ALLD.COD+ZWE+ZMB+ZAF.206.USD.V?startPeriod=2015&endPeriod=2024&"
    "dimensionAtObservation=AllDimensions&format=jsondata"
)
_WITS_TARIFF_PROFILE = "wits_tariff"
_WITS_TARIFF_ENTRY_URL = (
    "https://wits.worldbank.org/API/V1/wits/datasource/trn/dataavailability/"
    "country/180;716;894;710/year/2015;2016;2017;2018;2019;2020;2021"
)
_UNCTAD_FDI_PROFILE = "unctad_fdi"
_UNCTAD_FDI_ENTRY_URL = "https://unctadstat-api.unctad.org/bulkdownload/US.FdiFlowsStock/US_FdiFlowsStock"


class CollectionPlanValidationError(ValueError):
    """Raised when the checked-in collection plan is incomplete or unsafe."""


@dataclass(frozen=True)
class PlannedChannelPolicy:
    terms_state: str
    terms_url: str
    storage_scope: str
    rag_scope: str
    reviewed_at: str
    notes: str


@dataclass(frozen=True)
class PlannedChannel:
    name: str
    entry_url: str
    collector_type: str
    link_role: str
    status: str
    collector_config: dict[str, Any]
    policy: PlannedChannelPolicy | None


@dataclass(frozen=True)
class PlannedSource:
    excel_row: int
    automation_status: str
    reason: str
    official_homepage_url: str | None
    source_name_override: str | None
    channels: tuple[PlannedChannel, ...]


@dataclass(frozen=True)
class PolicyDefaults:
    storage_scope: str
    rag_scope: str
    channel_status: str


@dataclass(frozen=True)
class CollectionPlan:
    schema_version: int
    source_file: str
    policy_defaults: PolicyDefaults
    sources: tuple[PlannedSource, ...]


def load_collection_plan(path: str | Path | None = None) -> CollectionPlan:
    plan_path = Path(path) if path is not None else _default_plan_path()
    try:
        payload: Any = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CollectionPlanValidationError(f"unable to read collection plan: {plan_path}") from exc

    if not isinstance(payload, dict) or set(payload) != _ROOT_KEYS:
        raise CollectionPlanValidationError("collection plan root fields do not match the schema")
    if payload["schema_version"] != SCHEMA_VERSION:
        raise CollectionPlanValidationError(f"collection plan schema_version must be {SCHEMA_VERSION}")
    if payload["excel_rows"] != "4-53":
        raise CollectionPlanValidationError("collection plan excel_rows must be '4-53'")

    source_file = payload["source_file"]
    if not isinstance(source_file, str) or not source_file.strip():
        raise CollectionPlanValidationError("collection plan source_file must be a non-empty string")
    policy_defaults = _validate_policy_defaults(payload["policy_defaults"])

    raw_plans = payload["plans"]
    if not isinstance(raw_plans, list) or len(raw_plans) != len(EXPECTED_EXCEL_ROWS):
        raise CollectionPlanValidationError("collection plan must contain exactly 50 source rows")
    sources = tuple(_validate_source(item) for item in raw_plans)
    rows = tuple(source.excel_row for source in sources)
    if rows != EXPECTED_EXCEL_ROWS:
        raise CollectionPlanValidationError(
            "collection plan excel_row values must be consecutive from 4 through 53"
        )
    if sum(len(source.channels) for source in sources) != EXPECTED_CHANNEL_COUNT:
        raise CollectionPlanValidationError(
            f"collection plan must contain exactly {EXPECTED_CHANNEL_COUNT} channels"
        )

    return CollectionPlan(
        schema_version=SCHEMA_VERSION,
        source_file=source_file.strip(),
        policy_defaults=policy_defaults,
        sources=sources,
    )


def _default_plan_path() -> Path:
    return Path(__file__).resolve().parents[3] / "data" / "source_collection_plan.json"


def _validate_policy_defaults(value: Any) -> PolicyDefaults:
    if not isinstance(value, dict) or value != _POLICY_DEFAULTS:
        raise CollectionPlanValidationError(
            "policy_defaults must be metadata storage, no RAG, and shadow channel status"
        )
    return PolicyDefaults(**value)


def _validate_source(value: Any) -> PlannedSource:
    if (
        not isinstance(value, dict)
        or not _PLAN_REQUIRED_KEYS.issubset(value)
        or not set(value).issubset(_PLAN_REQUIRED_KEYS | _PLAN_OPTIONAL_KEYS)
    ):
        raise CollectionPlanValidationError("each source plan must contain only the declared fields")
    excel_row = value["excel_row"]
    if not isinstance(excel_row, int) or isinstance(excel_row, bool):
        raise CollectionPlanValidationError("source plan excel_row must be an integer")
    automation_status = value["automation_status"]
    if automation_status not in _AUTOMATION_STATUSES:
        raise CollectionPlanValidationError("source plan automation_status is invalid")
    reason = value["reason"]
    if not isinstance(reason, str) or not reason.strip():
        raise CollectionPlanValidationError("source plan reason must be a non-empty string")
    raw_homepage = value.get("official_homepage_url")
    official_homepage_url = _validate_url(raw_homepage) if raw_homepage is not None else None
    raw_source_name = value.get("source_name_override")
    if raw_source_name is not None and (not isinstance(raw_source_name, str) or not raw_source_name.strip()):
        raise CollectionPlanValidationError("source_name_override must be a non-empty string")
    source_name_override = raw_source_name.strip() if raw_source_name is not None else None
    raw_channels = value["channels"]
    if not isinstance(raw_channels, list) or not raw_channels:
        raise CollectionPlanValidationError("each source plan must contain at least one channel")
    channels = tuple(_validate_channel(item) for item in raw_channels)

    expected_channel_status = "blocked" if automation_status == "blocked" else "shadow"
    if any(channel.status != expected_channel_status for channel in channels):
        raise CollectionPlanValidationError(
            "blocked sources require blocked channels and runnable sources require shadow channels"
        )
    if automation_status != "blocked":
        for channel in channels:
            if channel.collector_type != "html" or channel.collector_config.get("mode") != "auto_list":
                continue
            markers = channel.collector_config.get("include_path_markers")
            if (
                not isinstance(markers, list)
                or not 1 <= len(markers) <= 20
                or any(not isinstance(marker, str) or not marker.strip() for marker in markers)
            ):
                raise CollectionPlanValidationError(
                    "non-blocked auto_list channels require reviewed include_path_markers"
                )
    return PlannedSource(
        excel_row=excel_row,
        automation_status=automation_status,
        reason=reason.strip(),
        official_homepage_url=official_homepage_url,
        source_name_override=source_name_override,
        channels=channels,
    )


def _validate_channel(value: Any) -> PlannedChannel:
    if (
        not isinstance(value, dict)
        or not _CHANNEL_KEYS.issubset(value)
        or not set(value).issubset(_CHANNEL_KEYS | _CHANNEL_OPTIONAL_KEYS)
    ):
        raise CollectionPlanValidationError("each channel must contain only the declared fields")
    name = value["name"]
    if not isinstance(name, str) or not name.strip():
        raise CollectionPlanValidationError("channel name must be a non-empty string")
    entry_url = _validate_url(value["entry_url"])
    collector_type = value["collector_type"]
    link_role = value["link_role"]
    status = value["status"]
    config = value["collector_config"]
    policy = _validate_channel_policy(value.get("policy"))
    if collector_type not in _COLLECTOR_TYPES:
        raise CollectionPlanValidationError("channel collector_type is invalid")
    if link_role not in _LINK_ROLES:
        raise CollectionPlanValidationError("channel link_role is invalid")
    if status not in _CHANNEL_STATUSES:
        raise CollectionPlanValidationError("channel status is invalid")
    if not isinstance(config, dict) or any(not isinstance(key, str) for key in config):
        raise CollectionPlanValidationError("channel collector_config must be an object")
    if policy is not None:
        parsed_entry = urlsplit(entry_url)
        licensed_crossref = (
            collector_type == "api"
            and link_role == "discovery"
            and (parsed_entry.hostname or "").casefold() == "api.crossref.org"
            and parsed_entry.path.rstrip("/").endswith("/works")
            and config.get("abstract_license_gate") in {"creative_commons", "source_provided"}
        )
        reviewed_ncpssd = (
            collector_type == "html"
            and link_role == "discovery"
            and config.get("mode") == "management_world"
            and config.get("profile") == "mworld_org"
            and config.get("ncpssd_gch") == "95499X"
        )
        if collector_type not in {"rss", "api", "html"} or (
            link_role != "official" and not (licensed_crossref or reviewed_ncpssd)
        ):
            raise CollectionPlanValidationError(
                "official_abstract policy requires an official RSS, API, or reviewed HTML channel"
            )
        if collector_type == "html" and config.get("profile") is not None:
            profile_name = config.get("profile") or config.get("mode")
            if profile_name not in _REVIEWED_HTML_ABSTRACT_PROFILES:
                raise CollectionPlanValidationError(
                    "official_abstract HTML policy requires a reviewed abstract profile"
                )
        if collector_type == "api":
            field_map = config.get("field_map")
            if not isinstance(field_map, dict) or not isinstance(field_map.get("summary"), str):
                raise CollectionPlanValidationError(
                    "official_abstract API policy requires an explicit summary field mapping"
                )
            if licensed_crossref and field_map.get("summary") != "abstract":
                raise CollectionPlanValidationError(
                    "Crossref abstract policy requires the abstract field mapping and a reviewed gate"
                )
    if collector_type == "html" and config.get("mode") not in _HTML_MODES:
        raise CollectionPlanValidationError("HTML plan channel mode is invalid")
    if collector_type == "html" and config.get("mode") == "cssn_periodical":
        parsed_entry = urlsplit(entry_url)
        if (
            parsed_entry.hostname or ""
        ).casefold() != "ejournaliwep.cssn.cn" or parsed_entry.path != "/qkjj/sjjjyzz/":
            raise CollectionPlanValidationError(
                "cssn_periodical requires the reviewed official periodical entry URL"
            )
        limits = {
            "max_issues": 4,
            "max_pages_per_issue": 4,
            "max_items": 20,
        }
        for key, maximum in limits.items():
            item = config.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum:
                raise CollectionPlanValidationError(
                    f"cssn_periodical {key} must be an integer between 1 and {maximum}"
                )
    if collector_type == "html" and config.get("mode") == "management_world":
        parsed_entry = urlsplit(entry_url)
        entry_host = (parsed_entry.hostname or "").casefold()
        entry_query = parse_qs(parsed_entry.query)
        reviewed_discovery = (
            entry_host == "www.macrodatas.cn"
            and parsed_entry.path.startswith("/list/1/0/0/")
            and parsed_entry.path.rstrip("/") != "/list/1/0/0"
            and not parsed_entry.query
            and not parsed_entry.fragment
            and link_role == "discovery"
        )
        reviewed_official = (
            entry_host == "www.ncpssd.cn"
            and parsed_entry.path == "/journal/details"
            and entry_query == {"gch": ["95499X"], "langType": ["1"], "nav": ["1"]}
            and not parsed_entry.fragment
            and link_role == "official"
        )
        if not (reviewed_discovery or reviewed_official):
            raise CollectionPlanValidationError(
                "management_world requires a reviewed Macrodatas discovery or NCPSD official entry"
            )
        limits = {
            "max_issues": 2,
            "max_items_per_issue": 20,
            "max_items": 40,
        }
        for key, maximum in limits.items():
            item = config.get(key)
            if isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= maximum:
                raise CollectionPlanValidationError(
                    f"management_world {key} must be an integer between 1 and {maximum}"
                )
        if config.get("ncpssd_gch") != "95499X":
            raise CollectionPlanValidationError("management_world requires the reviewed NCPSD journal code")
    if collector_type == "html" and config.get("mode") == "cie_current":
        parsed_entry = urlsplit(entry_url)
        if (
            (parsed_entry.hostname or "").casefold() != "ciejournal.ajcass.com"
            or parsed_entry.path not in {"", "/"}
            or parsed_entry.query
            or parsed_entry.fragment
            or link_role != "official"
        ):
            raise CollectionPlanValidationError("cie_current requires the reviewed official journal homepage")
        max_items = config.get("max_items")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
            raise CollectionPlanValidationError("cie_current max_items must be an integer between 1 and 20")
    if collector_type == "html" and config.get("mode") == "iss_africa_report":
        parsed_entry = urlsplit(entry_url)
        if (
            parsed_entry.scheme != "https"
            or (parsed_entry.hostname or "").casefold() != "issafrica.org"
            or parsed_entry.path.rstrip("/") != "/research/africa-report"
            or parsed_entry.query
            or parsed_entry.fragment
        ):
            raise CollectionPlanValidationError(
                "iss_africa_report requires the reviewed official listing URL"
            )
        max_items = config.get("max_items")
        if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
            raise CollectionPlanValidationError(
                "iss_africa_report max_items must be an integer between 1 and 20"
            )
    if collector_type == "html" and config.get("mode") == "reviewed_metadata":
        profile_name = config.get("profile")
        profile = REVIEWED_HTML_PROFILES.get(profile_name) if isinstance(profile_name, str) else None
        if profile is None:
            raise CollectionPlanValidationError("reviewed_metadata profile is invalid")
        if entry_url != profile.entry_url or link_role != "official":
            raise CollectionPlanValidationError(
                "reviewed_metadata requires its exact official entry URL and official link role"
            )
        if set(config) - {"mode", "profile", "max_items", "document_type"}:
            raise CollectionPlanValidationError("reviewed_metadata config contains unsupported fields")
        max_items = config.get("max_items")
        if (
            isinstance(max_items, bool)
            or not isinstance(max_items, int)
            or not 1 <= max_items <= profile.max_items
        ):
            raise CollectionPlanValidationError(
                f"{profile_name} max_items must be between 1 and {profile.max_items}"
            )
        if profile.self_page and max_items != 1:
            raise CollectionPlanValidationError(
                f"{profile_name} self-page profile max_items must be exactly 1"
            )
    if collector_type == "api":
        profile = config.get("profile")
        if profile not in {
            None,
            "ajcass_current",
            "aerc_wordpress_publications",
            "dspace_search",
            "carnegie_research_api",
            "world_bank_indicators",
            _OECD_ODA_PROFILE,
            _IMF_WEO_PROFILE,
            _NBS_ANNUAL_PROFILE,
            _MOFCOM_TRADE_PROFILE,
            _MOFCOM_ODI_PROFILE,
            _COMTRADE_PROFILE,
            _WITS_TARIFF_PROFILE,
            _UNCTAD_FDI_PROFILE,
        }:
            raise CollectionPlanValidationError("API plan profile is invalid")
        if profile == _COMTRADE_PROFILE and (
            entry_url != _COMTRADE_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _COMTRADE_PROFILE,
                "scope_id": "mvp-structured-data-v1",
                "dataset_key": "un-comtrade-goods-annual-hs",
            }
        ):
            raise CollectionPlanValidationError(
                "UN Comtrade profile requires its exact fixed Data entry and scope config"
            )
        if profile == _OECD_ODA_PROFILE and (
            entry_url != _OECD_ODA_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _OECD_ODA_PROFILE,
                "scope_id": "oecd-oda-shadow-v1",
                "dataset_key": "oecd-dac2a-oda-disbursements",
            }
        ):
            raise CollectionPlanValidationError("oecd_oda requires the reviewed fixed query and scope config")
        if profile == _IMF_WEO_PROFILE and (
            entry_url != _IMF_WEO_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _IMF_WEO_PROFILE,
                "scope_id": "imf-weo-debt-shadow-v1",
                "dataset_key": "imf-weo-government-debt",
            }
        ):
            raise CollectionPlanValidationError(
                "imf_weo_manual requires the official dataset page and reviewed manual scope config"
            )
        if profile == _NBS_ANNUAL_PROFILE and (
            entry_url != _NBS_ANNUAL_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _NBS_ANNUAL_PROFILE,
                "scope_id": "nbs-annual-industry-metals-manual-v1",
                "dataset_key": "nbs-china-annual-industry-metals",
            }
        ):
            raise CollectionPlanValidationError(
                "nbs_annual_manual requires the official annual-data page and reviewed scope config"
            )
        if profile == _MOFCOM_TRADE_PROFILE and (
            entry_url != _MOFCOM_TRADE_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _MOFCOM_TRADE_PROFILE,
                "scope_id": "mofcom-south-africa-trade-monthly-manual-v1",
                "dataset_key": "mofcom-china-south-africa-goods-trade",
            }
        ):
            raise CollectionPlanValidationError(
                "mofcom_trade_manual requires the reviewed official table and delivered scope"
            )
        if profile == _MOFCOM_ODI_PROFILE and (
            entry_url != _MOFCOM_ODI_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _MOFCOM_ODI_PROFILE,
                "scope_id": "mofcom-nonfinancial-odi-monthly-manual-v1",
                "dataset_key": "mofcom-china-nonfinancial-odi",
            }
        ):
            raise CollectionPlanValidationError(
                "mofcom_odi_manual requires the reviewed official table and delivered scope"
            )
        if profile == _WITS_TARIFF_PROFILE and (
            entry_url != _WITS_TARIFF_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _WITS_TARIFF_PROFILE,
                "scope_id": "wits-mfn-hs6-shadow-v1",
                "dataset_key": "wits-trains-mfn-hs6",
            }
        ):
            raise CollectionPlanValidationError(
                "wits_tariff requires the reviewed availability query and scope config"
            )
        if profile == _UNCTAD_FDI_PROFILE and (
            entry_url != _UNCTAD_FDI_ENTRY_URL
            or link_role != "official"
            or config
            != {
                "profile": _UNCTAD_FDI_PROFILE,
                "scope_id": "unctad-fdi-shadow-v1",
                "dataset_key": "unctad-fdi-flows-stock",
            }
        ):
            raise CollectionPlanValidationError(
                "unctad_fdi requires the reviewed official bulk file and scope config"
            )
        if profile == "dspace_search":
            parsed_entry = urlsplit(entry_url)
            query = parse_qs(parsed_entry.query, keep_blank_values=True)
            if (
                parsed_entry.scheme != "https"
                or (parsed_entry.hostname or "").casefold() != "publication.aercafricalibrary.org"
                or parsed_entry.path != "/server/api/discover/search/objects"
                or parsed_entry.fragment
                or query
                != {
                    "size": ["20"],
                    "page": ["0"],
                    "sort": ["dc.date.accessioned,DESC"],
                }
            ):
                raise CollectionPlanValidationError(
                    "dspace_search requires the reviewed AERC first-page endpoint"
                )
            max_items = config.get("max_items")
            if max_items != 20 or isinstance(max_items, bool):
                raise CollectionPlanValidationError("dspace_search max_items must be exactly 20")
        if profile == "aerc_wordpress_publications":
            expected_entry = (
                "https://aercafrica.org/wp-json/wp/v2/publications"
                "?per_page=20&page=1&orderby=date&order=desc"
                "&_fields=id,date,modified,link,slug,title,type,status"
            )
            if (
                entry_url != expected_entry
                or link_role != "official"
                or config
                != {
                    "profile": "aerc_wordpress_publications",
                    "max_items": 20,
                    "document_type": "report",
                }
            ):
                raise CollectionPlanValidationError(
                    "aerc_wordpress_publications requires the reviewed official first-page endpoint"
                )
        if profile == "world_bank_indicators":
            expected_entry = (
                "https://api.worldbank.org/v2/country/COD;ZWE;ZMB;ZAF/indicator/"
                "SP.POP.TOTL?date=2015:2024&format=json&per_page=100&source=2"
            )
            if (
                entry_url != expected_entry
                or link_role != "official"
                or config
                != {
                    "profile": "world_bank_indicators",
                    "scope_id": "mvp-structured-data-v1",
                    "dataset_key": "world-bank-indicators-v2",
                }
            ):
                raise CollectionPlanValidationError(
                    "world_bank_indicators requires the reviewed scope and exact first query"
                )
        if profile == "carnegie_research_api":
            parsed_entry = urlsplit(entry_url)
            host = (parsed_entry.hostname or "").casefold()
            if (
                parsed_entry.scheme != "https"
                or host not in {"carnegieendowment.org", "www.carnegieendowment.org"}
                or parsed_entry.path != "/api/research"
                or link_role != "official"
            ):
                raise CollectionPlanValidationError(
                    "carnegie_research_api requires the reviewed official "
                    "/api/research endpoint on carnegieendowment.org"
                )
            max_items = config.get("max_items")
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 50:
                raise CollectionPlanValidationError(
                    "carnegie_research_api max_items must be an integer between 1 and 50"
                )
    if collector_type == "rss":
        profile = config.get("profile")
        if profile not in {None, "cnn_news_sitemap", "carnegie_sitemap", "chatham_critical_minerals"}:
            raise CollectionPlanValidationError("RSS plan profile is invalid")
        if profile == "carnegie_sitemap":
            parsed_entry = urlsplit(entry_url)
            host = (parsed_entry.hostname or "").casefold()
            if (
                parsed_entry.scheme != "https"
                or host not in {"carnegieendowment.org", "www.carnegieendowment.org"}
                or not parsed_entry.path.startswith("/sitemaps/")
            ):
                raise CollectionPlanValidationError(
                    "carnegie_sitemap requires a valid sitemap URL on carnegieendowment.org"
                )
            max_items = config.get("max_items")
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 50:
                raise CollectionPlanValidationError(
                    "carnegie_sitemap max_items must be an integer between 1 and 50"
                )
        if profile == "cnn_news_sitemap":
            parsed_entry = urlsplit(entry_url)
            if (
                parsed_entry.scheme != "https"
                or (parsed_entry.hostname or "").casefold() != "www.cnn.com"
                or parsed_entry.path != "/sitemap/news.xml"
                or config.get("include_path_marker") != "/politics/"
            ):
                raise CollectionPlanValidationError(
                    "cnn_news_sitemap requires the reviewed CNN news sitemap and politics marker"
                )
            max_items = config.get("max_items")
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 50:
                raise CollectionPlanValidationError(
                    "cnn_news_sitemap max_items must be an integer between 1 and 50"
                )
        if profile == "chatham_critical_minerals":
            parsed_entry = urlsplit(entry_url)
            if (
                parsed_entry.scheme != "https"
                or (parsed_entry.hostname or "").casefold() != "www.chathamhouse.org"
                or parsed_entry.path != "/path/83/feed.xml"
                or parsed_entry.query
                or parsed_entry.fragment
                or link_role != "official"
                or set(config) != {"profile", "max_items"}
            ):
                raise CollectionPlanValidationError(
                    "chatham_critical_minerals requires the reviewed official expert-comments feed"
                )
            max_items = config.get("max_items")
            if isinstance(max_items, bool) or not isinstance(max_items, int) or not 1 <= max_items <= 20:
                raise CollectionPlanValidationError(
                    "chatham_critical_minerals max_items must be an integer between 1 and 20"
                )
    if collector_type == "pdf" and config.get("profile") not in {None, "bounded_metadata"}:
        raise CollectionPlanValidationError("PDF plan profile is invalid")
    return PlannedChannel(
        name=name.strip(),
        entry_url=entry_url,
        collector_type=collector_type,
        link_role=link_role,
        status=status,
        collector_config=dict(config),
        policy=policy,
    )


def _validate_channel_policy(value: Any) -> PlannedChannelPolicy | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _CHANNEL_POLICY_KEYS:
        raise CollectionPlanValidationError("channel policy must contain only the reviewed policy fields")
    if value["terms_state"] != "allowed_with_conditions":
        raise CollectionPlanValidationError(
            "reviewed channel policy terms_state must be allowed_with_conditions"
        )
    terms_url = _validate_url(value["terms_url"])
    if not terms_url.startswith("https://"):
        raise CollectionPlanValidationError("reviewed channel policy terms_url must use HTTPS")
    if value["storage_scope"] != "official_abstract" or value["rag_scope"] != "none":
        raise CollectionPlanValidationError(
            "reviewed channel policy may store only official abstracts and cannot enable RAG"
        )
    reviewed_at = value["reviewed_at"]
    if not isinstance(reviewed_at, str):
        raise CollectionPlanValidationError("reviewed_at must be an ISO 8601 timestamp")
    try:
        parsed_reviewed_at = datetime.fromisoformat(reviewed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CollectionPlanValidationError("reviewed_at must be an ISO 8601 timestamp") from exc
    if parsed_reviewed_at.tzinfo is None:
        raise CollectionPlanValidationError("reviewed_at must include a timezone")
    notes = value["notes"]
    if not isinstance(notes, str) or not notes.strip():
        raise CollectionPlanValidationError("reviewed channel policy notes must be non-empty")
    return PlannedChannelPolicy(
        terms_state=value["terms_state"],
        terms_url=terms_url,
        storage_scope=value["storage_scope"],
        rag_scope=value["rag_scope"],
        reviewed_at=parsed_reviewed_at.isoformat(),
        notes=notes.strip(),
    )


def _validate_url(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CollectionPlanValidationError("channel entry_url must be a non-empty string")
    value = value.strip()
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError as exc:
        raise CollectionPlanValidationError("channel entry_url is invalid") from exc
    if (
        parsed.scheme.lower() not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 80, 443}
    ):
        raise CollectionPlanValidationError("channel entry_url must be a safe absolute HTTP(S) URL")
    return value
